# JOIN 扇出（fan-out，也称行复制放大）检测：
# 当 JOIN 对端与驱动表是一对多关系时，驱动表一行会被对端多行复制；
# 此时对驱动表列做 SUM/AVG 或 COUNT(*) 会成倍虚高（如订单 JOIN 订单明细后
# SUM(订单金额) 按明细行数翻倍）。本模块提供两层检测：
#   static  —— 纯 AST：关系图谱给出基数方向 + 驱动表方向 + 聚合形态，零数据库开销，
#               产出"风险提示"（可能误报，交 LLM 裁决，不直接终止）
#   dynamic —— 在 static 命中后执行 COUNT(*) vs COUNT(DISTINCT 驱动表主键)
#               计数对比，确认后才提示纠错；计数查询不取明细行，开销很小
# 关键口径（避免误报"数多端行"的正常查询，如 COUNT(*) FROM flights JOIN airports）：
#   多端表驱动时每行在外键完整性下至多匹配一个父行，行不被复制，COUNT(*) 安全；
#   仅当驱动表本身是一端、或链上还存在另一个多端表（多A→一P←多B）时计数才放大。
#   子查询/CTE 内部有独立 JOIN 作用域，其中的聚合不参与外层扇出分析。
# 开关：features.fanout_check（总开关）+ nl2sql.yml fanout.mode = off/static/dynamic

from typing import List, Optional, Tuple

import sqlglot
from sqlglot import exp

from app.nl2sql.relation_graph import RelationGraph
from app.nl2sql.sql_executor import SQLExecutor
from app.utils.logger_handler import setup_logger

logger = setup_logger()


