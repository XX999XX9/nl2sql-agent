# Skill 加载与注册：从 prompts/skills/*.md 读取带 YAML 头的技能文件，
# 统一注册进 CapabilityRegistry。技能正文不对外开放，只凭 Gate Grant 读取。

import logging
import re
from pathlib import Path
from typing import Dict, List, Tuple

import yaml

from .registry import CapabilityRegistry

log = logging.getLogger(__name__)

_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.S)
_REQUIRED = ("name", "title", "risk", "when_to_use")


def parse_skill_file(path: Path) -> Tuple[dict, str]:
    raw = path.read_text(encoding="utf-8")
    m = _FM_RE.match(raw)
    if not m:
        raise ValueError(f"{path.name} 缺少 YAML 头（--- name/title/risk/when_to_use ---）")
    meta = yaml.safe_load(m.group(1)) or {}
    for k in _REQUIRED:
        if k not in meta:
            raise ValueError(f"{path.name} 技能头缺少字段: {k}")
    if str(meta.get("risk", "")).lower() not in ("low", "medium", "high"):
        raise ValueError(f"{path.name} risk 非法")
    return meta, m.group(2).strip()


def register_skills(registry: CapabilityRegistry, skills_dir: str | Path) -> List[str]:
    """注册目录内全部技能，返回已注册技能名列表；单个文件损坏只告警不阻断（fail-open 仅针对资产加载，
    准入决策仍由 Gate fail-closed 把关）"""
    skills_dir = Path(skills_dir)
    names: List[str] = []
    if not skills_dir.is_dir():
        log.warning("Skill 目录不存在: %s", skills_dir)
        return names
    for p in sorted(skills_dir.glob("*.md")):
        try:
            meta, body = parse_skill_file(p)
            name = str(meta["name"])
            registry.register_skill(
                name=name,
                title=str(meta["title"]),
                risk=str(meta["risk"]).lower(),
                when_to_use=str(meta["when_to_use"]),
                text_loader=(lambda body=body: body),
            )
            names.append(name)
        except Exception as e:
            log.warning("Skill 文件 %s 加载失败（跳过）: %s", p.name, e)
    log.info("已注册 Skill %d 个: %s", len(names), ", ".join(names))
    return names


def load_skill_knowledge(skills_dir: str | Path) -> str:
    """把目录内全部技能文件的正文渲染为可内联进系统提示词的知识文本。

    与注册表/Grant 通道相互独立：这是服务端启动时把技能知识直接写回提示词，
    模型无需（也无法）通过函数调用发现或申请技能；skill 资产文件与注册架构保持
    不动，本函数只是它们的另一个只读消费方。单个文件损坏跳过不阻断。"""
    skills_dir = Path(skills_dir)
    blocks: List[str] = []
    if not skills_dir.is_dir():
        return ""
    for p in sorted(skills_dir.glob("*.md")):
        try:
            meta, body = parse_skill_file(p)
            blocks.append(
                f"■ {meta['title']}\n"
                f"适用场景：{meta['when_to_use']}\n"
                f"{body}"
            )
        except Exception as e:
            log.warning("Skill 知识渲染跳过 %s: %s", p.name, e)
    return "\n\n".join(blocks)
