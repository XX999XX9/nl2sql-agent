# Langfuse 观测接入层（旁路、可开关）
#
# 职责：把一次问数的执行过程上报为 Langfuse Trace：
#   trace（一轮问答）
#     └─ span（每个图节点：意图/表筛选/生成/校验/执行/解读…）
#           └─ generation（节点内的每次 LLM 调用：模型、prompt、补全、usage、耗时）
# 每个 span 都带 elapsed_ms 与 token（usage_details），前端/后端可直接看每步耗时与成本。
#
# 设计约束（与主链路的关系）：
#   1. 纯旁路：SDK 初始化、上报、flush 的任何异常都在这里吞掉，主链路行为不因观测改变；
#   2. 未配置密钥 / 开关关闭时 get_tracer() 返回 None，所有调用点退化为空操作；
#   3. SDK 句柄绝不写入 TurnTracker / LangGraph State（msgpack 检查点无法序列化），
#      根 trace 以 run_id 为键放在进程内注册表，节点按 run_id 显式挂到根 trace 之下；
#   4. token 以模型 API 返回的 usage 为准（本地估算只做兜底，这里不参与）；
#   5. 自研评测（test_env 的 detail/summary/Trace 页面）保持原样，Langfuse 只做补充观测与
#      可选的 Dataset/Score 上报。

import os
import threading
import time
from collections import OrderedDict
from contextvars import ContextVar
from typing import Any, Dict, List, Optional

from app.utils.config_handler import load_env_file, load_yaml
from app.utils.logger_handler import setup_logger
from app.utils.path_tool import BACKEND_ROOT

logger = setup_logger()

# Langfuse 密钥通常写在 backend/.env：与主配置保持一致，导入时加载一次（不覆盖已有环境变量）
try:
    load_env_file(BACKEND_ROOT / ".env")
except Exception:
    pass

_SWITCH_ENV = "NL2SQL_LANGFUSE"          # on/off 覆盖 features.langfuse
_EVAL_ENV = "NL2SQL_LANGFUSE_EVAL"       # 评测 Dataset/Score 上报开关

try:                                      # SDK 缺失时静默降级，不影响服务启动
    from langfuse import Langfuse
    from langfuse.types import TraceContext
    _SDK_AVAILABLE = True
except Exception as _e:                   # pragma: no cover - 依赖缺失的兜底分支
    Langfuse = None                       # type: ignore
    TraceContext = None                   # type: ignore
    _SDK_AVAILABLE = False
    logger.warning("langfuse SDK 不可用，观测关闭：%s", _e)

# 当前 span / 当前根 trace：节点包装器与 LLM 客户端在同一执行上下文内读取，
# 避免把句柄塞进检查点状态（跨线程时以 run_id 注册表为准）
_CURRENT_SPAN: ContextVar[Optional["SpanHandle"]] = ContextVar("lf_current_span", default=None)
_CURRENT_TRACE: ContextVar[Optional["SpanHandle"]] = ContextVar("lf_current_trace", default=None)

_MAX_ROOTS = 4096          # 进程内 run_id → 根 trace 的注册表上限（评测连跑时防止无界增长）
_CLIP_TEXT = 2000          # 单条消息/输出写入 Langfuse 的最大字符数（控制上报体积）
_CLIP_OUTPUT = 4000


def clip_text(value: Any, limit: int = _CLIP_TEXT) -> Any:
    """截断长文本：保留首尾，避免把整段 Schema/结果集推给 Langfuse"""
    if not isinstance(value, str):
        return value
    if len(value) <= limit:
        return value
    head = limit * 2 // 3
    return f"{value[:head]}…[truncated {len(value) - limit} chars]…{value[-(limit - head):]}"


def clip_messages(messages: List[dict]) -> List[dict]:
    """LLM 输入裁剪：保留 role 与截断后的 content/tool_calls 摘要"""
    out = []
    for m in messages or []:
        item = {"role": m.get("role", ""), "content": clip_text(m.get("content", ""))}
        if m.get("tool_calls"):
            item["tool_calls"] = [
                {"name": (tc.get("function") or {}).get("name", ""),
                 "arguments": clip_text((tc.get("function") or {}).get("arguments", ""), 600)}
                for tc in m["tool_calls"]
            ]
        if m.get("tool_call_id"):
            item["tool_call_id"] = m["tool_call_id"]
        out.append(item)
    return out


