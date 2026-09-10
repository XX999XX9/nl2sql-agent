# 输出模板：SQL 展示块 / 解释卡片 / 统计信息条等模板定义
# 前端渲染组件与后端格式化出口共用同一套结构与命名

# SQL 块元信息（供前端 sql_block.js 渲染）
SQL_BLOCK_TEMPLATE = {
    "label": "SQL",
    "copy_hint": "复制",
    "highlight": "sql",          # highlight.js 语言
    "format": True,              # 是否走 sql-formatter 美化
}

# 解释卡片
EXPLANATION_TEMPLATE = {
    "label": "解读",
    "markdown": True,            # 解释文本支持 Markdown 渲染
    "max_lines": 20,
}

# 统计条
USAGE_TEMPLATE = {
    "summary": "{elapsed} · tokens {total}（输入 {prompt} + 输出 {completion}）",
    "steps": True,
}

# 消息类型 → 模板映射
TEMPLATES = {
    "sql": SQL_BLOCK_TEMPLATE,
    "explanation": EXPLANATION_TEMPLATE,
    "usage": USAGE_TEMPLATE,
}


def render_usage_summary(elapsed_ms: int, prompt: int, completion: int, total: int) -> str:
    """渲染统计摘要文案（后端统一出口，前端兜底同款逻辑）"""
    elapsed = f"{elapsed_ms / 1000:.1f}s" if elapsed_ms >= 1000 else f"{elapsed_ms}ms"
    return USAGE_TEMPLATE["summary"].format(
        elapsed=elapsed, total=total, prompt=prompt, completion=completion,
    )
