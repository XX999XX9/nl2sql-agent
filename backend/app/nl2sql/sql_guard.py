# 安全守卫（升级版）：关键词粗筛 + sqlglot AST 细筛双重过滤
# - 关键词层：DROP/DELETE/ALTER/TRUNCATE 等危险词快速拦截（含注释剥离防绕过）
# - AST 层：解析失败/多语句拼接/非 SELECT 语句类型直接拦截
# - 执行保护：SELECT 自动追加强制 LIMIT
# 与数据库只读账号形成三保险

import os
import re

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from app.utils.config_handler import load_yaml
from app.utils.path_tool import BACKEND_ROOT


class SQLGuardError(Exception):
    """守卫拦截异常，message 为拦截原因"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class SQLGuard:
    """SQL 安全守卫：只放行 SELECT，并保证带 LIMIT"""

    FORBIDDEN_KEYWORDS = (
        "insert", "update", "delete", "drop", "alter", "truncate", "create",
        "replace", "merge", "grant", "revoke", "rename", "call", "exec",
        "lock", "unlock", "shutdown", "kill", "set", "load_file", "outfile",
        "dumpfile", "information_schema.",
        # 时间/资源型阻塞：拖住连接或用结果集放大的方式打垮数据库
        "sleep", "benchmark",
        # 命名锁：读查询也会留下/释放全局锁，影响其他会话
        "get_lock", "release_lock",
        # 报错注入：把查询结果拼进错误信息带出去
        "updatexml", "extractvalue",
    )

    def __init__(self):
        self._db_config = load_yaml(BACKEND_ROOT / "config" / "database.yml")["database"]

    def check(self, sql: str) -> str:
        """校验并改写 SQL：关键词粗筛 → AST 细筛 → 追加强制 LIMIT，返回安全 SQL"""
        if not sql or not sql.strip():
            raise SQLGuardError("SQL 为空")

        # 1. 关键词粗筛：对原始 SQL 先扫一遍（注释/字符串中的危险词也拦截，宁枉勿纵），
        #    再对剥注释后的可执行体扫描（词边界匹配，避免 updated_at 等列名误伤）
        for target in (sql, self._strip_comments(sql)):
            lowered = target.lower()
            for kw in self.FORBIDDEN_KEYWORDS:
                word = kw.rstrip(".")
                if re.search(rf"\b{re.escape(word)}\b", lowered):
                    raise SQLGuardError(f"检测到禁止的关键词: {word.upper()}（仅允许 SELECT 查询）")

        cleaned = self._strip_comments(sql)
        if not cleaned:
            raise SQLGuardError("SQL 为空")

        # 2. AST 细筛：语法可解析 + 单条语句 + SELECT 类型
        try:
            statements = [s for s in sqlglot.parse(cleaned, read="mysql") if s is not None]
        except ParseError as e:
            raise SQLGuardError(f"SQL 无法解析（疑似注入或语法错误）：{str(e).splitlines()[0]}")
        if not statements:
            raise SQLGuardError("SQL 为空")
        if len(statements) > 1:
            raise SQLGuardError("禁止多条语句拼接执行")
        stmt = statements[0]
        if not isinstance(stmt, (exp.Select, exp.Union)):
            raise SQLGuardError(
                f"仅允许 SELECT 查询（AST 检测到 {type(stmt).__name__} 语句）"
            )

        # 3. LIMIT 保护：无 LIMIT 的 SELECT 追加强制 LIMIT（AST 检测，覆盖 UNION 场景）
        #    环境变量 NL2SQL_FORCED_LIMIT 可覆盖配置；0 = 不追加
        #    （评测场景关闭截断，保证 pred 结果集不被裁短而与 gold 误判不一致；
        #    生产配置仍为 database.yml 的 forced_limit，默认 1000）
        if not stmt.args.get("limit"):
            env_limit = os.environ.get("NL2SQL_FORCED_LIMIT")
            limit = int(env_limit) if env_limit is not None else int(self._db_config["forced_limit"])
            if limit > 0:
                stmt = stmt.limit(limit)

        return stmt.sql(dialect="mysql")

    @staticmethod
    def _strip_comments(sql: str) -> str:
        """去掉 -- 行注释与 /* */ 块注释，防止注释绕过关键词检查"""
        sql = re.sub(r"--[^\n]*", " ", sql)
        sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
        return sql.strip()
