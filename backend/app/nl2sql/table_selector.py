# 表字段识别（SQL 生成前的第一阶段）：让 LLM 在全量表结构上先做裁剪——
# 模型只需输出精简 JSON：needed（确定使用的表 + use_columns 仅会用到的字段）、
# maybe（可能使用，仅表名）；other_columns 与 excluded 由本模块依据真实 Schema
# 反推补全（模型不回抄，节省输出 token，且不信任模型清单的完整性）。
# 第二阶段 SQL 生成器只收这份裁剪结果而非全量 Schema，需要更多表时走表结构获取工具。
# 任何解析/清洗失败一律返回 None，由流水线回退全量 Schema（宁可不裁剪，不可阻断）。

import json
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from app.model.factory import LLMClient
from app.nl2sql.schema_manager import SchemaManager
from app.utils.file_handler import read_text
from app.utils.logger_handler import setup_logger
from app.utils.path_tool import BACKEND_ROOT

logger = setup_logger()


@dataclass
class TableSelection:
    """表字段识别的结构化结果（列名均已按真实 Schema 清洗）"""

    # needed 项: {"table","comment"(数据库表级注释),"use_columns","other_columns"}
    needed: List[dict] = field(default_factory=list)
    maybe: List[str] = field(default_factory=list)
    excluded: List[str] = field(default_factory=list)
    raw_output: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass
