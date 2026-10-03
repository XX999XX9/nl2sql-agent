# CapabilityOrchestrator —— SQL 生成节点内的原生 function calling 循环。
#
# 唯一链路（OpenAI Chat Completions Tools 协议）：
#   模型响应 tool_calls（apply_skill / get_table_schema / 未来注册的工具）
#   -> 逐个组装 CapabilityRequest -> SkillToolGate 单点裁决
#   -> 凭 Grant 经 CapabilityRegistry 取 Skill 正文 / 执行 Tool
#   -> 结果以 role=tool 消息逐条回喂 -> 模型继续调用或输出最终 SQL。
# 任何一步都不存在 Gate 之外的能力获取路径（Registry 强制校验 Grant）。
#
# 防死循环：同一被拒能力重复调用由 Gate 的 R429-LOOP-GUARD 拦截；tool_call 总轮次封顶
#   max_cap_rounds（默认 2），超限后最后一轮不再下发 tools 定义（模型只能直接出 SQL）。
# 降级：Gate 不健康时 bypass 能力协议（模型自行发挥、DQL 正常执行，系统侧校验管道不变）；
#   模型端不支持 tools 或编排异常时，回退到无工具的一次性普通生成。

import json
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .gate import SkillToolGate
from .protocol import CapabilityRequest, ExecutionContext, Principal
from .registry import CapabilityDenied, CapabilityRegistry

logger = logging.getLogger(__name__)

SKILL_DISPATCH_TOOL = "apply_skill"   # 技能申请统一入口函数（枚举由注册表动态生成）


@dataclass
class CapabilityTrace:
    round_index: int
    requested_skills: List[str] = field(default_factory=list)
    requested_tools: List[str] = field(default_factory=list)
    granted: List[str] = field(default_factory=list)
    denied: List[dict] = field(default_factory=list)   # [{kind,name,rule_id,reason}]
    malformed: str = ""


def render_catalog(registry: CapabilityRegistry) -> str:
    """能力清单文本（仅元数据：名称/用途/风险/参数；不含技能正文）。
    注入系统提示，作为模型判断"该申请哪个技能"的场景依据（比 enum 描述更详细）"""
    lines: List[str] = []
    skills = registry.catalog("skill")
    if skills:
        lines.append("Skill 写作指引（risk 均为 low；通过函数 apply_skill(skill_name=名称) 申请）：")
        for m in skills:
            lines.append(f"- {m.name}【{m.title}】：{m.when_to_use}")
    tools = registry.catalog("tool")
    if tools:
        lines.append("")
        lines.append("Tool 工具（直接以同名函数调用）：")
        for m in tools:
            lines.append(f"- {m.name}【{m.title}】（risk={m.risk}）：{m.when_to_use}")
            if m.args_schema:
                lines.append(f"  参数：{m.args_schema}")
    return "\n".join(lines)


