# 内置工具集（目前仅 get_table_schema）。
# 工具统一注册进 CapabilityRegistry，只能由编排层凭 Gate Grant 调用；
# 参数在执行时再次做业务校验（Grant 只保证参数与被批准参数一致，不保证参数合法）。

import logging
from typing import List

from .registry import CapabilityRegistry

log = logging.getLogger(__name__)

MAX_TABLES_PER_CALL = 20     # 单次最多补充的表数，防"拉全库"式滥用
MAX_TABLE_NAME_LEN = 128


def _get_table_schema_runner(schema_manager):
    def run(args: dict, ctx) -> str:
        tables = args.get("tables")
        if not isinstance(tables, list) or not tables:
            return "【工具错误】get_table_schema 参数 tables 必须是非空表名数组，例如 " \
                   '{"tables": ["orders", "users"]}'
        # 归一化 + 长度/类型校验
        norm: List[str] = []
        for t in tables:
            if not isinstance(t, str) or not t.strip() or len(t) > MAX_TABLE_NAME_LEN:
                return f"【工具错误】非法表名: {t!r}"
            t = t.strip()
            if t not in norm:
                norm.append(t)
        if len(norm) > MAX_TABLES_PER_CALL:
            return f"【工具错误】单次最多申请 {MAX_TABLES_PER_CALL} 张表，请精简为真正需要的表"
        # 存在性校验：只能取当前库真实存在的表（未知表直接以错误文本回喂，不泄露其他信息）
        try:
            available = set(schema_manager.get_table_names())
        except Exception as e:
            log.warning("get_table_schema 读取表清单失败: %s", e)
            available = set(ctx.extra.get("known_tables", []))
        missing = [t for t in norm if t not in available]
        if missing:
            return f"【工具错误】以下表在当前数据库不存在，请核对表名（可参考上游识别结果中的表名）：{', '.join(missing)}"
        try:
            text = schema_manager.build_context_for_tables(norm)
        except Exception as e:
            log.warning("get_table_schema 构造表结构失败: %s", e)
            return f"【工具错误】表结构读取失败: {e}"
        log.info("get_table_schema 补充表结构: %s", ", ".join(norm))
        return f"【get_table_schema 返回（表: {', '.join(norm)}）】\n{text}"
    return run


def register_builtin_tools(registry: CapabilityRegistry, schema_manager) -> List[str]:
    registry.register_tool(
        name="get_table_schema",
        title="获取指定表的完整结构",
        risk="low",
        when_to_use="上游表字段识别标注为 maybe/excluded 的表可能需要使用、或确认关联所需表时，按表名补取结构",
        args_schema='{"tables": ["表名1", "表名2"]}  # 表名数组，单次最多 %d 张' % MAX_TABLES_PER_CALL,
        parameters={
            "type": "object",
            "properties": {
                "tables": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": MAX_TABLES_PER_CALL,
                    "description": "需要补取完整结构的真实表名列表，单次最多 %d 张" % MAX_TABLES_PER_CALL,
                },
            },
            "required": ["tables"],
        },
        runner=_get_table_schema_runner(schema_manager),
    )
    return ["get_table_schema"]
