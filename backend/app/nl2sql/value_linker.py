# 实体值链接：抽取问题中的候选实体值，对库中字符串列采样值做精确/模糊匹配
# （滑动窗口 + SequenceMatcher 字符相似度），将"笔记本架→笔记本支架"、
# "机器键盘→机械键盘"类用户表述映射为库中真实值，作为补充上下文注入 Prompt，
# 从源头消除值不匹配导致的"查不到数"
#
# 适用场景（2026-10 收紧）：仅中文/编号类场景启用，不在英文场景工作。
# 候选实体只抽取三类：①引号包裹内容；②连续中文片段；③含数字的编号/代码串。
# 英文专有名词序列、全大写缩写不再抽取（英文 Spider 类问题误报远多于收益）。
#
# 防误报设计（2026-10 修复"Port Chelsea→Port Oceane"类误导）：
# 1. 只在抽取出的"实体片段"内部做窗口匹配，不再对整句任意字符切片；
# 2. 用户实体在库中精确存在时短路，不再对该实体做任何模糊"纠正"；
# 3. 模糊命中需同时通过双向覆盖率、首字符锚定、最大连续块、多 token 核心词
#    相似度（去掉 Port/New/Saint 等通用地名前缀后仍要相似）多重门槛；
# 4. 长文本列（description/summary/content 等）不参与采样；
# 5. 同一实体在同一列模糊命中多个值视为歧义，全部丢弃不注入。
#
# 采样规模约束：先 COUNT(DISTINCT)，独特值超过 400 的高基数列整列放弃
# （枚举/字典类短值列才采样）；未超过的列最多取 200 个 DISTINCT 值。

import re
import time
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Dict, List, Optional, Tuple

import pymysql

from app.utils.config_handler import load_yaml
from app.utils.logger_handler import setup_logger
from app.utils.path_tool import BACKEND_ROOT

logger = setup_logger()

# 匹配命中阈值
_VALUE_COVERAGE = 0.6     # 匹配字符数占库值长度的比例
_WINDOW_COVERAGE = 0.6    # 匹配字符数占用户窗口长度的比例（双向覆盖）
_MIN_FUZZY_MATCH = 3      # 模糊命中至少 3 个匹配字符（短值只走精确匹配，防误报）
_MIN_BLOCK = 2            # 至少有一个连续匹配块达到该长度（防纯散落字符拼凑）
_SAMPLE_LIMIT = 200       # 低基数列每列采样 DISTINCT 值上限
_MAX_DISTINCT = 400       # 列的 DISTINCT 总数超过该值则整列不采样（高基数列放弃）
_CACHE_TTL = 3600         # 采样缓存有效期（秒）
_MAX_CASEFIX_LEN = 3      # 大小写纠正提示的最短值长度

# 长文本列名特征（这类列存的是段落/Lorem ipsum，参与值链接只会制造噪声）
_LONG_TEXT_HINT = ("description", "summary", "content", "comment", "remark",
                   "note", "detail", "_text")

# 多 token 值匹配时忽略的通用前缀词（仅对引号包裹的英文实体仍可能生效）
_GENERIC_TOKENS = {
    "port", "new", "old", "north", "south", "east", "west", "upper", "lower",
    "great", "grand", "san", "santa", "saint", "st", "fort", "mount", "la",
    "el", "los", "las", "de", "le", "les", "van", "von", "der", "die",
    "das", "the", "of", "and",
}

# 候选实体抽取正则（仅中文/编号场景：引号内容、CJK 连续片段、含数字代码串）
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]{2,}")
_CODE_RUN = re.compile(r"[A-Za-z0-9]*\d[A-Za-z0-9\-]{1,}")
_QUOTED = re.compile(r'["“「\']([^"”」\']{2,60})["”」\']')


@dataclass
class ValueMapping:
    """单条值链接命中"""

    column: str       # 库中列（table.column）
    user_term: str    # 用户问题中的表述
    db_value: str     # 库中真实值
    exact: bool       # 是否精确命中（无需纠正）


def _match_chars(a: str, b: str) -> int:
    """两串的匹配字符数（SequenceMatcher 全部匹配块累计，容错非连续字符差异）"""
    if not a or not b:
        return 0
    return sum(block.size for block in SequenceMatcher(None, a, b).get_matching_blocks())


