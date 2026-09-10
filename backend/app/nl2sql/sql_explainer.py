# SQL 解释生成：调用 LLM 将最终 SQL 翻译为业务化中文阐释
# （用到的表/条件/聚合/排序），供前端展示；dml 分支必需，query 分支顺带受益

from app.model.factory import LLMClient
from app.nl2sql.schema_manager import SchemaManager
from app.utils.file_handler import read_text
from app.utils.logger_handler import setup_logger
from app.utils.path_tool import BACKEND_ROOT

logger = setup_logger()


class SQLExplainer:
    """SQL 业务化解释器"""

    def __init__(self, llm: LLMClient, schema_manager: SchemaManager):
        self.llm = llm
        self.schema_manager = schema_manager
        self._template = read_text(BACKEND_ROOT / "prompts" / "explain_prompt.txt")

    def explain(self, sql: str) -> str:
        """生成中文解释；失败返回空串（解释是增益能力，不阻断主链路）"""
        if not sql:
            return ""
        try:
            result = self.llm.chat([
                {"role": "user", "content": self._template.format(
                    schema_context=self.schema_manager.schema_context,
                    sql=sql,
                )},
            ])
            return result.content.strip()
        except Exception as e:
            logger.warning("SQL 解释生成失败: %s", e)
            return ""
