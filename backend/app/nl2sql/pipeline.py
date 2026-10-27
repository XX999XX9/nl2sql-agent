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
#   - 多轮会话、Trace、断点续跑共用同一事实源：LangGraph 检查点（Redis）。
#     会话视图与 Trace 视图都是检查点的投影，避免正文双写；
#   - events / turns 两个列表通道使用 DeltaChannel：每个检查点只落增量写，
#     读取时沿父检查点重放，轮次增长不会让单条检查点线性膨胀；
#   - 线程按“用户 + 会话”隔离（一次性提问用独立线程），删除会话即删除线程。
#   - 功能开关接线：intent_classifier（关闭直走 query）/ metrics_enabled（Tracker 空转）/
#     self_correction（max_rounds 生效为 0）/ schema_retriever（None 走全量）

import time
import uuid
import os
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Annotated, Iterator, List, Optional, Sequence, TypedDict

import sqlglot
from langgraph.channels import DeltaChannel
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from sqlglot import exp

from app.formatter.output_formatter import OutputFormatter
from app.metrics.turn_tracker import TurnTracker
from app.model.factory import ModelFactory
from app.nl2sql.conversation import ConversationManager
from app.persistence.redis_checkpointer import RedisCheckpointSaver
from app.persistence.redis_store import thread_id_for
from app.persistence.store import RUN_STATUS_ERROR, RUN_STATUS_OK, SessionRunStore
from app.nl2sql.explain_gate import ExplainGate
from app.nl2sql.fanout_checker import FanoutChecker
from app.nl2sql.intent_classifier import IntentClassifier
from app.nl2sql.relation_graph import RelationGraph
from app.nl2sql.result_cache import ResultCache
from app.nl2sql.schema_manager import SchemaManager
from app.nl2sql.schema_retriever import SchemaRetriever
from app.nl2sql.self_corrector import SelfCorrector
from app.nl2sql.sql_executor import SQLExecutor, SQLExecutionError
from app.nl2sql.sql_explainer import SQLExplainer
from app.nl2sql.table_selector import ClarificationRequest, TableSelector
from app.nl2sql.capabilities.layer import build_capability_layer
from app.nl2sql.capabilities.protocol import ExecutionContext
from app.nl2sql.sql_generator import GenerationResult, SQLGenerator
from app.nl2sql.sql_guard import SQLGuard, SQLGuardError
from app.nl2sql.sql_validator import SQLValidator, SQLValidationError
from app.nl2sql.term_mapper import TermMapper
from app.nl2sql.value_linker import ValueLinker
from app.utils.config_handler import load_yaml
from app.utils.logger_handler import setup_logger
from app.utils.path_tool import BACKEND_ROOT

logger = setup_logger()

RESULT_PAGE_SIZE = 50   # result 事件携带的首屏行数（其余走分页拉取）
REPLAY_ROWS_LIMIT = 50  # 会话轮次里保留的结果快照行数（控制检查点体积）

# 当前轮事件的随流缓冲：finalize 节点据此把本轮事件写回状态（供 Trace 投影）。
# 用 ContextVar 承载，可在不改变节点函数签名的情况下旁路收集。
_EMIT_BUFFER: ContextVar[Optional[List[dict]]] = ContextVar("emit_buffer", default=None)


def _reducer_append(state: Optional[list], writes: Sequence) -> list:
    """DeltaChannel 追加式 reducer：把每次写（可含多个元素）顺序拼到列表尾部。
    必须确定性且与批处理无关（顺序无关基准：同批内容一致）。"""
    out = list(state) if state else []
    for write in writes:
        if isinstance(write, list):
            out.extend(write)
        else:
            out.append(write)
    return out


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
    sql_label: str = ""                 # 解读节点裁判：conform/risk/unknown；空=未裁判（解读关闭）
    need_clarification: bool = False    # 表筛选阶段判定需向用户澄清（本轮不出 SQL）
    clarify_id: Optional[str] = None
    clarification_questions: List[str] = field(default_factory=list)
    reference_turn_id: Optional[str] = None   # 本轮显式引用的历史轮（透出供前端展示）
    usage: dict = field(default_factory=dict)
    term_mappings: List[dict] = field(default_factory=list)
    value_mappings: List[dict] = field(default_factory=list)
    correction_trail: List[dict] = field(default_factory=list)   # 语义/校验纠错轨迹（评测 corrections 口径）
    optimization_trail: List[dict] = field(default_factory=list) # EXPLAIN 性能优化轨迹（独立计数）
    capability_trail: List[dict] = field(default_factory=list)   # Skill/Tool 申请轨迹（供调用正确率/误用率评测）
    error: Optional[str] = None
    # error_kind 错误类型汇总（全链路唯一定义处，新增类型时同步更新这里与 routes/chat.py 的错误码映射）：
    #   generate           模型生成失败/未产出有效 SQL（query、dml、meta 三类意图的生成节点）
    #   validation_syntax  静态校验：SQL 语法错误/无法解析/多条语句/非 SELECT 语句（sql_validator）
    #   validation_join    静态校验：无 ON/USING 的笛卡尔积 JOIN（sql_validator）
    #   validation_table   静态校验：引用了不存在的表（sql_validator）
    #   validation_column  静态校验：引用了不存在的字段（sql_validator）
    #   guard              安全守卫拦截（sql_guard，如写操作/危险函数）
    #   syntax             数据库执行报错：SQL 语法/执行计划层面被 MySQL 拒绝（sql_executor）
    #   permission         数据库执行报错：账号无权限（sql_executor）
    #   timeout            数据库执行超时（sql_executor）
    #   other              数据库执行报错：其他未归类异常（sql_executor）
    #   correction         达到纠错轮次上限后仍失败（pipeline 终态节点）
    # 评测脚本 run_eval.py 另有“题目级状态”：pass/fail（执行结果与 gold 不一致）/
    # crash（pipeline 内部抛异常，本题未跑完）/gold_error（标准答案不可执行，剔除分母）/
    # pipeline_error（整库流水线构建失败）——那是评测口径，不属于本字段。
    error_kind: Optional[str] = None
    session_id: Optional[str] = None
    run_id: Optional[str] = None                # 本次执行 id（Trace 页面的入口标识）
    turn_id: Optional[str] = None               # 本轮对话 id（引用功能使用）


