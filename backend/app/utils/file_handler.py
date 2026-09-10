# 文件处理：读写、上传保存、MD5 校验等通用文件操作

import hashlib
from pathlib import Path


def read_text(path) -> str:
    return Path(path).read_text(encoding="utf-8")


def write_text(path, content: str):
    Path(path).write_text(content, encoding="utf-8")


def md5(content: bytes) -> str:
    return hashlib.md5(content).hexdigest()