class CapabilityOrchestrator:
    def __init__(self, generator, gate: SkillToolGate, registry: CapabilityRegistry,
                 max_cap_rounds: int = 2):
        self.generator = generator
        self.gate = gate
        self.registry = registry
        self.max_cap_rounds = max_cap_rounds
        self.catalog_text = render_catalog(registry)

    # ------------------------------------------------------------ tools 定义
    def build_openai_tools(self) -> List[dict]:
        """从注册表动态生成 OpenAI tools 数组（注册表是工具定义的唯一来源）：
        apply_skill 为技能统一申请入口；其余每个注册 tool 一个 function。"""
        tools: List[dict] = [{
            "type": "function",
            "function": {
                "name": SKILL_DISPATCH_TOOL,
                "description": (
                    "申请获取某个 SQL 写作技能的详细指引。生成 SQL 前，只要目标语句涉及该技能"
                    "覆盖的场景就必须先调用本函数获取指引，再输出 SQL；可在同一条回复中并行调用"
                    "多个技能。严禁在未获取相关技能指引的情况下直接生成涉及该场景的 SQL。"),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "skill_name": {
                            "type": "string",
                            "enum": [m.name for m in self.registry.catalog("skill")],
                            "description": "技能名称（见系统提示中的技能清单）",
                        },
                    },
                    "required": ["skill_name"],
                },
            },
        }]
        for m in self.registry.catalog("tool"):
            tools.append({
                "type": "function",
                "function": {
                    "name": m.name,
                    "description": m.when_to_use,
                    "parameters": m.parameters or {"type": "object", "properties": {}},
                },
            })
        return tools

    # ------------------------------------------------------------------
    def run(self, question: str, tables: Optional[List[str]] = None,
            conversation_context: str = "", reference_context: str = "",
            selected_schema: str = "",
            candidate_tables: Optional[List[str]] = None,
            attempt: int = 0, opt_rounds: int = 0,
            principal: Optional[Principal] = None) -> dict:
        ctx = ExecutionContext(
            phase="generate", question=question, intent="query",
            candidate_tables=candidate_tables, attempt=attempt,
            opt_rounds=opt_rounds, principal=principal or Principal(),
            extra={"known_tables": list(candidate_tables or [])},
        )
        # Gate 不可用（规则文件缺失/损坏）时的策略：bypass 能力协议，让模型自行发挥，
        # 走与普通生成完全相同的后续流程（静态校验/SQL Guard/扇出/EXPLAIN 门禁/执行均不变），
        # DQL 照常执行。依据：当前已注册的 Skill/Tool 全部为只读低风险能力。
        # 【预埋-未来必须恢复 fail-closed】一旦注册写操作或 medium/high 风险能力，
        # 本 bypass 分支必须删除/改为按风险拒绝。
        if not self.gate.healthy:
            logger.warning("Gate 不可用，bypass 能力协议：模型自行发挥，DQL 正常执行（系统侧校验管道不变）")
            self.gate.audit.record({
                "action": "bypass", "rule_id": "R503-BYPASS",
                "kind": "-", "name": "-", "phase": "generate",
                "reason": "Gate 不可用；当前能力均为只读低风险，bypass 让模型自行发挥",
                "context": ctx.snapshot(),
            })
            gen = self.generator.generate(
                question=question, tables=tables,
                conversation_context=conversation_context,
                reference_context=reference_context,
                selected_schema=selected_schema,
                capability_catalog="",
            )
            return {"result": gen, "trace": [], "bypass": "gate_unhealthy"}

        messages = self.generator.build_query_messages(
            question=question, tables=tables,
            conversation_context=conversation_context,
            reference_context=reference_context,
            selected_schema=selected_schema,
            capability_catalog=self.catalog_text,
        )
        tools_spec = self.build_openai_tools()
        trace: List[CapabilityTrace] = []
        merged = None
        try:
            for round_index in range(1, self.max_cap_rounds + 2):  # 末轮为强制出 SQL 轮
                force_final = round_index > self.max_cap_rounds
                # 末轮不再下发 tools：模型只能直接输出 SQL（有界终止）
                resp = self.generator.llm.chat(
                    messages, tools=None if force_final else tools_spec)
                merged = resp if merged is None else self._merge_tokens(merged, resp)

                if not resp.tool_calls or force_final:
                    if resp.tool_calls and force_final:
                        logger.warning("能力调用轮次已耗尽（%d），忽略 tool_calls，直接按文本输出提取 SQL",
                                       self.max_cap_rounds)
                    result = self._to_result(merged, self.generator.extract_sql(resp.content or ""))
                    return {"result": result, "trace": trace, "bypass": ""}

                tr = CapabilityTrace(round_index=round_index)
                # assistant 的 tool_calls 消息必须原样回拼（OpenAI 协议要求）
                messages.append({
                    "role": "assistant",
                    "content": resp.content or None,
                    "tool_calls": resp.tool_calls,
                })
                outputs = self._serve_tool_calls(resp.tool_calls, ctx, tr)
                for tool_call_id, content in outputs:
                    messages.append({"role": "tool", "tool_call_id": tool_call_id, "content": content})
                trace.append(tr)

            result = self._to_result(merged, self.generator.extract_sql(
                merged.content if merged else ""))
            return {"result": result, "trace": trace, "bypass": ""}
        except Exception as e:
            logger.warning("能力编排异常（含模型端不支持 tools 的可能），回退一次性普通生成: %s",
                           str(e).splitlines()[0])
            result = self.generator.generate(
                question=question, tables=tables,
                conversation_context=conversation_context,
                reference_context=reference_context,
                selected_schema=selected_schema,
                capability_catalog="",
            )
            return {"result": result, "trace": trace, "bypass": "orchestrator_error"}

    # ------------------------------------------------------------ 逐个服务 tool_call
    def _serve_tool_calls(self, tool_calls: List[dict], ctx: ExecutionContext,
                          tr: CapabilityTrace) -> List[tuple]:
        """返回 [(tool_call_id, 回喂文本), ...]（每个 id 必须有对应响应）"""
        results: List[tuple] = []
        cache: Dict[str, tuple] = {}   # 同轮重复调用去重裁决：sig -> text
        for tc in tool_calls:
            tcid = tc.get("id", "")
            fn = tc.get("function") or {}
            name = fn.get("name") or ""
            raw_args = fn.get("arguments") or ""
            try:
                args = json.loads(raw_args) if raw_args.strip() else {}
                if not isinstance(args, dict):
                    raise ValueError("参数必须是 JSON 对象")
            except Exception as e:
                tr.malformed = f"{name} 参数不是合法 JSON: {str(e)[:80]}"
                results.append((tcid, f"【工具参数错误】{name} 的参数不是合法 JSON（{e}）。"
                                      "请按函数参数 schema 修正后重新调用，或直接输出最终 SQL。"))
                continue

            # apply_skill 统一入口 -> skill 能力请求；其余名字按 tool 裁决（未注册由 Gate R404 拒绝）
            if name == SKILL_DISPATCH_TOOL:
                skill_name = str(args.get("skill_name") or "").strip()
                if not skill_name:
                    tr.malformed = "apply_skill 缺少 skill_name"
                    results.append((tcid, "【工具参数错误】apply_skill 必须提供 skill_name。"))
                    continue
                req = CapabilityRequest(kind="skill", name=skill_name, args={})
                tr.requested_skills.append(skill_name)
            else:
                req = CapabilityRequest(kind="tool", name=name, args=args)
                tr.requested_tools.append(name)

            sig = f"{req.key()}@{req.args_hash()}"
            if sig in cache:
                results.append((tcid, cache[sig]))
                continue

            d = self.gate.decide(req, ctx)
            if not d.allowed:
                tr.denied.append({"kind": req.kind, "name": req.name,
                                  "rule_id": d.rule_id, "reason": d.reason})
                text = (f"【申请被拒绝：{req.kind} {req.name}｜规则 {d.rule_id}】\n原因：{d.reason}\n"
                        + (f"纠偏指引：{d.correction_hint}\n" if d.correction_hint else "")
                        + "请按指引改用其他方式，禁止重复调用该被拒能力，之后直接输出最终 SQL。")
                cache[sig] = text
                results.append((tcid, text))
                continue

            grant = d.to_grant()
            try:
                if req.kind == "skill":
                    body = self.registry.grant_skill_text(grant, req.name)
                    tr.granted.append(f"skill:{req.name}")
                    text = f"【技能 {req.name} 指引已批准，请严格据此生成】\n{body}"
                else:
                    body = self.registry.run_tool(grant, req.name, req.args, ctx)
                    tr.granted.append(f"tool:{req.name}")
                    text = f"【工具 {req.name} 已批准并执行，返回如下】\n{body}"
            except CapabilityDenied as e:
                tr.denied.append({"kind": req.kind, "name": req.name,
                                  "rule_id": "R401-GRANT-INVALID", "reason": str(e)})
                text = f"【拒绝执行：{req.kind} {req.name}｜R401-GRANT-INVALID】{e}"
            cache[sig] = text
            results.append((tcid, text))
        return results

    # ------------------------------------------------------------------
    @staticmethod
    def _merge_tokens(acc, resp):
        for f in ("prompt_tokens", "completion_tokens", "total_tokens"):
            if hasattr(acc, f) and hasattr(resp, f):
                try:
                    setattr(acc, f, getattr(acc, f) + getattr(resp, f))
                except Exception:
                    pass
        if hasattr(acc, "content"):
            acc.content = resp.content
        if hasattr(resp, "tool_calls"):
            acc.tool_calls = resp.tool_calls
        return acc

    def _to_result(self, resp, sql: str):
        from app.nl2sql.sql_generator import GenerationResult
        if resp is None:
            return GenerationResult()
        return GenerationResult(
            sql=sql,
            raw_output=getattr(resp, "content", "") or "",
            prompt_tokens=getattr(resp, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(resp, "completion_tokens", 0) or 0,
            total_tokens=getattr(resp, "total_tokens", 0) or 0,
            model=getattr(resp, "model", "") or "",
        )