class NL2SQLState(TypedDict, total=False):
    """图状态：result/tracker 为可变对象引用（节点就地更新）；
    其余为控制字段，经节点返回值更新、供条件边路由消费。
    events / turns 使用 DeltaChannel：每个检查点只保存本步新增的事件/轮次，
    读取时沿检查点父链重放，避免全量列表随轮次线性膨胀。"""
    question: str
    session_id: Optional[str]
    user_id: str                  # 多用户隔离维度
    run_id: str                   # 单次执行 id（Trace 关联键）
    result: PipelineResult
    tracker: TurnTracker
    events: Annotated[List[dict], DeltaChannel(_reducer_append, snapshot_frequency=10)]
    turns: Annotated[List[dict], DeltaChannel(_reducer_append, snapshot_frequency=30)]
    attempt: int                  # 纠错轮次：0=原始 SQL，1..N 为重写
    max_rounds: int               # 纠错上限（开关关闭时为 0）
    error_message: str            # 回喂给纠错节点的错误描述
    extra_context: str            # 上下文增强产物（纠错重写复用）
    conversation_context: str     # 多轮上下文（模板中紧跟用户问题）
    reference_context: str        # 术语映射/值链接（模板中置于表结构之后）
    candidate_tables: Optional[List[str]]   # Schema 检索命中的候选表
    reference_turn_id: Optional[str]        # 显式引用的历史轮 ID（None=默认带上一轮）
    clarify_id: Optional[str]               # 本轮是对某条澄清的回答（恢复暂存后已置空）
    selected_schema: str          # 第一阶段表字段识别裁剪后的 Schema 文本（空=回退全量）
    table_selection: dict         # 表字段识别结构化结果（日志/观测用）
    fanout_waived: bool         # 当前 SQL 已经 LLM 裁决为扇出误报，跳过扇出检测；
                                # 任何纠错重写产生新 SQL 后必须清除（在 q_correct 节点）
    opt_rounds: int             # 性能优化（EXPLAIN 门禁）独立轮次：0=未优化，最多 max_opt_rounds
    route: str                    # 当前节点决定的下一跳（条件边消费）


def _emit(event_type: str, data: dict):
    """经 LangGraph custom 流通道推送一条 SSE 协议事件；
    同时写入本轮缓冲（iter_run 设置），finalize 会把它落到检查点的 events 通道。
    无流上下文（invoke 直调）时静默跳过。"""
    event = {"type": event_type, "data": data, "ts": round(time.time(), 3)}
    buffer = _EMIT_BUFFER.get()
    if buffer is not None:
        buffer.append(event)
    try:
        writer = get_stream_writer()
        if writer:
            writer(event)
    except Exception:
        pass  # 非流式调用场景，忽略


_UNSET = object()   # 区分“没传 checkpointer”和“显式传 None（关闭检查点）”


