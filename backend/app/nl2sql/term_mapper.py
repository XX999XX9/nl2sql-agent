# 业务术语映射（语义层）：读取 semantic_map.yml，识别问题中的业务术语，
# 将"营收→SUM(total_money)"等业务黑话翻译为表/字段/聚合/过滤条件，
# 作为补充上下文注入 Prompt，降低字段歧义导致的生成错误

from dataclasses import dataclass
from typing import List

from app.utils.config_handler import load_yaml
from app.utils.path_tool import BACKEND_ROOT


@dataclass
class TermMapping:
    """单条术语命中"""

    term: str
    type: str        # table / column / aggregation / condition
    translation: str # 人可读的翻译结果
    expression: str  # 数据库可理解的表达（表名/字段名/聚合式/条件式）


class TermMapper:
    """术语识别与翻译：按 term 长度降序做最长匹配，避免"用户名"被"用户"抢先命中"""

    def __init__(self):
        config = load_yaml(BACKEND_ROOT / "config" / "semantic_map.yml")
        self._mappings = sorted(config.get("mappings", []), key=lambda m: len(m["term"]), reverse=True)

    def map(self, question: str) -> List[TermMapping]:
        """返回问题中命中的全部术语映射（已按最长匹配去重）"""
        hits: List[TermMapping] = []
        consumed_spans = []  # 已被更长术语占用的字符区间，防止短术语重复命中同一片段

        for m in self._mappings:
            term = m["term"]
            if not term:
                continue   # 防御：空 term 会使 find 永远命中当前位置导致死循环
            start = 0
            while True:
                idx = question.find(term, start)
                if idx < 0:
                    break
                span = (idx, idx + len(term))
                # 与已占用区间无交集才算新命中
                if not any(s < span[1] and span[0] < e for s, e in consumed_spans):
                    consumed_spans.append(span)
                    hits.append(self._translate(term, m))
                start = idx + len(term)

        return hits

    @staticmethod
    def _translate(term: str, m: dict) -> TermMapping:
        mtype = m["type"]
        if mtype == "table":
            translation, expression = f"表 {m['table']}", m["table"]
        elif mtype == "column":
            translation, expression = f"{m['table']}.{m['column']}", f"{m['table']}.{m['column']}"
        elif mtype == "aggregation":
            translation, expression = f"{m.get('desc', m['expression'])}", m["expression"]
        else:  # condition
            translation, expression = f"{m.get('desc', m['expression'])}", m["expression"]
        return TermMapping(term=term, type=mtype, translation=translation, expression=expression)

    def build_context(self, question: str) -> tuple:
        """返回 (注入 Prompt 的上下文文本, 命中列表)；无命中时文本为空"""
        hits = self.map(question)
        if not hits:
            return "", []
        lines = ["【术语映射】用户问题中的业务术语与数据库表达的对应关系（生成 SQL 时必须采用右侧表达）："]
        for h in hits:
            lines.append(f"- \"{h.term}\" → {h.expression}（{h.translation}）")
        return "\n".join(lines), hits
