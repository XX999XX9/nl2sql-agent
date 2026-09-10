# 结果分页模型：ResultPage（列名/行数据/页码/总页数/总行数/是否截断/query_id）

from typing import List, Optional

from pydantic import BaseModel


class ResultPage(BaseModel):
    """按 query_id 分页拉取的结果页"""

    query_id: str
    columns: List[str] = []
    rows: List[list] = []
    page: int = 1
    page_size: int = 50
    total_pages: int = 1
    total: int = 0
    truncated: bool = False
    sql: str = ""
