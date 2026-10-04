# SkillToolGate —— Skill 注入与 Tool 调用的唯一决策点。
#
# 设计原则落地：
#   2.1.1 单一决策点：编排层只能通过 decide() 取得 Grant，再凭 Grant 访问 Registry；
#         Registry 无 Grant 拒绝服务，架构上封死旁路（不靠代码约定）。
#   2.1.2 默认拒绝：无任何规则显式 Allow（含能力未注册）→ Deny。
#   2.1.3 Fail-closed：规则加载失败 / Gate 异常 / 审计器异常时拒绝放行；
#         Gate 不可用时编排层自动收不到任何 Skill/Tool（等价只读降级）。
#   2.1.4 规则与代码分离：策略全部在 config/gate_rules.yml，由 RuleEngine 执行。
#   2.1.5 可解释可审计：Decision 带 rule_id/reason/快照/纠偏指引；每条裁决写审计。
#   2.2.4 Skill↔Tool 联动：cross_check() 为预埋钩子（风险等级联动矩阵暂不生效）。
#   2.2.6 防死循环：同一请求被 Deny 后重复发起，直接按防循环规则拒绝并强提示。

import copy
from typing import List, Optional

from .audit import AuditLogger
from .protocol import CapabilityRequest, Decision, ExecutionContext
from .registry import CapabilityRegistry
from .rules import RuleEngine

RULE_UNKNOWN = "R404-UNKNOWN"           # 能力未注册
RULE_DEFAULT_DENY = "R403-DEFAULT"      # 无规则命中
RULE_GATE_DOWN = "R503-FAILCLOSED"      # Gate/规则不可用
RULE_LOOP = "R429-LOOP-GUARD"           # 重复请求被拦截
RULE_GATE_ERROR = "R500-GATE-ERROR"     # 评估过程异常
RULE_APPROVAL_DISABLED = "R403-APPROVAL-OFF"  # 命中审批规则但审批功能未上线


