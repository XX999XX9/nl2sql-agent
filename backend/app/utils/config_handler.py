# 配置读取：统一加载 YAML 配置，支持 .env 加载与 ${VAR:-default} 环境变量插值

import os
import re
from pathlib import Path

import yaml

from app.utils.path_tool import BACKEND_ROOT

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def load_env_file(path, override: bool = False) -> dict:
    """解析 .env 文件并写入 os.environ（默认不覆盖已有环境变量），返回解析出的键值对"""
    env_path = Path(path)
    parsed = {}
    if not env_path.exists():
        return parsed
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        parsed[key] = value
        if override or key not in os.environ:
            os.environ[key] = value
    return parsed


def _interpolate(value):
    """递归插值：${VAR} 读取环境变量，${VAR:-default} 支持默认值"""

    if isinstance(value, dict):
        return {k: _interpolate(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate(v) for v in value]
    if isinstance(value, str):
        def _replace(match):
            name, default = match.group(1), match.group(2)
            env_value = os.environ.get(name)
            if env_value:
                return env_value
            return default if default is not None else match.group(0)

        return _ENV_PATTERN.sub(_replace, value)
    return value


# 模块导入时自动加载 backend/.env（幂等，不覆盖已有环境变量），
# 保证任何 load_yaml 调用路径下敏感配置均已就绪
load_env_file(BACKEND_ROOT / ".env")


def load_yaml(path, overrides: dict = None) -> dict:
    """加载 YAML：先应用 .env 与环境变量插值，再用 overrides 覆盖"""
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    data = _interpolate(data)
    if overrides:
        data.update(overrides)
    return data
