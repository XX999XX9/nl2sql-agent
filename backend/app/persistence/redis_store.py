# Redis 基础设施：连接、键空间与用户维度命名。
#
# 设计说明：
#   - 所有键统一带业务前缀（默认 nl2sql）与用户维度，天然实现多用户隔离；
#   - 会话、轮次、图状态等“事实”存于 LangGraph 检查点（RedisCheckpointSaver）；
#   - 本模块只承载连接与命名规则，不保存业务正文。

import os
from functools import lru_cache

import redis


def redis_url() -> str:
    return os.environ.get("NL2SQL_REDIS_URL", "redis://localhost:6379/0")


def key_prefix() -> str:
    return os.environ.get("NL2SQL_REDIS_PREFIX", "nl2sql")


@lru_cache
def get_redis_client() -> redis.Redis:
    """进程内复用的 Redis 连接（decode_responses=False，检查点按二进制存储）"""
    client = redis.Redis.from_url(
        redis_url(),
        decode_responses=False,
        socket_connect_timeout=3,
        socket_timeout=10,
        health_check_interval=30,
    )
    client.ping()
    return client


def session_meta_key(user_id: str, session_id: str) -> str:
    return f"{key_prefix()}:session:{user_id}:{session_id}"


def user_sessions_key(user_id: str) -> str:
    return f"{key_prefix()}:user:{user_id}:sessions"


def clarify_key(user_id: str, session_id: str, clarify_id: str) -> str:
    return f"{key_prefix()}:clarify:{user_id}:{session_id}:{clarify_id}"


def run_meta_key(user_id: str, run_id: str) -> str:
    return f"{key_prefix()}:run:{user_id}:{run_id}"


def user_runs_key(user_id: str) -> str:
    return f"{key_prefix()}:user:{user_id}:runs"


def session_runs_key(user_id: str, session_id: str) -> str:
    return f"{key_prefix()}:session:{user_id}:{session_id}:runs"


def thread_id_for(user_id: str, session_id: str) -> str:
    """一个会话对应一个 LangGraph 线程：检查点、轮次、恢复全部围绕该线程"""
    return f"{user_id}:{session_id}"
