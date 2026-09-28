# 自动纠错链路：执行失败/校验失败/值不存在（查询返回 0 行且 WHERE 等值过滤值
# 不在库中）时，将错误信息连同表结构与出错 SQL 回喂大模型重写（最多 N 轮，
# 默认 2），每轮重写产物仍需过校验 + 守卫；值不存在时把该列采样到的真实取值
# 按与原值的字符相似度降序排列（最可能的正确值排最前、最多 N 个）一并回喂，
# 让模型改用正确值；记录纠错轨迹供前端展示

import json
import re
from difflib import SequenceMatcher
from typing import List, Optional, Tuple

import sqlglot
from sqlglot import exp

from app.model.factory import LLMClient
from app.nl2sql.schema_manager import SchemaManager
from app.nl2sql.value_linker import ValueLinker
from app.utils.config_handler import load_yaml
from app.utils.file_handler import read_text
from app.utils.logger_handler import setup_logger
from app.utils.path_tool import BACKEND_ROOT

logger = setup_logger()

_CANDIDATE_LIMIT = 20   # 值缺失提示中最多列出该列 20 个真实取值


class SelfCorrector:
    """SQL 自动纠错器：诊断 + 回喂重写"""

    def __init__(self, llm: LLMClient, schema_manager: SchemaManager,
                 value_linker: ValueLinker, max_rounds: int = 2, relation_graph=None):
        self.llm = llm
        self.schema_manager = schema_manager
        self.value_linker = value_linker
        self.max_rounds = max_rounds
        self.relation_graph = relation_graph   # 关系图谱：纠错重写时同样注入关联键区块
        self._template = read_text(BACKEND_ROOT / "prompts" / "correct_prompt.txt")
        self._judge_template = read_text(BACKEND_ROOT / "prompts" / "fanout_judge_prompt.txt")

    # ------------------------------------------------------------------
    # 诊断：0 行结果是否因 WHERE 等值过滤值不存在
    def diagnose_zero_row(self, sql: str) -> Tuple[str, List[dict]]:
        """检查 SQL 中的字符串等值条件；返回 (错误描述, 缺失值列表)。
        仅当某条件值确实不在该列采样取值中才判定缺失（合法 0 行不触发纠错）"""
        try:
            stmt = sqlglot.parse_one(sql, read="mysql")
        except Exception:
            return "", []

        # 别名/表名 → 真实表名 映射 + 语句涉及的真实表列表
        table_names = []
        alias_map = {}
        for t in stmt.find_all(exp.Table):
            if t.name not in table_names:
                table_names.append(t.name)
            alias_map[t.name] = t.name
            if t.alias:
                alias_map[t.alias] = t.name

        missing = []
        for eq in stmt.find_all(exp.EQ):
            col, lit = self._eq_column_literal(eq)
            if col is None:
                continue
            if col.table:
                # 限定列（别名或表名）：解析为真实表；须为字符串采样列，
                # 日期/数值列的等值条件无从查证，不诊断（与非限定列路径一致）
                real_table = alias_map.get(col.table)
                if not real_table or not self.value_linker.get_column_values(real_table, col.name):
                    continue
                candidates = [real_table]
            else:
                # 非限定列：取语句中拥有该（字符串采样）列的所有表
                candidates = [t for t in table_names if self.value_linker.get_column_values(t, col.name)]
                if not candidates:
                    continue
            # 值不在任何候选表该列取值中 → 缺失
            if all(
                lit.lower() not in (v.lower() for v in self.value_linker.get_column_values(t, col.name))
                for t in candidates
            ):
                missing.append({
                    "table": candidates[0],
                    "column": col.name,
                    "value": lit,
                    "candidates": self._rank_candidates(
                        self.value_linker.get_column_values(candidates[0], col.name), lit
                    ),
                })

        if not missing:
            return "", []
        lines = ["查询返回 0 行，以下 WHERE 条件值在库中不存在："]
        for m in missing:
            lines.append(
                f"- {m['table']}.{m['column']} = '{m['value']}' 不存在，"
                f"该列实际取值：{'、'.join(m['candidates'])}"
            )
        return "\n".join(lines), missing

    @staticmethod
    def _eq_column_literal(eq: exp.EQ) -> Tuple[Optional[exp.Column], Optional[str]]:
        """从等值节点解出 (列, 字符串字面量)；非"列=字符串"结构返回 (None, None)"""
        left, right = eq.this, eq.expression
        if isinstance(left, exp.Column) and isinstance(right, exp.Literal) and right.is_string:
            return left, right.this
        if isinstance(right, exp.Column) and isinstance(left, exp.Literal) and left.is_string:
            return right, left.this
        return None, None

    @staticmethod
    def _rank_candidates(values: List[str], target: str, limit: int = _CANDIDATE_LIMIT) -> List[str]:
        """候选值按与原值的字符相似度（SequenceMatcher ratio，忽略大小写）降序
        取前 limit 个——采样顺序靠前不代表最接近，最可能的正确值应排最前"""
        if not values:
            return []
        target_l = target.lower()
        scored = sorted(
            values,
            key=lambda v: SequenceMatcher(None, target_l, v.lower()).ratio(),
            reverse=True,
        )
        return scored[:limit]

    # ------------------------------------------------------------------
    # 扇出裁决：静态告警交 LLM 结合问题语义判定（一次调用；risky 时同批返回重写 SQL）
    def adjudicate_fanout(self, question: str, sql: str, risk_message: str,
                          tables: Optional[List[str]] = None) -> Tuple[str, str, Optional[str], dict]:
        """返回 (verdict, reason, rewritten_sql, usage)。
        safe    告警不成立，保留原 SQL；
        risky   告警成立，rewritten_sql 为有效重写时非空（为空说明模型未给出可用改写）；
        unknown 调用异常/输出不可解析/verdict 非法——调用方回退静态纠错旧链路。
        解析失败一律 unknown，绝不默认 safe（漏检）或默认 risky（强制重写）。"""
        relation_context = (
            self.relation_graph.build_context_for_tables(tables)
            if self.relation_graph else ""
        )
        prompt = self._judge_template.format(
            schema_context=self.schema_manager.schema_context,
            relation_context=relation_context,
            question=question,
            sql=sql,
            risk=risk_message,
        )
        try:
            result = self.llm.chat([{"role": "user", "content": prompt}])
        except Exception as e:
            logger.warning("扇出裁决 LLM 调用失败: %s", str(e).splitlines()[0])
            return "unknown", f"LLM 调用异常: {e}", None, {}
        usage = {
            "prompt_tokens": result.prompt_tokens,
            "completion_tokens": result.completion_tokens,
            "total_tokens": result.total_tokens,
        }
        data = self._extract_json_object(result.content or "")
        if not isinstance(data, dict):
            logger.warning("扇出裁决输出无法解析为 JSON，回退旧链路: %s",
                           (result.content or "")[:200].replace("\n", " "))
            return "unknown", "裁决输出解析失败", None, usage
        verdict = str(data.get("verdict", "")).strip().lower()
        if verdict not in ("safe", "risky"):
            logger.warning("扇出裁决 verdict 非法: %r", data.get("verdict"))
            return "unknown", f"非法 verdict: {data.get('verdict')!r}", None, usage
        reason = str(data.get("reason", ""))[:200]
        rewritten = None
        if verdict == "risky":
            raw = data.get("sql")
            rewritten = self._extract_sql(str(raw)) if raw else None
            if not rewritten:
                logger.warning("扇出裁决 risky 但未给出有效重写 SQL，回退旧链路")
        return verdict, reason, rewritten, usage

    @staticmethod
    def _extract_json_object(content: str):
        """从模型输出提取 JSON 对象：优先整串/代码块解析，再退化为首个平衡花括号片段；
        花括号扫描感知字符串与转义；任何失败返回 None（不猜测内容）"""
        if not content:
            return None
        text = content.strip()
        m = re.search(r"```(?:json)?\s*(.+?)```", text, re.S | re.I)
        if m:
            text = m.group(1).strip()
        try:
            return json.loads(text)
        except Exception:
            pass
        start = text.find("{")
        if start < 0:
            return None
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except Exception:
                        return None
        return None

    # ------------------------------------------------------------------
    # 重写：错误信息回喂 LLM
    def rewrite(self, question: str, sql: str, error_message: str,
                extra_context: str = "", tables: Optional[List[str]] = None) -> Tuple[Optional[str], dict]:
        """回喂错误重写 SQL；返回 (新SQL 或 None, token usage)；
        tables 为该轮 Schema 检索命中表（None=全量），据此注入关系键区块"""
        relation_context = (
            self.relation_graph.build_context_for_tables(tables)
            if self.relation_graph else ""
        )
        prompt = self._template.format(
            schema_context=self.schema_manager.schema_context,
            relation_context=relation_context,
            question=question,
            sql=sql,
            error=error_message,
        )
        if extra_context:
            prompt = f"{extra_context}\n\n{prompt}"
        result = self.llm.chat([{"role": "user", "content": prompt}])
        new_sql = self._extract_sql(result.content)
        usage = {
            "prompt_tokens": result.prompt_tokens,
            "completion_tokens": result.completion_tokens,
            "total_tokens": result.total_tokens,
        }
        return new_sql, usage

    @staticmethod
    def _extract_sql(content: str) -> Optional[str]:
        """从模型输出提取 SQL：```sql 块 → 任意代码块 → 裸语句"""
        if not content:
            return None
        m = re.search(r"```sql\s*(.+?)```", content, re.S | re.I)
        if not m:
            m = re.search(r"```\s*(.+?)```", content, re.S)
        text = m.group(1) if m else content
        text = text.strip().rstrip(";").strip()
        # 裸输出且不含 SELECT（纠错目标均为查询）视为无效
        if not text or not re.search(r"\b(select|with)\b", text, re.I):
            return None
        return text
