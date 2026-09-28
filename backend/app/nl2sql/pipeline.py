# 流水线编排器（LangGraph StateGraph 版）：整条 NL2SQL 链路建模为"节点 + 条件边"图，
# 由 LangGraph 引擎调度执行；iter_run 经 stream_mode="custom" 逐事件产出 SSE 协议，
# run() 消费同一图返回聚合结果——单一事实来源，JSON 与 SSE 两个出口共享同一流程。
#
# 图结构（节点 → 条件边路由）：
#   START → intent ─┬─ query ─→ q_context → q_generate → q_validate ⇄ q_correct
#                   │                （纠错循环：validate/fanout/execute 的可纠正失败
#                 （意图分流）        经 q_correct 重写后回到 q_validate，最多 max_rounds 轮）
#                   │           → q_validate → q_guard → q_fanout → q_execute → q_cache → q_explain
#                   ├─ dml   ─→ d_context → d_generate → d_explain（仅生成绝不执行）
#                   └─ meta  ─→ m_answer（Schema 元数据直答，不生成 SQL）
#   三分支汇合 → finalize（usage 统计 + 记轮次 + done 事件）→ END
#
# 设计要点：
#   - 事件协议与旧版生成器完全兼容（intent/sql/executing/correcting/explanation/
#     result/usage/error/done），前端与 chat.py 路由零改动
#   - PipelineResult / TurnTracker 以可变对象引用注入 State（图内就地更新），
#     控制字段（attempt/route/error_message）经节点返回值走 LangGraph 通道语义
#   - 多轮会话持久化仍由 ConversationManager 承载（业务字段：标题/结果快照/回放），
#     图为单轮无状态执行，后续如需断点续跑可无缝挂 LangGraph checkpointer
#   - 功能开关接线：intent_classifier（关闭直走 query）/ metrics_enabled（Tracker 空转）/
#     self_correction（max_rounds 生效为 0）/ schema_retriever（None 走全量）

import uuid
from dataclasses import dataclass, field
from typing import Iterator, List, Optional, TypedDict

import sqlglot
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from sqlglot import exp

from app.formatter.output_formatter import OutputFormatter
from app.metrics.turn_tracker import TurnTracker
from app.model.factory import ModelFactory
from app.nl2sql.conversation import ConversationManager
from app.nl2sql.fanout_checker import FanoutChecker
from app.nl2sql.intent_classifier import IntentClassifier
from app.nl2sql.relation_graph import RelationGraph
from app.nl2sql.result_cache import ResultCache
from app.nl2sql.schema_manager import SchemaManager
from app.nl2sql.schema_retriever import SchemaRetriever
from app.nl2sql.self_corrector import SelfCorrector
from app.nl2sql.sql_executor import SQLExecutor, SQLExecutionError
from app.nl2sql.sql_explainer import SQLExplainer
from app.nl2sql.sql_generator import SQLGenerator
from app.nl2sql.sql_guard import SQLGuard, SQLGuardError
from app.nl2sql.sql_validator import SQLValidator, SQLValidationError
from app.nl2sql.term_mapper import TermMapper
from app.nl2sql.value_linker import ValueLinker
from app.utils.config_handler import load_yaml
from app.utils.logger_handler import setup_logger
from app.utils.path_tool import BACKEND_ROOT

logger = setup_logger()

RESULT_PAGE_SIZE = 50   # result 事件携带的首屏行数（其余走分页拉取）


@dataclass
class PipelineResult:
    """单轮问答完整结果（run() 的聚合返回）"""

    question: str = ""
    intent: str = "query"
    sql: str = ""
    executed: bool = True
    columns: List[str] = field(default_factory=list)
    rows: List[list] = field(default_factory=list)
    total: int = 0
    truncated: bool = False
    query_id: Optional[str] = None
    raw_output: str = ""
    answer: str = ""
    explanation: str = ""
    usage: dict = field(default_factory=dict)
    term_mappings: List[dict] = field(default_factory=list)
    value_mappings: List[dict] = field(default_factory=list)
    correction_trail: List[dict] = field(default_factory=list)   # 纠错轨迹：每轮 {round, error, sql}
    error: Optional[str] = None
    error_kind: Optional[str] = None
    session_id: Optional[str] = None


