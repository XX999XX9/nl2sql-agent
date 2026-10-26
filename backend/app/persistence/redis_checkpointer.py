# 基于官方 langgraph-checkpoint-redis 的 LangGraph 检查点存储。
#
# Redis 8 已内置 RedisJSON 与 RediSearch（单机单实例加载两个模块即可），
# 因此这里直接使用官方 RedisSaver，不再自研键协议：
#   - 每个超级步一个检查点，支持断点续跑与历史回看；
#   - DeltaChannel 的增量快照由官方序列化器按 __delta_snapshot__ 标记处理；
#   - 线程删除、按时间列举交给 RediSearch 索引完成。
#
# 这一层只负责三件事：
#   1. 复用项目的 Redis 地址配置（NL2SQL_REDIS_URL）；
#   2. 给键空间加业务前缀（前缀不能含冒号：官方实现用冒号分隔键段）；
#   3. 显式允许业务 dataclass 走 msgpack 白名单。

from __future__ import annotations

from typing import Optional

from langgraph.checkpoint.redis import RedisSaver
from langgraph.checkpoint.redis.jsonplus_redis import JsonPlusRedisSerializer
from langgraph.checkpoint.serde.types import _DeltaSnapshot

from app.persistence.redis_store import key_prefix, redis_url


class RedisCheckpointSaver(RedisSaver):
    """官方 RedisSaver 的项目适配层（同步实现，FastAPI 的 sync 路由直接可用）"""

    def __init__(self, client=None, prefix: Optional[str] = None):
        base = prefix or key_prefix()
        kwargs = {
            # 官方实现用 ":" 分隔键段，前缀里不能再出现冒号
            "checkpoint_prefix": f"{base}_checkpoint",
            "checkpoint_write_prefix": f"{base}_checkpoint_write",
        }
        if client is None:
            # 交给官方工厂建连接：它固定 RESP2，规避 redis-py 8 的 Search 返回差异
            super().__init__(redis_url=redis_url(), **kwargs)
        else:
            super().__init__(redis_client=client, **kwargs)
        # 官方默认序列化器是宽松模式；这里收敛成白名单，
        # 只允许业务 dataclass 参与 msgpack 扩展编码。
        self.serde = JsonPlusRedisSerializer(
            allowed_msgpack_modules=[
                ("app.nl2sql.pipeline", "PipelineResult"),
                ("app.metrics.turn_tracker", "TurnTracker"),
            ],
        )
        # 创建 RediSearch 索引（幂等）。缺少 RedisJSON/RediSearch 模块时会直接报错。
        self.setup()

    # ------------------------------------------------------------------ 写入标记
    def put(self, config, checkpoint, metadata, new_versions):
        """补齐 has_writes 标记。

        官方 0.5.2 假定 put_writes 发生在 put 之后（由 put_writes 反向置位
        has_writes）。当前 LangGraph 的运行顺序是「先落写入，再落检查点」，
        put 会把 has_writes 重置成 False，导致 get_tuple 跳过 pending_writes，
        DeltaChannel 无法沿祖先链重放。这里在落盘后按写入注册表补一次标记。
        """
        result = super().put(config, checkpoint, metadata, new_versions)
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        checkpoint_id = checkpoint["id"]
        try:
            if not (self._key_registry and self._key_registry.has_writes(
                thread_id, checkpoint_ns, checkpoint_id
            )):
                return result
            key = self._make_redis_checkpoint_key_cached(
                thread_id, checkpoint_ns, checkpoint_id
            )
            self._redis.json().merge(key, "$", {"has_writes": True})
        except Exception:
            # 标记失败只影响增量通道回放，不影响检查点本身；不阻断主流程
            pass
        return result

    # ------------------------------------------------------------------ DeltaChannel
    @staticmethod
    def _restore_delta_snapshots(value):
        """把官方序列化器写下的 __delta_snapshot__ 标记还原成 _DeltaSnapshot。

        官方 JsonPlusRedisSerializer 为了存进 RedisJSON，会把 DeltaChannel 的
        快照对象转成普通 dict；读取路径没有把它转回来，导致增量重放失效。
        """
        if isinstance(value, dict):
            if value.get("__delta_snapshot__") is True and "value" in value:
                return _DeltaSnapshot(
                    RedisCheckpointSaver._restore_delta_snapshots(value["value"])
                )
            return {
                k: RedisCheckpointSaver._restore_delta_snapshots(v)
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [RedisCheckpointSaver._restore_delta_snapshots(v) for v in value]
        return value

    def _deserialize_channel_values(self, channel_values):
        values = super()._deserialize_channel_values(channel_values)
        return self._restore_delta_snapshots(values)
