# token 计数：优先取 LLM API 返回的 usage（最准确，TurnTracker.add_tokens 已用）；
# 本模块提供缺失 usage 时的本地估算兜底。
#
# 注意：tiktoken 首次 get_encoding 会联网下载词表，且内部 requests 无超时，
# 在受限网络下会无限阻塞。因此这里只在“本地已有词表缓存”时才启用 tiktoken，
# 否则直接走字符估算，保证导入 / 启动不依赖外网。

import os
import tempfile

_encoder = None
_encoder_loaded = False


def _char_estimate(text: str) -> int:
    """粗估：中文≈1字/token，英文≈4字符/token"""
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return cjk + (len(text) - cjk) // 4


def _get_encoder():
    """惰性加载 tiktoken 编码器；仅本地已有词表缓存时启用，避免联网阻塞"""
    global _encoder, _encoder_loaded
    if _encoder_loaded:
        return _encoder
    _encoder_loaded = True
    try:
        cache_dir = (os.environ.get("TIKTOKEN_CACHE_DIR")
                     or os.environ.get("DATA_GYM_CACHE_DIR"))
        default_cache = os.path.join(tempfile.gettempdir(), "data-gym-cache")
        has_cache = bool(cache_dir) or (
            os.path.isdir(default_cache) and bool(os.listdir(default_cache))
        )
        if not has_cache:
            return None
        import tiktoken
        _encoder = tiktoken.get_encoding("cl100k_base")
    except Exception:
        _encoder = None
    return _encoder


def estimate_tokens(text: str) -> int:
    """本地估算文本 token 数：有本地词表时用 tiktoken，否则字符估算"""
    if not text:
        return 0
    encoder = _get_encoder()
    if encoder is not None:
        try:
            return len(encoder.encode(text))
        except Exception:
            pass
    return _char_estimate(text)
