# 声明式规则引擎：策略与代码分离。规则存 YAML（可独立测试/版本化/灰度/回滚，改策略不发版）。
# 规则结构：
#   id / description / priority（升序评估）/ action(allow|deny) / reason / correction_hint
#   when: 条件列表，全部满足才命中（AND）。支持：
#     kind: skill | tool | "*"
#     name_in: [..] / name: ".."
#     risk_lte / risk_gte: low|medium|high
#     roles_any: [..]          （主体角色，当前固定 readonly；预埋账户体系）
#     phase_in: [..]
#     required_permission: ..  （【预埋-暂不生效】接入权限码后启用）
#     tables_in_scope: true    （【预埋-暂不生效】接入行级/库表范围后启用）
# 评估语义：按 priority 升序逐条匹配，首条命中即决策（显式 deny 规则应给更小 priority）；
#          无任何规则命中 → 由 Gate 执行默认拒绝（default-deny）。

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from .protocol import CapabilityRequest, ExecutionContext

_RISK_RANK = {"low": 1, "medium": 2, "high": 3}

# 已生效的条件键；其余键视为预埋条件，出现时不参与匹配（并在 Gate 健康检查时提示）
SUPPORTED_COND_KEYS = {
    "kind", "name", "name_in", "risk_lte", "risk_gte", "roles_any", "phase_in",
}
FORWARD_COND_KEYS = {"required_permission", "tables_in_scope"}


@dataclass
class Rule:
    id: str
    priority: int
    action: str
    reason: str
    when: Dict[str, Any] = field(default_factory=dict)
    description: str = ""
    correction_hint: str = ""


class RuleConfigError(Exception):
    pass


class RuleEngine:
    def __init__(self, rules: List[Rule], forward_keys: Optional[List[str]] = None):
        self.rules = sorted(rules, key=lambda r: r.priority)
        self.forward_only_keys = set(forward_keys or FORWARD_COND_KEYS)

    @classmethod
    def from_yaml(cls, path: str | Path) -> "RuleEngine":
        p = Path(path)
        with open(p, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        raw = data.get("rules")
        if not isinstance(raw, list) or not raw:
            raise RuleConfigError(f"{p} 缺少有效 rules 列表（fail-closed：规则不可为空）")
        rules: List[Rule] = []
        for i, item in enumerate(raw):
            try:
                rid = str(item["id"])
                action = str(item["action"]).lower()
                if action not in ("allow", "deny"):
                    raise ValueError(f"action 非法: {action}")
                rules.append(Rule(
                    id=rid,
                    priority=int(item.get("priority", 100)),
                    action=action,
                    reason=str(item.get("reason", "")),
                    when=dict(item.get("when") or {}),
                    description=str(item.get("description", "")),
                    correction_hint=str(item.get("correction_hint", "")),
                ))
            except (KeyError, TypeError, ValueError) as e:
                raise RuleConfigError(f"{p} 第 {i + 1} 条规则非法: {e}") from e
        return cls(rules, list(data.get("forward_only_keys", [])))

    def evaluate(self, req: CapabilityRequest, ctx: ExecutionContext
                 ) -> Optional[tuple]:
        """返回 (Rule, 命中说明) ；无规则命中返回 None（调用方默认拒绝）"""
        for rule in self.rules:
            ok, detail = self._match(rule.when, req, ctx)
            if ok:
                return rule, detail
        return None

    # ------------------------------------------------------------- 条件评估
    def _match(self, when: Dict[str, Any], req: CapabilityRequest,
               ctx: ExecutionContext):
        details: List[str] = []
        for key, expect in when.items():
            if key in self.forward_only_keys:
                continue  # 预埋条件：暂不生效
            checker = _COND_CHECKERS.get(key)
            if checker is None:
                return False, f"未知条件 {key}"
            ok, detail = checker(expect, req, ctx)
            if not ok:
                return False, detail
            details.append(detail)
        return True, "; ".join(details)


def _risk(v: Optional[str]) -> int:
    return _RISK_RANK.get((v or "").lower(), 0)


def _c_kind(expect, req, ctx):
    ok = expect == "*" or req.kind == expect
    return ok, f"kind={req.kind}"


def _c_name(expect, req, ctx):
    ok = req.name == expect
    return ok, f"name={req.name}"


def _c_name_in(expect, req, ctx):
    ok = req.name in (expect or [])
    return ok, f"name={req.name} in {expect}"


def _c_risk_lte(expect, req, ctx):
    # 风险等级随请求元数据由 Gate 补入 args.__risk；未知能力风险视为 high（默认拒绝倾向）
    r = req.args.get("__risk")
    ok = _risk(r) <= _risk(expect)
    return ok, f"risk={r}<= {expect}"


def _c_risk_gte(expect, req, ctx):
    r = req.args.get("__risk")
    ok = _risk(r) >= _risk(expect)
    return ok, f"risk={r}>= {expect}"


def _c_roles_any(expect, req, ctx):
    ok = bool(set(ctx.principal.roles) & set(expect or []))
    return ok, f"roles={ctx.principal.roles}∩{expect}"


def _c_phase(expect, req, ctx):
    ok = ctx.phase in (expect or [])
    return ok, f"phase={ctx.phase}"


_COND_CHECKERS = {
    "kind": _c_kind,
    "name": _c_name,
    "name_in": _c_name_in,
    "risk_lte": _c_risk_lte,
    "risk_gte": _c_risk_gte,
    "roles_any": _c_roles_any,
    "phase_in": _c_phase,
}
