# 元信息模型：TableInfo / ColumnInfo / SemanticMapItem

from typing import List, Optional

from pydantic import BaseModel


class ColumnInfo(BaseModel):
    """字段信息"""

    name: str
    type: str
    nullable: bool = True
    key: str = ""            # PRI / UNI / MUL / ''
    comment: str = ""


class TableInfo(BaseModel):
    """表信息"""

    name: str
    comment: str = ""
    columns: List[ColumnInfo] = []


class SemanticMapItem(BaseModel):
    """语义映射条目（业务术语 → 数据库表达）"""

    term: str
    type: str                # table / column / aggregation / condition
    expression: str
    translation: str = ""
