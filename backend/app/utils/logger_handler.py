# 日志工具：日志器配置、按日期轮转、格式化输出

import logging
from logging.handlers import TimedRotatingFileHandler

from app.utils.path_tool import BACKEND_ROOT


def setup_logger(name: str = "agent", level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(level)

    log_dir = BACKEND_ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    handler = TimedRotatingFileHandler(
        log_dir / f"{name}.log", when="midnight", backupCount=30, encoding="utf-8"
    )
    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    handler.setFormatter(formatter)

    console = logging.StreamHandler()
    console.setFormatter(formatter)

    logger.addHandler(handler)
    logger.addHandler(console)
    return logger