class SkillToolGate:
    def __init__(self, registry: CapabilityRegistry, engine: Optional[RuleEngine],
                 audit: AuditLogger, healthy: bool = True,
                 approval_enabled: bool = False):
        self.registry = registry
        self.engine = engine
        self.audit = audit
        self.healthy = healthy and engine is not None
        # 【人工审批-预埋，当前恒 False】True 时 ask 规则裁决为挂起等待审批；
        # False（默认）时 ask 一律按拒绝处理（fail-closed），与现有 bypass/FC 协议不冲突。
        self.approval_enabled = approval_enabled

    # ------------------------------------------------------------ 批量决策
    def decide_batch(self, requests: List[CapabilityRequest],
                     ctx: ExecutionContext) -> List[Decision]:
        return [self.decide(req, ctx) for req in requests]

    def decide(self, req: CapabilityRequest, ctx: ExecutionContext) -> Decision:
        try:
            if not self.healthy:
                # 决策层保留 fail-closed 语义（直接调用 decide 时拒绝）。
                # 注意：当前编排层策略是 bypass（见 orchestrator）——现有能力均为只读低风险，
                # Gate 故障时让模型自行发挥、DQL 照常执行；此 R503 分支为预埋防线，
                # 未来注册 medium/high 能力后编排层必须改为走这里 fail-closed。
                return self._deny(req, ctx, RULE_GATE_DOWN,
                                  "Gate 不可用，fail-closed 拒绝该能力申请",
                                  "不依赖额外技能与工具，仅凭已提供的通用规则与表结构直接生成只读 SQL")
            # 2.2.6 防死循环：同 key+同参数 被拒后重复请求
            denied_keys = ctx.extra.setdefault("denied_keys", set())
            sig = f"{req.key()}@{req.args_hash()}"
            if sig in denied_keys:
                d = self._deny(
                    req, ctx, RULE_LOOP,
                    "该请求此前已被拒绝，重复发起触发防死循环保护",
                    "此能力本轮不可用，禁止再次请求；请依据已提供的信息直接完成 SQL 生成")
                return d

            # 2.1.2 默认拒绝之第一重：能力必须先在统一注册层登记
            risk = self.registry.risk_of(req.kind, req.name)
            if risk is None:
                d = self._deny(req, ctx, RULE_UNKNOWN,
                               f"未注册的{req.kind}：{req.name}（默认拒绝）",
                               f"请只使用能力清单中列出的{req.kind}名称，检查拼写后重试或不使用该能力")
                denied_keys.add(sig)
                return d

            # 风险等级由 Gate 从注册元数据注入规则评估（不信任请求方自报风险）
            eval_req = copy.deepcopy(req)
            eval_req.args["__risk"] = risk
            hit = self.engine.evaluate(eval_req, ctx)
            if hit is None:
                d = self._deny(req, ctx, RULE_DEFAULT_DENY,
                               "没有任何规则明确允许该请求（默认拒绝）",
                               "请改用清单中允许的能力，或不使用额外能力直接生成 SQL")
                denied_keys.add(sig)
                return d
            rule, detail = hit
            if rule.action == "deny":
                d = self._deny(req, ctx, rule.id,
                               f"{rule.reason}（匹配条件：{detail}）",
                               rule.correction_hint or "请改用其他允许的能力或直接生成 SQL")
                denied_keys.add(sig)
                return d
            if rule.action == "ask":
                # 人工审批裁决点：功能上线后这里返回 action="ask"，由编排层挂起申请、
                # 等待用户经 /approval 接口给出结论（ApprovalStore 已在 approval.py 预埋）。
                # 当前审批未启用：fail-closed 按拒绝处理，绝不在无人确认时放行高危能力。
                if not self.approval_enabled:
                    d = self._deny(
                        req, ctx, RULE_APPROVAL_DISABLED,
                        f"{rule.reason}（匹配条件：{detail}）；人工审批功能当前未开放，"
                        f"命中规则 {rule.id} 的申请一律拒绝",
                        rule.correction_hint or "请改用低风险只读能力或直接生成 SQL")
                    denied_keys.add(sig)
                    return d
                d = Decision(request=req, action="ask", rule_id=rule.id,
                             reason=f"{rule.reason}（匹配条件：{detail}）",
                             correction_hint=rule.correction_hint,
                             context_snapshot=ctx.snapshot())
                self._audit(d, ctx)
                return d
            return self._allow(req, ctx, rule.id, f"{rule.reason}（{detail}）")
        except Exception as e:  # 2.1.3 Gate 自身故障 → fail-closed
            return self._deny(req, ctx, RULE_GATE_ERROR, f"Gate 评估异常：{e}",
                              "请忽略该能力调用，直接依据已有信息生成只读 SQL")

    # ------------------------------------------------------------ 2.2.4（预埋）
    def cross_check(self, decisions: List[Decision], ctx: ExecutionContext
                    ) -> List[Decision]:
        """Skill↔Tool 联动校验钩子。
        【预埋-暂不实现】未来在此依据"风险等级调用矩阵"做联动限制，例如：
        高风险 Skill 不允许联动数据导出 Tool、某些 Tool 仅允许在特定 Skill 上下文内调用。
        当前所有能力 risk=low 且无联动场景，直接原样返回；矩阵配置建议放 gate_rules.yml
        的 linkage 段，本函数签名保持不变，未来实现不影响编排层。"""
        return decisions

    # ------------------------------------------------------------ 裁决构造+审计
    def _allow(self, req, ctx, rule_id, reason) -> Decision:
        d = Decision(request=req, action="allow", rule_id=rule_id, reason=reason,
                     context_snapshot=ctx.snapshot())
        self._audit(d, ctx)
        return d

    def _deny(self, req, ctx, rule_id, reason, hint) -> Decision:
        d = Decision(request=req, action="deny", rule_id=rule_id, reason=reason,
                     correction_hint=hint, context_snapshot=ctx.snapshot())
        self._audit(d, ctx)
        return d

    def _audit(self, d: Decision, ctx: ExecutionContext) -> None:
        self.audit.record({
            "decision_id": d.decision_id,
            "action": d.action,
            "rule_id": d.rule_id,
            "kind": d.request.kind,
            "name": d.request.name,
            "args": d.request.args,
            "reason": d.reason,
            "correction_hint": d.correction_hint,
            "phase": ctx.phase,
            "context": d.context_snapshot,
        })
