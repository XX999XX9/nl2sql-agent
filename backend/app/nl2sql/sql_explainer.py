# SQL 解释生成：调用 LLM 将最终 SQL 翻译为业务化中文阐释
# （用到的表/条件/聚合/排序），供前端展示；dml 分支必需，query 分支顺带受益。
# 同时兼任“SQL 是否符合用户问题”的裁判：输出结构化标签 conform/risk，
# 代码侧严格校验，只接受这两种取值；任何解析/取值异常一律降级 unknown（前端灰色“未知”）。

import json
import re

from typing import List, Optional

from app.model.factory import LLMClient
from app.nl2sql.schema_manager import SchemaManager
from app.utils.file_handler import read_text
from app.utils.logger_handler import setup_logger
from app.utils.path_tool import BACKEND_ROOT

logger = setup_logger()

LABEL_CONFORM = "conform"   # 符合
LABEL_RISK = "risk"         # 风险
LABEL_UNKNOWN = "unknown"   # 结构化输出解析失败/取值非法
VALID_LABELS = (LABEL_CONFORM, LABEL_RISK)


class SQLExplainer:
    """SQL 业务化解释器 + SQL/问题一致性裁判"""

    def __init__(self, llm: LLMClient, schema_manager: SchemaManager):
        self.llm = llm
        self.schema_manager = schema_manager
        self._template = read_text(BACKEND_ROOT / "prompts" / "explain_prompt.txt")

    def explain(self, sql: str, question: str = "",
                tables: Optional[List[str]] = None) -> tuple:
        """返回 (解读文本, 标签, usage)；标签取值 conform/risk/unknown。
        tables 非空时只注入这些涉及表的结构（与生成/裁决节点口径一致），为空回退全量。
        失败时文本回退空串、标签 unknown（解读是增益能力，不阻断主链路）；
        usage 始终返回（含失败时的零值），调用方负责计入本轮 token 统计。"""
        zero_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        if not sql:
            return "", LABEL_UNKNOWN, zero_usage
        schema_context = (
            self.schema_manager.build_context_for_tables(tables)
            if tables else self.schema_manager.schema_context
        )
        try:
            result = self.llm.chat([
                {"role": "user", "content": self._template.format(
                    schema_context=schema_context,
                    sql=sql,
                    question=question or "（未提供）",
                )},
            ])
        except Exception as e:
            logger.warning("SQL 解释生成失败: %s", e)
            return "", LABEL_UNKNOWN, zero_usage
        usage = {
            "prompt_tokens": getattr(result, "prompt_tokens", 0) or 0,
            "completion_tokens": getattr(result, "completion_tokens", 0) or 0,
            "total_tokens": getattr(result, "total_tokens", 0) or 0,
        }

        content = (result.content or "").strip()
        data = self._extract_json_object(content)
        if not isinstance(data, dict):
            logger.warning("解读输出无法解析为 JSON，标签降级 unknown: %s",
                           content[:200].replace("\n", " "))
            return content, LABEL_UNKNOWN, usage
        label = str(data.get("label", "")).strip().lower()
        explanation = str(data.get("explanation", "")).strip()
        # 严格白名单：只接受 conform/risk；非法标签即使文本可用也判 unknown
        if label not in VALID_LABELS:
            logger.warning("解读标签非法（%r），降级 unknown", label)
            return explanation or content, LABEL_UNKNOWN, usage
        if not explanation:
            logger.warning("解读 JSON 缺少 explanation，标签降级 unknown")
            return content, LABEL_UNKNOWN, usage
        return explanation, label, usage

    @staticmethod
    def _extract_json_object(content: str):
        """从模型输出提取 JSON 对象：整串/代码块先试，再退化为首个平衡花括号片段"""
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
