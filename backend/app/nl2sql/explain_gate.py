# EXPLAIN 性能门禁：执行前对每条最终 SQL 跑 EXPLAIN（不实际执行），
# 大表 type=ALL 且 key=NULL（全表扫描且未命中任何索引）判 perf 类问题，不执行、
# 回喂纠错（执行计划 + 可用索引名）。性能优化独立计数（opt_rounds，最多 N 次，
# 默认 2），与语义纠错 attempt 完全分开；检测日志固定带 access type，
# 对 range 以下（index/ALL）输出精简完整计划，为后续按 type 筛查慢 SQL 预留。

from typing import List, Optional

from app.utils.logger_handler import setup_logger

logger = setup_logger()

# MySQL EXPLAIN access type 从优到劣；range 以下（更差）即 index、ALL
_TYPE_RANK = {
    "system": 0, "const": 1, "eq_ref": 2, "ref": 3,
    "fulltext": 4, "ref_or_null": 5, "index_merge": 6, "unique_subquery": 7,
    "index_subquery": 8, "range": 9, "index": 10, "ALL": 11,
}
_VERBOSE_TYPES = {"index", "all"}   # range 以下：输出完整计划（type 已统一小写）
_BLOCK_TYPES = {"all"}              # 目前仅 ALL+无索引+大表 拦截；index 索引全扫只记录
_PLAN_FIELDS = ("id", "select_type", "table", "type", "possible_keys", "key",
                "key_len", "ref", "rows", "filtered", "Extra")


class ExplainGate:
    """EXPLAIN 性能门禁：inspect() 单次检测，重写循环由 pipeline 编排"""

    def __init__(self, executor, large_table_rows: int = 10000,
                 max_opt_rounds: int = 2, enabled: bool = True):
        self.executor = executor
        self.large_table_rows = large_table_rows
        self.max_opt_rounds = max_opt_rounds
        self.enabled = enabled

    # ------------------------------------------------------------------
    def inspect(self, sql: str) -> dict:
        """单次 EXPLAIN 检测。
        返回 {ok, blocked, worst_type, plans(原始), message}；
        ok=False 表示 EXPLAIN 自身失败（调用方放行执行，不阻断主链路）"""
        try:
            plans = self.executor.explain(sql)
        except Exception as e:
            logger.warning("EXPLAIN 门禁检测失败（放行执行）: %s", str(e).splitlines()[0])
            return {"ok": False, "blocked": False, "worst_type": None, "plans": [], "message": ""}
        if not plans:
            return {"ok": True, "blocked": False, "worst_type": None, "plans": [], "message": ""}

        types_present = [str(p.get("type") or "?").lower() for p in plans]
        worst = max(types_present, key=lambda t: _TYPE_RANK.get(t, 6))
        summary = " | ".join(
            f"{p.get('table')}:{str(p.get('type') or '?').lower()}/"
            f"{p.get('key') or 'NULL'}/rows={p.get('rows')}"
            for p in plans
        )
        # 每次检测固定带 type 概览（后续可按 type 关键字筛查慢 SQL）
        logger.info("EXPLAIN 门禁检测 worst_type=%s [%s] | SQL: %s",
                    worst, summary, sql[:150].replace("\n", " "))
        if worst in _VERBOSE_TYPES:
            logger.info("EXPLAIN 完整计划（worst=%s，range 以下）:\n%s",
                        worst, self._format_plans(plans))

        offenders = self._find_offenders(plans)
        if not offenders:
            return {"ok": True, "blocked": False, "worst_type": worst,
                    "plans": plans, "message": ""}

        message = self._build_message(offenders, plans)
        return {"ok": True, "blocked": True, "worst_type": worst,
                "plans": plans, "message": message}

    # ------------------------------------------------------------------
    def _find_offenders(self, plans: List[dict]) -> List[dict]:
        """命中拦截的计划行：真实表（排除 <derived>/<union> 物化表）、
        type=ALL、key 为空、估算扫描行数达大表阈值"""
        out = []
        for p in plans:
            tbl = str(p.get("table") or "")
            if tbl.startswith("<"):
                continue
            if str(p.get("type") or "").lower() not in _BLOCK_TYPES:
                continue
            if p.get("key"):
                continue
            try:
                scan_rows = int(p.get("rows") or 0)
            except (TypeError, ValueError):
                scan_rows = 0
            if scan_rows >= self.large_table_rows:
                out.append(p)
        return out

    def _build_message(self, offenders: List[dict], plans: List[dict]) -> str:
        """回喂纠错的 perf 错误信息：问题计划 + 可用索引 + 改写边界"""
        lines = ["SQL 性能门禁未通过（perf）：以下大表执行计划为 type=ALL（全表扫描）"
                 "且 key=NULL（未使用任何索引），禁止直接执行："]
        for p in offenders:
            lines.append(
                f"- 表 {p.get('table')}：type=ALL，key=NULL，预计扫描 {p.get('rows')} 行"
                f"，possible_keys={p.get('possible_keys') or '无'}"
            )
        # 可用索引：优先 EXPLAIN 给出的 possible_keys；为空再查 SHOW/STATISTICS 真实索引
        tables = [str(p.get("table")) for p in offenders]
        lines.append("可用索引信息：")
        for tbl in tables:
            possible = next((str(p.get("possible_keys")) for p in offenders
                             if str(p.get("table")) == tbl and p.get("possible_keys")), None)
            if possible:
                lines.append(f"- {tbl} 优化器候选索引：{possible}")
                continue
            try:
                idx_rows = self.executor.table_indexes(tbl)
            except Exception:
                idx_rows = []
            if not idx_rows:
                lines.append(f"- {tbl}：当前无任何索引（无法通过改 WHERE/JOIN 利用索引，"
                             "在保持结果语义前提下用可过滤的关联表条件缩小扫描，或对子查询先过滤）")
                continue
            grouped = {}
            for r in idx_rows:
                grouped.setdefault(r["index_name"], []).append(
                    f"{r['column']}{'(唯一)' if r['non_unique'] == 0 else ''}")
            lines.append(f"- {tbl}：" + "；".join(
                f"{name}({','.join(cols)})" for name, cols in grouped.items()))
        lines.append(
            "请在不改变结果集（行数、列、排序、语义口径完全一致）的前提下重写 SQL 以利用上述索引："
            "调整过滤条件使索引列可被命中（避免对索引列包函数/隐式类型转换）、"
            "用索引列做 JOIN ON 并调整驱动顺序、把过滤下推到子查询；"
            "严禁用增加 LIMIT、删减 DISTINCT/GROUP BY 或弱化过滤条件等改变结果的方式规避扫描。"
        )
        return "\n".join(lines)

    @staticmethod
    def _format_plans(plans: List[dict]) -> str:
        keep = [k for k in _PLAN_FIELDS if any(k in p for p in plans)]
        table = []
        for p in plans:
            table.append("  " + " | ".join(f"{k}={p.get(k)}" for k in keep))
        return "\n".join(table)