def _env_on(name: str) -> Optional[bool]:
    raw = os.environ.get(name)
    if raw is None:
        return None
    return raw.strip().lower() in ("1", "on", "true", "yes")


class SpanHandle:
    """Langfuse span 的轻量句柄：记录起止时间、累计 token，结束时一次性上报。

    只暴露本层需要的最小能力，便于在 SDK 缺失/异常时整体降级为空操作。
    """

    __slots__ = ("obs", "name", "trace_id", "observation_id", "start",
                 "prompt_tokens", "completion_tokens", "total_tokens",
                 "_ended", "_is_root")

    def __init__(self, obs, name: str, trace_id: str, observation_id: str,
                 is_root: bool = False):
        self.obs = obs
        self.name = name
        self.trace_id = trace_id
        self.observation_id = observation_id
        self.start = time.perf_counter()
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.total_tokens = 0
        self._ended = False
        self._is_root = is_root

    def add_usage(self, prompt_tokens: int = 0, completion_tokens: int = 0,
                  total_tokens: int = 0):
        """把一次 LLM 调用的 token 计入当前 span（供 usage_details 展示）"""
        self.prompt_tokens += int(prompt_tokens or 0)
        self.completion_tokens += int(completion_tokens or 0)
        self.total_tokens += int(total_tokens or 0)

    def finish(self, output: Any = None, metadata: Optional[dict] = None,
               error: Optional[str] = None, level: str = "DEFAULT"):
        """结束 span：写 output/metadata/usage/错误，再关闭底层观测"""
        if self._ended:
            return
        self._ended = True
        try:
            payload = dict(metadata or {})
            payload["elapsed_ms"] = round((time.perf_counter() - self.start) * 1000, 1)
            payload["prompt_tokens"] = self.prompt_tokens
            payload["completion_tokens"] = self.completion_tokens
            payload["total_tokens"] = self.total_tokens
            updates: Dict[str, Any] = {"metadata": payload}
            if output is not None:
                updates["output"] = output
            if self.total_tokens or self.prompt_tokens or self.completion_tokens:
                updates["usage_details"] = {
                    "input": self.prompt_tokens,
                    "output": self.completion_tokens,
                    "total": self.total_tokens,
                }
            if error:
                updates["level"] = "ERROR"
                updates["status_message"] = clip_text(str(error), 500)
            elif level != "DEFAULT":
                updates["level"] = level
            self.obs.update(**updates)
        except Exception:
            pass
        try:
            self.obs.end()
        except Exception:
            pass


