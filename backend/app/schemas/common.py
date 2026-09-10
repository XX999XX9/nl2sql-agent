# 通用模型：ApiResponse 统一包装、错误码定义、分页参数

from typing import Generic, Optional, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class ErrorCode:
    """全局错误码：0 为成功，业务错误按模块分段"""

    OK = 0
    BAD_REQUEST = 400
    NOT_FOUND = 404
    INTERNAL_ERROR = 500
    # NL2SQL 业务段
    LLM_ERROR = 1001        # 大模型调用失败
    SQL_INVALID = 2001      # SQL 静态校验失败
    SQL_DENIED = 2002       # 安全守卫拦截
    DB_EXECUTE_ERROR = 2003  # 数据库执行失败


class PageParams(BaseModel):
    """通用分页参数"""

    page: int = 1
    page_size: int = 50


class ApiResponse(BaseModel, Generic[T]):
    """统一响应包装：code=0 表示成功"""

    code: int = ErrorCode.OK
    message: str = "ok"
    data: Optional[T] = None
