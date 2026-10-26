# 多轮会话：对 LangGraph 检查点做“会话视图”投影。
#
# 改造前：JSON 文件保存全部轮次，带 24h TTL，服务重启依赖文件回放；
# 改造后：
#   - 事实源 = 检查点状态中的 turns（追加式 DeltaChannel），本模块只读取；
#   - Redis 仅保存会话目录（id/标题/创建时间）与人工澄清的临时现场；
#   - 会话不再自动过期，删除会话时同时删除对应检查点线程。

import json
import time
import uuid
from typing import List, Optional

from app.persistence.redis_store import (
    clarify_key,
    get_redis_client,
    thread_id_for,
)
from app.persistence.store import Projection, SessionRunStore

CLARIFY_TTL = 30 * 60            # 待澄清现场的有效期（秒）：只是临时交互状态
REPLAY_ROWS_LIMIT = 50           # 每轮结果快照最多保留的行数（回放够用，控制检查点体积）


class ConversationManager:
    """会话管理器：会话目录 + 检查点投影 + 澄清暂存"""

    def __init__(self, store: SessionRunStore = None, checkpointer=None):
        self.store = store or SessionRunStore()
        self.projection = Projection(self.store)
        self.checkpointer = checkpointer
        self._r = get_redis_client()

    def bind_graph(self, graph_provider) -> None:
        """由流水线注入编译后的图：会话正文只从检查点读"""
        self.projection.bind_graph(graph_provider)

    # ------------------------------------------------------------------ CRUD
    def create(self, user_id: str = "default", title: str = "新会话") -> dict:
        return self.store.create_session(user_id, title)

    def get(self, user_id: str, session_id: str) -> Optional[dict]:
        return self.projection.session_detail(user_id, session_id)

    def list_sessions(self, user_id: str = "default") -> List[dict]:
        return self.projection.list_sessions(user_id)

    def delete(self, user_id: str, session_id: str) -> bool:
        existed = self.store.delete_session(user_id, session_id)
        if self.checkpointer is not None:
            self.checkpointer.delete_thread(thread_id_for(user_id, session_id))
        return existed

    # ------------------------------------------------------------------ 轮次投影
    def _turns(self, user_id: str, session_id: str) -> List[dict]:
        return self.projection._turns(user_id, session_id)

    def last_tables(self, user_id: str, session_id: str) -> List[str]:
        """最近一轮涉及表（多轮场景：检索候选表需强制并入上轮表）"""
        turns = self._turns(user_id, session_id) if session_id else []
        return list(turns[-1].get("tables") or []) if turns else []

    def build_context(self, user_id: str, session_id: str,
                      reference_turn_id: Optional[str] = None) -> str:
        """构建注入 Prompt 的多轮上下文。
        - 显式引用（reference_turn_id 命中）：只注入被引用轮，标注为“引用的历史问答”；
        - 否则：取最近一轮（省略式追问的默认上下文）。"""
        if not session_id:
            return ""
        turns = self._turns(user_id, session_id)
        if not turns:
            return ""
        if reference_turn_id:
            ref = self.get_turn(user_id, session_id, reference_turn_id)
            if ref is not None:
                lines = [
                    "【引用的历史问答】用户在本轮明确引用了同一会话中的以下这轮对话，"
                    "请以它为基础结合本轮新问题改写/生成 SQL：",
                    f"引用轮问题：{ref['question']}",
                    f"引用轮 SQL：{ref.get('sql', '')}",
                ]
                if ref.get("tables"):
                    lines.append(f"引用轮涉及的表：{', '.join(ref['tables'])}")
                return "\n".join(lines)
            # 引用轮不存在：静默退化为默认最近一轮，不阻断提问
        last = turns[-1]
        lines = [
            "【多轮对话上下文】以下是同一会话中上一轮的对话记录，本轮问题可能是对它的省略式追问（如\"按月份分组\"\"只看前10\"），请结合上下文理解并基于上一轮 SQL 改写生成本轮 SQL：",
            f"上一轮问题：{last['question']}",
            f"上一轮 SQL：{last.get('sql', '')}",
        ]
        if last.get("tables"):
            lines.append(f"上一轮涉及的表：{', '.join(last['tables'])}")
        return "\n".join(lines)

    def get_turn(self, user_id: str, session_id: str,
                 turn_id: str) -> Optional[dict]:
        for turn in self._turns(user_id, session_id):
            if turn.get("turn_id") == turn_id:
                return turn
        return None

    def turn_tables(self, user_id: str, session_id: str,
                    turn_id: str) -> List[str]:
        turn = self.get_turn(user_id, session_id, turn_id)
        return list(turn.get("tables") or []) if turn else []

    # ------------------------------------------------------------------ 澄清暂存
    def save_clarify(self, user_id: str, session_id: str, question: str,
                     questions: List[str],
                     reference_turn_id: Optional[str] = None) -> Optional[str]:
        """表筛选阶段发起人工澄清：暂存原始问题与待确认点，等待用户补充后续跑。
        现场放在 Redis 并带 30 分钟 TTL（临时交互状态，不是会话事实）。"""
        if self.store.session_meta(user_id, session_id) is None:
            return None
        clarify_id = uuid.uuid4().hex[:12]
        payload = {
            "question": question,
            "questions": list(questions),
            "reference_turn_id": reference_turn_id,
            "created_at": time.time(),
        }
        self._r.setex(
            clarify_key(user_id, session_id, clarify_id),
            CLARIFY_TTL,
            json.dumps(payload, ensure_ascii=False),
        )
        return clarify_id

    def take_clarify(self, user_id: str, session_id: str,
                     clarify_id: str) -> Optional[dict]:
        """取出并删除一条待澄清记录（一次性消费；过期/不存在返回 None）"""
        raw = self._r.getdel(clarify_key(user_id, session_id, clarify_id))
        if raw is None:
            return None
        try:
            item = json.loads(raw)
        except Exception:
            return None
        if time.time() - item.get("created_at", 0) > CLARIFY_TTL:
            return None
        return item
