# 对话接口：接收自然语言问题，驱动 NL2SQL 完整流水线
# - POST /api/chat        非流式 JSON 版本（聚合结果）
# - POST /api/chat/stream SSE 流式版本：intent → sql → executing →
#   correcting(如有) → explanation → result → usage → done 事件序列

import json

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from app.api.deps import get_pipeline
from app.nl2sql.pipeline import NL2SQLPipeline
from app.schemas.chat import ChatRequest, ChatResponse
from app.schemas.common import ErrorCode

router = APIRouter()


@router.post("", response_model=ChatResponse)
def chat(
    body: ChatRequest,
    pipeline: NL2SQLPipeline = Depends(get_pipeline),
):
    """非流式：意图分流 → 查询执行/仅生成SQL/元信息回答（与 SSE 同一 pipeline）"""
    result = pipeline.run(
        body.message, session_id=body.session_id,
        reference_turn_id=body.reference_turn_id,
        clarify_id=body.clarify_id,
    )
    code = ErrorCode.OK if result.error is None else ErrorCode.INTERNAL_ERROR
    if result.error_kind == "guard":
        code = ErrorCode.SQL_DENIED
    elif result.error_kind and result.error_kind.startswith("validation_"):
        code = ErrorCode.SQL_INVALID
    elif result.error_kind == "generate":
        code = ErrorCode.LLM_ERROR
    elif result.error_kind in ("syntax", "timeout", "permission"):
        code = ErrorCode.DB_EXECUTE_ERROR

    return ChatResponse(
        code=code,
        message=result.error or "ok",
        question=result.question,
        intent=result.intent,
        session_id=result.session_id,
        sql=result.sql,
        executed=result.executed,
        columns=result.columns,
        rows=result.rows,
        total=result.total,
        truncated=result.truncated,
        query_id=result.query_id,
        raw_output=result.raw_output,
        answer=result.answer,
        explanation=result.explanation,
        sql_label=result.sql_label,
        need_clarification=result.need_clarification,
        clarify_id=result.clarify_id,
        clarification_questions=result.clarification_questions,
        reference_turn_id=result.reference_turn_id,
        term_mappings=result.term_mappings,
        value_mappings=result.value_mappings,
        correction_trail=result.correction_trail,
        optimization_trail=result.optimization_trail,
        capability_trail=result.capability_trail,
        usage=result.usage,
        error=result.error,
        error_kind=result.error_kind,
    )


@router.post("/stream")
def chat_stream(
    body: ChatRequest,
    pipeline: NL2SQLPipeline = Depends(get_pipeline),
):
    """流式 SSE：逐事件推送流水线进度（事件协议见 schemas/chat.ChatEvent）"""

    def event_stream():
        try:
            for event in pipeline.iter_run(
                body.message, session_id=body.session_id,
                reference_turn_id=body.reference_turn_id,
                clarify_id=body.clarify_id,
            ):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception as e:  # 流中途异常兜底：保证前端一定收到终止事件
            yield f"data: {json.dumps({'type': 'error', 'data': {'message': f'内部错误：{e}', 'kind': 'internal'}}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type': 'done', 'data': {}}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",   # nginx 不缓冲，逐事件透传
        },
    )
