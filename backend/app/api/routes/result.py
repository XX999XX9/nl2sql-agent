# 结果接口：按 query_id 从结果缓存分页拉取数据，支撑前端翻页与导出

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import get_result_cache
from app.nl2sql.result_cache import ResultCache
from app.schemas.common import ApiResponse
from app.schemas.result import ResultPage

router = APIRouter()


@router.get("/{query_id}", response_model=ApiResponse[ResultPage])
def get_result_page(
    query_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    cache: ResultCache = Depends(get_result_cache),
):
    """分页拉取指定 query_id 的结果集"""
    data = cache.get_page(query_id, page=page, page_size=page_size)
    if not data:
        raise HTTPException(status_code=404, detail="结果不存在或已过期")
    return ApiResponse(data=ResultPage(**data))
