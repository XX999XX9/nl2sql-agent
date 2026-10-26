# 基于纯 Redis 协议的 LangGraph 检查点存储。
#
# 官方 langgraph-checkpoint-redis 依赖 RedisJSON/RedisSearch（需要 FT.INFO 等模块），
# 普通 Redis 无法使用；这里只使用字符串 / Hash / ZSet / Set 这几类原生结构，
# 实现 BaseCheckpointSaver 的同步读写接口，从而支持：
#   - 每个超级步一个检查点，断点续跑；
#   - DeltaChannel：检查点里只保存增量写，读取时沿 parent 链重放（基类默认实现）。
#
# 键空间（前缀由 NL2SQL_REDIS_PREFIX 控制）：
#   {p}:ckpt:{thread_id}:{ns}:{checkpoint_id}   Hash  检查点本体 + 元数据 + parent
#   {p}:writes:{thread_id}:{ns}:{checkpoint_id} Hash  本检查点的待提交写（channel -> value）
#   {p}:thr:{thread_id}:{ns}                    ZSet  checkpoint_id -> 全局单调序号
#   {p}:threads                                 Set   "thread_id\x1fnamespace" 全集
#   {p}:seq                                     INCR  全局序号，保证跨线程写入顺序
#
# 检查点与写记录都用 LangGraph 自带的 JsonPlusSerializer 序列化，因此 DeltaChannel
# 的增量快照（_DeltaSnapshot）也能原样落盘，无需额外处理。

from __future__ import annotations

from typing import Any, Iterator, Optional, Sequence

from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    PendingWrite,
    WRITES_IDX_MAP,
)
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from app.persistence.redis_store import get_redis_client, key_prefix

_NS_SEP = "\x1f"          # 线程集合成员的命名空间分隔符
_TYPE_SEP = b"\x00"       # 写记录中 serde 类型名与二进制体的分隔符


