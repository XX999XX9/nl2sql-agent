# 流水线编排器（生成器核心）：iter_run 逐步产出 SSE 事件，run() 消费同一生成器
# 返回聚合结果——单一事实来源，JSON 与 SSE 两个出口共享同一流程。
#
# 分支路由：意图分类 →
#   query：多轮上下文 → 术语/值链接 → 生成 → AST校验 → 守卫 → 执行 → 结果缓存 → 解释 → 记轮次
#   dml  ：生成（专用模板）→ 解释，仅返回 SQL 绝不执行
#   meta ：基于 Schema 元数据直接回答
# 每步经 TurnTracker 埋点（耗时 + token 分步），finish() 后经 usage 事件推送

from dataclasses import dataclass, field
from typing import Iterator, List, Optional

import sqlglot
from sqlglot import exp

from app.formatter.output_formatter import OutputFormatter
from app.metrics.turn_tracker import TurnTracker
from app.model.factory import ModelFactory
from app.nl2sql.conversation import ConversationManager
from app.nl2sql.intent_classifier import IntentClassifier
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


class NL2SQLPipeline:
    """NL2SQL 完整流水线编排入口"""

    def __init__(self, conversation_manager: ConversationManager = None,
                 result_cache: ResultCache = None):
        factory = ModelFactory()
        self.llm = factory.create_llm()
        self.schema_manager = SchemaManager()
        self.conversation = conversation_manager or ConversationManager()
        self.result_cache = result_cache or ResultCache()
        agent_cfg = load_yaml(BACKEND_ROOT / "config" / "agent.yml")
        features = agent_cfg.get("features", {})
        # 功能开关（agent.yml features.*，默认全开）
        self.intent_enabled = features.get("intent_classifier", True)
        self.metrics_enabled = features.get("metrics_enabled", True)
        self.corrector_enabled = features.get("self_correction", True)
        self.intent_classifier = IntentClassifier(self.llm) if self.intent_enabled else None
        self.term_mapper = TermMapper()
        self.value_linker = ValueLinker()
        self.schema_retriever = self._init_schema_retriever(agent_cfg, factory)
        self.generator = SQLGenerator(self.llm, self.schema_manager)
        self.validator = SQLValidator(self.schema_manager)
        self.guard = SQLGuard()
        self.executor = SQLExecutor()
        self.explainer = SQLExplainer(self.llm, self.schema_manager)
        self.formatter = OutputFormatter()
        self.corrector = SelfCorrector(
            self.llm, self.schema_manager, self.value_linker,
            max_rounds=load_yaml(BACKEND_ROOT / "config" / "nl2sql.yml").get("correction", {}).get("max_rounds", 2),
        )

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
    # 生成器核心：iter_run 产出事件 dict；run() 消费并返回聚合结果
    # ==================================================================
    def iter_run(self, question: str, session_id: str = None) -> Iterator[dict]:
        result = PipelineResult(question=question, session_id=session_id)
        tracker = TurnTracker()
        return_result = result  # 供 run() 经 StopIteration.value 取回

        # 0. 意图分类
        with tracker.step("意图分类"):
            result.intent = self.intent_classifier.classify(question)
        logger.info("意图分类结果: %s | 问题: %s", result.intent, question)
        yield {"type": "intent", "data": {"intent": result.intent}}

        if result.intent == "dml":
            gen = self._iter_dml(question, session_id, result, tracker)
        elif result.intent == "meta":
            gen = self._iter_meta(question, result, tracker)
        else:
            gen = self._iter_query(question, session_id, result, tracker)
        yield from gen

        # 终止事件：usage 摘要 + done
        usage = tracker.finish()
        result.usage = usage
        yield {"type": "usage", "data": usage}

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

        yield {"type": "done", "data": {}}
        return return_result

    def run(self, question: str, session_id: str = None) -> PipelineResult:
        """同步聚合版本：消费 iter_run 事件流，返回 PipelineResult"""
        gen = self.iter_run(question, session_id)
        try:
            while True:
                next(gen)
        except StopIteration as e:
            return e.value

    # ==================================================================
    # query 分支：完整链路
    # ==================================================================
    def _iter_query(self, question, session_id, result, tracker):
        # 1. 上下文增强：多轮上下文 + Schema检索 + 术语映射 + 值链接
        with tracker.step("上下文增强"):
            conv_ctx = self.conversation.build_context(session_id) if session_id else ""
            # Schema 检索（大库 Top-K；小库/未开启返回 None 走全量）；
            # 多轮场景上轮 SQL 涉及表强制并入候选
            last_tables = self.conversation.last_tables(session_id)
            candidate_tables = (
                self.schema_retriever.select_tables(question, extra_tables=last_tables)
                if self.schema_retriever else None
            )
            term_ctx, term_hits = self.term_mapper.build_context(question)
            value_ctx, value_hits = self.value_linker.build_context(question, tables=candidate_tables)
            result.term_mappings = [
                {"term": h.term, "expression": h.expression, "translation": h.translation} for h in term_hits
            ]
            result.value_mappings = [
                {"user_term": m.user_term, "db_value": m.db_value, "column": m.column} for m in value_hits
            ]
            extra_context = "\n\n".join(c for c in (conv_ctx, term_ctx, value_ctx) if c)

        # 2. 生成 SQL
        with tracker.step("SQL生成"):
            try:
                gen = self.generator.generate(question, extra_context=extra_context, tables=candidate_tables)
                tracker.add_tokens(gen.prompt_tokens, gen.completion_tokens, gen.total_tokens)
            except Exception as e:
                logger.error("SQL 生成失败: %s", e, exc_info=e)
                result.error, result.error_kind = f"SQL 生成失败：{e}", "generate"
                yield {"type": "error", "data": {"message": result.error, "kind": result.error_kind}}
                return
        result.sql = gen.sql
        result.raw_output = gen.raw_output
        if not gen.sql:
            result.error, result.error_kind = "模型未能生成有效 SQL", "generate"
            yield {"type": "error", "data": {"message": result.error, "kind": result.error_kind}}
            return
        yield {"type": "sql", "data": {"sql": result.sql}}

        # 3~5. 校验 → 守卫 → 执行；可纠正失败（校验错/执行错/值不存在0行）
        #       进入自动纠错循环（最多 max_rounds 轮回喂重写，每轮仍过校验+守卫）；
        #       self_correction 开关关闭时 max_rounds 生效为 0（只跑原始 SQL）
        error_message = ""
        max_rounds = self.corrector.max_rounds if self.corrector_enabled else 0
        for attempt in range(max_rounds + 1):     # attempt 0 为原始 SQL，1..N 为重写
            if attempt > 0:
                # 纠错重写：错误信息 + 出错 SQL 回喂 LLM
                with tracker.step("自动纠错"):
                    try:
                        rewritten, usage = self.corrector.rewrite(
                            question, result.sql, error_message, extra_context=extra_context,
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
                    result.error, result.error_kind = error_message or "纠错重写失败", "correction"
                    yield {"type": "error", "data": {"message": result.error, "kind": result.error_kind}}
                    return
                result.sql = rewritten
                result.correction_trail.append(
                    {"round": attempt, "error": error_message, "sql": rewritten}
                )
                yield {"type": "correcting", "data": {
                    "round": attempt, "error": error_message, "sql": rewritten,
                }}
                yield {"type": "sql", "data": {"sql": rewritten}}

            # 静态校验（可纠正：语法/幻觉表字段 → 回喂重写）
            with tracker.step("静态校验"):
                try:
                    self.validator.validate(result.sql)
                except SQLValidationError as e:
                    logger.warning("静态校验失败(%s): %s | SQL: %s", e.kind, e, result.sql)
                    error_message = f"SQL 校验失败：{e}"
                    if attempt >= max_rounds:
                        result.error, result.error_kind = error_message, f"validation_{e.kind}"
                        yield {"type": "error", "data": {"message": result.error, "kind": result.error_kind}}
                        return
                    continue

            # 安全守卫（不可纠正：安全拦截直接终止）
            with tracker.step("安全守卫"):
                try:
                    result.sql = self.guard.check(result.sql)
                except SQLGuardError as e:
                    logger.warning("安全守卫拦截: %s | SQL: %s", e.reason, result.sql)
                    result.error, result.error_kind = f"安全守卫拦截：{e.reason}", "guard"
                    yield {"type": "error", "data": {"message": result.error, "kind": result.error_kind}}
                    return

            # 执行
            with tracker.step("SQL执行"):
                yield {"type": "executing", "data": {"sql": result.sql}}
                try:
                    exec_result = self.executor.execute(result.sql)
                except SQLExecutionError as e:
                    logger.error("SQL 执行失败(%s): %s", e.kind, e)
                    error_message = f"SQL 执行失败：{e}"
                    if attempt >= max_rounds:
                        result.error, result.error_kind = error_message, e.kind
                        yield {"type": "error", "data": {"message": result.error, "kind": result.error_kind}}
                        return
                    continue

            # 执行成功：0 行且存在"值不存在"疑点 → 回喂重写（合法 0 行不触发）
            if not exec_result["rows"] and attempt < max_rounds:
                diag, _missing = self.corrector.diagnose_zero_row(result.sql)
                if diag:
                    logger.info("0 行疑似值缺失，进入纠错: %s", diag)
                    error_message = diag
                    continue

            # 落地本轮流结果
            result.columns = exec_result["columns"]
            result.rows = exec_result["rows"]
            result.truncated = exec_result["truncated"]
            result.total = exec_result["total"] if exec_result["total"] >= 0 else len(result.rows)
            break

        # 6. 结果入缓存（分页拉取支撑），推送首屏数据
        if not result.error:
            result.query_id = self.result_cache.put(
                result.columns, result.rows, result.total, result.truncated, sql=result.sql,
            )
            yield {
                "type": "result",
                "data": {
                    "query_id": result.query_id,
                    "columns": result.columns,
                    "rows": result.rows[:RESULT_PAGE_SIZE],
                    "total": result.total,
                    "truncated": result.truncated,
                    "executed": True,
                    "page_size": RESULT_PAGE_SIZE,
                },
            }

        # 7. 解释（成功失败都解释已生成的 SQL 供用户确认）
        if result.sql:
            with tracker.step("解释生成"):
                result.explanation = self.explainer.explain(result.sql)
            if result.explanation:
                yield {"type": "explanation", "data": {"text": result.explanation}}

    # ==================================================================
    # dml 分支：仅生成 + 解释，绝不执行
    # ==================================================================
    def _iter_dml(self, question, session_id, result, tracker):
        with tracker.step("上下文增强"):
            conv_ctx = self.conversation.build_context(session_id) if session_id else ""
            last_tables = self.conversation.last_tables(session_id)
            candidate_tables = (
                self.schema_retriever.select_tables(question, extra_tables=last_tables)
                if self.schema_retriever else None
            )
        with tracker.step("SQL生成"):
            try:
                gen = self.generator.generate(question, extra_context=conv_ctx, mode="dml", tables=candidate_tables)
                tracker.add_tokens(gen.prompt_tokens, gen.completion_tokens, gen.total_tokens)
            except Exception as e:
                logger.error("DML SQL 生成失败: %s", e, exc_info=e)
                result.error, result.error_kind = f"SQL 生成失败：{e}", "generate"
                yield {"type": "error", "data": {"message": result.error, "kind": result.error_kind}}
                return
        result.sql = gen.sql
        result.raw_output = gen.raw_output
        result.executed = False
        if not gen.sql:
            result.error, result.error_kind = "模型未能生成有效 SQL", "generate"
            yield {"type": "error", "data": {"message": result.error, "kind": result.error_kind}}
            return

        yield {"type": "sql", "data": {"sql": result.sql}}

        with tracker.step("解释生成"):
            explanation = self.explainer.explain(result.sql) or "该语句为写操作，按安全策略仅生成不执行。"
        result.explanation = f"（按安全策略，写操作仅生成 SQL 供确认，不会执行）{explanation}"
        yield {"type": "explanation", "data": {"text": result.explanation}}

        # dml 不执行，推送空结果标记
        yield {"type": "result", "data": {
            "query_id": None, "columns": [], "rows": [], "total": 0,
            "truncated": False, "executed": False,
        }}

    # ==================================================================
    # meta 分支：Schema 元数据直接回答
    # ==================================================================
    def _iter_meta(self, question, result, tracker):
        result.sql = ""
        result.executed = False
        with tracker.step("元信息回答"):
            try:
                llm_result = self.llm.chat([
                    {"role": "user", "content":
                        f"你是数据库助手。仅依据下面的表结构信息回答用户问题，用通俗中文简短作答，"
                        f"不要生成 SQL：\n\n{self.schema_manager.schema_context}\n\n【用户问题】{question}"},
                ])
                tracker.add_tokens(llm_result.prompt_tokens, llm_result.completion_tokens, llm_result.total_tokens)
                result.answer = self.formatter.format_answer(llm_result.content)
            except Exception as e:
                logger.error("元信息回答失败: %s", e)
                result.error, result.error_kind = f"元信息回答失败：{e}", "generate"
                yield {"type": "error", "data": {"message": result.error, "kind": result.error_kind}}
                return
        if result.answer:
            yield {"type": "explanation", "data": {"text": result.answer}}

    # ==================================================================
    @staticmethod
    def _extract_tables(sql: str) -> List[str]:
        """从 SQL 提取涉及的真实表名（供会话记录）"""
        try:
            stmt = sqlglot.parse_one(sql, read="mysql")
            return sorted({t.name for t in stmt.find_all(exp.Table)})
        except Exception:
            return []
