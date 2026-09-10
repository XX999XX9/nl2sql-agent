# 规范化：清理模型输出噪声——冗余寒暄前缀、未闭合标记、代码块外的 SQL 残留等，
# 保证出口文本干净统一

import re
from typing import List

# 常见寒暄/确认性前缀（"好的，""当然，如下："等），出现在解释文本开头时剥离
_PREFIX_RE = re.compile(
    r"^\s*(好的|当然|没问题|明白了|收到)[，,。！!\s]*(如下)?[：:]?\s*"
    r"|(这条|该)?(SQL|查询|语句)?的?(意思|含义)?(是|如下)[：:]\s*",
    re.IGNORECASE,
)
# Markdown 代码围栏残留
_FENCE_RE = re.compile(r"^\s*```[\w]*\s*|\s*```\s*$")
# 解释文本中被反引号包裹的裸 SQL 片段（统一保留，交由前端 Markdown 渲染）
# 连续空行压缩
_MULTI_BLANK_RE = re.compile(r"\n{3,}")


def normalize_text(text: str) -> str:
    """清理解释/回答类文本：去寒暄前缀、去围栏残留、压缩空行"""
    if not text:
        return ""
    cleaned = text.strip()
    # 循环剥离前缀（可能叠加"好的，如下："）
    for _ in range(3):
        new = _PREFIX_RE.sub("", cleaned, count=1)
        if new == cleaned:
            break
        cleaned = new.strip()
    cleaned = _FENCE_RE.sub("", cleaned).strip()
    cleaned = _MULTI_BLANK_RE.sub("\n\n", cleaned)
    return cleaned


def normalize_sql(sql: str) -> str:
    """清理 SQL 文本：去围栏、去首尾空白与多余分号"""
    if not sql:
        return ""
    cleaned = _FENCE_RE.sub("", sql.strip())
    return cleaned.strip().rstrip(";").strip()


def strip_reasoning_noise(text: str, max_lines: int = 20) -> str:
    """截断过长的解释文本（模型偶发复读/发散），max_lines 行封顶"""
    if not text:
        return ""
    lines: List[str] = text.splitlines()
    if len(lines) <= max_lines:
        return text
    return "\n".join(lines[:max_lines]) + "\n…（已截断）"
