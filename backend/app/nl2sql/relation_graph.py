# 表关联关系图谱：为 JOIN 生成提供"确定性元数据"，把模型"看列名猜关联"变为"照抄关联键"。
#
# 边（RelationEdge）统一按有向存储：child（外键所在表，一对多关系的"多端"）
# → parent（被引用表，"一端"）；路径推导时按无向图处理。
# 三级数据源，按可信度从高到低：
#   1. explicit       —— 数据库显式声明的外键（FK，Foreign Key，见 SchemaManager 对
#                       information_schema.REFERENTIAL_CONSTRAINTS 的查询），100% 可信
#   2. naming       —— 无 FK 库（如 Spider 由 SQLite 转换的库）按严格命名约定推断：
#                       xxx_id 列 → 主键为 xxx_id/id 的表；同名列且一端是主键
#   3. naming_verified —— 命名推断边再经"采样 JOIN 验证"（真实数据上两列能 JOIN 上、
#                       匹配率达标）后保留，臆想边在这一步被剔除
# 持久化：data/relation_store/relations.json，按 schema_fingerprint 失效，重启零重复计算；
# 任何环节异常都降级为"无关系图"（返回空上下文），绝不阻断主链路。
# 开关：features.relation_graph（总开关）；nl2sql.yml relation.infer_by_naming / verify_inferred

import json
import re
import time
from collections import defaultdict, deque
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional, Set, Tuple

import pymysql
from sqlglot import exp

from app.nl2sql.schema_manager import SchemaManager
from app.utils.config_handler import load_yaml
from app.utils.logger_handler import setup_logger
from app.utils.path_tool import BACKEND_ROOT

logger = setup_logger()

STORE_DIR = BACKEND_ROOT / "data" / "relation_store"     # 关系图谱持久化目录（评测脚本可 patch 按库隔离）

# 同名列推断（规则 B）时排除的通用列名：这些名字在多表同名是巧合而非关联
_GENERIC_COLUMNS = {
    "id", "uid", "name", "code", "type", "status", "date", "year", "month", "day",
    "city", "country", "gender", "age", "title", "description", "comment", "comments",
    "address", "phone", "email", "created_at", "updated_at", "rank", "score",
    "price", "count", "number", "num", "first_name", "last_name", "full_name",
    "label", "text", "value", "content", "notes", "color", "time", "datetime",
}
_VERIFY_SAMPLE = 200          # 采样验证每边取子表非空值的行数上限
_VERIFY_MIN_MATCH_RATIO = 0.05  # 采样行中能 JOIN 上的最低比例（FK 通常接近 1）
_MAX_VERIFY_QUERIES = 150     # 单次建图采样验证 SQL 数上限（防启动过慢，超出的推断边丢弃）


@dataclass
class RelationEdge:
    """一条表间关联（有向：child 多端 → parent 一端）"""

    child_table: str
    child_column: str
    parent_table: str
    parent_column: str
    source: str = "explicit"            # explicit / naming_verified / naming
    confidence: float = 1.0

    def key(self) -> tuple:
        return (self.child_table, self.child_column, self.parent_table, self.parent_column)


