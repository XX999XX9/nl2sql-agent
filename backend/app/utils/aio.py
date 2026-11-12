# 同步/异步桥接：给评测脚本、CLI、离线预热等同步调用方提供统一入口
# 常驻一个后台事件循环，避免 asyncio.run 反复建/毁 loop 导致异步连接池失效

import asyncio
import threading
from typing import Optional

_LOOP: Optional[asyncio.AbstractEventLoop] = None
_LOCK = threading.Lock()


def get_sync_loop() -> asyncio.AbstractEventLoop:
    global _LOOP
    with _LOCK:
        if _LOOP is None or _LOOP.is_closed():
            loop = asyncio.new_event_loop()
            threading.Thread(target=loop.run_forever, name="nl2sql-sync-loop", daemon=True).start()
            _LOOP = loop
        return _LOOP


def run_sync(coro):
    """在常驻 loop 上执行协程并同步等待结果；已在事件循环内调用则报错提示用 await"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run_coroutine_threadsafe(coro, get_sync_loop()).result()
    raise RuntimeError("同步入口不能在已运行的事件循环中调用，请改用 await")