class NL2SQLState(TypedDict, total=False):
    """图状态：result/tracker 为可变对象引用（节点就地更新）；
    其余为控制字段，经节点返回值更新、供条件边路由消费"""
    question: str
    session_id: Optional[str]
    result: PipelineResult
    tracker: TurnTracker
    attempt: int                  # 纠错轮次：0=原始 SQL，1..N 为重写
    max_rounds: int               # 纠错上限（开关关闭时为 0）
    error_message: str            # 回喂给纠错节点的错误描述
    extra_context: str            # 上下文增强产物（纠错重写复用）
    conversation_context: str     # 多轮上下文（模板中紧跟用户问题）
    reference_context: str        # 术语映射/值链接（模板中置于表结构之后）
    candidate_tables: Optional[List[str]]   # Schema 检索命中的候选表
    fanout_waived: bool         # 当前 SQL 已经 LLM 裁决为扇出误报，跳过扇出检测；
                                # 任何纠错重写产生新 SQL 后必须清除（在 q_correct 节点）
    route: str                    # 当前节点决定的下一跳（条件边消费）


def _emit(event_type: str, data: dict):
    """经 LangGraph custom 流通道推送一条 SSE 协议事件；
    无流上下文（invoke 直调）时静默跳过"""
    try:
        writer = get_stream_writer()
        if writer:
            writer({"type": event_type, "data": data})
    except Exception:
        pass  # 非流式调用场景，忽略