class NL2SQLPipeline:
    """NL2SQL 完整流水线编排入口（LangGraph StateGraph）"""

    def __init__(self, conversation_manager: ConversationManager = None,
                 result_cache: ResultCache = None,
                 checkpointer=_UNSET, run_store: SessionRunStore = None):
        factory = ModelFactory()
        self.llm = factory.create_llm()
        self.schema_manager = SchemaManager()
        # 检查点：LangGraph 原生持久化，默认开启（Redis 是事实源）。
        # NL2SQL_CHECKPOINTING=off 可关闭，主要用于批量评测（不需要 Trace，省掉写放大）；
        # 显式传入 checkpointer（API 依赖注入）时以调用方为准。
        env_ckpt = os.environ.get("NL2SQL_CHECKPOINTING")
        self.checkpoint_enabled = not (
            env_ckpt is not None
            and env_ckpt.strip().lower() in ("0", "off", "false", "no")
        )
        if checkpointer is _UNSET:
            self.checkpointer = (RedisCheckpointSaver() if self.checkpoint_enabled
                                 else None)
        else:
            self.checkpointer = checkpointer
        self.run_store = run_store if run_store is not None else (
            SessionRunStore() if self.checkpointer is not None else None
        )
        self.conversation = conversation_manager or ConversationManager(
            checkpointer=self.checkpointer)
        if getattr(self.conversation, "checkpointer", None) is None:
            self.conversation.checkpointer = self.checkpointer
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
        # 第一阶段表字段识别（query 主链；失败自动回退全量 Schema）
        self.table_selector = TableSelector(self.llm, self.schema_manager, self.relation_graph)
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
        explain_cfg = nl2sql_cfg.get("explain", {})
        # 环境变量 NL2SQL_EXPLAIN_GATE 可覆盖 features.explain_gate（评测库索引未经
        # 生产级维护时用 off 关闭：无索引可利用时优化重写无收益且可能改坏语义）
        env_gate = os.environ.get("NL2SQL_EXPLAIN_GATE")
        gate_enabled = (
            env_gate.strip().lower() in ("1", "on", "true", "yes")
            if env_gate is not None else features.get("explain_gate", True)
        )
        self.explain_gate = ExplainGate(
            self.executor,
            large_table_rows=int(explain_cfg.get("large_table_rows", 10000)),
            max_opt_rounds=int(explain_cfg.get("max_opt_rounds", 2)),
            enabled=gate_enabled,
        )
        # Skill/Tool 统一能力层：资产注册 + 声明式规则 Gate（fail-closed）+ 申请编排
        self.capability = build_capability_layer(self.generator, self.schema_manager, nl2sql_cfg)
        self.explainer = SQLExplainer(self.llm, self.schema_manager)
        # 解读节点开关：默认开启；评测脚本（test_env）默认通过环境变量关闭以省 token；
        # 环境变量取值 1/on/true/yes=开启，0/off/false/no=关闭
        env_switch = os.environ.get("NL2SQL_SQL_EXPLAINER")
        if env_switch is not None:
            self.explainer_enabled = env_switch.strip().lower() in ("1", "on", "true", "yes")
        else:
            self.explainer_enabled = bool(features.get("sql_explainer", True))
        # 人工澄清开关（表筛选阶段向用户反问）：默认开启；评测等无人值守场景
        # 通过 NL2SQL_CLARIFICATION=off 关闭，关闭后模型即使返回澄清协议也按
        # “识别失败→回退全量 Schema 继续生成”处理，绝不挂起等待
        env_clar = os.environ.get("NL2SQL_CLARIFICATION")
        if env_clar is not None:
            self.clarification_enabled = env_clar.strip().lower() in ("1", "on", "true", "yes")
        else:
            self.clarification_enabled = bool(features.get("clarification", True))
        self.formatter = OutputFormatter()
        self.corrector = SelfCorrector(
            self.llm, self.schema_manager, self.value_linker,
            max_rounds=nl2sql_cfg.get("correction", {}).get("max_rounds", 2),
            relation_graph=self.relation_graph,
            skill_knowledge=self.capability.orchestrator.skill_knowledge,
        )
        self.graph = self._build_graph()
        # 会话正文从检查点投影：把编译后的图交给会话层（懒读取，避免循环依赖）
        self.conversation.bind_graph(lambda: self.graph)

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
    def _traced(self, node):
        """节点包装：把该节点执行期间 _emit 出的事件一并写进状态。

        这样每个检查点既有“执行了哪个节点”，也有“该节点产出了哪些事件”，
        Trace 投影才能把一次问答的事件还原到它真正发生的步骤上。
        节点返回值仍是普通 dict，只多一个 events 字段（DeltaChannel 追加）。"""
        def wrapped(state):
            buffer = _EMIT_BUFFER.get()
            start = len(buffer) if buffer is not None else 0
            ret = node(state) or {}
            if buffer is None or len(buffer) <= start:
                return ret
            merged = dict(ret)
            merged["events"] = list(merged.get("events") or []) + buffer[start:]
            return merged
        return wrapped

    def _build_graph(self):
        g = StateGraph(NL2SQLState)

        # ---- 节点注册（统一包一层事件采集） ----
        g.add_node("intent", self._traced(self._n_intent))
        g.add_node("q_context", self._traced(self._n_q_context))
        g.add_node("q_select", self._traced(self._n_q_select))
        g.add_node("q_perf", self._traced(self._n_q_perf))
        g.add_node("q_opt_correct", self._traced(self._n_q_opt_correct))
        g.add_node("q_generate", self._traced(self._n_q_generate))
        g.add_node("q_validate", self._traced(self._n_q_validate))
        g.add_node("q_guard", self._traced(self._n_q_guard))
        g.add_node("q_fanout", self._traced(self._n_q_fanout))
        g.add_node("q_execute", self._traced(self._n_q_execute))
        g.add_node("q_correct", self._traced(self._n_q_correct))
        g.add_node("q_cache", self._traced(self._n_q_cache))
        g.add_node("q_explain", self._traced(self._n_q_explain))
        g.add_node("d_context", self._traced(self._n_d_context))
        g.add_node("d_generate", self._traced(self._n_d_generate))
        g.add_node("d_explain", self._traced(self._n_d_explain))
        g.add_node("m_answer", self._traced(self._n_meta))
        g.add_node("finalize", self._traced(self._n_finalize))

        # ---- 骨架 ----
        g.add_edge(START, "intent")
        # 意图三分流
        g.add_conditional_edges("intent", self._r_intent,
                                {"q_context": "q_context", "d_context": "d_context", "m_answer": "m_answer"})
        # query 主链
        g.add_edge("q_context", "q_select")
        g.add_conditional_edges("q_select", self._r_q_select,
                                {"q_generate": "q_generate", "finalize": "finalize"})
        g.add_conditional_edges("q_generate", self._r_generate,
                                {"q_validate": "q_validate", "finalize": "finalize"})
        g.add_conditional_edges("q_validate", self._r_validate,
                                {"q_guard": "q_guard", "q_correct": "q_correct", "finalize": "finalize"})
        g.add_conditional_edges("q_guard", self._r_guard,
                                {"q_fanout": "q_fanout", "finalize": "finalize"})
        g.add_conditional_edges("q_fanout", self._r_fanout,
                                # q_validate：LLM 裁决 risky 后采用其重写 SQL，回全链路复检；
                                # 放行统一进 q_perf（EXPLAIN 性能门禁）再到执行
                                {"q_perf": "q_perf", "q_correct": "q_correct",
                                 "q_validate": "q_validate"})
        g.add_conditional_edges("q_perf", self._r_perf,
                                # q_opt_correct：EXPLAIN 命中性能问题且优化轮次未满
                                {"q_execute": "q_execute", "q_opt_correct": "q_opt_correct"})
        g.add_conditional_edges("q_opt_correct", self._r_opt_correct,
                                # 性能重写后回静态校验走全链路复检；重写失败则放行执行不阻断
                                {"q_validate": "q_validate", "q_execute": "q_execute"})
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

        return g.compile(checkpointer=self.checkpointer)

    # ==================================================================
    # 对外入口：iter_run 产出事件 dict；run() 消费并返回聚合结果
    # ==================================================================
    def iter_run(self, question: str, session_id: str = None,
                 reference_turn_id: str = None,
                 clarify_id: str = None,
                 user_id: str = "default") -> Iterator[dict]:
        # 人工澄清恢复：用户带着 clarify_id 回答时，取回暂存的原始问题，
        # 把补充信息拼入有效问题后续跑（暂存一次性消费）；无效/过期 clarify_id 静默忽略
        if clarify_id and session_id:
            pending = self.conversation.take_clarify(user_id, session_id, clarify_id)
            if pending:
                logger.info("恢复澄清会话 %s，用户补充：%s", clarify_id, question[:50])
                question = (
                    f"{pending['question']}\n"
                    f"【用户针对待澄清问题的补充信息，请据此生成 SQL】{question}"
                )
                reference_turn_id = pending.get("reference_turn_id") or reference_turn_id
        run_id = uuid.uuid4().hex[:16]
        result = PipelineResult(question=question, session_id=session_id, run_id=run_id)
        result.reference_turn_id = reference_turn_id
        tracker = TurnTracker(enabled=self.metrics_enabled)
        initial: NL2SQLState = {
            "question": question,
            "session_id": session_id,
            "user_id": user_id,
            "run_id": run_id,
            "reference_turn_id": reference_turn_id,
            "clarify_id": clarify_id,
            "result": result,                       # 可变引用：图内就地更新
            "tracker": tracker,
            # DeltaChannel 的初始值：新线程从空开始；已有线程追加空列表等价于不变
            "events": [],
            "turns": [],
            "attempt": 0,
            "max_rounds": self.corrector.max_rounds if self.corrector_enabled else 0,
            "error_message": "",
            "extra_context": "",
            "candidate_tables": None,
            "fanout_waived": False,
            "selected_schema": "",
            "table_selection": {},
            "opt_rounds": 0,
            "route": "",
        }
        # 线程按“用户 + 会话”隔离；一次性提问也分配临时线程，保证每次执行都有 Trace
        thread_id = (thread_id_for(user_id, session_id) if session_id
                     else f"{user_id}:run:{run_id}")
        config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
        if self.run_store is not None:
            self.run_store.start_run(user_id, run_id, session_id, thread_id, question)

        token = _EMIT_BUFFER.set([])
        finished = False
        try:
            # custom 通道透出各节点 _emit 的事件；结束后经 StopIteration.value 返回 result
            yield from (chunk for chunk in self.graph.stream(
                initial, config, stream_mode="custom"))
        except Exception as e:
            finished = True
            if self.run_store is not None:
                self.run_store.finish_run(
                    user_id, run_id, status=RUN_STATUS_ERROR, error=str(e))
            raise
        else:
            finished = True
            if self.run_store is not None:
                self.run_store.finish_run(
                    user_id, run_id,
                    status=RUN_STATUS_ERROR if result.error else RUN_STATUS_OK,
                    error=result.error or "",
                    turn_id=result.turn_id or "",
                )
        finally:
            # 客户端断开时生成器被关闭（GeneratorExit 不走 except/else），
            # 这里把运行状态收敛，避免运行目录长期停留在 running
            if not finished and self.run_store is not None:
                try:
                    self.run_store.finish_run(
                        user_id, run_id, status=RUN_STATUS_ERROR,
                        error="执行被中断（客户端断开连接）",
                    )
                except Exception:
                    logger.warning("中断收尾失败：run_id=%s", run_id)
            try:
                _EMIT_BUFFER.reset(token)
            except (ValueError, RuntimeError):
                # 生成器可能在另一个上下文/线程中被关闭，此时复位令牌会报错；
                # 该上下文即将销毁，忽略即可
                pass
        return result

    def run(self, question: str, session_id: str = None,
            reference_turn_id: str = None, clarify_id: str = None,
            user_id: str = "default") -> PipelineResult:
        """同步聚合版本：消费图执行的事件流，返回 PipelineResult"""
        gen = self.iter_run(question, session_id,
                            reference_turn_id=reference_turn_id,
                            clarify_id=clarify_id, user_id=user_id)
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
            if self.intent_classifier:
                result.intent, intent_usage = self.intent_classifier.classify(state["question"])
                tracker.add_tokens(intent_usage["prompt_tokens"],
                                   intent_usage["completion_tokens"], intent_usage["total_tokens"])
            else:
                result.intent = "query"
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
        user_id = state.get("user_id", "default")
        reference_turn_id = state.get("reference_turn_id")
        with tracker.step("上下文增强"):
            # 显式引用：只注入被引用轮（替代默认上一轮）；否则注入最近一轮（省略式追问）
            conv_ctx = (
                self.conversation.build_context(
                    user_id, session_id, reference_turn_id=reference_turn_id)
                if session_id else ""
            )
            # Schema 检索（大库 Top-K；小库/未开启返回 None 走全量）；
            # 引用/上轮 SQL 涉及表强制并入；命中表再沿关系图扩展 JOIN 邻接表
            if session_id and reference_turn_id:
                forced_tables = self.conversation.turn_tables(
                    user_id, session_id, reference_turn_id)
            else:
                forced_tables = (self.conversation.last_tables(user_id, session_id)
                                 if session_id else [])
            candidate_tables = self._select_candidate_tables(question, forced_tables)
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

    def _n_q_select(self, state: NL2SQLState) -> dict:
        """第一阶段：表字段识别（LLM 结构化裁剪：needed/maybe/excluded + 字段二分）。
        成功 → 裁剪后 Schema 文本注入第二阶段；调用/解析/清洗失败 → 空串回退全量 Schema。
        模型判定问题信息不足 → 人工澄清：暂存现场后路由 finalize，本轮不出 SQL；
        用户补充后带 clarify_id 重新发起（iter_run 入口恢复现场）。"""
        tracker = state["tracker"]
        with tracker.step("表字段识别"):
            try:
                sel = self.table_selector.select(
                    state["question"], tables=state.get("candidate_tables"),
                    conversation_context=state.get("conversation_context", ""),
                )
            except Exception as e:
                logger.error("表字段识别节点异常（回退全量 Schema）: %s", str(e).splitlines()[0])
                sel = None
            # token 在步骤块内入账（含澄清协议/开关关闭两种形态），避免归入“其他”桶
            if sel is not None:
                tracker.add_tokens(sel.prompt_tokens, sel.completion_tokens, sel.total_tokens)
        if isinstance(sel, ClarificationRequest):
            if self.clarification_enabled:
                return self._handle_clarification(state, sel, tracker)
            # 开关关闭（评测/无人值守）：token 已计入，流程按识别失败回退全量继续
            logger.info("人工澄清已关闭，忽略澄清请求继续生成：%s", sel.questions)
            sel = None
        if sel is None:
            return {"selected_schema": "", "table_selection": {}, "route": "q_generate"}
        return {
            "selected_schema": self.table_selector.render_context(sel),
            "table_selection": {
                "needed": sel.needed, "maybe": sel.maybe, "excluded": sel.excluded,
            },
            "route": "q_generate",
        }

    def _handle_clarification(self, state: NL2SQLState,
                              clar: ClarificationRequest, tracker) -> dict:
        """构造人工澄清暂停：无会话（无法持久化恢复）时放弃澄清、回退全量继续，
        避免在不可恢复场景下卡死用户"""
        result = state["result"]
        session_id = state.get("session_id")
        if not session_id:
            logger.warning("无 session_id，无法暂存澄清现场，回退全量 Schema 继续")
            return {"selected_schema": "", "table_selection": {}, "route": "q_generate"}
        clarify_id = self.conversation.save_clarify(
            state.get("user_id", "default"),
            session_id,
            question=state["question"],
            questions=clar.questions,
            reference_turn_id=state.get("reference_turn_id"),
        )
        # token 已在「表字段识别」步骤块内统一入账，此处不再重复 add_tokens
        result.need_clarification = True
        result.clarify_id = clarify_id
        result.clarification_questions = list(clar.questions)
        _emit("clarification", {"clarify_id": clarify_id,
                                "questions": list(clar.questions)})
        logger.info("发起人工澄清 %s：%s", clarify_id, clar.questions)
        return {"route": "finalize"}

    def _r_q_select(self, state: NL2SQLState) -> str:
        return state.get("route") or "q_generate"

    def _n_q_generate(self, state: NL2SQLState) -> dict:
        result, tracker = state["result"], state["tracker"]
        with tracker.step("SQL生成"):
            try:
                # 生成经能力编排器：模型按需申请 Skill/get_table_schema，一切申请过 Gate
                cap_out = self.capability.orchestrator.run(
                    state["question"],
                    tables=state["candidate_tables"],
                    conversation_context=state.get("conversation_context", ""),
                    reference_context=state.get("reference_context", ""),
                    selected_schema=state.get("selected_schema", ""),
                    candidate_tables=(state["candidate_tables"]
                                      or self.schema_manager.get_table_names()),
                    attempt=state.get("attempt", 0),
                    opt_rounds=state.get("opt_rounds", 0),
                )
                gen = cap_out["result"]
                if cap_out.get("bypass") == "gate_unhealthy":
                    logger.info("本次生成 bypass 能力层（Gate 不可用，模型自行发挥）")
                for tr in cap_out["trace"]:
                    logger.info("能力申请[%s] 第%d轮：请求 skills=%s tools=%s；批准=%s；拒绝=%s%s",
                                "generate", tr.round_index, tr.requested_skills, tr.requested_tools,
                                tr.granted,
                                [f"{d['kind']}:{d['name']}@{d['rule_id']}" for d in tr.denied],
                                f"；格式错误：{tr.malformed}" if tr.malformed else "")
                self._record_cap_trail(result, cap_out["trace"], phase="generate")
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

    @staticmethod
    def _record_cap_trail(result, trace, phase: str):
        """把能力编排轨迹落入结果（phase 区分首次生成 generate / 纠错重写 correct）"""
        for tr in trace:
            result.capability_trail.append({
                "phase": phase,
                "round": tr.round_index,
                "skills_requested": list(tr.requested_skills),
                "tools_requested": list(tr.requested_tools),
                "granted": list(tr.granted),
                "denied": list(tr.denied),
                "malformed": tr.malformed,
            })

    def _capability_rewrite(self, state: NL2SQLState) -> dict:
        """纠错/性能优化重写走能力层 FC 循环：纠错模型可按需（非强制）申请疑似出错环节的
        技能、或为可能缺失的表申请 get_table_schema；Gate 不健康/编排异常时回退
        corrector.rewrite 的无工具一次性重写。返回编排器标准输出 {result, trace, bypass}"""
        question = state["question"]
        sql = state["result"].sql
        error_message = state["error_message"]
        tables = state.get("candidate_tables")
        messages = self.corrector.build_rewrite_messages(
            question, sql, error_message, tables=tables,
            capability_catalog=self.capability.orchestrator.catalog_text,
            skill_knowledge=self.capability.orchestrator.skill_knowledge,
            extra_context=state.get("extra_context", ""),
        )
        ctx = ExecutionContext(
            phase="correct", question=question, intent="query",
            candidate_tables=tables,
            attempt=state.get("attempt", 0),
            opt_rounds=state.get("opt_rounds", 0),
            extra={"known_tables": list(tables or [])},
        )

        def fallback():
            new_sql, usage = self.corrector.rewrite(
                question, sql, error_message,
                extra_context=state.get("extra_context", ""), tables=tables)
            return GenerationResult(
                sql=new_sql or "",
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
                total_tokens=usage.get("total_tokens", 0),
            )

        return self.capability.orchestrator.run_messages(
            messages, ctx=ctx, fallback=fallback,
            extract_sql=self.corrector._extract_sql,
            skill_policy="optional",
        )

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
            return {"route": "q_perf"}
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
            return {"route": "q_perf"}
        logger.warning("JOIN 扇出%s: %s | SQL: %s",
                       "确认" if "确认" in fanout_msg else "风险", fanout_msg[:80], result.sql[:120])
        if state["attempt"] >= state["max_rounds"]:
            logger.info("扇出告警但纠错轮次耗尽，放行进入性能门禁")
            return {"route": "q_perf"}

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
            return {"route": "q_perf", "fanout_waived": True}
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

    def _n_q_perf(self, state: NL2SQLState) -> dict:
        """EXPLAIN 性能门禁：执行前检测；大表 type=ALL 且 key=NULL 且优化轮次未满
        → q_opt_correct 回喂重写（独立 opt_rounds，不计入语义纠错）；
        通过 / EXPLAIN 失败 / 优化轮次耗尽 → q_execute（耗尽仍命中仅告警，不阻断）"""
        result, tracker = state["result"], state["tracker"]
        if not self.explain_gate.enabled:
            return {"route": "q_execute"}
        with tracker.step("性能门禁"):
            verdict = self.explain_gate.inspect(result.sql)
        if not verdict.get("ok") or not verdict.get("blocked"):
            return {"route": "q_execute"}
        if state["opt_rounds"] < self.explain_gate.max_opt_rounds:
            logger.warning("EXPLAIN 性能门禁命中，进入性能优化第 %d 轮（worst_type=%s）",
                           state["opt_rounds"] + 1, verdict["worst_type"])
            return {"route": "q_opt_correct", "error_message": verdict["message"]}
        logger.warning("EXPLAIN 性能门禁第 %d 次检测仍命中（优化轮次已耗尽），放行执行: %s",
                       state["opt_rounds"] + 1, result.sql[:120].replace("\n", " "))
        return {"route": "q_execute"}

    def _r_perf(self, state: NL2SQLState) -> str:
        return state["route"]

    def _n_q_opt_correct(self, state: NL2SQLState) -> dict:
        """性能优化重写：独立 opt_rounds 计数，只写 optimization_trail，
        不增 attempt、不写 correction_trail（评测 corrections 口径不受影响）；
        重写后回 q_validate 经守卫/扇出/性能门禁全链路复检；重写失败放行执行不阻断"""
        result, tracker = state["result"], state["tracker"]
        opt_round = state["opt_rounds"] + 1
        with tracker.step("性能优化"):
            try:
                cap_out = self._capability_rewrite(state)
                gen = cap_out["result"]
                self._record_cap_trail(result, cap_out["trace"], phase="correct")
                if cap_out.get("bypass"):
                    logger.info("性能优化重写 bypass 能力层（%s）", cap_out["bypass"])
                tracker.add_tokens(
                    gen.prompt_tokens, gen.completion_tokens, gen.total_tokens)
                rewritten = gen.sql or None
            except Exception as e:
                logger.error("性能优化重写调用失败: %s", e)
                rewritten = None
        if not rewritten:
            logger.warning("性能优化重写未返回有效 SQL，放行执行")
            return {"route": "q_execute", "opt_rounds": opt_round}
        result.sql = rewritten
        result.optimization_trail.append(
            {"round": opt_round, "error": state["error_message"], "sql": rewritten}
        )
        _emit("correcting", {"round": opt_round, "kind": "perf",
                             "error": state["error_message"], "sql": rewritten})
        _emit("sql", {"sql": rewritten})
        return {"route": "q_validate", "opt_rounds": opt_round, "fanout_waived": False}

    def _r_opt_correct(self, state: NL2SQLState) -> str:
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
                cap_out = self._capability_rewrite(state)
                gen = cap_out["result"]
                self._record_cap_trail(result, cap_out["trace"], phase="correct")
                if cap_out.get("bypass"):
                    logger.info("纠错重写 bypass 能力层（%s）", cap_out["bypass"])
                tracker.add_tokens(
                    gen.prompt_tokens, gen.completion_tokens, gen.total_tokens)
                rewritten = gen.sql or None
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
        """解释生成 + SQL/问题一致性裁判：失败返回空串不阻断（增益能力）。
        解读开关关闭时直接跳过（sql_label 置空，前端不渲染徽章）；
        开启时只向解读模型注入最终 SQL 实际涉及的表结构。"""
        result, tracker = state["result"], state["tracker"]
        if not self.explainer_enabled:
            result.sql_label = ""
            return {}
        with tracker.step("解释生成"):
            explanation, label, usage = self.explainer.explain(
                result.sql, state["question"],
                tables=self._extract_tables(result.sql))
            result.explanation, result.sql_label = explanation, label
            tracker.add_tokens(usage["prompt_tokens"],
                               usage["completion_tokens"], usage["total_tokens"])
        if result.explanation:
            _emit("explanation", {"text": result.explanation,
                                  "label": result.sql_label})
        return {}

    # ==================================================================
    # 节点：dml 分支（仅生成 + 解释，绝不执行）
    # ==================================================================
    def _n_d_context(self, state: NL2SQLState) -> dict:
        result, tracker = state["result"], state["tracker"]
        question, session_id = state["question"], state["session_id"]
        user_id = state.get("user_id", "default")
        reference_turn_id = state.get("reference_turn_id")
        with tracker.step("上下文增强"):
            conv_ctx = (
                self.conversation.build_context(
                    user_id, session_id, reference_turn_id=reference_turn_id)
                if session_id else ""
            )
            if session_id and reference_turn_id:
                forced_tables = self.conversation.turn_tables(
                    user_id, session_id, reference_turn_id)
            else:
                forced_tables = (self.conversation.last_tables(user_id, session_id)
                                 if session_id else [])
            candidate_tables = self._select_candidate_tables(question, forced_tables)
        result.executed = False
        return {"extra_context": conv_ctx, "candidate_tables": candidate_tables}

    def _n_d_generate(self, state: NL2SQLState) -> dict:
        result, tracker = state["result"], state["tracker"]
        with tracker.step("SQL生成"):
            # 【预埋-当前不生效】DML 仅生成展示、从不执行，故不经能力 Gate。
            # 未来若开放写执行：写类 Tool 必须注册为 medium/high 风险（R100 自动拒绝），
            # 且 Gate 不健康（fail-closed）时在此直接终止 DML，降级为只读模式。
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
        prefix = "（按安全策略，写操作仅生成 SQL 供确认，不会执行）"
        if self.explainer_enabled:
            with tracker.step("解释生成"):
                explanation, label, usage = self.explainer.explain(
                    result.sql, state["question"],
                    tables=self._extract_tables(result.sql))
                tracker.add_tokens(usage["prompt_tokens"],
                                   usage["completion_tokens"], usage["total_tokens"])
        else:
            explanation, label = "", ""
        result.explanation = f"{prefix}{explanation or '该语句为写操作，按安全策略仅生成不执行。'}"
        result.sql_label = label
        # 无 label（解读关闭）时前端只显示文案、不渲染裁判徽章
        _emit("explanation",
              {"text": result.explanation, **({"label": label} if label else {})})
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

        # 轮次作为“会话事实”直接写回图状态（turns 是 DeltaChannel，检查点只存增量）；
        # 失败轮次、meta、人工澄清暂停轮均不记录（澄清只暂存现场，不产生问答轮）
        turn_id = None
        turn_delta: List[dict] = []
        if (session_id and not result.error
                and result.intent in ("query", "dml")
                and not result.need_clarification):
            turn_id = uuid.uuid4().hex[:12]
            result.turn_id = turn_id
            snapshot = None
            if result.intent == "query" and result.executed:
                snapshot = {
                    "columns": result.columns,
                    "rows": result.rows[:REPLAY_ROWS_LIMIT],
                    "total": result.total,
                    "truncated": result.truncated,
                }
            turn_delta.append({
                "turn_id": turn_id,
                "question": question,
                "sql": result.sql,
                "tables": self._extract_tables(result.sql),
                "intent": result.intent,
                "timestamp": time.time(),
                "explanation": result.explanation,
                "sql_label": result.sql_label,
                "result": snapshot,
                "usage": usage,
                "run_id": result.run_id,
            })
        # 澄清暂停时 done 同时回传 clarify 字段，非澄清轮回传 turn_id/run_id 供前端“引用/看链路”
        _emit("done", {
            "turn_id": turn_id,
            "run_id": result.run_id,
            "clarify_id": result.clarify_id,
            "need_clarification": result.need_clarification,
        })
        # 本轮事件由节点包装器按步写入 events 通道（见 _traced），这里只提交轮次增量
        return {"turns": turn_delta}

    # ==================================================================
    @staticmethod
    def _extract_tables(sql: str) -> List[str]:
        """从 SQL 提取涉及的真实表名（供会话记录）"""
        try:
            stmt = sqlglot.parse_one(sql, read="mysql")
            return sorted({t.name for t in stmt.find_all(exp.Table)})
        except Exception:
            return []
