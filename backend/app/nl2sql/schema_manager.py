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
        self._foreign_keys: Dict[str, List[dict]] = {}  # 表名 -> 外键列表（见 refresh 中结构）
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

                # 显式外键：REFERENTIAL_CONSTRAINTS 定位外键约束，KEY_COLUMN_USAGE
                # 给出"本表列 → 被引用表.列"映射（复合外键按 ORDINAL_POSITION 顺序）
                self._foreign_keys = {name: [] for (name, _c) in tables}
                cur.execute(
                    "SELECT kcu.TABLE_NAME, kcu.COLUMN_NAME, "
                    "kcu.REFERENCED_TABLE_NAME, kcu.REFERENCED_COLUMN_NAME "
                    "FROM information_schema.REFERENTIAL_CONSTRAINTS rc "
                    "JOIN information_schema.KEY_COLUMN_USAGE kcu "
                    "  ON rc.CONSTRAINT_SCHEMA = kcu.CONSTRAINT_SCHEMA "
                    " AND rc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME "
                    "WHERE rc.CONSTRAINT_SCHEMA = %s "
                    "ORDER BY kcu.TABLE_NAME, kcu.ORDINAL_POSITION",
                    (self._db_config["name"],),
                )
                for tbl, col, ref_tbl, ref_col in cur.fetchall():
                    self._foreign_keys.setdefault(tbl, []).append({
                        "column": col,
                        "ref_table": ref_tbl,
                        "ref_column": ref_col,
                    })
            self._context = self._build_context()
            logger.info(
                "Schema 解析完成：%d 张表，%d 条显式外键",
                len(self._tables), sum(len(v) for v in self._foreign_keys.values()),
            )
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
        """当前库全部表结构的 MD5 指纹（供检索器/关系图谱检测结构变更、自动重建）"""
        import hashlib
        payload = {
            tbl: [{"c": c["name"], "t": c["type"], "k": c["key"], "m": c["comment"]}
                  for c in cols]
            for tbl, cols in sorted(self._tables.items())
        }
        # 外键也纳入指纹：外键增删必须触发关系图谱重建
        payload["__fks__"] = [
            [fk["table"], fk["column"], fk["ref_table"], fk["ref_column"]]
            for fk in sorted(self.all_foreign_keys(), key=lambda x: (x["table"], x["column"]))
        ]
        return hashlib.md5(repr(payload).encode()).hexdigest()

    def get_table_names(self) -> List[str]:
        return list(self._tables.keys())

    def get_table(self, table_name: str) -> List[dict]:
        return self._tables.get(table_name, [])

    def get_foreign_keys(self, table_name: str) -> List[dict]:
        """该表声明的显式外键：[{column, ref_table, ref_column}]（外键所在表为"多端"）"""
        return list(self._foreign_keys.get(table_name, []))

    def all_foreign_keys(self) -> List[dict]:
        """全库显式外键边（含表名）：[{table, column, ref_table, ref_column}]"""
        return [
            {"table": tbl, **fk}
            for tbl, fks in self._foreign_keys.items() for fk in fks
        ]

    def get_primary_keys(self, table_name: str) -> List[str]:
        """该表主键列名列表（单列主键常见；复合主键返回多列）"""
        return [c["name"] for c in self._tables.get(table_name, []) if c["key"] == "PRI"]
