# 结果缓存：按 query_id 缓存最近一次查询结果集（LRU + TTL 淘汰），
# 支撑前端分页拉取与 CSV 导出；执行成功后由 pipeline 写入

import threading
import time
import uuid
from collections import OrderedDict
from typing import Optional

from app.utils.logger_handler import setup_logger

logger = setup_logger()

MAX_ENTRIES = 50        # LRU 容量：最多缓存 50 个结果集
TTL_SECONDS = 30 * 60   # 单个结果缓存 30 分钟


class ResultCache:
    """线程安全的 query_id → 结果集缓存"""

    def __init__(self, max_entries: int = MAX_ENTRIES, ttl: int = TTL_SECONDS):
        self._store: "OrderedDict[str, dict]" = OrderedDict()
        self._lock = threading.Lock()
        self._max_entries = max_entries
        self._ttl = ttl

    def put(self, columns: list, rows: list, total: int, truncated: bool, sql: str = "") -> str:
        """缓存结果集，返回分配的 query_id"""
        query_id = uuid.uuid4().hex[:16]
        with self._lock:
            self._evict_locked()
            self._store[query_id] = {
                "columns": columns,
                "rows": rows,
                "total": total,
                "truncated": truncated,
                "sql": sql,
                "created_at": time.time(),
            }
            self._store.move_to_end(query_id)
        logger.info("结果入缓存 %s：%d 行", query_id, len(rows))
        return query_id

    def get(self, query_id: str) -> Optional[dict]:
        """取完整缓存条目；不存在或过期返回 None"""
        with self._lock:
            entry = self._store.get(query_id)
            if not entry:
                return None
            if time.time() - entry["created_at"] > self._ttl:
                del self._store[query_id]
                return None
            self._store.move_to_end(query_id)
            return entry

    def get_page(self, query_id: str, page: int = 1, page_size: int = 50) -> Optional[dict]:
        """分页读取：返回 {columns, rows, page, page_size, total_pages, total, truncated}"""
        entry = self.get(query_id)
        if not entry:
            return None
        total = len(entry["rows"])
        total_pages = max(1, (total + page_size - 1) // page_size)
        page = max(1, min(page, total_pages))
        start = (page - 1) * page_size
        return {
            "query_id": query_id,
            "columns": entry["columns"],
            "rows": entry["rows"][start : start + page_size],
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
            "total": entry["total"],
            "truncated": entry["truncated"],
            "sql": entry["sql"],
        }

    def delete(self, query_id: str) -> bool:
        with self._lock:
            return self._store.pop(query_id, None) is not None

    def _evict_locked(self):
        """淘汰：先清过期，再按 LRU 缩容到容量内"""
        now = time.time()
        expired = [k for k, v in self._store.items() if now - v["created_at"] > self._ttl]
        for k in expired:
            del self._store[k]
        while len(self._store) >= self._max_entries:
            self._store.popitem(last=False)   # 弹出最久未使用