class RedisCheckpointSaver(BaseCheckpointSaver):
    """纯 Redis 检查点存储（同步实现，FastAPI 的 sync 路由直接可用）"""

    def __init__(self, client=None, prefix: Optional[str] = None):
        # 显式允许业务类型走 msgpack：PipelineResult/TurnTracker 都是 dataclass，
        # 不加白名单会退化成 pickle 兜底并打警告
        serde = JsonPlusSerializer(
            allowed_msgpack_modules=[
                ("app.nl2sql.pipeline", "PipelineResult"),
                ("app.metrics.turn_tracker", "TurnTracker"),
            ],
        )
        super().__init__(serde=serde)
        self._r = client or get_redis_client()
        self._prefix = prefix or key_prefix()

    # ------------------------------------------------------------------ 键构造
    def _ckpt_key(self, thread_id: str, ns: str, checkpoint_id: str) -> str:
        return f"{self._prefix}:ckpt:{thread_id}:{ns}:{checkpoint_id}"

    def _writes_key(self, thread_id: str, ns: str, checkpoint_id: str) -> str:
        return f"{self._prefix}:writes:{thread_id}:{ns}:{checkpoint_id}"

    def _thread_key(self, thread_id: str, ns: str) -> str:
        return f"{self._prefix}:thr:{thread_id}:{ns}"

    @property
    def _threads_key(self) -> str:
        return f"{self._prefix}:threads"

    def _next_seq(self) -> int:
        return int(self._r.incr(f"{self._prefix}:seq"))

    @staticmethod
    def _parts(config) -> tuple[str, str]:
        conf = config.get("configurable", {}) if config else {}
        return conf.get("thread_id", ""), conf.get("checkpoint_ns", "") or ""

    @staticmethod
    def _config(thread_id: str, ns: str, checkpoint_id: str) -> dict:
        return {"configurable": {
            "thread_id": thread_id,
            "checkpoint_ns": ns,
            "checkpoint_id": checkpoint_id,
        }}

    # ------------------------------------------------------------------ 序列化
    def _dump(self, obj: Any) -> tuple[bytes, bytes]:
        type_, data = self.serde.dumps_typed(obj)
        return type_.encode("utf-8"), data

    def _load(self, type_: bytes, data: bytes) -> Any:
        return self.serde.loads_typed((type_.decode("utf-8"), data))

    # ------------------------------------------------------------------ 读取
    def get_tuple(self, config) -> Optional[CheckpointTuple]:
        thread_id, ns = self._parts(config)
        if not thread_id:
            return None
        checkpoint_id = config.get("configurable", {}).get("checkpoint_id")
        if not checkpoint_id:
            # 未指定检查点：取该线程最新一个
            latest = self._r.zrevrange(self._thread_key(thread_id, ns), 0, 0)
            if not latest:
                return None
            checkpoint_id = latest[0].decode("utf-8")

        raw = self._r.hgetall(self._ckpt_key(thread_id, ns, checkpoint_id))
        if not raw:
            return None

        checkpoint = self._load(raw[b"ckpt_type"], raw[b"ckpt_data"])
        metadata = self._load(raw[b"meta_type"], raw[b"meta_data"])
        parent_id = raw.get(b"parent", b"").decode("utf-8")
        parent_config = (
            self._config(thread_id, ns, parent_id) if parent_id else None
        )

        return CheckpointTuple(
            config=self._config(thread_id, ns, checkpoint_id),
            checkpoint=checkpoint,
            metadata=metadata,
            parent_config=parent_config,
            pending_writes=self._read_writes(thread_id, ns, checkpoint_id),
        )

    def _read_writes(self, thread_id: str, ns: str,
                     checkpoint_id: str) -> list[PendingWrite]:
        raw = self._r.hgetall(self._writes_key(thread_id, ns, checkpoint_id))
        if not raw:
            return []
        items: list[tuple[int, PendingWrite]] = []
        for field, blob in raw.items():
            # field = "{task_id}|{idx}"；idx 可能为负（__error__ / __scheduled__ 等）
            idx = int(field.rsplit(b"|", 1)[1])
            type_, data = blob.split(_TYPE_SEP, 1)
            channel, value = self._load(type_, data)
            task_id = field.rsplit(b"|", 1)[0].decode("utf-8")
            items.append((idx, (task_id, channel, value)))
        items.sort(key=lambda it: it[0])
        return [it[1] for it in items]

    def list(self, config, *, filter: Optional[dict] = None,
             before: Optional[str] = None,
             limit: Optional[int] = None) -> Iterator[CheckpointTuple]:
        thread_id, ns = self._parts(config)
        entries: list[tuple[int, str, str, str]] = []
        if thread_id:
            entries.extend(self._scan_thread(thread_id, ns, before))
        else:
            # 全局列举：所有线程合并后按写入序号倒序（Trace 页面按时间回看）
            for member in self._r.sscan_iter(self._threads_key):
                tid, tns = member.decode("utf-8").split(_NS_SEP, 1)
                entries.extend(self._scan_thread(tid, tns, None))
            entries.sort(key=lambda e: e[0], reverse=True)

        emitted = 0
        for _, tid, tns, cid in entries:
            if limit is not None and emitted >= limit:
                return
            tup = self.get_tuple(self._config(tid, tns, cid))
            if tup is None:
                continue
            if filter and not all(
                tup.metadata.get(k) == v for k, v in filter.items()
            ):
                continue
            emitted += 1
            yield tup

    def _scan_thread(self, thread_id: str, ns: str,
                     before: Optional[str]) -> list[tuple[int, str, str, str]]:
        key = self._thread_key(thread_id, ns)
        if before:
            score = self._r.zscore(key, before)
            if score is None:
                return []
            raw = self._r.zrevrangebyscore(key, f"({score}", "-inf")
        else:
            raw = self._r.zrevrange(key, 0, -1, withscores=True)
            if raw and isinstance(raw[0], tuple):
                return [(int(score), thread_id, ns, cid.decode("utf-8"))
                        for cid, score in raw]
        return [(0, thread_id, ns, cid.decode("utf-8")) for cid in raw]

    # ------------------------------------------------------------------ 写入
    def put(self, config, checkpoint: Checkpoint,
            metadata: CheckpointMetadata,
            new_versions: ChannelVersions) -> dict:
        thread_id, ns = self._parts(config)
        checkpoint_id = checkpoint["id"]
        parent_id = config.get("configurable", {}).get("checkpoint_id") or ""

        ckpt_type, ckpt_data = self._dump(checkpoint)
        meta_type, meta_data = self._dump(metadata)
        pipe = self._r.pipeline()
        pipe.hset(self._ckpt_key(thread_id, ns, checkpoint_id), mapping={
            "ckpt_type": ckpt_type,
            "ckpt_data": ckpt_data,
            "meta_type": meta_type,
            "meta_data": meta_data,
            "parent": parent_id.encode("utf-8"),
        })
        pipe.zadd(self._thread_key(thread_id, ns), {checkpoint_id: self._next_seq()})
        pipe.sadd(self._threads_key, f"{thread_id}{_NS_SEP}{ns}")
        pipe.execute()
        return self._config(thread_id, ns, checkpoint_id)

    def put_writes(self, config, writes: Sequence[tuple[str, Any]],
                   task_id: str, task_path: str = "") -> None:
        thread_id, ns = self._parts(config)
        checkpoint_id = config.get("configurable", {}).get("checkpoint_id")
        if not checkpoint_id:
            return
        key = self._writes_key(thread_id, ns, checkpoint_id)
        mapping = {}
        for idx, (channel, value) in enumerate(writes):
            # 特殊通道（__error__/__scheduled__/__interrupt__ 等）用固定负索引，
            # 与官方 saver 的语义保持一致
            real_idx = WRITES_IDX_MAP.get(channel, idx)
            type_, data = self._dump((channel, value))
            mapping[f"{task_id}|{real_idx}".encode("utf-8")] = type_ + _TYPE_SEP + data
        if mapping:
            self._r.hset(key, mapping=mapping)

    # ------------------------------------------------------------------ 删除
    def delete_thread(self, thread_id: str) -> None:
        r = self._r
        for pattern in (
            f"{self._prefix}:ckpt:{thread_id}:*",
            f"{self._prefix}:writes:{thread_id}:*",
            f"{self._prefix}:thr:{thread_id}:*",
        ):
            for key in r.scan_iter(match=pattern, count=500):
                r.delete(key)
        prefix = f"{thread_id}{_NS_SEP}"
        for member in r.sscan_iter(self._threads_key):
            if member.decode("utf-8", errors="ignore").startswith(prefix):
                r.srem(self._threads_key, member)
