# 应用入口：创建 FastAPI 实例、注册路由、挂载 CORS 中间件与全局异常处理器

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import router
from app.config import settings
from app.utils.logger_handler import setup_logger
from app.utils.path_tool import PROJECT_ROOT

logger = setup_logger()


def create_app() -> FastAPI:
    app = FastAPI(title=settings.app_name, version="0.1.0")

    # CORS 中间件
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 注册路由（统一挂 /api 前缀，与前端 BASE_URL 及 nginx 代理路径一致）
    app.include_router(router, prefix="/api")

    # 全局异常处理器：兜底未捕获异常，返回统一 JSON 错误
    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        logger.error("未捕获异常 %s %s: %s", request.method, request.url.path, exc, exc_info=exc)
        return JSONResponse(
            status_code=500,
            content={"code": 500, "message": "服务内部错误"},
        )

    @app.middleware("http")
    async def no_cache_html(request: Request, call_next):
        resp = await call_next(request)
        # HTML 入口禁缓存（静态 js/css 仍走 etag 协商），避免页面骨架更新后浏览器用旧 HTML
        if resp.headers.get("content-type", "").startswith("text/html"):
            resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    # 前端静态资源托管（默认开启；生产环境可用 NL2SQL_SERVE_FRONTEND=false
    # 关闭，改由 nginx 托管并反代 /api）
    if settings.serve_frontend:
        frontend_dir = PROJECT_ROOT / "frontend"
        if frontend_dir.exists():
            app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")

    return app


app = create_app()