class RelationGraph:
    """表关联关系图谱：FK + 命名推断（+采样验证）构建，提供 Prompt 注入 /
    JOIN 路径推导 / 召回邻接扩展 / ON 键校验 / 扇出基数方向判断"""

    def __init__(self, schema_manager: SchemaManager):
        self.sm = schema_manager
        cfg = load_yaml(BACKEND_ROOT / "config" / "nl2sql.yml").get("relation", {})
        self._infer_naming = cfg.get("infer_by_naming", True)
        self._verify_inferred = cfg.get("verify_inferred", True)
        self._store_file = STORE_DIR / "relations.json"
        self.edges: List[RelationEdge] = []
        self._adj: Dict[str, List[Tuple[str, RelationEdge]]] = defaultdict(list)
        self._ready = False
        self._build()

    # ================================================================== 构建
    def _build(self):
        fingerprint = self.sm.schema_fingerprint()
        if self._load_if_fresh(fingerprint):
            self._ready = True
            logger.info("关系图谱命中快照：%d 条关联边", len(self.edges))
            return
        try:
            self.edges = self._collect_explicit()
            if self._infer_naming:
                inferred = self._infer_by_naming()
                if self._verify_inferred:
                    inferred = self._verify_edges(inferred)
                self.edges.extend(inferred)
            self._rebuild_index()
            self._save(fingerprint)
            self._ready = True
            n_explicit = sum(1 for e in self.edges if e.source == "explicit")
            logger.info(
                "关系图谱构建完成：%d 条边（显式 %d / 推断 %d）",
                len(self.edges), n_explicit, len(self.edges) - n_explicit,
            )
        except Exception as e:
            # 任何构建失败降级为空图：Prompt 不出关联区块，后续功能全部空转
            self.edges = []
            self._adj = defaultdict(list)
            self._ready = False
            logger.error("关系图谱构建失败（降级为空图，不阻断主链路）: %s", e)

    # ------------------------------------------------------------------
    def _collect_explicit(self) -> List[RelationEdge]:
        edges = []
        for fk in self.sm.all_foreign_keys():
            edges.append(RelationEdge(
                child_table=fk["table"], child_column=fk["column"],
                parent_table=fk["ref_table"], parent_column=fk["ref_column"],
                source="explicit", confidence=1.0,
            ))
        return edges

    def _infer_by_naming(self) -> List[RelationEdge]:
        """严格命名约定推断（宁漏勿滥）：
        A. 子表 xxx_id 列 → 存在的 xxx 表（其主键为 xxx_id 或单列 id）
        B. 同名列且恰好一端是主键（排除通用列名）"""
        edges: List[RelationEdge] = []
        seen = {e.key() for e in self._collect_explicit()}
        tables = self.sm.get_table_names()
        pk_map = {t: self.sm.get_primary_keys(t) for t in tables}

        # 规则 A：xxx_id → xxx 表
        for child in tables:
            for col in self.sm.get_table(child):
                name = col["name"]
                m = re.match(r"^(.+)_id$", name, re.I)
                if not m:
                    continue
                base = m.group(1)
                parent = self._resolve_table_name(base, tables)
                if not parent or parent == child:
                    continue
                parent_pks = pk_map[parent]
                if name in parent_pks:
                    parent_col = name
                elif parent_pks == ["id"]:
                    parent_col = "id"
                else:
                    continue
                edge = RelationEdge(child, name, parent, parent_col, source="naming", confidence=0.6)
                if edge.key() not in seen:
                    edges.append(edge)
                    seen.add(edge.key())

        # 规则 B：同名列 + 一端主键
        col_owners: Dict[str, List[str]] = defaultdict(list)
        for tbl in tables:
            for col in self.sm.get_table(tbl):
                col_owners[col["name"]].append(tbl)
        for col_name, owners in col_owners.items():
            if col_name.lower() in _GENERIC_COLUMNS or len(owners) < 2 or len(set(owners)) < 2:
                continue
            pk_owners = [t for t in set(owners) if col_name in pk_map[t]]
            if len(pk_owners) != 1:
                continue    # 没有主键端或多个主键端：关系有歧义，放弃
            parent = pk_owners[0]
            for child in owners:
                if child == parent:
                    continue
                edge = RelationEdge(child, col_name, parent, col_name, source="naming", confidence=0.5)
                if edge.key() not in seen:
                    edges.append(edge)
                    seen.add(edge.key())
        return edges

    @staticmethod
    def _resolve_table_name(base: str, tables: List[str]) -> Optional[str]:
        """base 与表名匹配：精确 → 大小写不敏感精确"""
        if base in tables:
            return base
        lower_map = {t.lower(): t for t in tables}
        return lower_map.get(base.lower())

    # ------------------------------------------------------------------ 采样验证
    def _connect(self):
        cfg = load_yaml(BACKEND_ROOT / "config" / "database.yml")["database"]
        return pymysql.connect(
            host=cfg["host"], port=int(cfg["port"]), user=cfg["user"],
            password=cfg["password"], database=cfg["name"],
            charset=cfg.get("charset", "utf8mb4"),
            connect_timeout=int(cfg.get("connect_timeout", 10)),
            read_timeout=5,
        )

    def _verify_edges(self, inferred: List[RelationEdge]) -> List[RelationEdge]:
        """在真实数据上验证推断边：子表采样非空值与父表 JOIN，匹配上才保留；
        表名/列名全部来自 information_schema，可用反引号安全拼接"""
        if not inferred:
            return []
        kept, queries_used = [], 0
        conn = None
        try:
            conn = self._connect()
            with conn.cursor() as cur:
                cur.execute("SET SESSION max_execution_time = 3000")
                for edge in inferred:
                    if queries_used >= _MAX_VERIFY_QUERIES:
                        logger.warning("采样验证达到 %d 条上限，剩余 %d 条推断边丢弃",
                                     _MAX_VERIFY_QUERIES, len(inferred) - queries_used)
                        break
                    # LEFT JOIN：COUNT(父表关联键) 为匹配行数，COUNT(*) 为采样总数；
                    # 匹配比例低于阈值（巧合重合）即视为臆想边剔除
                    sql = (
                        f"SELECT COUNT(`{edge.parent_table}`.`{edge.parent_column}`), COUNT(*) FROM ("
                        f"SELECT `{edge.child_column}` FROM `{edge.child_table}` "
                        f"WHERE `{edge.child_column}` IS NOT NULL LIMIT {_VERIFY_SAMPLE}"
                        f") s LEFT JOIN `{edge.parent_table}` "
                        f"ON s.`{edge.child_column}` = `{edge.parent_table}`.`{edge.parent_column}`"
                    )
                    try:
                        queries_used += 1
                        cur.execute(sql)
                        matched, sampled = cur.fetchone()
                        matched, sampled = int(matched), int(sampled)
                    except Exception as e:
                        logger.debug("推断边验证失败被剔除: %s.%s=%s.%s | %s",
                                     edge.child_table, edge.child_column,
                                     edge.parent_table, edge.parent_column, e)
                        continue
                    if sampled <= 0 or matched / sampled < _VERIFY_MIN_MATCH_RATIO:
                        continue
                    edge.source = "naming_verified"
                    edge.confidence = round(matched / sampled, 2)
                    kept.append(edge)
        except Exception as e:
            # 验证通道整体不可用（连不上/权限）时退回保守策略：丢弃全部推断边，
            # 只保留显式 FK（未经验证的推断边不进入 Prompt，避免错误引导模型）
            logger.warning("关系边采样验证不可用，仅保留显式外键: %s", e)
            return []
        finally:
            if conn:
                conn.close()
        return kept

    # ------------------------------------------------------------------ 索引/持久化
    def _rebuild_index(self):
        self._adj = defaultdict(list)
        for e in self.edges:
            self._adj[e.child_table].append((e.parent_table, e))
            self._adj[e.parent_table].append((e.child_table, e))

    def _load_if_fresh(self, fingerprint: str) -> bool:
        try:
            if not self._store_file.exists():
                return False
            data = json.loads(self._store_file.read_text(encoding="utf-8"))
            if data.get("fingerprint") != fingerprint:
                return False
            self.edges = [RelationEdge(**e) for e in data.get("edges", [])]
            self._rebuild_index()
            return bool(self.edges) or self._store_file.exists()
        except Exception:
            return False

    def _save(self, fingerprint: str):
        try:
            self._store_file.parent.mkdir(parents=True, exist_ok=True)
            payload = {"fingerprint": fingerprint, "built_at": time.time(),
                      "edges": [asdict(e) for e in self.edges]}
            tmp = self._store_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self._store_file)
        except Exception as e:
            logger.warning("关系图谱持久化失败（不影响当次运行）: %s", e)

    # ================================================================== 查询能力
    @property
    def ready(self) -> bool:
        return self._ready

    def edges_between(self, t1: str, t2: str) -> List[RelationEdge]:
        """两表间的全部关联边（无向，按 child→parent 原始方向返回）"""
        return [
            e for e in self.edges
            if (e.child_table == t1 and e.parent_table == t2)
            or (e.child_table == t2 and e.parent_table == t1)
        ]

    def is_many_to_one(self, many_table: str, one_table: str) -> bool:
        """many_table 相对 one_table 是否处于"多端"（外键在 many_table）——扇出检测用"""
        return any(
            e.child_table == many_table and e.parent_table == one_table
            for e in self.edges_between(many_table, one_table)
        )

    def neighbors(self, tables: List[str], hops: int = 1) -> List[str]:
        """表集合的 N 跳关联邻接表（向量检索命中表扩展 JOIN 邻接表用）"""
        result: Set[str] = set(tables)
        frontier = set(tables)
        for _ in range(max(1, hops)):
            nxt: Set[str] = set()
            for t in frontier:
                nxt.update(nb for nb, _e in self._adj.get(t, []))
            nxt -= result
            if not nxt:
                break
            result |= nxt
            frontier = nxt
        return [t for t in result if t in set(self.sm.get_table_names())]

    def shortest_path(self, src: str, dst: str) -> Optional[List[RelationEdge]]:
        """两表间最短 JOIN 路径（BFS，无向图），返回有序边列表；不连通返回 None"""
        if src == dst:
            return []
        prev: Dict[str, Tuple[str, RelationEdge]] = {}
        q = deque([src])
        seen = {src}
        while q:
            cur = q.popleft()
            for nb, edge in self._adj.get(cur, []):
                if nb in seen:
                    continue
                seen.add(nb)
                prev[nb] = (cur, edge)
                if nb == dst:
                    path = []
                    node = dst
                    while node != src:
                        pnode, e = prev[node]
                        path.append(e)
                        node = pnode
                    return list(reversed(path))
                q.append(nb)
        return None

    def connecting_paths(self, tables: List[str]) -> List[List[RelationEdge]]:
        """把候选表集合按最短路径连通（最小生成树式贪心 BFS），返回需要展示的路径列表。
        只连通同一连通分量内的表；不同分量（无关联）不强连。"""
        tables = list(dict.fromkeys(tables))
        if len(tables) < 2:
            return []
        connected = {tables[0]}
        remaining = set(tables[1:])
        paths: List[List[RelationEdge]] = []
        while remaining:
            best = None  # (距离, 目标表, 路径)
            for src in list(connected):
                for dst in remaining:
                    path = self.shortest_path(src, dst)
                    if path is not None and (best is None or len(path) < best[0]):
                        best = (len(path), dst, path)
            if best is None:
                break    # 剩余表与已连通集合不在同一分量
            _, dst, path = best
            paths.append(path)
            for e in path:
                connected |= {e.child_table, e.parent_table}
            remaining.discard(dst)
        return paths

    # ------------------------------------------------------------------ Prompt 注入
    def build_context_for_tables(self, tables: Optional[List[str]]) -> str:
        """生成 Prompt 的【表关联关系】区块；无可用边/未就绪/不同库切换异常时返回空串"""
        if not self._ready or not self.edges:
            return ""
        targets = tables if tables is not None else self.sm.get_table_names()
        targets = [t for t in targets if t in set(self.sm.get_table_names())]
        shown: Set[tuple] = set()
        lines: List[str] = []
        for path in self.connecting_paths(targets):
            for e in path:
                if e.key() in shown:
                    continue
                shown.add(e.key())
                src_tag = "显式外键" if e.source == "explicit" else "数据验证推断"
                lines.append(
                    f"- {e.child_table}.{e.child_column} = {e.parent_table}.{e.parent_column}"
                    f"（多对一：{e.child_table} 为多端，{e.parent_table} 为一端；{src_tag}）"
                )
        if not lines:
            return ""
        return "【表关联关系】（多表 JOIN 必须使用以下关联键作为 ON 条件，禁止臆造）\n" + "\n".join(lines) + "\n"

    # ------------------------------------------------------------------ AST 辅助
    def unknown_join_warnings(self, stmt: exp.Expression) -> List[str]:
        """检查 JOIN ON 条件是否使用了已知关联键；未使用的产出告警（只告警不拦截）。
        CTE/派生表别名、非限定列无法静态解析，跳过。
        本版本 sqlglot：from_.this 为首表，select.joins 为依次加入的表，Join.this 为右表"""
        alias_map, unresolvable = self._alias_maps(stmt)
        from_ = stmt.args.get("from") or stmt.args.get("from_")
        accumulated: Set[str] = set()
        if from_ is not None and isinstance(from_.this, exp.Table):
            accumulated.add(from_.this.name)
        warnings = []
        for join in stmt.args.get("joins") or []:
            right = join.this
            on = join.args.get("on")
            if on is None or not isinstance(right, exp.Table):
                continue    # 无 ON 的笛卡尔积由 SQLValidator 硬拦截；派生表跳过
            pairs = self._on_column_pairs(on, alias_map, unresolvable)
            pairs = [p for p in pairs if p is not None]
            if not pairs:
                accumulated.add(right.name)
                continue
            known = any(self._pair_is_edge(*p) for p in pairs)
            if not known:
                t1, _c1, t2, _c2 = pairs[0]
                warnings.append(
                    f"JOIN {t1} 与 {t2} 的 ON 条件未使用已知关联键"
                    f"（实际写的是 {', '.join(f'{a}.{b}={c}.{d}' for a, b, c, d in pairs)}）"
                )
            accumulated.add(right.name)
        return warnings

    @staticmethod
    def _alias_maps(stmt: exp.Expression):
        alias_map: Dict[str, str] = {}
        unresolvable: Set[str] = set()
        for t in stmt.find_all(exp.Table):
            alias_map[t.name] = t.name
            if t.alias:
                alias_map[t.alias] = t.name
        for cte in stmt.find_all(exp.CTE):
            if cte.alias:
                unresolvable.add(cte.alias)
        for sub in stmt.find_all(exp.Subquery):
            if sub.alias:
                unresolvable.add(sub.alias)
        return alias_map, unresolvable

    def _on_column_pairs(self, on_expr: exp.Expression, alias_map: Dict[str, str],
                          unresolvable: Set[str]) -> List[Optional[tuple]]:
        """解出 ON 表达式中的"表.列 = 表.列"等值对（AND 连接可含多对）"""
        pairs = []
        eqs = list(on_expr.find_all(exp.EQ)) if not isinstance(on_expr, exp.EQ) else [on_expr]
        for eq in eqs:
            left, right = eq.this, eq.expression
            if not (isinstance(left, exp.Column) and isinstance(right, exp.Column)):
                continue
            if not left.table or not right.table:
                continue    # 非限定列无法可靠判定归属
            t1, t2 = alias_map.get(left.table), alias_map.get(right.table)
            if not t1 or not t2 or t1 in unresolvable or t2 in unresolvable:
                pairs.append(None)
                continue
            pairs.append((t1, left.name, t2, right.name))
        return pairs

    def _pair_is_edge(self, t1: str, c1: str, t2: str, c2: str) -> bool:
        for e in self.edges_between(t1, t2):
            if ((e.child_table == t1 and e.child_column == c1
                 and e.parent_table == t2 and e.parent_column == c2)
                    or (e.child_table == t2 and e.child_column == c2
                         and e.parent_table == t1 and e.parent_column == c1)):
                return True
        return False
