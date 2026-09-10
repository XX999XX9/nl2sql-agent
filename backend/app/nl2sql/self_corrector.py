# 自动纠错链路：执行失败/校验失败/值不存在（查询返回 0 行且 WHERE 等值过滤值
# 不在库中）时，将错误信息连同表结构与出错 SQL 回喂大模型重写（最多 N 轮，
# 默认 2），每轮重写产物仍需过校验 + 守卫；值不存在时把该列采样到的真实取值
# 一并回喂，让模型改用正确值；记录纠错轨迹供前端展示

import re
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
                 value_linker: ValueLinker, max_rounds: int = 2):
        self.llm = llm
        self.schema_manager = schema_manager
        self.value_linker = value_linker
        self.max_rounds = max_rounds
        self._template = read_text(BACKEND_ROOT / "prompts" / "correct_prompt.txt")

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
                # 限定列（别名或表名）：解析为真实表
                real_table = alias_map.get(col.table)
                if not real_table:
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
                    "candidates": self.value_linker.get_column_values(candidates[0], col.name)[:_CANDIDATE_LIMIT],
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

    # ------------------------------------------------------------------
    # 重写：错误信息回喂 LLM
    def rewrite(self, question: str, sql: str, error_message: str,
                extra_context: str = "") -> Tuple[Optional[str], dict]:
        """回喂错误重写 SQL；返回 (新SQL 或 None, token usage)"""
        prompt = self._template.format(
            schema_context=self.schema_manager.schema_context,
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
