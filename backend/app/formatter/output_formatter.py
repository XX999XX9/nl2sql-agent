# 格式化器：按消息类型（解释/回答/SQL/统计）套用对应模板做统一出口格式化；
# 流式场景做增量格式化（仅对已完成段落应用格式，避免半截 Markdown 渲染错乱）

from typing import Optional

from app.formatter.normalizer import normalize_sql, normalize_text, strip_reasoning_noise
from app.formatter.templates import EXPLANATION_TEMPLATE, render_usage_summary


class OutputFormatter:
    """出口统一格式化器：pipeline 各出口文本先过 normalizer，再按模板约束"""

    def format_explanation(self, text: str) -> str:
        """解释卡片文本：去噪 + 行数封顶"""
        if not text:
            return ""
        cleaned = normalize_text(text)
        if EXPLANATION_TEMPLATE["max_lines"]:
            cleaned = strip_reasoning_noise(cleaned, EXPLANATION_TEMPLATE["max_lines"])
        return cleaned

    def format_answer(self, text: str) -> str:
        """meta 分支直接回答：仅去噪不截断（回答主体信息量优先）"""
        return normalize_text(text)

    def format_sql(self, sql: str) -> str:
        """SQL 展示文本：去围栏/分号残留（语法高亮由前端完成）"""
        return normalize_sql(sql)

    def format_usage(self, usage: dict) -> Optional[str]:
        """统计摘要文案；usage 为空返回 None"""
        if not usage or not usage.get("total_tokens"):
            return None
        return render_usage_summary(
            usage.get("elapsed_ms", 0),
            usage.get("prompt_tokens", 0),
            usage.get("completion_tokens", 0),
            usage.get("total_tokens", 0),
        )

    # ------------------------------------------------------------------
    # 增量格式化（流式场景预留：文本按段落缓冲，仅对完成段落应用格式）
    def format_incremental(self, buffer: str, chunk: str) -> tuple:
        """返回 (可安全渲染文本, 保留缓冲)：
        以换行为界，只有完整行才交给渲染，半截内容留待下一轮"""
        combined = buffer + chunk
        if "\n" not in combined:
            return "", combined
        idx = combined.rindex("\n")
        ready, rest = combined[: idx + 1], combined[idx + 1 :]
        return normalize_text(ready), rest
