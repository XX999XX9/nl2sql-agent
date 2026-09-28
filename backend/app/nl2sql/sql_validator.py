# SQL 静态校验：基于 sqlglot AST 解析——验证表名/字段名真实存在（防幻觉表/字段）、
# 检测语法错误；校验失败产出结构化错误信息（kind: syntax/table/column），
# 供纠错链路消费，省一次数据库往返

from typing import Set

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, TokenError

from app.nl2sql.schema_manager import SchemaManager
from app.utils.logger_handler import setup_logger

logger = setup_logger()


class SQLValidationError(Exception):
    """静态校验失败：kind 标注类别（syntax/table/column）"""

    def __init__(self, message: str, kind: str = "syntax"):
        super().__init__(message)
        self.kind = kind


class SQLValidator:
    """AST 级 SQL 静态校验器"""

    def __init__(self, schema_manager: SchemaManager, check_cartesian: bool = True):
        self.schema_manager = schema_manager
        self.check_cartesian = check_cartesian   # 无 ON/USING 的 JOIN（笛卡尔积）拦截开关

    def validate(self, sql: str):
        """校验失败抛 SQLValidationError；通过则返回解析后的语句节点"""
        try:
            statements = sqlglot.parse(sql, read="mysql")
        except (ParseError, TokenError) as e:   # TokenError：非法 token（如嵌套注释/特殊字符），同样属语法错
            raise SQLValidationError(f"SQL 语法错误：{e}", kind="syntax")
        statements = [s for s in statements if s is not None]
        if not statements:
            raise SQLValidationError("SQL 为空或无法解析", kind="syntax")
        if len(statements) > 1:
            raise SQLValidationError("不允许多条语句拼接", kind="syntax")

        stmt = statements[0]
        # 顶层必须是 SELECT（WITH ... SELECT 解析为带 with 参数的 Select）
        if not isinstance(stmt, (exp.Select, exp.Union)):
            raise SQLValidationError(
                f"仅支持 SELECT 查询，当前语句类型为 {type(stmt).__name__}", kind="syntax"
            )

        # 0. 无关联条件的 JOIN（笛卡尔积）：FROM a, b / JOIN b 无 ON/USING 均拦截；
        #    显式 CROSS JOIN 与 NATURAL JOIN 放行（意图明确，NL2SQL 中极少见）
        if self.check_cartesian:
            for join in stmt.find_all(exp.Join):
                if join.kind == "CROSS" or join.args.get("natural"):
                    continue
                if join.args.get("on") is None and join.args.get("using") is None:
                    raise SQLValidationError(
                        "存在没有 ON/USING 关联条件的 JOIN（会产生笛卡尔积，结果行数被成倍放大）。"
                        "请为 JOIN 补充正确的等值关联条件（参考【表关联关系】中的关联键）；"
                        "如确需笛卡尔积请使用显式 CROSS JOIN 语法",
                        kind="join",
                    )

        valid_tables: Set[str] = set(self.schema_manager.get_table_names())
        # 查询内自定义的 CTE 名视为合法"表"
        cte_names = {cte.alias for cte in stmt.find_all(exp.CTE) if cte.alias}
        valid_tables |= cte_names

        # 1. 表名校验（含子查询/JOIN 中的表；CTE 定义自身引用的真实表也会被覆盖到）
        unknown = {t.name for t in stmt.find_all(exp.Table)} - valid_tables
        if unknown:
            raise SQLValidationError(
                f"表中不存在：{', '.join(sorted(unknown))}。可用的表：{', '.join(sorted(valid_tables))}",
                kind="table",
            )

        # 2. 别名解析：
        #    table_alias → 真实表名（可校验字段）；CTE/派生表别名 → 无法静态验证，跳过
        table_alias_map: dict = {}          # 表别名 -> 真实表名
        unresolvable_aliases: Set[str] = set()
        for t in stmt.find_all(exp.Table):
            if t.alias:
                table_alias_map[t.alias] = t.name
        for cte in stmt.find_all(exp.CTE):
            if cte.alias:
                unresolvable_aliases.add(cte.alias)
        for sub in stmt.find_all(exp.Subquery):
            if sub.alias:
                unresolvable_aliases.add(sub.alias)
        # SELECT 别名（派生列，可被外层/同层引用）
        derived_columns: Set[str] = set()
        for alias_node in stmt.find_all(exp.Alias):
            if alias_node.alias:
                derived_columns.add(alias_node.alias)

        # 3. 字段名校验
        for col in stmt.find_all(exp.Column):
            name = col.name
            if name == "*":
                continue
            qualifier = col.table
            if qualifier:
                if qualifier in unresolvable_aliases or qualifier in derived_columns:
                    continue  # CTE/派生表引用，无法进一步静态验证
                # 别名限定 → 解析回真实表校验
                real_table = table_alias_map.get(qualifier, qualifier if qualifier in valid_tables else None)
                if real_table and real_table in valid_tables:
                    table_columns = {c["name"] for c in self.schema_manager.get_table(real_table)}
                    if name not in table_columns:
                        raise SQLValidationError(
                            f"字段不存在：表 {real_table} 中没有字段 {name}，"
                            f"可用字段：{', '.join(sorted(table_columns))}",
                            kind="column",
                        )
                continue
            # 非限定字段：允许出现在任何一张被引用表的字段并集或派生列中
            referenced = {t.name for t in stmt.find_all(exp.Table)}
            allowed = set(derived_columns)
            for tbl in referenced:
                allowed |= {c["name"] for c in self.schema_manager.get_table(tbl)}
            if name not in allowed:
                raise SQLValidationError(
                    f"字段不存在：{name} 不在任何被引用的表中", kind="column"
                )

        return stmt
