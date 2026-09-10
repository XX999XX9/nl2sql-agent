# 会话模型：SessionCreate / SessionInfo

from typing import List, Optional

from pydantic import BaseModel


class SessionCreate(BaseModel):
    """创建会话请求（title 可省略）"""

    title: Optional[str] = None


class SessionInfo(BaseModel):
    """会话摘要信息"""

    session_id: str
    title: str
    created_at: float
    turn_count: int = 0
