# 人工审批存储（预埋，当前未启用）
#
# 用途：Gate 对 action="ask" 的高风险能力申请返回“待审批”裁决后，编排层在此创建
# PendingApproval，暂停该次能力调用并向用户推送审批事件；用户批准/拒绝后，
# 编排层凭审批结论创建一次性 Grant（带 args 哈希 + TTL）访问 Registry，
# 架构上仍然不存在旁路（一切执行必经 Registry 验 Grant）。
#
# 当前状态（2026-10 预埋）：
#   - gate_rules.yml 的 meta.approval_enabled=false，ask 命中即在 Gate 降级为 deny；
#   - 本模块无任何调用方，HTTP 审批接口与前端交互尚未实现；
#   - 现网 Skill/Tool 全部 risk=low，R110-HIGH-APPROVAL 不会被命中。
# 未来上线步骤（均不改变既有决策协议）：
#   1. 配置开关 approval_enabled=true；
#   2. 编排器收到 Decision(action="ask") 时调用 ApprovalStore.create() 并暂停，
#      经 SSE 推送 approval_required 事件（含申请参数快照/风险/rule_id）；
#   3. 新增 POST /api/approval 接口（approve/reject），写回本存储；
#   4. 编排器恢复执行：approved → 生成 Grant 调 Registry；rejected/超时 → 按拒绝回喂模型。

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional

APPROVAL_TTL = 10 * 60   # 待审批记录默认有效期（秒）：超时视为拒绝（fail-closed）


@dataclass
class PendingApproval:
    """一条待人工审批的高风险能力申请快照"""

    approval_id: str
    session_id: str
    kind: str                    # skill / tool
    name: str
    args: dict                   # 申请参数快照（批准后重建 Grant 时做 args 哈希比对）
    rule_id: str
    reason: str
    decision_id: str             # 触发本审批的 Gate 裁决 ID（审计串联）
    created_at: float = field(default_factory=time.time)
    ttl: int = APPROVAL_TTL
    status: str = "pending"      # pending / approved / rejected / expired
    decided_by: Optional[str] = None   # 预埋：账户体系接入后记录审批人
    decided_at: Optional[float] = None


class ApprovalStore:
    """待审批申请存储（内存实现；多实例部署时替换为 Redis/DB，接口保持不变）"""

    def __init__(self, ttl: int = APPROVAL_TTL):
        self._items: Dict[str, PendingApproval] = {}
        self._lock = threading.Lock()
        self._ttl = ttl

    def create(self, session_id: str, kind: str, name: str, args: dict,
               rule_id: str, reason: str, decision_id: str) -> PendingApproval:
        with self._lock:
            item = PendingApproval(
                approval_id=uuid.uuid4().hex[:12],
                session_id=session_id, kind=kind, name=name,
                args=dict(args or {}), rule_id=rule_id, reason=reason,
                decision_id=decision_id, ttl=self._ttl,
            )
            self._items[item.approval_id] = item
            return item

    def get(self, approval_id: str) -> Optional[PendingApproval]:
        with self._lock:
            item = self._items.get(approval_id)
            if item and item.status == "pending" and self._expired(item):
                item.status = "expired"
                return None
            return item

    def decide(self, approval_id: str, approved: bool,
               approver: Optional[str] = None) -> Optional[PendingApproval]:
        """写入审批结论（一次性；pending 之外的状态不可变更）"""
        with self._lock:
            item = self._items.get(approval_id)
            if item is None or item.status != "pending" or self._expired(item):
                return None
            item.status = "approved" if approved else "rejected"
            item.decided_by = approver
            item.decided_at = time.time()
            return item

    def list_pending(self, session_id: Optional[str] = None) -> List[PendingApproval]:
        with self._lock:
            return [
                it for it in self._items.values()
                if it.status == "pending" and not self._expired(it)
                and (session_id is None or it.session_id == session_id)
            ]

    @staticmethod
    def _expired(item: PendingApproval) -> bool:
        return time.time() - item.created_at > item.ttl
