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
    turn_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])  # 稳定标识，供“引用”指定
    sql: str = ""
    tables: List[str] = field(default_factory=list)
    intent: str = "query"
    timestamp: float = field(default_factory=time.time)
    explanation: str = ""                       # SQL 业务化解释
    sql_label: str = ""                         # 解读节点裁判标签：conform/risk/unknown
    result: Optional[dict] = None               # 结果快照 {columns, rows, total, truncated}
    usage: Optional[dict] = None                # 本轮耗时/token 统计


CLARIFY_TTL = 30 * 60                 # 待澄清问题有效期（秒）：用户长时间不答则丢弃

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
                        "clarifies": dict(s.get("clarifies", {})),
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
                            "turn_id": t.turn_id,
                            "question": t.question,
                            "sql": t.sql,
                            "tables": t.tables,
                            "intent": t.intent,
                            "timestamp": t.timestamp,
                            "explanation": t.explanation,
                            "sql_label": t.sql_label,
                            "result": t.result,
                            "usage": t.usage,
                        }
                        for t in s["turns"]
                    ],
                    "clarifies": s.get("clarifies", {}),
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
                "clarifies": {},
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
                 explanation: str = "", sql_label: str = "",
                 result: dict = None, usage: dict = None) -> Optional[Turn]:
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
                explanation=explanation, sql_label=sql_label,
                result=result, usage=usage,
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

    def build_context(self, session_id: str,
                      reference_turn_id: Optional[str] = None) -> str:
        """构建注入 Prompt 的多轮上下文。
        - 显式引用（reference_turn_id 命中）：只注入被引用轮，标注为“引用的历史问答”，
          不再自动带上最近一轮（引用语义替代默认上一轮，避免跨话题污染）；
        - 否则：取最近一轮（省略式追问的默认上下文）。"""
        session = self._sessions.get(session_id) if session_id else None
        if not session or not session["turns"]:
            return ""
        if reference_turn_id:
            ref = self.get_turn(session_id, reference_turn_id)
            if ref is None:
                # 引用轮不存在/已过期：静默退化为默认最近一轮，不阻断提问
                ref = session["turns"][-1]
                quoted = False
            else:
                quoted = True
            if quoted:
                lines = [
                    "【引用的历史问答】用户在本轮明确引用了同一会话中的以下这轮对话，"
                    "请以它为基础结合本轮新问题改写/生成 SQL：",
                    f"引用轮问题：{ref.question}",
                    f"引用轮 SQL：{ref.sql}",
                ]
                if ref.tables:
                    lines.append(f"引用轮涉及的表：{', '.join(ref.tables)}")
                return "\n".join(lines)
        last = session["turns"][-1]
        lines = [
            "【多轮对话上下文】以下是同一会话中上一轮的对话记录，本轮问题可能是对它的省略式追问（如\"按月份分组\"\"只看前10\"），请结合上下文理解并基于上一轮 SQL 改写生成本轮 SQL：",
            f"上一轮问题：{last.question}",
            f"上一轮 SQL：{last.sql}",
        ]
        if last.tables:
            lines.append(f"上一轮涉及的表：{', '.join(last.tables)}")
        return "\n".join(lines)

    def get_turn(self, session_id: str, turn_id: str) -> Optional[Turn]:
        """按 turn_id 取会话内某一轮（引用功能）；不存在返回 None"""
        session = self._sessions.get(session_id)
        if not session:
            return None
        for t in session["turns"]:
            if t.turn_id == turn_id:
                return t
        return None

    def turn_tables(self, session_id: str, turn_id: str) -> List[str]:
        """指定轮涉及的表（引用场景：候选表强制并入引用轮的表）"""
        t = self.get_turn(session_id, turn_id)
        return list(t.tables or []) if t else []

    # ------------------------------------------------------------------ 澄清暂存
    def save_clarify(self, session_id: str, question: str,
                     questions: List[str], reference_turn_id: Optional[str] = None) -> Optional[str]:
        """表筛选阶段发起人工澄清：暂存原始问题与待确认点，等待用户补充后续跑。
        返回 clarify_id；会话不存在返回 None。"""
        with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                return None
            clarify_id = uuid.uuid4().hex[:12]
            session.setdefault("clarifies", {})[clarify_id] = {
                "question": question,
                "questions": list(questions),
                "reference_turn_id": reference_turn_id,
                "created_at": time.time(),
            }
            self._save()
            return clarify_id

    def take_clarify(self, session_id: str, clarify_id: str) -> Optional[dict]:
        """取出并删除一条待澄清记录（一次性消费；过期/不存在返回 None）"""
        with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                return None
            item = session.get("clarifies", {}).pop(clarify_id, None)
            if item is None:
                return None
            self._save()
            if time.time() - item.get("created_at", 0) > CLARIFY_TTL:
                logger.info("待澄清记录已过期，丢弃：%s", clarify_id)
                return None
            return item

    # ------------------------------------------------------------------ 内部
    def _cleanup_locked(self):
        """懒清理过期会话与过期待澄清记录"""
        now = time.time()
        changed = False
        for s in self._sessions.values():
            clarifies = s.get("clarifies") or {}
            alive = {cid: c for cid, c in clarifies.items()
                     if now - c.get("created_at", 0) <= CLARIFY_TTL}
            if len(alive) != len(clarifies):
                s["clarifies"] = alive
                changed = True
        if changed:
            self._save()
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
                "turn_id": t.turn_id,
                "question": t.question,
                "sql": t.sql,
                "tables": t.tables,
                "intent": t.intent,
                "timestamp": t.timestamp,
                "explanation": t.explanation,
                "sql_label": t.sql_label,
                "result": t.result,
                "usage": t.usage,
            }
            for t in session["turns"]
        ]
        return info