class ValueLinker:
    """值链接器：采样字符串列的 DISTINCT 值并缓存，对问题做模糊匹配"""

    def __init__(self):
        self._db_config = load_yaml(BACKEND_ROOT / "config" / "database.yml")["database"]
        agent_cfg = load_yaml(BACKEND_ROOT / "config" / "agent.yml")
        self._enabled = agent_cfg.get("features", {}).get("value_linker", True)
        self._execution_timeout = int(self._db_config.get("execution_timeout", 30))
        self._samples: Dict[str, List[str]] = {}   # "table.column" -> [值...]
        self._sampled_at = 0.0

    # ------------------------------------------------------------------
    def _ensure_samples(self, force: bool = False):
        """采样全部表的短字符串列（varchar/char，排除 text 与长文本列）：
        先 COUNT(DISTINCT)，独特值 > _MAX_DISTINCT 的高基数列整列放弃；
        其余列取至多 _SAMPLE_LIMIT 个 DISTINCT 值。结果带 TTL 缓存"""
        if not force and self._samples and time.time() - self._sampled_at < _CACHE_TTL:
            return
        conn = self._connect()
        samples: Dict[str, List[str]] = {}
        skipped_high_card = 0
        try:
            with conn.cursor() as cur:
                # COUNT(DISTINCT)/DISTINCT 采样在宽表上是真正的重查询，
                # 必须与会话级超时对齐，避免采样把连接长期占住
                cur.execute(
                    f"SET SESSION max_execution_time = {self._execution_timeout * 1000}"
                )
                cur.execute(
                    "SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA = %s AND DATA_TYPE IN ('varchar', 'char') "
                    "ORDER BY TABLE_NAME, ORDINAL_POSITION",
                    (self._db_config["name"],),
                )
                cols = [(t, c) for t, c in cur.fetchall()
                        if not any(h in c.lower() for h in _LONG_TEXT_HINT)]
                for tbl, col in cols:
                    key = f"{tbl}.{col}"
                    cur.execute(
                        f"SELECT COUNT(DISTINCT `{col}`) FROM `{tbl}` "
                        f"WHERE `{col}` IS NOT NULL",
                    )
                    distinct_n = int(cur.fetchone()[0] or 0)
                    if distinct_n > _MAX_DISTINCT:
                        skipped_high_card += 1
                        continue
                    cur.execute(
                        f"SELECT DISTINCT `{col}` FROM `{tbl}` WHERE `{col}` IS NOT NULL LIMIT %s",
                        (_SAMPLE_LIMIT,),
                    )
                    samples[key] = [str(v[0]) for v in cur.fetchall()]
        finally:
            conn.close()
        self._samples = samples
        self._sampled_at = time.time()
        total = sum(len(v) for v in samples)
        logger.info("值链接采样完成：%d 列共 %d 个候选值（跳过 %d 个 DISTINCT>%d 的高基数列）",
                    len(samples), total, skipped_high_card, _MAX_DISTINCT)

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
    def get_column_values(self, table: str, column: str) -> List[str]:
        """取指定列的采样真实取值（供纠错诊断与提示）"""
        self._ensure_samples()
        return self._samples.get(f"{table}.{column}", [])

    def has_value(self, table: str, column: str, value: str) -> bool:
        """判断值是否存在于该列采样取值中（忽略大小写）"""
        return value.lower() in (v.lower() for v in self.get_column_values(table, column))

    # ------------------------------------------------------------------
    @staticmethod
    def _extract_terms(question: str) -> List[Tuple[str, int, int]]:
        """从问题中抽取候选实体片段 [(term, start, end)]，只在这些片段内部做匹配。
        仅中文/编号场景启用，只抽三类：
        - 引号包裹内容（用户显式标注，跨语言保留，如 "A102"、"Port Chelsea"）；
        - 连续中文片段（≥2字）；
        - 含数字的编号/代码串（订单号、型号等，语言无关）。
        纯英文自然语言问题抽不到任何片段 → 值链接整体不生效。"""
        found: List[Tuple[str, int, int]] = []

        for m in _QUOTED.finditer(question):
            found.append((m.group(1), m.start(1), m.end(1)))
        for m in _CJK_RUN.finditer(question):
            found.append((m.group(0), m.start(), m.end()))
        for m in _CODE_RUN.finditer(question):
            found.append((m.group(0), m.start(), m.end()))

        # 去重（同一片段只保留一次），长片段优先（保证精确短路先于模糊）
        uniq = {}
        for term, s, e in found:
            uniq.setdefault(term, (s, e))
        out = [(t, *se) for t, se in uniq.items()]
        out.sort(key=lambda x: -len(x[0]))
        return out

    @staticmethod
    def _tokens(text: str) -> List[str]:
        return [t for t in re.split(r"[\s/&\-]+", text.lower()) if t]

    @classmethod
    def _core_tokens_align(cls, seg: str, value: str) -> bool:
        """多 token 值（含空格的英文地名等）去掉通用前缀词后，核心词之间必须仍有
        一对高度相似；单 token 值（中文词、单个英文词）跳过此校验。
        拦截 "Port Chelse"→"Port Oceane"（chelse 与 oceane 并不同源）。"""
        ta, tb = cls._tokens(seg), cls._tokens(value)
        if len(ta) < 2 or len(tb) < 2:
            return True
        ca = [t for t in ta if t not in _GENERIC_TOKENS]
        cb = [t for t in tb if t not in _GENERIC_TOKENS]
        if not ca or not cb:
            return True
        for x in ca:
            for y in cb:
                if x == y:
                    return True
                if SequenceMatcher(None, x, y).ratio() >= 0.72:
                    return True
                # 一方是另一方前缀且仅差 1~2 个字符（Chelse/Chelsea 类截断）
                if (x.startswith(y) or y.startswith(x)) and abs(len(x) - len(y)) <= 2:
                    return True
        return False

    @classmethod
    def _fuzzy_hit(cls, seg: str, value: str) -> bool:
        """seg（用户实体内部窗口）与库值是否构成可信模糊命中"""
        seg_l, value_l = seg.lower(), value.lower()
        blocks = SequenceMatcher(None, seg_l, value_l).get_matching_blocks()
        matched = sum(b.size for b in blocks)
        if matched < _MIN_FUZZY_MATCH:
            return False
        if matched / len(value_l) < _VALUE_COVERAGE:
            return False
        if matched / len(seg_l) < _WINDOW_COVERAGE:
            return False
        if seg_l[0] != value_l[0]:
            return False
        if max((b.size for b in blocks), default=0) < _MIN_BLOCK:
            return False
        if not cls._core_tokens_align(seg, value):
            return False
        return True

    def link(self, question: str, tables: List[str] = None) -> List[ValueMapping]:
        """匹配问题中提及的实体值；精确命中只记录不纠正，模糊命中产出映射；
        tables 限定候选表（Schema 检索命中表，None=全库扫描）"""
        if not self._enabled:
            return []
        self._ensure_samples()
        terms = self._extract_terms(question)
        exact_terms = set()   # 已在库中精确存在的实体：不再做模糊"纠正"
        mappings: List[ValueMapping] = []

        for column, values in self._samples.items():
            if tables is not None and column.split(".")[0] not in tables:
                continue
            # 同实体同列候选收集（用于歧义丢弃）：term_lower -> [db_value...]
            fuzzy_buckets: Dict[str, List[str]] = {}

            for value in values:
                value_l = value.lower()
                for term, ts, te in terms:
                    term_l = term.lower()
                    # ---- 精确命中（大小写可能不同）----
                    if value_l in term_l:
                        if value != term and len(value) >= _MAX_CASEFIX_LEN:
                            mappings.append(ValueMapping(column, term, value, exact=False))
                        exact_terms.add(term)
                        continue
                    if term in exact_terms:
                        continue
                    # ---- 模糊命中：窗口只在实体片段内部滑动 ----
                    n = len(value)
                    best: Optional[Tuple[str, int]] = None
                    # n-1/n/n+1：兼容用户漏字、等长、多字三种拼写差异
                    for size in (n - 1, n, n + 1):
                        if size > len(term):
                            continue
                        for i in range(0, len(term) - size + 1):
                            seg = term[i:i + size]
                            if not self._fuzzy_hit(seg, value):
                                continue
                            mc = _match_chars(seg.lower(), value_l)
                            if best is None or mc > best[1]:
                                best = (seg, mc)
                    if best is not None:
                        fuzzy_buckets.setdefault(term_l, [])
                        if value not in fuzzy_buckets[term_l]:
                            fuzzy_buckets[term_l].append(value)

            # 无歧义（同实体同列唯一候选）才注入
            for term_l, cands in fuzzy_buckets.items():
                if term_l in {t.lower() for t in exact_terms}:
                    continue
                if len(cands) == 1:
                    term = next(t for t, _, _ in terms if t.lower() == term_l)
                    mappings.append(ValueMapping(column, term, cands[0], exact=False))

        # 去重：同一列同一库值只保留一条
        seen = set()
        unique = []
        for m in mappings:
            key = (m.column, m.db_value, m.user_term)
            if key not in seen:
                seen.add(key)
                unique.append(m)
        return unique

    # ------------------------------------------------------------------
    def build_context(self, question: str, tables: List[str] = None) -> tuple:
        """返回 (注入 Prompt 的上下文文本, 命中列表)；仅需要纠正时输出"""
        mappings = [m for m in self.link(question, tables) if not m.exact]
        if not mappings:
            return "", []
        lines = ["【值链接】以下为系统按字符相似度猜测的「用户表述 → 库中真实值」对照，"
                 "可能存在误判，请谨慎使用："]
        for m in mappings:
            lines.append(f"- 表 {m.column.split('.')[0]} 的 {m.column.split('.')[1]} 列："
                         f"用户提到\"{m.user_term}\"，疑似真实值为\"{m.db_value}\"")
        lines.append("使用原则：仅当按用户原文取值确实查不到结果时才可参考上述对照；"
                     "用户原文中出现且库中确实存在的值必须原样使用，禁止替换成其他相似值。")
        return "\n".join(lines), mappings
