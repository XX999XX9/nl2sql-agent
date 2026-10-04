# 意图分类前置：区分 查询(query)/增删改(dml)/元信息(meta) 三类意图
# dml 走"仅生成 SQL + 解释、不执行"分支；meta 直接从 Schema 元数据回答；
# 意图前置既节省成本，也是安全分支路由的依据

import re
from typing import Optional

from app.model.factory import LLMClient
from app.utils.file_handler import read_text
from app.utils.logger_handler import setup_logger
from app.utils.path_tool import BACKEND_ROOT

logger = setup_logger()

VALID_INTENTS = ("query", "dml", "meta")

# 关键词快速通道：明显的增删改/元信息诉求无需调 LLM
_DML_PATTERN = re.compile(
    r"删除|删掉|去掉|修改|更新|插入|添加|新增|创建表|建表|删表|清空|备份表|"
    r"\b(drop|delete|truncate|alter|insert\s+into|update\s+\w+\s+set|create\s+table)\b",
    re.IGNORECASE,
)
_META_PATTERN = re.compile(r"有哪些表|什么表|表结构|字段说明|所有表|数据库里(有|都有)什么|数据字典")


class IntentClassifier:
    """意图分类器：关键词快路径 + LLM 兜底"""

    def __init__(self, llm: LLMClient):
        self.llm = llm
        self._template = read_text(BACKEND_ROOT / "prompts" / "intent_prompt.txt")

    def classify(self, question: str) -> tuple:
        """返回 (intent, usage)：intent 为 query/dml/meta，usage 为本次 LLM 调用的
        token 用量（正则快路径不调模型，返回零值）；任何异常兜底为 query（最安全）"""
        zero = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        if _DML_PATTERN.search(question):
            return "dml", zero
        if _META_PATTERN.search(question):
            return "meta", zero
        intent, usage = self._classify_by_llm(question)
        return (intent or "query"), (usage or zero)

    def _classify_by_llm(self, question: str) -> tuple:
        try:
            result = self.llm.chat([
                {"role": "user", "content": self._template.format(question=question)},
            ])
            usage = {
                "prompt_tokens": getattr(result, "prompt_tokens", 0) or 0,
                "completion_tokens": getattr(result, "completion_tokens", 0) or 0,
                "total_tokens": getattr(result, "total_tokens", 0) or 0,
            }
            text = result.content.strip().lower()
            for intent in VALID_INTENTS:
                if intent in text:
                    return intent, usage
            return None, usage
        except Exception as e:
            logger.warning("LLM 意图分类失败，兜底 query: %s", e)
            return None, None