class LangfuseTracer:
    """Langfuse 客户端 + 根 trace 注册表的薄封装（所有方法失败均不抛出）"""

    def __init__(self, client, sample_rate: float = 1.0):
        self.client = client
        self.sample_rate = max(0.0, min(1.0, sample_rate))
        self._roots: "OrderedDict[str, SpanHandle]" = OrderedDict()
        self._lock = threading.Lock()

    # ----------------------------------------------------------- 根 trace
    def start_trace(self, run_id: str, *, name: str = "nl2sql.turn",
                    input: Any = None, user_id: str = "", session_id: str = "",
                    metadata: Optional[dict] = None) -> Optional[SpanHandle]:
        """创建/复用一轮问答的根 span（同时即 Langfuse 的 trace 根观测）"""
        if not run_id:
            return None
        with self._lock:
            existing = self._roots.get(run_id)
        if existing is not None:
            return existing
        try:
            obs = self.client.start_span(name=name, input=input, metadata=metadata or {})
            obs.update_trace(
                user_id=user_id or None,
                session_id=session_id or None,
                metadata=metadata or {},
            )
            root = SpanHandle(obs, name, obs.trace_id, obs.id, is_root=True)
        except Exception as e:
            logger.debug("Langfuse 根 trace 创建失败（忽略）：%s", e)
            return None
        with self._lock:
            self._roots[run_id] = root
            while len(self._roots) > _MAX_ROOTS:
                self._roots.popitem(last=False)
        return root

    def trace_id_for(self, run_id: str) -> Optional[str]:
        with self._lock:
            root = self._roots.get(run_id)
        return root.trace_id if root else None

    def finish_trace(self, root: Optional[SpanHandle], *, output: Any = None,
                     metadata: Optional[dict] = None, error: Optional[str] = None):
        """结束根 trace：trace 级 output/metadata 与根 span 一起收敛"""
        if root is None:
            return
        try:
            root.obs.update_trace(output=output, metadata=metadata or {})
        except Exception:
            pass
        root.finish(output=output, metadata=metadata, error=error)

    # ----------------------------------------------------------- 节点 span
    def node_span(self, run_id: str, name: str, *, input: Any = None,
                  metadata: Optional[dict] = None) -> Optional[SpanHandle]:
        """在指定 run 的根 trace 下开一个节点 span（显式 trace_context 指定父子关系，
        不依赖 OTel 线程上下文，LangGraph 跨线程调度也不会丢层级）"""
        with self._lock:
            root = self._roots.get(run_id)
        if root is None:
            return None
        try:
            obs = self.client.start_span(
                name=name,
                trace_context=TraceContext(trace_id=root.trace_id,
                                           parent_span_id=root.observation_id),
                input=input,
                metadata=metadata or {},
            )
            return SpanHandle(obs, name, root.trace_id, obs.id)
        except Exception as e:
            logger.debug("Langfuse 节点 span 创建失败（忽略）：%s", e)
            return None

    # ----------------------------------------------------------- LLM generation
    def llm_generation(self, *, name: str, model: str, input: Any,
                       model_parameters: Optional[dict] = None,
                       metadata: Optional[dict] = None) -> Optional[SpanHandle]:
        """在“当前节点 span”下开一次 LLM 调用观测；无节点 span 时挂到根 trace。
        返回句柄由调用方在拿到响应后调用 finish() 写入输出与 usage。"""
        parent = _CURRENT_SPAN.get() or _CURRENT_TRACE.get()
        if parent is None or parent._ended:
            return None
        try:
            obs = self.client.start_generation(
                name=name,
                trace_context=TraceContext(trace_id=parent.trace_id,
                                           parent_span_id=parent.observation_id),
                model=model,
                input=input,
                model_parameters=model_parameters or {},
                metadata=metadata or {},
            )
            return SpanHandle(obs, name, parent.trace_id, obs.id)
        except Exception as e:
            logger.debug("Langfuse generation 创建失败（忽略）：%s", e)
            return None

    # ----------------------------------------------------------- 评测上报
    def score_run(self, run_id: str, name: str, value: float,
                  *, data_type: str = "BOOLEAN", comment: str = ""):
        """给某次问答的 trace 打分（评测执行准确率等）；未开启时静默跳过"""
        trace_id = self.trace_id_for(run_id)
        if not trace_id:
            return
        try:
            self.client.create_score(
                trace_id=trace_id, name=name, value=value,
                data_type=data_type, comment=comment or None,
            )
        except Exception as e:
            logger.debug("Langfuse 打分失败（忽略）：%s", e)

    def ensure_dataset(self, reference, items: List[dict], description: str = "") -> bool:
        """建/复用评测 Dataset 并写入条目（id 幂等）。返回是否成功。
        items: [{"id","input","expected_output","metadata"}]"""
        try:
            self.client.create_dataset(name=reference, description=description or None)
            for it in items:
                self.client.create_dataset_item(
                    dataset_name=reference,
                    id=it.get("id"),
                    input=it.get("input"),
                    expected_output=it.get("expected_output"),
                    metadata=it.get("metadata") or {},
                )
            return True
        except Exception as e:
            logger.debug("Langfuse Dataset 写入失败（忽略）：%s", e)
            return False

    # ----------------------------------------------------------- 生命周期
    def flush(self):
        try:
            self.client.flush()
        except Exception:
            pass

    def shutdown(self):
        try:
            self.client.shutdown()
        except Exception:
            pass


