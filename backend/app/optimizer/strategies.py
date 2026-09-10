# 优化策略定义：清晰化/加约束/结构化/少样本增强/精简等策略模板与参数
# 每个策略提供注入元提示词的指导文本（guidance），驱动 LLM 按策略改写提示词

from dataclasses import dataclass
from typing import Dict


@dataclass(frozen=True)
class Strategy:
    """提示词优化策略"""

    key: str            # 唯一标识（API 参数）
    name: str           # 展示名
    description: str    # 一句话说明
    guidance: str       # 注入元提示词的具体改写指导


STRATEGIES: Dict[str, Strategy] = {
    "clarify": Strategy(
        key="clarify", name="清晰化",
        description="消除歧义、明确指令与角色边界",
        guidance=(
            "1. 找出模糊表述（如'适当'、'尽量'、'相关的'）并改为可执行的明确指令；"
            "2. 明确任务目标、角色定义与输入输出边界；"
            "3. 把长句拆为短句，一个指令只表达一个动作；"
            "4. 补充遗漏的关键前提条件。保持原意，不做无关扩写。"
        ),
    ),
    "constrain": Strategy(
        key="constrain", name="加约束",
        description="补充输出格式与行为约束",
        guidance=(
            "1. 补充明确的输出格式约定（结构/标记语言/字段顺序）；"
            "2. 增加行为边界：禁止做什么（如禁止编造、禁止输出多余寒暄）；"
            "3. 增加异常处理指引：输入不合法或无法完成任务时如何回应；"
            "4. 约束只加必要的，避免过度限制。"
        ),
    ),
    "structure": Strategy(
        key="structure", name="结构化",
        description="分节/要点化重组，层次清晰",
        guidance=(
            "1. 将提示词重组为清晰的分节结构（如：角色/任务/规则/输出格式）；"
            "2. 用编号或小节标题组织同类指令；"
            "3. 相似规则合并去重，冲突规则消除；"
            "4. 保留全部原有要求，只调整组织方式，不丢失语义。"
        ),
    ),
    "fewshot": Strategy(
        key="fewshot", name="少样本增强",
        description="补充典型输入输出示例",
        guidance=(
            "1. 根据任务构造 1~3 个典型示例，覆盖正常场景与边界场景；"
            "2. 示例格式统一为'输入 → 期望输出'；"
            "3. 示例必须与任务规则严格一致，不得自相矛盾；"
            "4. 若原提示词已有示例，优化示例的代表性与一致性。"
        ),
    ),
    "simplify": Strategy(
        key="simplify", name="精简",
        description="去冗余压缩，降低 token 消耗",
        guidance=(
            "1. 删除重复表达的规则与无效寒暄；"
            "2. 合并语义相同的条目；"
            "3. 用更短的句式表达同样的要求；"
            "4. 不允许丢失任何有效约束——精简是压缩表达，不是删减功能。"
        ),
    ),
}


def get_strategy(key: str) -> Strategy:
    """按 key 取策略；不存在抛 KeyError（路由层转 400）"""
    return STRATEGIES[key]
