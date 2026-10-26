# 依赖注入：提供全局单例（NL2SQLPipeline、会话管理器、结果缓存、模型工厂），供各路由通过 Depends 使用。
#
# 多用户改造后新增：
#   - RedisCheckpointSaver：LangGraph 检查点（事实源）
#   - SessionRunStore：会话目录 / 运行目录（索引，不含正文）
#   - Projection：把检查点投影成“会话视图”与“Trace 视图”
#   - get_user_id：从 X-User-Id 请求头解析当前用户（默认 default）

import os
import re
from functools import lru_cache
from urllib.parse import unquote

from fastapi import Header

from app.model.factory import ModelFactory
from app.nl2sql.conversation import ConversationManager
from app.nl2sql.pipeline import NL2SQLPipeline
from app.nl2sql.result_cache import ResultCache
from app.persistence.redis_checkpointer import RedisCheckpointSaver
from app.persistence.store import Projection, SessionRunStore

# 用户标识最终会进入 LangGraph 线程名（user:session），因此只剔除分隔符与控制字符；
# 其余（含中文）保留，便于界面按用户名区分。
_USER_ID_BAD_RE = re.compile(r"[:\x00-\x1f\x7f]")


def get_user_id(x_user_id: str = Header(default="default", alias="X-User-Id")) -> str:
    """多用户隔离维度：前端对用户名做 URL 编码放进请求头，这里解码后清洗。
    保留中文等多字节字符（Redis 键与线程名均支持 UTF-8），仅剔除冒号、控制字符。"""
    raw = unquote(x_user_id or "").strip()
    cleaned = _USER_ID_BAD_RE.sub("", raw)[:64].strip()
    return cleaned or "default"


@lru_cache
def get_model_factory() -> ModelFactory:
    return ModelFactory()


@lru_cache
def get_run_store() -> SessionRunStore:
    return SessionRunStore()


@lru_cache
def get_checkpointer():
    """检查点存储（事实源）。NL2SQL_CHECKPOINTING=off 时返回 None（仅批处理场景）。"""
    if os.environ.get("NL2SQL_CHECKPOINTING", "").strip().lower() in (
            "0", "off", "false", "no"):
        return None
    return RedisCheckpointSaver()


@lru_cache
def get_projection() -> Projection:
    return Projection(get_run_store())


@lru_cache
def get_conversation_manager() -> ConversationManager:
    return ConversationManager(store=get_run_store(), checkpointer=get_checkpointer())


@lru_cache
def get_result_cache() -> ResultCache:
    return ResultCache()


@lru_cache
def get_pipeline() -> NL2SQLPipeline:
    # 注入同一会话管理器与结果缓存实例，保证路由与流水线共享状态
    pipeline = NL2SQLPipeline(
        conversation_manager=get_conversation_manager(),
        result_cache=get_result_cache(),
        checkpointer=get_checkpointer(),
        run_store=get_run_store(),
    )
    # Trace 投影使用同一张编译后的图读取检查点历史
    get_projection().bind_graph(lambda: pipeline.graph)
    return pipeline


@lru_cache
def get_prompt_optimizer():
    # 提示词优化器单例（LLM 客户端复用）
    from app.optimizer.prompt_optimizer import PromptOptimizer
    return PromptOptimizer()