# ---------------------------------------------------------------- 单例入口
_TRACER: Optional[LangfuseTracer] = None
_TRACER_FAILED = False
_TRACER_LOCK = threading.Lock()
_CONFIG_ENABLED: Optional[bool] = None   # agent.yml 只读一次（开关在进程生命周期内不变）


def _config_enabled() -> bool:
    global _CONFIG_ENABLED
    if _CONFIG_ENABLED is not None:
        return _CONFIG_ENABLED
    try:
        cfg = load_yaml(BACKEND_ROOT / "config" / "agent.yml")
        _CONFIG_ENABLED = bool(cfg.get("features", {}).get("langfuse", False))
    except Exception:
        _CONFIG_ENABLED = False
    return _CONFIG_ENABLED


def langfuse_enabled() -> bool:
    """开关解析：环境变量 NL2SQL_LANGFUSE 优先，其次 agent.yml features.langfuse"""
    env = _env_on(_SWITCH_ENV)
    if env is not None:
        return env
    return _config_enabled()


def eval_report_enabled() -> bool:
    """评测 Dataset/Score 上报开关（默认关闭，避免批量评测污染观测库）"""
    env = _env_on(_EVAL_ENV)
    if env is None:
        return False
    return env


def get_tracer() -> Optional[LangfuseTracer]:
    """获取 Langfuse 观测单例；未启用/SDK 缺失/初始化失败一律返回 None"""
    global _TRACER, _TRACER_FAILED
    if not langfuse_enabled():
        return None
    if _TRACER is not None:
        return _TRACER
    if _TRACER_FAILED or not _SDK_AVAILABLE:
        return None
    with _TRACER_LOCK:
        if _TRACER is not None:
            return _TRACER
        pk = (os.environ.get("LANGFUSE_PUBLIC_KEY") or "").strip()
        sk = (os.environ.get("LANGFUSE_SECRET_KEY") or "").strip()
        host = (os.environ.get("LANGFUSE_HOST") or "https://cloud.langfuse.com").strip()
        if not pk or not sk:
            # 开了开关却没配密钥：只提示一次，不阻塞启动
            logger.warning("features.langfuse 已开启但缺少 LANGFUSE_PUBLIC_KEY/SECRET_KEY，观测保持关闭")
            _TRACER_FAILED = True
            return None
        try:
            sample_rate = float(os.environ.get("NL2SQL_LANGFUSE_SAMPLE_RATE") or 1.0)
        except ValueError:
            sample_rate = 1.0
        try:
            client = Langfuse(
                public_key=pk,
                secret_key=sk,
                host=host,
                tracing_enabled=True,
                flush_at=32,
                flush_interval=2.0,     # 异步批量上报，不阻塞请求线程
                timeout=5,
                sample_rate=sample_rate,
                release=(os.environ.get("NL2SQL_RELEASE") or "nl2sql-local"),
            )
            _TRACER = LangfuseTracer(client, sample_rate=sample_rate)
            logger.info("Langfuse 观测已启用：host=%s sample_rate=%s", host, sample_rate)
        except Exception as e:
            logger.warning("Langfuse 初始化失败，观测关闭：%s", e)
            _TRACER_FAILED = True
            return None
    return _TRACER


# ---------------------------------------------------------------- 上下文工具
def set_current_trace(handle: Optional[SpanHandle]):
    """设置当前根 trace（返回 token 供调用方 finally 复位）"""
    return _CURRENT_TRACE.set(handle)


def reset_current_trace(token):
    try:
        _CURRENT_TRACE.reset(token)
    except (ValueError, RuntimeError):
        pass


def set_current_span(handle: Optional[SpanHandle]):
    return _CURRENT_SPAN.set(handle)


def reset_current_span(token):
    try:
        _CURRENT_SPAN.reset(token)
    except (ValueError, RuntimeError):
        pass


def current_span() -> Optional[SpanHandle]:
    return _CURRENT_SPAN.get()
