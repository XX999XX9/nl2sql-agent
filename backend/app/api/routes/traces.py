# Trace 接口：单次 nl→SQL 执行的完整图执行链路（检查点历史的投影）
#
# 事实源是 LangGraph 检查点；这里不做任何写操作，只按用户维度读取：
#   - 列表：来自 Redis 运行目录（run_id / 问题 / 状态 / 时间戳）；
#   - 详情：来自该会话线程的检查点历史，逐检查点还原“执行了哪个节点、产出了什么事件、状态如何变化”。

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import get_pipeline, get_projection, get_user_id
from app.persistence.store import Projection
from app.schemas.common import ApiResponse

router = APIRouter()


@router.get("", response_model=ApiResponse[list])
def list_traces(
    session_id: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    user_id: str = Depends(get_user_id),
    projection: Projection = Depends(get_projection),
    _pipeline=Depends(get_pipeline),   # 保证图已编译并绑定到投影
):
    """当前用户的执行记录（可按会话过滤，按开始时间倒序）"""
    return ApiResponse(data=projection.list_traces(user_id, session_id=session_id, limit=limit))


@router.get("/{run_id}", response_model=ApiResponse[dict])
def get_trace(
    run_id: str,
    user_id: str = Depends(get_user_id),
    projection: Projection = Depends(get_projection),
    _pipeline=Depends(get_pipeline),
):
    """单次执行的逐检查点链路：节点、下一步、增量事件、状态快照"""
    detail = projection.trace_detail(user_id, run_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="执行记录不存在")
    return ApiResponse(data=detail)
