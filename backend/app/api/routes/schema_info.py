# 元信息接口：返回已接入的表列表、表结构、语义映射，供前端"数据字典"面板展示

from fastapi import APIRouter, Depends

from app.api.deps import get_pipeline
from app.nl2sql.pipeline import NL2SQLPipeline
from app.nl2sql.term_mapper import TermMapper
from app.schemas.common import ApiResponse
from app.schemas.schema_info import ColumnInfo, SemanticMapItem, TableInfo

router = APIRouter()


@router.get("/tables", response_model=ApiResponse[list])
def list_tables(pipeline: NL2SQLPipeline = Depends(get_pipeline)):
    """全部表及其字段结构（数据字典）"""
    sm = pipeline.schema_manager
    tables = []
    for name in sm.get_table_names():
        columns = [
            ColumnInfo(
                name=c["name"],
                type=c["type"],
                nullable=(c["nullable"] == "YES"),
                key=c["key"],
                comment=c["comment"],
            )
            for c in sm.get_table(name)
        ]
        comment = sm._table_comments.get(name, "")
        tables.append(TableInfo(name=name, comment=comment, columns=columns))
    return ApiResponse(data=tables)


@router.get("/semantic-map", response_model=ApiResponse[list])
def semantic_map():
    """语义映射（业务术语 → 数据库表达）"""
    mapper = TermMapper()
    items = [
        SemanticMapItem(
            term=m["term"],
            type=m["type"],
            translation=m.get("desc", ""),
            expression=m.get("table", "") and f"{m['table']}.{m.get('column', '')}".rstrip(".")
            or m.get("expression", ""),
        )
        for m in mapper._mappings
    ]
    return ApiResponse(data=items)
