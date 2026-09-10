# Schema 自动解析：读取表名/字段/类型/主外键/注释，结构化为内部元数据，
# 拼装 Prompt 上下文文本；带缓存与手动刷新

from typing import Dict, List

import pymysql

from app.utils.config_handler import load_yaml
from app.utils.logger_handler import setup_logger
from app.utils.path_tool import BACKEND_ROOT

logger = setup_logger()


class SchemaManager:
    """解析目标库全部表结构并缓存，产出供 Prompt 使用的 Schema 上下文文本"""

    def __init__(self):
        self._db_config = load_yaml(BACKEND_ROOT / "config" / "database.yml")["database"]
        self._tables: Dict[str, List[dict]] = {}   # 表名 -> 字段列表
        self._table_comments: Dict[str, str] = {}
        self._context: str = ""
        self.refresh()

    # ------------------------------------------------------------------
    def refresh(self):
        """重新读取 information_schema，刷新缓存（后续阶段接入定时刷新）"""
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT TABLE_NAME, TABLE_COMMENT FROM information_schema.TABLES "
                    "WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE' ORDER BY TABLE_NAME",
                    (self._db_config["name"],),
                )
                tables = cur.fetchall()
                self._tables = {name: [] for (name, _c) in tables}
                self._table_comments = {name: (comment or "") for (name, comment) in tables}

                cur.execute(
                    "SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_KEY, "
                    "COLUMN_COMMENT FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA = %s ORDER BY TABLE_NAME, ORDINAL_POSITION",
                    (self._db_config["name"],),
                )
                for tbl, col, ctype, nullable, key, comment in cur.fetchall():
                    self._tables.setdefault(tbl, []).append({
                        "name": col,
                        "type": ctype,
                        "nullable": nullable,
                        "key": key,          # PRI / UNI / MUL / ''
                        "comment": comment or "",
                    })
            self._context = self._build_context()
            logger.info("Schema 解析完成：%d 张表", len(self._tables))
        finally:
            conn.close()

    def _connect(self):
        return pymysql.connect(
            host=self._db_config["host"],
            port=int(self._db_config["port"]),
            user=self._db_config["user"],
            password=self._db_config["password"],
            database=self._db_config["name"],
            charset=self._db_config.get("charset", "utf8mb4"),
            connect_timeout=int(self._db_config.get("connect_timeout", 10)),
        )

    # ------------------------------------------------------------------
    def _build_context(self) -> str:
        """把表结构拼装为紧凑的文本上下文，注入 Prompt"""
        return self._build_context_for_tables(self._tables.keys())

    def _build_context_for_tables(self, tables) -> str:
        """限定表集合拼装上下文（Schema 检索命中表的子集注入）"""
        lines = [f"数据库 {self._db_config['name']} 包含以下表："]
        for tbl in tables:
            columns = self._tables.get(tbl)
            if columns is None:
                continue
            comment = f"（{self._table_comments[tbl]}）" if self._table_comments.get(tbl) else ""
            lines.append(f"\n表 {tbl}{comment}:")
            for col in columns:
                key_tag = ""
                if col["key"] == "PRI":
                    key_tag = " 主键"
                elif col["key"]:
                    key_tag = " 唯一键" if col["key"] == "UNI" else " 索引"
                nullable = "" if col["nullable"] == "NO" else " 可空"
                col_comment = f"，{col['comment']}" if col["comment"] else ""
                lines.append(f"- {col['name']} {col['type']}{key_tag}{nullable}{col_comment}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    @property
    def schema_context(self) -> str:
        """Prompt 用的 Schema 上下文文本（带缓存）"""
        if not self._context:
            self.refresh()
        return self._context

    def build_context_for_tables(self, tables: List[str]) -> str:
        """指定表集合的上下文（供 Schema 向量检索 Top-K 注入）"""
        return self._build_context_for_tables(tables)

    def schema_fingerprint(self) -> str:
        """当前库全部表结构的 MD5 指纹（供检索器检测表结构变更、自动重建索引）"""
        import hashlib
        payload = {
            tbl: [{"c": c["name"], "t": c["type"], "k": c["key"], "m": c["comment"]}
                  for c in cols]
            for tbl, cols in sorted(self._tables.items())
        }
        return hashlib.md5(repr(payload).encode()).hexdigest()

    def get_table_names(self) -> List[str]:
        return list(self._tables.keys())

    def get_table(self, table_name: str) -> List[dict]:
        return self._tables.get(table_name, [])
