# 实体值链接：抽取问题中的候选实体值，对库中字符串列采样值做精确/模糊匹配
# （滑动窗口 + SequenceMatcher 字符相似度），将"笔记本架→笔记本支架"、
# "机器键盘→机械键盘"类用户表述映射为库中真实值，作为补充上下文注入 Prompt，
# 从源头消除值不匹配导致的"查不到数"

import time
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Dict, List

import pymysql

from app.utils.config_handler import load_yaml
from app.utils.logger_handler import setup_logger
from app.utils.path_tool import BACKEND_ROOT

logger = setup_logger()

# 匹配命中阈值
_VALUE_COVERAGE = 0.6     # 匹配字符数覆盖库值长度的比例
_MIN_FUZZY_MATCH = 3      # 模糊命中至少 3 个匹配字符（短值只走精确匹配，防误报）
_SAMPLE_LIMIT = 200       # 每列采样 DISTINCT 值上限
_CACHE_TTL = 3600         # 采样缓存有效期（秒）


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
        self._samples: Dict[str, List[str]] = {}   # "table.column" -> [值...]
        self._sampled_at = 0.0

    # ------------------------------------------------------------------
    def _ensure_samples(self, force: bool = False):
        """采样全部表的字符串列（varchar/text）DISTINCT 值，带 TTL 缓存"""
        if not force and self._samples and time.time() - self._sampled_at < _CACHE_TTL:
            return
        conn = self._connect()
        samples: Dict[str, List[str]] = {}
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA = %s AND DATA_TYPE IN ('varchar', 'char', 'text') "
                    "ORDER BY TABLE_NAME, ORDINAL_POSITION",
                    (self._db_config["name"],),
                )
                for tbl, col in cur.fetchall():
                    key = f"{tbl}.{col}"
                    cur.execute(
                        f"SELECT DISTINCT `{col}` FROM `{tbl}` WHERE `{col}` IS NOT NULL LIMIT %s",
                        (_SAMPLE_LIMIT,),
                    )
                    samples[key] = [str(v[0]) for v in cur.fetchall()]
        finally:
            conn.close()
        self._samples = samples
        self._sampled_at = time.time()
        total = sum(len(v) for v in samples.values())
        logger.info("值链接采样完成：%d 列共 %d 个候选值", len(samples), total)

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
    def link(self, question: str, tables: List[str] = None) -> List[ValueMapping]:
        """匹配问题中提及的实体值；精确命中只记录不纠正，模糊命中产出映射；
        tables 限定候选表（Schema 检索命中表，None=全库扫描）"""
        if not self._enabled:
            return []
        self._ensure_samples()
        question_l = question.lower()
        mappings: List[ValueMapping] = []

        for column, values in self._samples.items():
            if tables is not None and column.split(".")[0] not in tables:
                continue
            for value in values:
                value_l = value.lower()
                if value_l in question_l:
                    # 精确命中（大小写可能不同）：值已正确，仅在大小写差异时提示
                    if value not in question and value_l in question_l:
                        mappings.append(ValueMapping(column, self._find_segment(question, value), value, exact=False))
                    continue
                # 快速预筛：库值与问题无任何二字公共片段则跳过（省窗口扫描）
                grams = {value_l[i : i + 2] for i in range(len(value_l) - 1)}
                if not any(g in question_l for g in grams):
                    continue
                # 模糊命中：最佳窗口匹配字符数覆盖库值 60% 以上
                seg, matched = self._best_window(question, value)
                if matched >= _MIN_FUZZY_MATCH and matched / len(value_l) >= _VALUE_COVERAGE:
                    mappings.append(ValueMapping(column, seg, value, exact=False))

        # 去重：同一列同一库值只保留一条
        seen = set()
        unique = []
        for m in mappings:
            key = (m.column, m.db_value)
            if key not in seen:
                seen.add(key)
                unique.append(m)
        return unique

    # ------------------------------------------------------------------
    @staticmethod
    def _best_window(question: str, value: str):
        """在问题中滑动与库值等长（或多一字）的窗口，返回 (最佳窗口, 匹配字符数)"""
        n = len(value)
        if n == 0:
            return "", 0
        value_l = value.lower()
        best_seg, best_matched = "", 0
        for size in (n, n + 1):
            for i in range(0, max(1, len(question) - size + 1)):
                seg = question[i : i + size]
                matched = _match_chars(seg.lower(), value_l)
                if matched > best_matched:
                    best_seg, best_matched = seg, matched
        return best_seg, best_matched

    @staticmethod
    def _find_segment(question: str, value: str) -> str:
        """从问题中截取与库值最相近的原始表述（精确命中时的表述定位）"""
        seg, _ = ValueLinker._best_window(question, value)
        return seg or question[: len(value)]

    # ------------------------------------------------------------------
    def build_context(self, question: str, tables: List[str] = None) -> tuple:
        """返回 (注入 Prompt 的上下文文本, 命中列表)；仅模糊命中需要纠正时输出"""
        mappings = [m for m in self.link(question, tables) if not m.exact]
        if not mappings:
            return "", []
        lines = ["【值链接】用户表述与库中真实存储值的对应关系（WHERE 条件必须使用库中真实值）："]
        for m in mappings:
            lines.append(f"- 用户说\"{m.user_term}\"，库中 {m.column} 的真实值为\"{m.db_value}\"")
        return "\n".join(lines), mappings
