# SQL 执行：SQLAlchemy 连接池 + pymysql 驱动、执行超时控制（max_execution_time）、
# 错误捕获与分类、结果行数截断与规范化（列名+行数据+总行数+是否截断）；
# 执行成功后将结果存入结果缓存并分配 query_id（缓存后续阶段接入）

import re
from datetime import date, datetime
from decimal import Decimal
from typing import List, Optional

from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError, OperationalError

from app.utils.config_handler import load_yaml
from app.utils.logger_handler import setup_logger
from app.utils.path_tool import BACKEND_ROOT

logger = setup_logger()

_TRAILING_LIMIT_RE = re.compile(r"\blimit\s+\d+\s*$", re.IGNORECASE)


class SQLExecutionError(Exception):
    """SQL 执行失败：kind 标注错误类别（syntax/permission/timeout/other）"""

    def __init__(self, message: str, kind: str = "other"):
        super().__init__(message)
        self.kind = kind


class SQLExecutor:
    """带连接池的只读查询执行器"""

    def __init__(self):
        cfg = load_yaml(BACKEND_ROOT / "config" / "database.yml")["database"]
        self._row_limit = int(cfg.get("result_row_limit", 1000))
        self._execution_timeout = int(cfg.get("execution_timeout", 30))
        self._engine = create_engine(
            f"mysql+pymysql://{cfg['user']}:{cfg['password']}@{cfg['host']}:{cfg['port']}/{cfg['name']}"
            "?charset=utf8mb4",
            pool_size=int(cfg.get("pool_size", 5)),
            max_overflow=int(cfg.get("max_overflow", 10)),
            pool_recycle=int(cfg.get("pool_recycle", 1800)),
            pool_pre_ping=True,
            connect_args={"connect_timeout": int(cfg.get("connect_timeout", 10))},
        )

    # ------------------------------------------------------------------
    def execute(self, sql: str) -> dict:
        """执行 SELECT 并返回 {columns, rows, total, truncated}"""
        with self._engine.connect() as conn:
            # MySQL 8：SELECT 语句的执行超时（毫秒）
            conn.exec_driver_sql(f"SET SESSION max_execution_time = {self._execution_timeout * 1000}")
            try:
                result = conn.execute(text(sql))
                columns = list(result.keys())
                rows = [self._normalize_row(row) for row in result.fetchall()]
            except DBAPIError as e:
                raise self._classify_error(e) from e

        truncated = len(rows) > self._row_limit
        if truncated:
            rows = rows[: self._row_limit]
        total = self._count_total(sql, truncated)
        logger.info("SQL 执行成功：%d 行（截断=%s）", len(rows), truncated)
        return {"columns": columns, "rows": rows, "total": total, "truncated": truncated}

    # ------------------------------------------------------------------
    def _count_total(self, sql: str, truncated: bool) -> int:
        """截断时用 COUNT 包装查询真实总行数（去掉追加强制的 LIMIT）"""
        if not truncated:
            return -1  # 未截断时 total 就是行数，由 pipeline 填充
        unbounded = _TRAILING_LIMIT_RE.sub("", sql).strip().rstrip(";")
        try:
            with self._engine.connect() as conn:
                value = conn.execute(text(f"SELECT COUNT(*) FROM ({unbounded}) AS _t")).scalar()
                return int(value)
        except Exception as e:  # COUNT 失败不影响主结果
            logger.warning("总行数统计失败: %s", e)
            return -1

    @staticmethod
    def _normalize_row(row) -> List:
        """行数据规范化：datetime/date → ISO 字符串（date.isoformat() 不支持 sep 参数，须分开处理），
        Decimal → float，保证可 JSON 序列化"""
        values = list(row) if not isinstance(row, dict) else list(row.values())
        result = []
        for v in values:
            if isinstance(v, datetime):
                result.append(v.isoformat(sep=" "))
            elif isinstance(v, date):
                result.append(v.isoformat())
            elif isinstance(v, Decimal):
                result.append(float(v))
            else:
                result.append(v)
        return result

    @staticmethod
    def _classify_error(e: DBAPIError) -> SQLExecutionError:
        """错误分类：语法错/权限错/超时，供后续纠错链路消费"""
        msg = str(e.orig) if getattr(e, "orig", None) else str(e)
        code = getattr(e.orig, "args", [None])[0]
        if code in (1064, 1149, 1146, 1054, 1051):  # 语法错误/表不存在/字段不存在
            return SQLExecutionError(msg, kind="syntax")
        if code in (1142, 1143, 1044, 1045):  # 权限不足
            return SQLExecutionError(msg, kind="permission")
        if code == 3024 or "max_execution_time" in msg.lower() or "timeout" in msg.lower():
            return SQLExecutionError(msg, kind="timeout")
        return SQLExecutionError(msg, kind="other")

    def dispose(self):
        self._engine.dispose()
