# 多轮对话上下文管理：保存每轮 {问题, SQL, 涉及表, 意图, 解释, 结果快照, usage}，
# 追问时将上一轮 SQL 显式注入 Prompt（比从对话文本重建准确）。
# 存储为 JSON 文件（backend/data/session_store.json），服务重启后会话不丢；
# 会话带 TTL 过期清理。后续多实例部署可替换为 Redis 等共享存储。

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from app.utils.logger_handler import setup_logger
from app.utils.path_tool import BACKEND_ROOT

logger = setup_logger()

SESSION_TTL = 24 * 3600            # 会话过期时间（秒）
STORE_FILE = BACKEND_ROOT / "data" / "session_store.json"
REPLAY_ROWS_LIMIT = 50             # 每轮结果快照最多保存的行数（回放展示够用，控制文件体积）


@dataclass
class Turn:
    """单轮对话记录（含回放所需的全部信息）"""

    question: str
    sql: str = ""
    tables: List[str] = field(default_factory=list)
    intent: str = "query"
    timestamp: float = field(default_factory=time.time)
    explanation: str = ""                       # SQL 业务化解释
    result: Optional[dict] = None               # 结果快照 {columns, rows, total, truncated}
    usage: Optional[dict] = None                # 本轮耗时/token 统计


class ConversationManager:
    """会话管理器：创建/查询/删除会话，构建多轮上下文；JSON 文件持久化"""

    def __init__(self, ttl: int = SESSION_TTL, store_file=STORE_FILE):
        self._sessions: Dict[str, dict] = {}
        self._ttl = ttl
        self._store_file = store_file
        self._lock = threading.Lock()
        self._load()

    # ------------------------------------------------------------------ 持久化
    def _load(self):
        """启动时从文件恢复会话"""
        try:
            if self._store_file.exists():
                with open(self._store_file, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                self._sessions = {
                    sid: {
                        "session_id": sid,
                        "title": s.get("title", "新会话"),
                        "created_at": s.get("created_at", time.time()),
                        "turns": [Turn(**t) for t in s.get("turns", [])],
                    }
                    for sid, s in raw.items()
                }
                logger.info("会话存储恢复：%d 个会话", len(self._sessions))
        except Exception as e:
            logger.warning("会话存储恢复失败（将使用空存储）: %s", e)
            self._sessions = {}

    def _save(self):
        """变更后落盘（原子写）"""
        try:
            self._store_file.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                sid: {
                    "title": s["title"],
                    "created_at": s["created_at"],
                    "turns": [
                        {
                            "question": t.question,
                            "sql": t.sql,
                            "tables": t.tables,
                            "intent": t.intent,
                            "timestamp": t.timestamp,
                            "explanation": t.explanation,
                            "result": t.result,
                            "usage": t.usage,
                        }
                        for t in s["turns"]
                    ],
                }
                for sid, s in self._sessions.items()
            }
            tmp = self._store_file.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            tmp.replace(self._store_file)
        except Exception as e:
            logger.error("会话存储写入失败: %s", e)

    # ------------------------------------------------------------------ CRUD
    def create(self, title: str = "新会话") -> dict:
        with self._lock:
            self._cleanup_locked()
            session_id = uuid.uuid4().hex[:12]
            self._sessions[session_id] = {
                "session_id": session_id,
                "title": title,
                "created_at": time.time(),
                "turns": [],
            }
            self._save()
        logger.info("创建会话 %s", session_id)
        return self._info(self._sessions[session_id])

    def get(self, session_id: str) -> Optional[dict]:
        """返回会话详情（含全部轮次）；不存在或已过期返回 None"""
        with self._lock:
            self._cleanup_locked()
            session = self._sessions.get(session_id)
            return self._detail(session) if session else None

    def list_sessions(self) -> List[dict]:
        with self._lock:
            self._cleanup_locked()
            return [self._info(s) for s in sorted(
                self._sessions.values(), key=lambda s: s["created_at"], reverse=True
            )]

    def delete(self, session_id: str) -> bool:
        with self._lock:
            removed = self._sessions.pop(session_id, None) is not None
            if removed:
                self._save()
            return removed

    # ------------------------------------------------------------------ 轮次
    def add_turn(self, session_id: str, question: str, sql: str,
                 tables: List[str] = None, intent: str = "query",
                 explanation: str = "", result: dict = None,
                 usage: dict = None) -> Optional[Turn]:
        """记录一轮对话（含回放所需的解释/结果快照/统计）；首个问题自动作为标题"""
        with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                return None
            # 结果快照截断，控制存储体积
            if result and isinstance(result.get("rows"), list):
                result = dict(result)
                result["rows"] = result["rows"][:REPLAY_ROWS_LIMIT]
            turn = Turn(
                question=question, sql=sql, tables=tables or [], intent=intent,
                explanation=explanation, result=result, usage=usage,
            )
            session["turns"].append(turn)
            if len(session["turns"]) == 1:
                session["title"] = question[:20]
            self._save()
            return turn

    def last_tables(self, session_id: str) -> List[str]:
        """最近一轮涉及表（多轮场景：检索候选表需强制并入上轮表）"""
        session = self._sessions.get(session_id)
        if not session or not session["turns"]:
            return []
        return list(session["turns"][-1].tables or [])

    def build_context(self, session_id: str) -> str:
        """构建注入 Prompt 的多轮上下文：取最近一轮的 SQL 与关键信息"""
        session = self._sessions.get(session_id)
        if not session or not session["turns"]:
            return ""
        last = session["turns"][-1]
        lines = [
            "【多轮对话上下文】以下是同一会话中上一轮的对话记录，本轮问题可能是对它的省略式追问（如\"按月份分组\"\"只看前10\"），请结合上下文理解并基于上一轮 SQL 改写生成本轮 SQL：",
            f"上一轮问题：{last.question}",
            f"上一轮 SQL：{last.sql}",
        ]
        if last.tables:
            lines.append(f"上一轮涉及的表：{', '.join(last.tables)}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ 内部
    def _cleanup_locked(self):
        """懒清理过期会话"""
        now = time.time()
        expired = [sid for sid, s in self._sessions.items() if now - s["created_at"] > self._ttl]
        for sid in expired:
            del self._sessions[sid]
        if expired:
            self._save()
            logger.info("清理过期会话 %d 个", len(expired))

    @staticmethod
    def _info(session: dict) -> dict:
        return {
            "session_id": session["session_id"],
            "title": session["title"],
            "created_at": session["created_at"],
            "turn_count": len(session["turns"]),
        }

    @staticmethod
    def _detail(session: dict) -> dict:
        info = ConversationManager._info(session)
        info["turns"] = [
            {
                "question": t.question,
                "sql": t.sql,
                "tables": t.tables,
                "intent": t.intent,
                "timestamp": t.timestamp,
                "explanation": t.explanation,
                "result": t.result,
                "usage": t.usage,
            }
            for t in session["turns"]
        ]
        return info
