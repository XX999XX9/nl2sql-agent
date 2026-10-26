# 会话与运行索引，以及“事实源投影”。
#
# 事实源只有一个：LangGraph 检查点（RedisCheckpointSaver）。
#   - 会话视图：投影检查点状态中的 turns（只保留人问 / 模型答所需字段）；
#   - Trace 视图：投影同一线程的检查点历史（节点、状态、事件、下一步）。
# 本模块的 Redis 键只做“目录/索引”：会话 id 集合、运行 id 集合、标题与时间戳，
# 不保存问答正文，因而不会与检查点出现双写不一致。

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, is_dataclass
from typing import Callable, Optional

from app.persistence.redis_store import (
    get_redis_client,
    run_meta_key,
    session_meta_key,
    session_runs_key,
    thread_id_for,
    user_runs_key,
    user_sessions_key,
)

DEFAULT_TITLE = "新会话"
RUN_STATUS_RUNNING = "running"
RUN_STATUS_OK = "ok"
RUN_STATUS_ERROR = "error"


def _decode(value) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


class SessionRunStore:
    """Redis 索引层：会话目录 + 运行目录（不含对话正文）"""

    def __init__(self, client=None):
        self._r = client or get_redis_client()

    # ------------------------------------------------------------------ 会话目录
    def create_session(self, user_id: str, title: str = DEFAULT_TITLE) -> dict:
        session_id = uuid.uuid4().hex[:12]
        created_at = time.time()
        pipe = self._r.pipeline()
        pipe.hset(session_meta_key(user_id, session_id), mapping={
            "session_id": session_id,
            "user_id": user_id,
            "title": title,
            "created_at": str(created_at),
        })
        pipe.sadd(user_sessions_key(user_id), session_id)
        pipe.execute()
        return {
            "session_id": session_id,
            "title": title,
            "created_at": created_at,
            "turn_count": 0,
        }

    def session_meta(self, user_id: str, session_id: str) -> Optional[dict]:
        raw = self._r.hgetall(session_meta_key(user_id, session_id))
        if not raw:
            return None
        return {
            "session_id": _decode(raw.get(b"session_id", session_id)),
            "title": _decode(raw.get(b"title", DEFAULT_TITLE)),
            "created_at": float(raw.get(b"created_at", 0) or 0),
        }

    def list_session_ids(self, user_id: str) -> list[str]:
        ids = [_decode(s) for s in self._r.smembers(user_sessions_key(user_id))]
        ids.sort(
            key=lambda sid: (self.session_meta(user_id, sid) or {}).get("created_at", 0),
            reverse=True,
        )
        return ids

    def delete_session(self, user_id: str, session_id: str) -> bool:
        existed = bool(self._r.exists(session_meta_key(user_id, session_id)))
        # 先取该会话的运行 id：删掉索引键之后就取不到了
        run_ids = self._run_ids_in_key(session_runs_key(user_id, session_id))
        pipe = self._r.pipeline()
        pipe.delete(session_meta_key(user_id, session_id))
        pipe.delete(session_runs_key(user_id, session_id))
        pipe.srem(user_sessions_key(user_id), session_id)
        pipe.execute()
        # 该会话的运行索引一并删除（检查点由调用方 delete_thread 清理）
        for run_id in run_ids:
            self._r.delete(run_meta_key(user_id, run_id))
            self._r.zrem(user_runs_key(user_id), run_id)
        return existed

    # ------------------------------------------------------------------ 运行目录
    def start_run(self, user_id: str, run_id: str, session_id: Optional[str],
                  thread_id: str, question: str) -> None:
        started_at = time.time()
        pipe = self._r.pipeline()
        pipe.hset(run_meta_key(user_id, run_id), mapping={
            "run_id": run_id,
            "user_id": user_id,
            "session_id": session_id or "",
            "thread_id": thread_id,
            "question": question,
            "started_at": str(started_at),
            "status": RUN_STATUS_RUNNING,
        })
        pipe.zadd(user_runs_key(user_id), {run_id: started_at})
        if session_id:
            pipe.zadd(session_runs_key(user_id, session_id), {run_id: started_at})
        pipe.execute()

    def finish_run(self, user_id: str, run_id: str, status: str = RUN_STATUS_OK,
                   error: str = "", turn_id: str = "") -> None:
        mapping = {
            "status": status,
            "finished_at": str(time.time()),
        }
        if error:
            mapping["error"] = error
        if turn_id:
            mapping["turn_id"] = turn_id
        self._r.hset(run_meta_key(user_id, run_id), mapping=mapping)

    def run_meta(self, user_id: str, run_id: str) -> Optional[dict]:
        raw = self._r.hgetall(run_meta_key(user_id, run_id))
        if not raw:
            return None
        return {
            "run_id": run_id,
            "session_id": _decode(raw.get(b"session_id", "")),
            "thread_id": _decode(raw.get(b"thread_id", "")),
            "question": _decode(raw.get(b"question", "")),
            "started_at": float(raw.get(b"started_at", 0) or 0),
            "finished_at": float(raw.get(b"finished_at", 0) or 0),
            "status": _decode(raw.get(b"status", RUN_STATUS_OK)),
            "error": _decode(raw.get(b"error", "")),
            "turn_id": _decode(raw.get(b"turn_id", "")),
        }

    def list_runs(self, user_id: str, session_id: Optional[str] = None,
                  limit: int = 100) -> list[dict]:
        key = (session_runs_key(user_id, session_id)
               if session_id else user_runs_key(user_id))
        ids = [_decode(rid) for rid in self._r.zrevrange(key, 0, max(limit - 1, 0))]
        runs = [self.run_meta(user_id, rid) for rid in ids]
        return [r for r in runs if r]

    def _run_ids_in_key(self, key: str) -> list[str]:
        return [_decode(rid) for rid in self._r.zrange(key, 0, -1)]


