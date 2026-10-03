# 能力统一注册包装层：所有 Skill / Tool 只能通过 CapabilityRegistry 注册，
# 且只能凭 Gate 的 Grant 取用/执行——代码层面不存在"绕过 Gate 直接拿技能正文/调工具"
# 的公开路径（正文/run 方法收敛在受保护的访问器内，强制校验 Grant）。

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from .protocol import ExecutionContext, Grant


class CapabilityDenied(Exception):
    """无 Grant / Grant 与请求不符时抛出（架构性拒绝，区别于 Gate 规则拒绝）"""


@dataclass
class CapabilityMeta:
    kind: str                   # skill / tool
    name: str
    title: str
    risk: str                   # low / medium / high
    when_to_use: str
    args_schema: str = ""       # 工具参数的自然语言说明（目录展示用）
    parameters: dict = None     # OpenAI function calling 的 JSON Schema（tool 用）


class CapabilityRegistry:
    """技能/工具的唯一注册与授权访问入口（fail-closed：未注册能力不存在即不可用）"""

    def __init__(self):
        self._meta: Dict[str, CapabilityMeta] = {}
        self._skill_text: Dict[str, Callable[[], str]] = {}
        self._tool_run: Dict[str, Callable[[dict, ExecutionContext], str]] = {}

    # ------------------------------------------------------------ 注册（唯一入口）
    def register_skill(self, name: str, title: str, risk: str, when_to_use: str,
                       text_loader: Callable[[], str]):
        """text_loader 为惰性加载器；正文只允许经 grant_skill_text 凭凭证读取"""
        self._meta[f"skill:{name}"] = CapabilityMeta("skill", name, title, risk, when_to_use)
        self._skill_text[name] = text_loader

    def register_tool(self, name: str, title: str, risk: str, when_to_use: str,
                      args_schema: str, runner: Callable[[dict, ExecutionContext], str],
                      parameters: dict = None):
        self._meta[f"tool:{name}"] = CapabilityMeta(
            "tool", name, title, risk, when_to_use, args_schema, parameters)
        self._tool_run[name] = runner

    # ------------------------------------------------------------ 目录（无需授权）
    def catalog(self, kind: Optional[str] = None) -> List[CapabilityMeta]:
        """能力清单（仅名称/用途/风险等元数据，不含技能正文、不触发工具）"""
        return [m for m in self._meta.values() if kind is None or m.kind == kind]

    def get_meta(self, kind: str, name: str) -> Optional[CapabilityMeta]:
        return self._meta.get(f"{kind}:{name}")

    def risk_of(self, kind: str, name: str) -> Optional[str]:
        m = self.get_meta(kind, name)
        return m.risk if m else None

    # ------------------------------------------------------------ 授权访问（凭 Grant）
    def grant_skill_text(self, grant: Grant, name: str) -> str:
        if not grant.valid_for("skill", name, {}):
            raise CapabilityDenied(f"skill {name} 授权凭证无效或已过期")
        loader = self._skill_text.get(name)
        if loader is None:
            raise CapabilityDenied(f"skill {name} 未注册（默认拒绝）")
        return loader()

    def run_tool(self, grant: Grant, name: str, args: dict,
                 ctx: ExecutionContext) -> str:
        if not grant.valid_for("tool", name, args):
            raise CapabilityDenied(f"tool {name} 授权凭证无效/参数与批准内容不符/已过期")
        runner = self._tool_run.get(name)
        if runner is None:
            raise CapabilityDenied(f"tool {name} 未注册（默认拒绝）")
        return runner(args, ctx)