class FanoutChecker:
    """JOIN 扇出检测：static（AST 风险）+ dynamic（计数确认）"""

    def __init__(self, relation_graph: Optional[RelationGraph], executor: SQLExecutor,
                 mode: str = "static"):
        self.graph = relation_graph
        self.executor = executor
        self.mode = mode if mode in ("off", "static", "dynamic") else "static"

    # ------------------------------------------------------------------ 入口
    def check(self, sql: str) -> Optional[str]:
        """返回扇出问题描述（用于回喂纠错）；无问题/关闭/无法分析返回 None"""
        if self.mode == "off" or self.graph is None or not self.graph.ready:
            return None
        try:
            stmt = sqlglot.parse_one(sql, read="mysql")
        except Exception:
            return None
        if isinstance(stmt, exp.Union):
            return None    # UNION 结构不做扇出分析

        fanout_pairs = self._fanout_pairs(stmt)
        if not fanout_pairs:
            return None

        risks = self._risky_aggregates(stmt, fanout_pairs)
        if not risks:
            return None

        pair_text = "、".join(f"{p}（一端）←JOIN {c}（多端）" for p, c in fanout_pairs[:3])
        agg_text = "、".join(dict.fromkeys(r[0] for r in risks))
        static_msg = (
            f"检测到 JOIN 扇出风险：{pair_text} 是一对多关联，JOIN 会复制一端表的行；"
            f"而 SQL 对一端表列使用了 {agg_text}，结果会被成倍放大。"
            "请先用子查询/CTE 对多端去重或聚合（GROUP BY 关联键）后再与一端表 JOIN。"
        )

        if self.mode == "dynamic":
            confirmed = self._dynamic_confirm(stmt)
            if confirmed is None:
                logger.info("扇出动态计数无法执行，回退为静态风险提示")
                return static_msg
            if not confirmed:
                logger.info("扇出动态计数未发现行复制，放行")
                return None
            ratio, c_all, c_dist = confirmed
            return (
                f"经计数验证确认 JOIN 扇出：JOIN 后行数 {c_all} 大于驱动表去重主键数 "
                f"{c_dist}（放大约 {ratio:.1f} 倍），对一端表列的 {agg_text} 结果虚高。"
                "请先用子查询/CTE 对多端去重或聚合后再 JOIN。"
            )
        return static_msg

    # ------------------------------------------------------------------ AST 分析
    def _fanout_pairs(self, stmt: exp.Expression) -> List[Tuple[str, str]]:
        """解析 JOIN 链，找出 (一端表, 多端表) 对；方向以关系图谱外键方向为准。
        本版本 sqlglot 结构：from_.this 为首表，select.joins 为依次加入的表，
        Join.this 即新加入的右表，其左侧是首表与此前已加入的全部表"""
        pairs: List[Tuple[str, str]] = []
        seen = set()
        from_ = stmt.args.get("from") or stmt.args.get("from_")
        accumulated: set = set()
        if from_ is not None and isinstance(from_.this, exp.Table):
            accumulated.add(from_.this.name)
        for join in stmt.args.get("joins") or []:
            right = join.this
            if not isinstance(right, exp.Table):
                continue    # 右侧是派生表/CTE，无法静态判定基数方向
            right_t = right.name
            for left_t in list(accumulated):
                if self.graph.is_many_to_one(right_t, left_t):
                    key = (left_t, right_t)          # 右表是多端
                elif self.graph.is_many_to_one(left_t, right_t):
                    key = (right_t, left_t)          # 左表是多端
                else:
                    continue
                if key not in seen:
                    seen.add(key)
                    pairs.append(key)
            accumulated.add(right_t)
        return pairs

    @staticmethod
    def _driving_table(stmt: exp.Expression) -> Optional[str]:
        """FROM 首表（驱动表）真实表名；派生表/子查询/无 FROM 返回 None"""
        from_ = stmt.args.get("from") or stmt.args.get("from_")
        if from_ is not None and isinstance(from_.this, exp.Table):
            return from_.this.name
        return None

    @staticmethod
    def _top_scope_aggregates(stmt: exp.Expression):
        """当前查询作用域内的聚合节点：沿 parent 链遇到 exp.Subquery 即属内层作用域
        （标量子查询/派生表/CTE 有独立 FROM-JOIN，其内部聚合不参与外层扇出分析）"""
        aggs = []
        for agg in stmt.find_all(exp.AggFunc):
            node, nested = agg.parent, False
            while node is not None and node is not stmt:
                if isinstance(node, exp.Subquery):
                    nested = True
                    break
                node = node.parent
            if not nested:
                aggs.append(agg)
        return aggs

    def _risky_aggregates(self, stmt: exp.Expression, fanout_pairs: List[Tuple[str, str]]):
        """扇出存在时真正危险的聚合（仅分析当前查询作用域）：
        - COUNT(*) 只在驱动表行可能被复制时危险：驱动表是一端、FROM 不可识别、
          或链上还挂着另一个多端表；驱动表是唯一多端（COUNT(*) FROM flights JOIN
          airports 数航班）时行不放大，放行；GROUP BY 一端主键的"每组多端行数"也放行
        - 对一端表列的 SUM/AVG 恒危险（一端行被复制后金额翻倍，即使分组也一样）；
        - COUNT(DISTINCT)/SUM(DISTINCT) 与只作用于多端列的聚合安全。
        sqlglot 把 COUNT(DISTINCT x) 解析为 Count(this=Distinct(...))"""
        one_side_tables = {p for p, _c in fanout_pairs}
        many_side_tables = {c for _p, c in fanout_pairs}
        grouped_by_one_pk = self._grouped_by_one_side_pk(stmt, one_side_tables)
        # COUNT(*) 是否被放大取决于驱动表在扇出对中的位置：
        # 驱动表是唯一多端时每行至多匹配一个父行（数多端行，安全）；驱动表是一端、
        # 无法识别（FROM 派生表）、或链上还挂着另一个多端表（多A→一P←多B）时才放大
        driving = self._driving_table(stmt)
        other_many = many_side_tables - ({driving} if driving else set())
        count_fanout_risky = driving is None or driving in one_side_tables or bool(other_many)
        risks = []
        for agg in self._top_scope_aggregates(stmt):
            is_distinct = isinstance(agg.this, exp.Distinct)
            if isinstance(agg, exp.Count):
                if is_distinct or grouped_by_one_pk or not count_fanout_risky:
                    continue
                risks.append(("COUNT 非去重计数（COUNT(*)）", None, None))
                continue
            if isinstance(agg, (exp.Sum, exp.Avg)):
                if is_distinct:
                    continue
                inner = agg.this
                if isinstance(inner, exp.Column):
                    qual = inner.table
                    if qual:
                        # 限定列：解析别名到真实表需要别名映射——这里复用图谱无法解析时按危险处理
                        real = self._resolve_alias(stmt, qual)
                        if real in many_side_tables and real not in one_side_tables:
                            continue    # 聚合的就是多端表自己的列，JOIN 不改变其语义
                        risks.append((f"{type(agg).__name__.upper()}({inner.name})", real, inner.name))
                    else:
                        risks.append((f"{type(agg).__name__.upper()}({inner.name})", None, inner.name))
                else:
                    risks.append((f"{type(agg).__name__.upper()}(表达式)", None, None))
        return risks

    @staticmethod
    def _resolve_alias(stmt: exp.Expression, alias_or_name: str) -> Optional[str]:
        for t in stmt.find_all(exp.Table):
            if t.name == alias_or_name or t.alias == alias_or_name:
                return t.name
        return None

    def _grouped_by_one_side_pk(self, stmt: exp.Expression, one_side_tables: set) -> bool:
        """GROUP BY 是否以某个一端表的主键为分组键（此时 COUNT(*) 即每组多端行数，安全）"""
        group = stmt.args.get("group")
        if group is None:
            return False
        for node in group.expressions if hasattr(group, "expressions") else [group]:
            col = node if isinstance(node, exp.Column) else node.find(exp.Column) if hasattr(node, "find") else None
            if col is None or not col.table:
                continue
            real_table = self._resolve_alias(stmt, col.table)
            if real_table in one_side_tables and col.name in self.graph.sm.get_primary_keys(real_table):
                return True
        return False

    # ------------------------------------------------------------------ 动态计数确认
    def _dynamic_confirm(self, stmt: exp.Expression) -> Optional[Tuple[float, int, int]]:
        """COUNT(*) vs COUNT(DISTINCT 驱动表主键)；不相等即确认扇出。
        返回 (放大倍数, 总行数, 去重主键数)；无法执行返回 None"""
        from_ = stmt.args.get("from") or stmt.args.get("from_")
        if from_ is None or not isinstance(from_.this, exp.Table):
            return None    # 驱动表是子查询/无 FROM，不做动态验证
        driving = from_.this
        driving_table = driving.name
        driving_alias = driving.alias or driving_table
        # 只有驱动表（FROM 首表）本身是被复制的一端时，行数计数才有放大效应；
        # 驱动表是多端时每行只出现一次，动态验证无意义
        one_side = {p for p, _c in self._fanout_pairs(stmt)}
        if driving_table not in one_side:
            return None
        pks = self.graph.sm.get_primary_keys(driving_table)
        if len(pks) != 1:
            return None    # 复合主键无法单列去重计数
        try:
            chk = stmt.copy()
            for arg in ("group", "order", "having", "limit"):
                chk.set(arg, None)
            chk.set("distinct", False)
            c_all = exp.alias_(exp.Count(this=exp.Star()), "c_all", quoted=True)
            c_dist = exp.alias_(
                exp.Count(this=exp.Distinct(expressions=[exp.column(pks[0], table=driving_alias)])),
                "c_dist", quoted=True,
            )
            chk.set("expressions", [c_all, c_dist])
            result = self.executor.execute(chk.sql(dialect="mysql"))
            if not result["rows"]:
                return None
            c_all_n, c_dist_n = int(result["rows"][0][0]), int(result["rows"][0][1])
            if c_dist_n == 0 or c_all_n == c_dist_n:
                return None     # 无行复制：扇出未发生
            return (c_all_n / c_dist_n, c_all_n, c_dist_n)
        except Exception as e:
            logger.warning("扇出动态计数查询失败: %s", str(e).splitlines()[0])
            return None