class NL2SQLPipeline:
    """NL2SQL 完整流水线编排入口（LangGraph StateGraph）"""

    def __init__(self, conversation_manager: ConversationManager = None,
                 result_cache: ResultCache = None):
        factory = ModelFactory()
        self.llm = factory.create_llm()
        self.schema_manager = SchemaManager()
        self.conversation = conversation_manager or ConversationManager()
        self.result_cache = result_cache or ResultCache()
        agent_cfg = load_yaml(BACKEND_ROOT / "config" / "agent.yml")
        nl2sql_cfg = load_yaml(BACKEND_ROOT / "config" / "nl2sql.yml")
        features = agent_cfg.get("features", {})
        # 功能开关（agent.yml features.*，默认全开；schema_retriever 默认关）
        self.intent_enabled = features.get("intent_classifier", True)
        self.metrics_enabled = features.get("metrics_enabled", True)
        self.corrector_enabled = features.get("self_correction", True)
        self.intent_classifier = IntentClassifier(self.llm) if self.intent_enabled else None
        self.term_mapper = TermMapper()
        self.value_linker = ValueLinker()
        self.schema_retriever = self._init_schema_retriever(agent_cfg, factory)
        # 表关联关系图谱（显式外键 + 命名推断 + 采样验证；构建失败内部降级为空图）
        self.relation_graph = RelationGraph(self.schema_manager) if features.get("relation_graph", True) else None
        self.generator = SQLGenerator(self.llm, self.schema_manager, self.relation_graph)
        self.validator = SQLValidator(
            self.schema_manager, check_cartesian=features.get("join_static_check", True),
        )
        self.guard = SQLGuard()
        self.executor = SQLExecutor()
        fanout_mode = nl2sql_cfg.get("fanout", {}).get("mode", "static")
        self.fanout_checker = (
            FanoutChecker(self.relation_graph, self.executor, mode=fanout_mode)
            if features.get("fanout_check", True) else FanoutChecker(None, self.executor, mode="off")
        )
        self.explainer = SQLExplainer(self.llm, self.schema_manager)
        self.formatter = OutputFormatter()
        self.corrector = SelfCorrector(
            self.llm, self.schema_manager, self.value_linker,
            max_rounds=nl2sql_cfg.get("correction", {}).get("max_rounds", 2),
            relation_graph=self.relation_graph,
        )
        self.graph = self._build_graph()

    def _init_schema_retriever(self, agent_cfg, factory) -> Optional[SchemaRetriever]:
        """按配置创建 Schema 检索器；未开启或初始化失败返回 None（全量路径）"""
        if not agent_cfg.get("features", {}).get("schema_retriever", False):
            return None
        try:
            return SchemaRetriever(self.schema_manager, factory.create_embedding())
        except Exception as e:
            logger.error("Schema 检索器初始化失败（降级全量）: %s", e)
            return None

    # ==================================================================
    # 图构建：节点 + 条件边
    # ==================================================================
    def _build_graph(self):
        g = StateGraph(NL2SQLState)

        # ---- 节点注册 ----
        g.add_node("intent", self._n_intent)
        g.add_node("q_context", self._n_q_context)
        g.add_node("q_generate", self._n_q_generate)
        g.add_node("q_validate", self._n_q_validate)
        g.add_node("q_guard", self._n_q_guard)
        g.add_node("q_fanout", self._n_q_fanout)
        g.add_node("q_execute", self._n_q_execute)
        g.add_node("q_correct", self._n_q_correct)
        g.add_node("q_cache", self._n_q_cache)
        g.add_node("q_explain", self._n_q_explain)
        g.add_node("d_context", self._n_d_context)
        g.add_node("d_generate", self._n_d_generate)
        g.add_node("d_explain", self._n_d_explain)
        g.add_node("m_answer", self._n_meta)
        g.add_node("finalize", self._n_finalize)

        # ---- 骨架 ----
        g.add_edge(START, "intent")
        # 意图三分流
        g.add_conditional_edges("intent", self._r_intent,
                                {"q_context": "q_context", "d_context": "d_context", "m_answer": "m_answer"})
        # query 主链
        g.add_edge("q_context", "q_generate")
        g.add_conditional_edges("q_generate", self._r_generate,
                                {"q_validate": "q_validate", "finalize": "finalize"})
        g.add_conditional_edges("q_validate", self._r_validate,
                                {"q_guard": "q_guard", "q_correct": "q_correct", "finalize": "finalize"})
        g.add_conditional_edges("q_guard", self._r_guard,
                                {"q_fanout": "q_fanout", "finalize": "finalize"})
        g.add_conditional_edges("q_fanout", self._r_fanout,
                                # q_validate：LLM 裁决 risky 后采用其重写 SQL，回全链路复检
                                {"q_execute": "q_execute", "q_correct": "q_correct",
                                 "q_validate": "q_validate"})
        g.add_conditional_edges("q_execute", self._r_execute,
                                {"q_cache": "q_cache", "q_correct": "q_correct", "finalize": "finalize"})
        g.add_conditional_edges("q_correct", self._r_correct,
                                {"q_validate": "q_validate", "finalize": "finalize"})
        g.add_edge("q_cache", "q_explain")
        g.add_edge("q_explain", "finalize")
        # dml 分支
        g.add_edge("d_context", "d_generate")
        g.add_conditional_edges("d_generate", self._r_d_generate,
                                {"d_explain": "d_explain", "finalize": "finalize"})
        g.add_edge("d_explain", "finalize")
        # meta 分支
        g.add_edge("m_answer", "finalize")
        g.add_edge("finalize", END)

        return g.compile()

    # ==================================================================
    # 对外入口：iter_run 产出事件 dict；run() 消费并返回聚合结果
    # ==================================================================
    def iter_run(self, question: str, session_id: str = None) -> Iterator[dict]:
        result = PipelineResult(question=question, session_id=session_id)
        tracker = TurnTracker(enabled=self.metrics_enabled)
        initial: NL2SQLState = {
            "question": question,
            "session_id": session_id,
            "result": result,                       # 可变引用：图内就地更新
            "tracker": tracker,
            "attempt": 0,
            "max_rounds": self.corrector.max_rounds if self.corrector_enabled else 0,
            "error_message": "",
            "extra_context": "",
            "candidate_tables": None,
            "fanout_waived": False,
            "route": "",
        }
        config = {"configurable": {"thread_id": session_id or uuid.uuid4().hex}}
        # custom 通道透出各节点 _emit 的事件；结束后经 StopIteration.value 返回 result
        yield from (chunk for chunk in self.graph.stream(initial, config, stream_mode="custom"))
        return result

    def run(self, question: str, session_id: str = None) -> PipelineResult:
        """同步聚合版本：消费图执行的事件流，返回 PipelineResult"""
        gen = self.iter_run(question, session_id)
        try:
            while True:
                next(gen)
        except StopIteration as e:
            return e.value

    # ==================================================================
    # 节点：意图分类（正则快路径 + LLM 兜底；开关关闭直走 query）
    # ==================================================================
    def _n_intent(self, state: NL2SQLState) -> dict:
        result, tracker = state["result"], state["tracker"]
        with tracker.step("意图分类"):
            result.intent = self.intent_classifier.classify(state["question"]) if self.intent_classifier else "query"
        logger.info("意图分类结果: %s | 问题: %s", result.intent, state["question"])
        _emit("intent", {"intent": result.intent})
        return {"route": result.intent}

    def _r_intent(self, state: NL2SQLState) -> str:
        return {"query": "q_context", "dml": "d_context", "meta": "m_answer"}[state["result"].intent]

    # ==================================================================
    # 节点：query 主链
    # ==================================================================
    def _select_candidate_tables(self, question: str, last_tables: Optional[List[str]]) -> Optional[List[str]]:
        """Schema 检索命中表 ∪ 上轮表 ∪ 关系图谱 1 跳邻接表（补 JOIN 关联表召回缺口）"""
        candidate_tables = (
            self.schema_retriever.select_tables(question, extra_tables=last_tables)
            if self.schema_retriever else None
        )
        if candidate_tables and self.relation_graph and self.relation_graph.ready:
            expanded = self.relation_graph.neighbors(candidate_tables, hops=1)
            if len(expanded) > len(candidate_tables):
                logger.info("候选表沿关系图谱扩展 1 跳邻接：%s → %s", candidate_tables, expanded)
            candidate_tables = expanded
        return candidate_tables

    def _n_q_context(self, state: NL2SQLState) -> dict:
        """上下文增强：多轮上下文 + Schema检索 + 术语映射 + 值链接"""
        result, tracker = state["result"], state["tracker"]
        question, session_id = state["question"], state["session_id"]
        with tracker.step("上下文增强"):
            conv_ctx = self.conversation.build_context(session_id) if session_id else ""
            # Schema 检索（大库 Top-K；小库/未开启返回 None 走全量）；
            # 多轮场景上轮 SQL 涉及表强制并入；命中表再沿关系图扩展 JOIN 邻接表
            last_tables = self.conversation.last_tables(session_id)
            candidate_tables = self._select_candidate_tables(question, last_tables)
            term_ctx, term_hits = self.term_mapper.build_context(question)
            value_ctx, value_hits = self.value_linker.build_context(question, tables=candidate_tables)
            result.term_mappings = [
                {"term": h.term, "expression": h.expression, "translation": h.translation} for h in term_hits
            ]
            result.value_mappings = [
                {"user_term": m.user_term, "db_value": m.db_value, "column": m.column} for m in value_hits
            ]
            extra_context = "\n\n".join(c for c in (conv_ctx, term_ctx, value_ctx) if c)
            # 模板分位注入：多轮上下文紧跟用户问题；术语/值链接置于表结构之后作参考
            reference_context = "\n\n".join(c for c in (term_ctx, value_ctx) if c)
        return {
            "extra_context": extra_context,
            "conversation_context": conv_ctx,
            "reference_context": reference_context,
            "candidate_tables": candidate_tables,
        }

    def _n_q_generate(self, state: NL2SQLState) -> dict:
        result, tracker = state["result"], state["tracker"]
        with tracker.step("SQL生成"):
            try:
                gen = self.generator.generate(
                    state["question"],
                    tables=state["candidate_tables"],
                    conversation_context=state.get("conversation_context", ""),
                    reference_context=state.get("reference_context", ""),
                )
                tracker.add_tokens(gen.prompt_tokens, gen.completion_tokens, gen.total_tokens)
            except Exception as e:
                logger.error("SQL 生成失败: %s", e, exc_info=e)
                result.error, result.error_kind = f"SQL 生成失败：{e}", "generate"
                _emit("error", {"message": result.error, "kind": result.error_kind})
                return {"route": "finalize"}
        result.sql = gen.sql
        result.raw_output = gen.raw_output
        if not gen.sql:
            result.error, result.error_kind = "模型未能生成有效 SQL", "generate"
            _emit("error", {"message": result.error, "kind": result.error_kind})
            return {"route": "finalize"}
        _emit("sql", {"sql": result.sql})
        return {"route": "q_validate"}

    def _r_generate(self, state: NL2SQLState) -> str:
        return state["route"]

    def _n_q_validate(self, state: NL2SQLState) -> dict:
        """静态校验：语法/幻觉表字段可纠正 → q_correct 回喂重写；耗尽 → 终止"""
        result, tracker = state["result"], state["tracker"]
        with tracker.step("静态校验"):
            try:
                self.validator.validate(result.sql)
            except SQLValidationError as e:
                logger.warning("静态校验失败(%s): %s | SQL: %s", e.kind, e, result.sql)
                error_message = f"SQL 校验失败：{e}"
                if state["attempt"] >= state["max_rounds"]:
                    result.error, result.error_kind = error_message, f"validation_{e.kind}"
                    _emit("error", {"message": result.error, "kind": result.error_kind})
                    return {"route": "finalize", "error_message": error_message}
                return {"route": "q_correct", "error_message": error_message}
        return {"route": "q_guard"}

    def _r_validate(self, state: NL2SQLState) -> str:
        return state["route"]

    def _n_q_guard(self, state: NL2SQLState) -> dict:
        """安全守卫：关键词+AST 双过滤 + 强制 LIMIT 注入；拦截不可纠正直接终止"""
        result, tracker = state["result"], state["tracker"]
        with tracker.step("安全守卫"):
            try:
                result.sql = self.guard.check(result.sql)
            except SQLGuardError as e:
                logger.warning("安全守卫拦截: %s | SQL: %s", e.reason, result.sql)
                result.error, result.error_kind = f"安全守卫拦截：{e.reason}", "guard"
                _emit("error", {"message": result.error, "kind": result.error_kind})
                return {"route": "finalize"}
        return {"route": "q_fanout"}

    def _r_guard(self, state: NL2SQLState) -> str:
        return state["route"]

    def _n_q_fanout(self, state: NL2SQLState) -> dict:
        """JOIN 扇出检测（守卫改写后的 SQL）：
        - fanout_waived：当前 SQL 已由 LLM 裁决为误报 → 直接放行（重写后该标记会被清除）
        - ON 键未使用已知关联键：仅记录告警，不拦截（可能存在合理的多候选键）
        - 静态/动态检出扇出且纠错轮次未满 → LLM 结合问题语义裁决（一次调用）：
            safe   原 SQL 置 fanout_waived 放行执行；
            risky  采用裁决返回的重写 SQL，回 q_validate 经全链路复检（含本检测）；
            unknown（调用/解析失败）或 risky 无有效改写/改写无变化 → 回退 q_correct
                   按静态提示走常规纠错（correct_prompt 含扇出规则）；
          所有分支均受 attempt/max_rounds 兜底，不会死循环；轮次耗尽则告警后放行不阻断"""
        result, tracker = state["result"], state["tracker"]
        if state.get("fanout_waived"):
            logger.info("扇出检测已裁决放行（fanout_waived），跳过: %s", result.sql[:120])
            return {"route": "q_execute"}
        with tracker.step("扇出检测"):
            if self.relation_graph and self.relation_graph.ready:
                try:
                    stmt = sqlglot.parse_one(result.sql, read="mysql")
                    for w in self.relation_graph.unknown_join_warnings(stmt):
                        logger.info("JOIN 关联键告警（仅记录）: %s", w)
                except Exception:
                    pass
            fanout_msg = self.fanout_checker.check(result.sql)
        if not fanout_msg:
            return {"route": "q_execute"}
        logger.warning("JOIN 扇出%s: %s | SQL: %s",
                       "确认" if "确认" in fanout_msg else "风险", fanout_msg[:80], result.sql[:120])
        if state["attempt"] >= state["max_rounds"]:
            logger.info("扇出告警但纠错轮次耗尽，放行执行")
            return {"route": "q_execute"}

        with tracker.step("扇出裁决"):
            try:
                verdict, reason, rewritten, usage = self.corrector.adjudicate_fanout(
                    state["question"], result.sql, fanout_msg,
                    tables=state.get("candidate_tables"),
                )
            except Exception as e:
                logger.warning("扇出裁决节点异常，回退常规纠错: %s", str(e).splitlines()[0])
                return {"route": "q_correct", "error_message": fanout_msg}
        tracker.add_tokens(
            usage.get("prompt_tokens", 0),
            usage.get("completion_tokens", 0),
            usage.get("total_tokens", 0),
        )
        logger.info("扇出裁决结果 verdict=%s：%s", verdict, reason)
        if verdict == "safe":
            # safe 不写 correction_trail：trail 长度即评测 corrections 口径，只记真实重写
            return {"route": "q_execute", "fanout_waived": True}
        if verdict == "risky" and rewritten:
            cur_norm = result.sql.strip().rstrip(";").strip()
            if rewritten.strip().rstrip(";").strip() != cur_norm:
                attempt = state["attempt"] + 1
                result.sql = rewritten
                result.correction_trail.append(
                    {"round": attempt, "error": fanout_msg, "sql": rewritten}
                )
                _emit("correcting", {"round": attempt, "error": fanout_msg, "sql": rewritten})
                _emit("sql", {"sql": rewritten})
                return {"route": "q_validate", "attempt": attempt}
            logger.info("扇出裁决 risky 但重写 SQL 与原 SQL 相同，回退常规纠错")
        return {"route": "q_correct", "error_message": fanout_msg}

    def _r_fanout(self, state: NL2SQLState) -> str:
        return state["route"]

    def _n_q_execute(self, state: NL2SQLState) -> dict:
        """执行：失败可纠正 → q_correct；成功 0 行且诊断值缺失 → q_correct；否则落地"""
        result, tracker = state["result"], state["tracker"]
        with tracker.step("SQL执行"):
            _emit("executing", {"sql": result.sql})
            try:
                exec_result = self.executor.execute(result.sql)
            except SQLExecutionError as e:
                logger.error("SQL 执行失败(%s): %s", e.kind, e)
                error_message = f"SQL 执行失败：{e}"
                if state["attempt"] >= state["max_rounds"]:
                    result.error, result.error_kind = error_message, e.kind
                    _emit("error", {"message": result.error, "kind": result.error_kind})
                    return {"route": "finalize", "error_message": error_message}
                return {"route": "q_correct", "error_message": error_message}

        # 执行成功：0 行且存在"值不存在"疑点 → 回喂重写（合法 0 行不触发）
        if not exec_result["rows"] and state["attempt"] < state["max_rounds"]:
            diag, _missing = self.corrector.diagnose_zero_row(result.sql)
            if diag:
                logger.info("0 行疑似值缺失，进入纠错: %s", diag)
                return {"route": "q_correct", "error_message": diag}

        # 落地本轮流结果
        result.columns = exec_result["columns"]
        result.rows = exec_result["rows"]
        result.truncated = exec_result["truncated"]
        result.total = exec_result["total"] if exec_result["total"] >= 0 else len(result.rows)
        return {"route": "q_cache"}

    def _r_execute(self, state: NL2SQLState) -> str:
        return state["route"]

    def _n_q_correct(self, state: NL2SQLState) -> dict:
        """纠错重写：错误信息 + 出错 SQL 回喂 LLM；重写产物回 q_validate 再过校验+守卫"""
        result, tracker = state["result"], state["tracker"]
        attempt = state["attempt"] + 1
        with tracker.step("自动纠错"):
            try:
                rewritten, usage = self.corrector.rewrite(
                    state["question"], result.sql, state["error_message"],
                    extra_context=state["extra_context"],
                    tables=state.get("candidate_tables"),
                )
                tracker.add_tokens(
                    usage.get("prompt_tokens", 0),
                    usage.get("completion_tokens", 0),
                    usage.get("total_tokens", 0),
                )
            except Exception as e:
                logger.error("纠错重写调用失败: %s", e)
                rewritten = None
        if not rewritten:
            result.error, result.error_kind = state["error_message"] or "纠错重写失败", "correction"
            _emit("error", {"message": result.error, "kind": result.error_kind})
            return {"route": "finalize", "attempt": attempt}
        result.sql = rewritten
        result.correction_trail.append(
            {"round": attempt, "error": state["error_message"], "sql": rewritten}
        )
        _emit("correcting", {"round": attempt, "error": state["error_message"], "sql": rewritten})
        _emit("sql", {"sql": rewritten})
        return {"route": "q_validate", "attempt": attempt, "fanout_waived": False}

    def _r_correct(self, state: NL2SQLState) -> str:
        return state["route"]

    def _n_q_cache(self, state: NL2SQLState) -> dict:
        """结果入缓存（query_id 分页拉取支撑），推送首屏数据"""
        result = state["result"]
        result.query_id = self.result_cache.put(
            result.columns, result.rows, result.total, result.truncated, sql=result.sql,
        )
        _emit("result", {
            "query_id": result.query_id,
            "columns": result.columns,
            "rows": result.rows[:RESULT_PAGE_SIZE],
            "total": result.total,
            "truncated": result.truncated,
            "executed": True,
            "page_size": RESULT_PAGE_SIZE,
        })
        return {}

    def _n_q_explain(self, state: NL2SQLState) -> dict:
        """解释生成：失败返回空串不阻断（增益能力）"""
        result, tracker = state["result"], state["tracker"]
        with tracker.step("解释生成"):
            result.explanation = self.explainer.explain(result.sql)
        if result.explanation:
            _emit("explanation", {"text": result.explanation})
        return {}

    # ==================================================================
    # 节点：dml 分支（仅生成 + 解释，绝不执行）
    # ==================================================================
    def _n_d_context(self, state: NL2SQLState) -> dict:
        result, tracker = state["result"], state["tracker"]
        question, session_id = state["question"], state["session_id"]
        with tracker.step("上下文增强"):
            conv_ctx = self.conversation.build_context(session_id) if session_id else ""
            last_tables = self.conversation.last_tables(session_id)
            candidate_tables = self._select_candidate_tables(question, last_tables)
        result.executed = False
        return {"extra_context": conv_ctx, "candidate_tables": candidate_tables}

    def _n_d_generate(self, state: NL2SQLState) -> dict:
        result, tracker = state["result"], state["tracker"]
        with tracker.step("SQL生成"):
            try:
                gen = self.generator.generate(
                    state["question"], extra_context=state["extra_context"],
                    mode="dml", tables=state["candidate_tables"],
                )
                tracker.add_tokens(gen.prompt_tokens, gen.completion_tokens, gen.total_tokens)
            except Exception as e:
                logger.error("DML SQL 生成失败: %s", e, exc_info=e)
                result.error, result.error_kind = f"SQL 生成失败：{e}", "generate"
                _emit("error", {"message": result.error, "kind": result.error_kind})
                return {"route": "finalize"}
        result.sql = gen.sql
        result.raw_output = gen.raw_output
        if not gen.sql:
            result.error, result.error_kind = "模型未能生成有效 SQL", "generate"
            _emit("error", {"message": result.error, "kind": result.error_kind})
            return {"route": "finalize"}
        _emit("sql", {"sql": result.sql})
        return {"route": "d_explain"}

    def _r_d_generate(self, state: NL2SQLState) -> str:
        return state["route"]

    def _n_d_explain(self, state: NL2SQLState) -> dict:
        result, tracker = state["result"], state["tracker"]
        with tracker.step("解释生成"):
            explanation = self.explainer.explain(result.sql) or "该语句为写操作，按安全策略仅生成不执行。"
        result.explanation = f"（按安全策略，写操作仅生成 SQL 供确认，不会执行）{explanation}"
        _emit("explanation", {"text": result.explanation})
        # dml 不执行，推送空结果标记
        _emit("result", {
            "query_id": None, "columns": [], "rows": [], "total": 0,
            "truncated": False, "executed": False,
        })
        return {}

    # ==================================================================
    # 节点：meta 分支（Schema 元数据直接回答，不生成 SQL）
    # ==================================================================
    def _n_meta(self, state: NL2SQLState) -> dict:
        result, tracker = state["result"], state["tracker"]
        result.sql = ""
        result.executed = False
        with tracker.step("元信息回答"):
            try:
                llm_result = self.llm.chat([
                    {"role": "user", "content":
                        f"你是数据库助手。仅依据下面的表结构信息回答用户问题，用通俗中文简短作答，"
                        f"不要生成 SQL：\n\n{self.schema_manager.schema_context}\n\n【用户问题】{state['question']}"},
                ])
                tracker.add_tokens(llm_result.prompt_tokens, llm_result.completion_tokens, llm_result.total_tokens)
                result.answer = self.formatter.format_answer(llm_result.content)
            except Exception as e:
                logger.error("元信息回答失败: %s", e)
                result.error, result.error_kind = f"元信息回答失败：{e}", "generate"
                _emit("error", {"message": result.error, "kind": result.error_kind})
                return {}
        if result.answer:
            _emit("explanation", {"text": result.answer})
        return {}

    # ==================================================================
    # 节点：公共尾部（三分支汇合）——usage 统计 + 记轮次 + done
    # ==================================================================
    def _n_finalize(self, state: NL2SQLState) -> dict:
        result, tracker = state["result"], state["tracker"]
        question, session_id = state["question"], state["session_id"]

        usage = tracker.finish()
        result.usage = usage
        _emit("usage", usage)

        # 记录轮次（含回放所需的解释/结果快照/统计）；失败轮次与 meta 不记录
        if session_id and not result.error and result.intent in ("query", "dml"):
            self.conversation.add_turn(
                session_id, question, result.sql,
                tables=self._extract_tables(result.sql), intent=result.intent,
                explanation=result.explanation,
                result=(
                    {
                        "columns": result.columns,
                        "rows": result.rows,
                        "total": result.total,
                        "truncated": result.truncated,
                    }
                    if result.intent == "query" and result.executed else None
                ),
                usage=usage,
            )
        _emit("done", {})
        return {}

    # ==================================================================
    @staticmethod
    def _extract_tables(sql: str) -> List[str]:
        """从 SQL 提取涉及的真实表名（供会话记录）"""
        try:
            stmt = sqlglot.parse_one(sql, read="mysql")
            return sorted({t.name for t in stmt.find_all(exp.Table)})
        except Exception:
            return []
