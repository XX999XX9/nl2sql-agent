# 会话接口：创建/查询/删除多轮对话会话及其历史
#
# 多用户改造后：所有接口都从请求头 X-User-Id 解析当前用户，
# 会话的可见性、列表、删除都限定在该用户维度内；
# 返回的会话详情是检查点（事实源）的投影——只含人的提问与模型的回答。

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import get_conversation_manager, get_user_id
from app.nl2sql.conversation import ConversationManager
from app.schemas.common import ApiResponse
from app.schemas.session import SessionCreate, SessionInfo

router = APIRouter()


@router.post("", response_model=ApiResponse[SessionInfo])
def create_session(
    body: SessionCreate = None,
    user_id: str = Depends(get_user_id),
    manager: ConversationManager = Depends(get_conversation_manager),
):
    """创建新会话，返回 session_id"""
    title = (body.title if body and body.title else "新会话")
    info = manager.create(user_id=user_id, title=title)
    return ApiResponse(data=info)


@router.get("", response_model=ApiResponse[list])
def list_sessions(
    user_id: str = Depends(get_user_id),
    manager: ConversationManager = Depends(get_conversation_manager),
):
    """当前用户的会话列表（按创建时间倒序）"""
    return ApiResponse(data=manager.list_sessions(user_id))


@router.get("/{session_id}", response_model=ApiResponse[dict])
def get_session(
    session_id: str,
    user_id: str = Depends(get_user_id),
    manager: ConversationManager = Depends(get_conversation_manager),
):
    """会话详情（含全部轮次历史；仅限当前用户自己的会话）"""
    detail = manager.get(user_id, session_id)
    if not detail:
        raise HTTPException(status_code=404, detail="会话不存在")
    return ApiResponse(data=detail)


@router.delete("/{session_id}", response_model=ApiResponse[dict])
def delete_session(
    session_id: str,
    user_id: str = Depends(get_user_id),
    manager: ConversationManager = Depends(get_conversation_manager),
):
    """删除会话（同时清理该用户的目录索引与 LangGraph 检查点线程）"""
    if not manager.delete(user_id, session_id):
        raise HTTPException(status_code=404, detail="会话不存在")
    return ApiResponse(data={"deleted": session_id})
