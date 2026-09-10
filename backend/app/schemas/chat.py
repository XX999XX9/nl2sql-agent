# 对话相关：ChatRequest（消息体）、ChatResponse（非流式完整响应）
# ChatEvent（SSE 事件统一结构）、TurnUsage（耗时 + token 统计摘要）

from typing import List, Optional

from pydantic import BaseModel


class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None   # 传入则启用多轮上下文；由前端创建会话后携带


class ChatEvent(BaseModel):
    """SSE 流式事件统一结构

    type 取值与 data 结构（事件协议，前后端共同遵守）：
    - intent       {intent: query/dml/meta}
    - sql          {sql}                       生成的 SQL（守卫改写后）
    - executing    {sql}                       开始执行
    - correcting   {round, error}              自动纠错轮次（纠错阶段接入）
    - explanation  {text}                      SQL 业务化解释
    - result       {query_id, columns, rows, total, truncated, executed}
    - usage        {elapsed_ms, prompt_tokens, completion_tokens, total_tokens, steps:[{name,elapsed_ms,...}]}
    - error        {message, kind}
    - done         {}                          本轮流结束
    """

    type: str
    data: dict = {}


class TurnUsage(BaseModel):
    """单轮 usage 摘要（usage 事件载荷）"""

    elapsed_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    steps: List[dict] = []


class ChatResponse(BaseModel):
    """非流式对话响应（JSON 版本，与 SSE 事件序列等价的信息聚合）"""

    code: int = 0
    message: str = "ok"
    question: str = ""
    intent: str = "query"
    session_id: Optional[str] = None
    sql: str = ""
    executed: bool = True
    columns: List[str] = []
    rows: List[list] = []
    total: int = 0
    truncated: bool = False
    query_id: Optional[str] = None
    raw_output: str = ""
    answer: str = ""
    explanation: str = ""
    term_mappings: List[dict] = []
    value_mappings: List[dict] = []
    correction_trail: List[dict] = []   # 纠错轨迹：每轮 {round, error, sql}
    usage: dict = {}
    error: Optional[str] = None
    error_kind: Optional[str] = None