class ClarificationRequest:
    """人工澄清请求：问题存在无法靠表结构消歧的缺失信息（如“这几天”“营收额口径”）。

    与 TableSelection 互斥：select() 返回本对象时，流水线暂停并向用户追问，
    用户补充后带着 clarify_id 重新发起，再走正常识别/生成链路。"""

    questions: List[str] = field(default_factory=list)   # 待用户确认的问题（可一次多个）
    raw_output: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class TableSelector:
    """LLM 表字段识别 + 结构化清洗 + 第二阶段 Prompt 上下文渲染"""

    def __init__(self, llm: LLMClient, schema_manager: SchemaManager, relation_graph=None):
        self.llm = llm
        self.schema_manager = schema_manager
        self.relation_graph = relation_graph
        self._template = read_text(BACKEND_ROOT / "prompts" / "table_select_prompt.txt")

    # ------------------------------------------------------------------
    def select(self, question: str, tables: Optional[List[str]] = None,
               conversation_context: str = ""):
        """调用识别模型并清洗结果；tables 限定候选表范围（None=全库）。
        返回 TableSelection（正常）/ ClarificationRequest（需向用户澄清）/ None（失败回退全量）"""
        all_tables = self.schema_manager.get_table_names() if tables is None else [
            t for t in tables if t in set(self.schema_manager.get_table_names())
        ]
        schema_context = (
            self.schema_manager.schema_context
            if tables is None
            else self.schema_manager.build_context_for_tables(tables)
        )
        relation_context = (
            self.relation_graph.build_context_for_tables(tables)
            if self.relation_graph else ""
        )
        prompt = self._template.format(
            schema_context=schema_context,
            relation_context=relation_context,
            question=question,
            conversation_context=conversation_context or "（无）",
        )
        try:
            result = self.llm.chat([{"role": "user", "content": prompt}])
        except Exception as e:
            logger.warning("表字段识别调用失败（回退全量 Schema）: %s", str(e).splitlines()[0])
            return None

        data = self._extract_json_object(result.content or "")
        if not isinstance(data, dict):
            logger.warning("表字段识别输出无法解析为 JSON（回退全量 Schema）: %s",
                           (result.content or "")[:200].replace("\n", " "))
            return None

        # 人工澄清：模型显式判定问题信息不足时，返回澄清请求（优先级高于表筛选结果）
        clar = self._parse_clarification(data)
        if clar is not None:
            clar.raw_output = result.content
            clar.prompt_tokens = result.prompt_tokens
            clar.completion_tokens = result.completion_tokens
            clar.total_tokens = result.total_tokens
            logger.info("表字段识别判定需人工澄清：%s", clar.questions)
            return clar

        sel = self._sanitize(data, all_tables)
        if sel is None:
            return None
        sel.raw_output = result.content
        sel.prompt_tokens = result.prompt_tokens
        sel.completion_tokens = result.completion_tokens
        sel.total_tokens = result.total_tokens
        logger.info("表字段识别完成：needed=%d maybe=%d excluded=%d",
                    len(sel.needed), len(sel.maybe), len(sel.excluded))
        return sel

    # ------------------------------------------------------------------ 澄清解析
    @staticmethod
    def _parse_clarification(data: dict) -> Optional[ClarificationRequest]:
        """严格识别澄清协议：need_clarification 必须是布尔 true，
        questions 必须是非空字符串数组（1~4 条），否则一律按普通筛选结果处理。"""
        if data.get("need_clarification") is not True:
            return None
        raw = data.get("questions")
        if not isinstance(raw, list):
            return None
        questions = [str(q).strip() for q in raw if str(q).strip()]
        if not questions:
            return None
        return ClarificationRequest(questions=questions[:4])

    # ------------------------------------------------------------------ 清洗
    def _sanitize(self, data: dict, all_tables: List[str]) -> Optional[TableSelection]:
        """按真实 Schema 清洗模型输出：表名/列名去幻觉，三类并集强制等于候选表全集"""
        table_set = set(all_tables)
        if not table_set:
            return None

        def as_str_list(v) -> List[str]:
            return [str(x).strip() for x in v if str(x).strip()] if isinstance(v, list) else []

        # needed：表必须存在；use_columns 只保留真实列；other_columns 以 Schema 为准重算补全
        needed: List[dict] = []
        needed_tables = set()
        raw_needed = data.get("needed", [])
        if not isinstance(raw_needed, list):
            return None
        for item in raw_needed:
            if not isinstance(item, dict):
                continue
            tbl = str(item.get("table", "")).strip()
            if tbl not in table_set or tbl in needed_tables:
                continue
            real_cols = {c["name"] for c in self.schema_manager.get_table(tbl)}
            use_cols = [c for c in dict.fromkeys(as_str_list(item.get("use_columns"))) if c in real_cols]
            use_set = set(use_cols)
            other_cols = [c["name"] for c in self.schema_manager.get_table(tbl) if c["name"] not in use_set]
            # 表说明只取自数据库表级注释，识别模型不得自行发挥
            needed.append({"table": tbl,
                           "comment": self.schema_manager.get_table_comment(tbl),
                           "use_columns": use_cols, "other_columns": other_cols})
            needed_tables.add(tbl)
        if not needed_tables:
            logger.warning("表字段识别未给出任何有效 needed 表（回退全量 Schema）")
            return None

        # maybe：真实表、与 needed 去重
        maybe = [t for t in dict.fromkeys(as_str_list(data.get("maybe")))
                 if t in table_set and t not in needed_tables]
        maybe_set = set(maybe)
        # excluded：以候选全集反推补齐（不信任模型清单的完整性），保持三类无遗漏无交叉
        excluded = [t for t in all_tables if t not in needed_tables and t not in maybe_set]
        return TableSelection(needed=needed, maybe=maybe, excluded=excluded)

    # ------------------------------------------------------------------ 渲染
    def render_context(self, sel: TableSelection) -> str:
        """渲染第二阶段（SQL 生成）使用的 schema_context 替代文本"""
        lines = [
            "以下内容是上游「表字段识别」节点根据用户问题与全库表结构分析得出的参考，"
            "请优先依据它生成 SQL。",
            "若需要「可能使用的表」或「不需要使用的表」中的字段信息，"
            "请先调用表结构获取工具 get_table_schema（入参 {\"tables\": [\"表名\", ...]}）获取后再生成。",
            "",
            "【确定使用的表】（字段已分为「使用字段」与「其他字段」，JOIN 主键/外键已包含在使用字段中）",
        ]
        for i, item in enumerate(sel.needed, 1):
            tbl = item["table"]
            comment = item.get("comment") or self.schema_manager.get_table_comment(tbl)
            lines.append(f"\n表 {tbl}{f'（{comment}）' if comment else ''}:")
            lines.append("  使用字段:")
            use_set = set(item["use_columns"])
            for col in self.schema_manager.get_table(tbl):
                if col["name"] in use_set:
                    lines.append(f"  - {self._col_text(col)}")
            if item["other_columns"]:
                others = []
                for col in self.schema_manager.get_table(tbl):
                    if col["name"] in set(item["other_columns"]):
                        others.append(f"{col['name']} {col['type']}")
                lines.append("  其他字段（通常无需使用）: " + "、".join(others))
            else:
                lines.append("  其他字段（通常无需使用）: 无")
        if sel.maybe:
            lines.append("\n【可能使用的表】（仅有表名；如需字段请调用 get_table_schema）")
            for t in sel.maybe:
                comment = self.schema_manager.get_table_comment(t)
                lines.append(f"- {t}{f'（{comment}）' if comment else ''}")
        if sel.excluded:
            lines.append("\n【不需要使用的表】（默认禁止使用；若确认确有必要，先调用 get_table_schema 获取其结构）")
            for t in sel.excluded:
                comment = self.schema_manager.get_table_comment(t)
                lines.append(f"- {t}{f'（{comment}）' if comment else ''}")
        return "\n".join(lines)

    @staticmethod
    def _col_text(col: dict) -> str:
        key_tag = ""
        if col["key"] == "PRI":
            key_tag = " 主键"
        elif col["key"] == "UNI":
            key_tag = " 唯一键"
        elif col["key"]:
            key_tag = " 索引"
        nullable = "" if col["nullable"] == "NO" else " 可空"
        col_comment = f"，{col['comment']}" if col["comment"] else ""
        return f"{col['name']} {col['type']}{key_tag}{nullable}{col_comment}"

    # ------------------------------------------------------------------ JSON
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
