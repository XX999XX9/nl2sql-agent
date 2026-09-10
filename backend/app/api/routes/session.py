# 会话接口：创建/查询/删除多轮对话会话及其历史

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import get_conversation_manager
from app.nl2sql.conversation import ConversationManager
from app.schemas.common import ApiResponse
from app.schemas.session import SessionCreate, SessionInfo

router = APIRouter()


@router.post("", response_model=ApiResponse[SessionInfo])
def create_session(
    body: SessionCreate = None,
    manager: ConversationManager = Depends(get_conversation_manager),
):
    """创建新会话，返回 session_id"""
    title = (body.title if body and body.title else "新会话")
    info = manager.create(title=title)
    return ApiResponse(data=info)


@router.get("", response_model=ApiResponse[list])
def list_sessions(manager: ConversationManager = Depends(get_conversation_manager)):
    """会话列表（按创建时间倒序）"""
    return ApiResponse(data=manager.list_sessions())


@router.get("/{session_id}", response_model=ApiResponse[dict])
def get_session(
    session_id: str,
    manager: ConversationManager = Depends(get_conversation_manager),
):
    """会话详情（含全部轮次历史）"""
    detail = manager.get(session_id)
    if not detail:
        raise HTTPException(status_code=404, detail="会话不存在或已过期")
    return ApiResponse(data=detail)


@router.delete("/{session_id}", response_model=ApiResponse[dict])
def delete_session(
    session_id: str,
    manager: ConversationManager = Depends(get_conversation_manager),
):
    """删除会话"""
    if not manager.delete(session_id):
        raise HTTPException(status_code=404, detail="会话不存在")
    return ApiResponse(data={"deleted": session_id})
