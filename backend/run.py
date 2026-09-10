# 启动脚本：以 uvicorn 启动 FastAPI，统一管理启动参数

import uvicorn

from app.config import settings

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
    )