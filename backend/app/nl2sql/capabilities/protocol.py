# 能力（Skill / Tool）治理协议层：请求、决策、授权凭证、执行上下文、审计条目。
# 架构约束（见 gate 设计原则）：
#   - Skill 正文与 Tool 执行只能凭 Gate 颁发的 Grant 访问（Grant 含决策 ID 与参数哈希，
#     执行前校验，防参数篡改）；Registry 不对外暴露未授权访问路径。
#   - 未来接入账户体系时，只需在组装 ExecutionContext 处注入真实 Principal
#     （角色/权限/行级范围），规则 DSL 已预埋 roles / required_permission 条件，无需改协议。

import hashlib
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class Principal:
    """请求主体（当前系统无账户体系，固定匿名只读主体；未来由认证中间件填充）"""

    id: str = "anonymous"
    roles: List[str] = field(default_factory=lambda: ["readonly"])
    permissions: List[str] = field(default_factory=list)   # 预埋：细粒度权限码
    row_scope: Optional[dict] = None                      # 预埋：行级数据范围（未来 RLS 注入）


@dataclass
class ExecutionContext:
    """一次决策的上下文快照输入（Gate 组装，审计随决策持久化）"""

    phase: str                          # generate / correct / dml / explain ...
    question: str = ""
    intent: str = "query"
    candidate_tables: Optional[List[str]] = None
    attempt: int = 0
    opt_rounds: int = 0
    principal: Principal = field(default_factory=Principal)
    extra: Dict[str, Any] = field(default_factory=dict)   # 预埋扩展位

    def snapshot(self) -> dict:
        """决策时的上下文快照（审计用；问题截断，不含 SQL 结果等大对象）"""
        return {
            "phase": self.phase,
            "question": (self.question or "")[:200],
            "intent": self.intent,
            "candidate_tables": self.candidate_tables,
            "attempt": self.attempt,
            "opt_rounds": self.opt_rounds,
            "principal_id": self.principal.id,
            "principal_roles": list(self.principal.roles),
        }


@dataclass
class CapabilityRequest:
    """模型发起的单个能力请求"""

    kind: str               # "skill" / "tool"
    name: str
    args: Dict[str, Any] = field(default_factory=dict)

    def args_hash(self) -> str:
        norm = str(self.args)
        return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]

    def key(self) -> str:
        return f"{self.kind}:{self.name}"


@dataclass
class Grant:
    """Gate 颁发的授权凭证：SkillProvider/ToolExecutor 只认凭证不认调用方"""

    decision_id: str
    kind: str
    name: str
    args_hash: str
    issued_at: float
    ttl_s: int = 300

    def valid_for(self, kind: str, name: str, args: Dict[str, Any]) -> bool:
        req = CapabilityRequest(kind=kind, name=name, args=args)
        return (
            self.kind == kind and self.name == name
            and self.args_hash == req.args_hash()
            and time.time() - self.issued_at <= self.ttl_s
        )


@dataclass
class Decision:
    """一次能力请求的裁决结果（可解释：命中规则/原因/纠偏指引）"""

    request: CapabilityRequest
    action: str                       # "allow" / "deny"
    rule_id: str
    reason: str
    correction_hint: str = ""
    context_snapshot: Dict[str, Any] = field(default_factory=dict)
    decision_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    decided_at: float = field(default_factory=time.time)

    @property
    def allowed(self) -> bool:
        return self.action == "allow"

    def to_grant(self) -> Grant:
        return Grant(
            decision_id=self.decision_id,
            kind=self.request.kind,
            name=self.request.name,
            args_hash=self.request.args_hash(),
            issued_at=self.decided_at,
        )
