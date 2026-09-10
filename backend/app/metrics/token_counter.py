# token 计数：优先取 LLM API 返回的 usage（最准确，TurnTracker.add_tokens 已用）；
# 本模块提供缺失 usage 时的本地估算兜底（tiktoken 可用则精确编码，否则按字符数估算）

try:
    import tiktoken as _tiktoken

    _ENCODER = _tiktoken.get_encoding("cl100k_base")

    def estimate_tokens(text: str) -> int:
        """本地估算文本 token 数（tiktoken 精确编码）"""
        if not text:
            return 0
        return len(_ENCODER.encode(text))

except ImportError:  # tiktoken 未安装时的粗估：中文≈1字/token，英文≈4字符/token

    def estimate_tokens(text: str) -> int:
        if not text:
            return 0
        cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
        return cjk + (len(text) - cjk) // 4
