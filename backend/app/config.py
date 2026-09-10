# 服务配置：读取环境变量，定义端口、CORS 白名单、调试开关等；端口固定 8002
# backend/.env 由 app.utils.config_handler 在导入时自动加载（LLM Key、数据库账号等）

import os
from dataclasses import dataclass

from app.utils.config_handler import load_env_file  # noqa: F401  导入即加载 .env


@dataclass
class Settings:
    app_name: str = "nl2sql-agent"
    host: str = os.getenv("NL2SQL_HOST", "0.0.0.0")
    port: int = int(os.getenv("NL2SQL_PORT", "8002"))
    # debug 仅控制热重载与详细日志；前端静态资源默认始终托管（生产由 nginx 接管）
    debug: bool = os.getenv("NL2SQL_DEBUG", "false").lower() == "true"
    serve_frontend: bool = os.getenv("NL2SQL_SERVE_FRONTEND", "true").lower() == "true"
    cors_origins: list = None

    def __post_init__(self):
        if self.cors_origins is None:
            self.cors_origins = ["*"]


settings = Settings()