class Projection:
    """把检查点读成两类视图：会话（只含问答）与 Trace（完整执行链路）"""

    # 状态里不进入 Trace 预览的字段（体积大且对排障无直接帮助）
    _SKIP_STATE = {"result", "tracker", "events", "turns", "scratch",
                   "conversation_context", "reference_context"}

    def __init__(self, store: SessionRunStore, graph_provider: Callable = None):
        self.store = store
        self._graph_provider = graph_provider

    def bind_graph(self, graph_provider: Callable) -> None:
        """延迟绑定编译后的图（避免 deps 构造顺序上的循环依赖）"""
        self._graph_provider = graph_provider

    def _graph(self):
        if self._graph_provider is None:
            raise RuntimeError("图尚未绑定，无法读取检查点")
        return self._graph_provider()

    # ------------------------------------------------------------------ 会话投影
    def _turns(self, user_id: str, session_id: str) -> list[dict]:
        try:
            snap = self._graph().get_state(
                {"configurable": {"thread_id": thread_id_for(user_id, session_id),
                                  "checkpoint_ns": ""}}
            )
            return list(snap.values.get("turns") or [])
        except Exception:
            return []

    def session_info(self, user_id: str, session_id: str) -> Optional[dict]:
        meta = self.store.session_meta(user_id, session_id)
        if meta is None:
            return None
        turns = self._turns(user_id, session_id)
        title = turns[0].get("question", "")[:20] if turns else meta["title"]
        return {
            "session_id": session_id,
            "title": title or DEFAULT_TITLE,
            "created_at": meta["created_at"],
            "turn_count": len(turns),
        }

    def list_sessions(self, user_id: str) -> list[dict]:
        result = []
        for session_id in self.store.list_session_ids(user_id):
            info = self.session_info(user_id, session_id)
            if info:
                result.append(info)
        return result

    def session_detail(self, user_id: str, session_id: str) -> Optional[dict]:
        info = self.session_info(user_id, session_id)
        if info is None:
            return None
        info["turns"] = self._turns(user_id, session_id)
        return info

    # ------------------------------------------------------------------ Trace 投影
    def list_traces(self, user_id: str, session_id: Optional[str] = None,
                    limit: int = 100) -> list[dict]:
        return self.store.list_runs(user_id, session_id=session_id, limit=limit)

    def trace_detail(self, user_id: str, run_id: str) -> Optional[dict]:
        meta = self.store.run_meta(user_id, run_id)
        if meta is None:
            return None
        steps = []
        try:
            history = list(self._graph().get_state_history(
                {"configurable": {"thread_id": meta["thread_id"], "checkpoint_ns": ""}}
            ))
        except Exception:
            history = []
        # get_state_history 是最新在前；这里反转成时间顺序，便于前端画链路
        chronological = list(reversed(history))
        steps = []
        prev = None
        prev_events = 0
        # 事件通道是跨轮累积的：基线要跟着所有检查点推进，
        # 否则某一轮的第一个检查点会把上一轮的事件当成自己的增量。
        for snap in chronological:
            events = list(snap.values.get("events") or [])
            new_events = events[prev_events:]
            prev_events = len(events)
            # input 检查点只是“下一次调用开始”的登记（内容还是上一轮状态），
            # 不承载节点执行与事件，不进入链路；loop 检查点才是真正的超级步。
            if (snap.values.get("run_id") != run_id
                    or snap.metadata.get("source") == "input"):
                prev = snap
                continue
            steps.append({
                "checkpoint_id": snap.config["configurable"].get("checkpoint_id", ""),
                "step": snap.metadata.get("step"),
                "source": snap.metadata.get("source", ""),
                "node": self._executed_nodes(prev, snap),
                "next": list(snap.next or []),
                "created_at": getattr(snap, "created_at", None),
                "events": new_events,
                "state": self._state_preview(snap.values),
            })
            prev = snap
        return {**meta, "steps": steps}

    @staticmethod
    def _executed_nodes(prev, snap) -> list[str]:
        """由上一个检查点的 next 反推本检查点是由哪些节点执行产生的。
        LangGraph 1.2 的 metadata 不再携带 writes，next 是稳定可用的信号。"""
        if prev is None or snap.metadata.get("source") == "input":
            return ["input"]
        return list(prev.next or [])

    @classmethod
    def _state_preview(cls, values: dict) -> dict:
        result = values.get("result")
        result_dict = asdict(result) if is_dataclass(result) else (result or {})
        preview = {
            "intent": values.get("intent") or result_dict.get("intent") or "",
            "route": values.get("route") or "",
            "attempt": values.get("attempt", 0),
            "opt_rounds": values.get("opt_rounds", 0),
            "sql": result_dict.get("sql", ""),
            "error_message": values.get("error_message", ""),
            "error": result_dict.get("error"),
            "error_kind": result_dict.get("error_kind"),
            "candidate_tables": values.get("candidate_tables") or [],
            "selected_schema": cls._truncate(values.get("selected_schema", "")),
            "table_selection": values.get("table_selection") or {},
            "fanout_waived": values.get("fanout_waived", False),
            "explanation": result_dict.get("explanation", ""),
            "sql_label": result_dict.get("sql_label", ""),
            "row_total": result_dict.get("total", 0),
            "turn_count": len(values.get("turns") or []),
        }
        for key, value in values.items():
            if key in cls._SKIP_STATE or key in preview:
                continue
            preview[key] = cls._truncate(value)
        return preview

    @staticmethod
    def _truncate(value, limit: int = 400):
        if isinstance(value, str) and len(value) > limit:
            return value[:limit] + "…"
        if isinstance(value, (list, dict)):
            text = str(value)
            return text if len(text) <= limit else text[:limit] + "…"
        return value
