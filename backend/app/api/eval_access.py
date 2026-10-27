# 内部工具（评测日志 / 提示词工作台）的访问控制
#
# 设计要点：
#   - 口令只在服务端校验，前端不再持有明文口令，也不再把“已验证”当作可信状态；
#   - 校验通过后签发短期令牌（HMAC-SHA256 签名，签名含过期时间），
#     后续请求携带 X-Eval-Token，服务端验签 + 校验过期；
#   - 令牌无状态，不依赖 Redis，签名密钥可用 NL2SQL_EVAL_SECRET 固定
#     （未配置时进程启动随机生成，后端重启后需重新输入口令，属预期行为）。
import hashlib
import hmac
import os
import secrets
import time

from fastapi import Header, HTTPException

PASSWORD = os.getenv("NL2SQL_EVAL_PASSWORD", "666")
TOKEN_TTL = int(os.getenv("NL2SQL_EVAL_TOKEN_TTL", "7200"))
_SECRET = os.getenv("NL2SQL_EVAL_SECRET") or secrets.token_urlsafe(32)


def verify_password(password: str) -> bool:
    """常量时间比较，避免按字符逐位试探"""
    return hmac.compare_digest((password or "").strip(), PASSWORD)


def issue_token() -> tuple[str, int]:
    """签发令牌：返回 (token, 有效期秒数)"""
    expires_at = int(time.time()) + TOKEN_TTL
    sig = hmac.new(_SECRET.encode(), str(expires_at).encode(), hashlib.sha256).hexdigest()
    return f"{expires_at}.{sig}", TOKEN_TTL


def token_valid(token: str) -> bool:
    if not token or "." not in token:
        return False
    expires_raw, sig = token.split(".", 1)
    try:
        expires_at = int(expires_raw)
    except ValueError:
        return False
    if expires_at < int(time.time()):
        return False
    expected = hmac.new(
        _SECRET.encode(), expires_raw.encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, sig)


def require_eval_access(
    x_eval_token: str = Header(default="", alias="X-Eval-Token"),
) -> None:
    """路由依赖：无有效令牌一律 401（默认拒绝）"""
    if not token_valid(x_eval_token):
        raise HTTPException(
            status_code=401,
            detail="访问未授权或令牌已过期，请重新输入访问口令",
        )
