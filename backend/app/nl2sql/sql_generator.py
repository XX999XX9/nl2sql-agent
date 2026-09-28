# SQL 生成核心：组装完整 Prompt（系统提示+Schema 上下文+用户问题）调用 LLM，
# 解析模型输出，提取 SQL 语句，返回结构化结果

import re
from dataclasses import dataclass, field
from typing import List, Optional

from app.model.factory import LLMClient
from app.nl2sql.schema_manager import SchemaManager
from app.utils.file_handler import read_text
from app.utils.logger_handler import setup_logger
from app.utils.path_tool import BACKEND_ROOT

logger = setup_logger()

_SQL_BLOCK_RE = re.compile(r"```sql\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_ANY_BLOCK_RE = re.compile(r"```\w*\s*(.*?)```", re.DOTALL)


@dataclass
class GenerationResult:
    """SQL 生成结果"""

    sql: str = ""
    raw_output: str = ""                     # 模型原始输出（纠错/展示用）
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    model: str = ""


class SQLGenerator:
    """Prompt 组装 + LLM 调用 + SQL 提取；支持 query（SELECT）与 dml（仅生成不执行）两种模板"""

    def __init__(self, llm: LLMClient, schema_manager: SchemaManager,
                 relation_graph=None):
        self.llm = llm
        self.schema_manager = schema_manager
        self.relation_graph = relation_graph   # 表关联关系图谱（None 时不注入关联区块）
        self._system_prompt = read_text(BACKEND_ROOT / "prompts" / "system_prompt.txt").strip()
        self._template = read_text(BACKEND_ROOT / "prompts" / "nl2sql_prompt.txt")
        self._dml_template = read_text(BACKEND_ROOT / "prompts" / "dml_prompt.txt")

    def generate(self, question: str, extra_context: str = "", mode: str = "query",
                 tables: Optional[List[str]] = None,
                 conversation_context: str = "",
                 reference_context: str = "") -> GenerationResult:
        """组装 Prompt 调用 LLM 并提取 SQL；
        query 模式：多轮上下文经 {conversation_context} 紧跟用户问题，
        术语映射/值链接经 {reference_context} 置于表结构之后（模板内固定位置）；
        extra_context 仅 dml 模式使用（前置拼接，dml 模板无对应占位符）；
        mode=query 生成 SELECT，mode=dml 生成增删改语句（仅展示不执行）；
        tables 为 Schema 检索命中的表集合（None=全量 Schema 注入）"""
        schema_context = (
            self.schema_manager.schema_context
            if tables is None
            else self.schema_manager.build_context_for_tables(tables)
        )
        if mode == "dml":
            template = self._dml_template
            user_prompt = template.format(schema_context=schema_context, question=question)
            if extra_context:
                user_prompt = f"{extra_context}\n\n{user_prompt}"
        else:
            template = self._template
            relation_context = (
                self.relation_graph.build_context_for_tables(tables)
                if self.relation_graph else ""
            )
            user_prompt = template.format(
                schema_context=schema_context,
                relation_context=relation_context,
                question=question,
                conversation_context=conversation_context,
                reference_context=reference_context,
            )

        result = self.llm.chat([
            {"role": "system", "content": self._system_prompt},
            {"role": "user", "content": user_prompt},
        ])

        sql = self._extract_sql(result.content)
        if not sql:
            logger.warning("未能从模型输出中提取 SQL，原始输出: %s", result.content[:200])
        return GenerationResult(
            sql=sql,
            raw_output=result.content,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            total_tokens=result.total_tokens,
            model=result.model,
        )

    @staticmethod
    def _extract_sql(text: str) -> str:
        """从模型输出提取 SQL：优先 ```sql 块 → 任意代码块 → 裸语句兜底"""
        text = text.strip()
        if not text:
            return ""
        match = _SQL_BLOCK_RE.search(text) or _ANY_BLOCK_RE.search(text)
        sql = match.group(1).strip() if match else text
        # 去掉可能的行注释与首尾残留的中文说明行
        sql = "\n".join(
            line for line in sql.splitlines() if not line.strip().startswith("--")
        ).strip()
        # 去掉结尾多余的分号（executor 统一处理）
        return sql.rstrip(";").strip()
