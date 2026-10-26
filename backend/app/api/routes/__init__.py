# 路由注册汇总，供 main.py 统一 include_router
# prompt_optimize 工作台路由受 agent.yml features.prompt_optimize 开关控制

from fastapi import APIRouter

from app.api.routes import chat, eval_logs, result, session, schema_info, traces
from app.utils.config_handler import load_yaml
from app.utils.path_tool import BACKEND_ROOT

router = APIRouter()
router.include_router(chat.router, prefix="/chat", tags=["chat"])
router.include_router(result.router, prefix="/result", tags=["result"])
router.include_router(session.router, prefix="/session", tags=["session"])
router.include_router(traces.router, prefix="/traces", tags=["traces"])
router.include_router(schema_info.router, prefix="/schema", tags=["schema_info"])
router.include_router(eval_logs.router, prefix="/eval", tags=["eval_logs"])

_prompt_optimize_enabled = load_yaml(BACKEND_ROOT / "config" / "agent.yml").get(
    "features", {}
).get("prompt_optimize", True)
if _prompt_optimize_enabled:
    from app.api.routes import prompt_optimize

    router.include_router(prompt_optimize.router, prefix="/prompt_optimize", tags=["prompt_optimize"])
