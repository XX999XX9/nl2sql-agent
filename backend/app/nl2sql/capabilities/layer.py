# 能力层装配工厂：注册资产 -> 加载声明式规则 -> 审计器 -> Gate -> 编排器。
# 规则文件缺失/解析失败时 engine=None，Gate 以 healthy=False 启动（fail-closed：
# 所有 Skill/Tool 请求被拒，Agent 自动降级为仅凭通用规则的只读生成）。

import logging
from dataclasses import dataclass
from pathlib import Path

from app.utils.path_tool import BACKEND_ROOT

from .audit import AuditLogger, JsonlAuditSink
from .gate import SkillToolGate
from .orchestrator import CapabilityOrchestrator
from .registry import CapabilityRegistry
from .rules import RuleConfigError, RuleEngine
from .skills import register_skills
from .tools import register_builtin_tools

logger = logging.getLogger(__name__)


@dataclass
class CapabilityLayer:
    registry: CapabilityRegistry
    gate: SkillToolGate
    orchestrator: CapabilityOrchestrator
    healthy: bool


def build_capability_layer(generator, schema_manager, nl2sql_cfg: dict) -> CapabilityLayer:
    cap_cfg = nl2sql_cfg.get("capabilities", {})
    registry = CapabilityRegistry()
    skill_names = register_skills(registry, BACKEND_ROOT / "prompts" / "skills")
    tool_names = register_builtin_tools(registry, schema_manager)

    audit_path = Path(cap_cfg.get("audit_path", BACKEND_ROOT / "logs" / "gate_audit.jsonl"))
    audit = AuditLogger(JsonlAuditSink(audit_path))

    # 【预埋】未来可从配置/灰度开关指定规则版本目录；当前固定 config/gate_rules.yml
    rules_path = Path(cap_cfg.get("rules_path", BACKEND_ROOT / "config" / "gate_rules.yml"))
    engine = None
    try:
        engine = RuleEngine.from_yaml(rules_path)
        logger.info("Gate 规则加载成功：%s（%d 条）", rules_path, len(engine.rules))
    except (RuleConfigError, FileNotFoundError, OSError) as e:
        # 编排层策略：Gate 不可用时 bypass 能力协议（模型自行发挥、DQL 正常执行，
        # 系统侧校验管道不变）；决策层 R503 fail-closed 作为预埋防线保留。
        logger.error("Gate 规则加载失败，能力层将被 bypass（模型自行发挥，DQL 正常）: %s", e)

    # 人工审批开关（预埋，当前配置恒为 false；未配置时按 false 处理）
    approval_enabled = bool((engine.meta if engine else {}).get("approval_enabled", False))
    gate = SkillToolGate(registry, engine, audit, healthy=engine is not None,
                         approval_enabled=approval_enabled)
    orchestrator = CapabilityOrchestrator(
        generator, gate, registry,
        max_cap_rounds=int(cap_cfg.get("max_cap_rounds", 2)),
    )
    logger.info("能力层就绪：skills=%s tools=%s healthy=%s",
                skill_names, tool_names, gate.healthy)
    return CapabilityLayer(registry, gate, orchestrator, gate.healthy)
