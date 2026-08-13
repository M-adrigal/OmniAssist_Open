import hashlib
import hmac
import json
import os
import secrets
import time
from base64 import urlsafe_b64encode, urlsafe_b64decode
from collections import defaultdict
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from server.models import LoginRequest, LoginResponse, ChangePasswordRequest, CurrentUserResponse
from server.database import (
    authenticate,
    change_password,
    get_user_by_id,
    check_permission,
    get_role_permissions,
    get_user_must_change,
    validate_password_strength,
    DB_PATH,
)
from agent.logger import get_logger

router = APIRouter(prefix="/api/auth", tags=["auth"])
logger = get_logger("auth")

TOKEN_SECRET = secrets.token_hex(32)
TOKEN_TTL = 1800

_active_tokens: dict[str, float] = {}

# ---- 登录暴力破解防护（单进程内存态；多 worker 时改为共享存储如 Redis）----
_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_WINDOW = 15 * 60
_LOGIN_BLOCK = 15 * 60
_login_failures: dict[str, dict] = defaultdict(lambda: {"count": 0, "first": 0.0, "blocked_until": 0.0})


def _client_ip(request: Request) -> str:
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _login_allowed(key: str) -> tuple[bool, int]:
    """返回 (是否允许, 剩余封禁秒数)。"""
    now = time.time()
    rec = _login_failures.get(key)
    if rec and rec["blocked_until"] > now:
        return False, int(rec["blocked_until"] - now)
    return True, 0


def _record_login_failure(key: str):
    now = time.time()
    rec = _login_failures[key]
    rec["count"] += 1
    if rec["count"] >= _LOGIN_MAX_ATTEMPTS:
        rec["blocked_until"] = now + _LOGIN_BLOCK
        logger.warning(f"登录失败次数过多，已临时封锁: {key}")


def _reset_login_failures(key: str):
    _login_failures.pop(key, None)


def _generate_token(user_id: int, username: str, user_type: str) -> str:
    payload = {
        "uid": user_id,
        "un": username,
        "ut": user_type,
        "iat": int(time.time()),
        "exp": int(time.time()) + TOKEN_TTL,
    }
    payload_json = json.dumps(payload, separators=(",", ":"))
    payload_b64 = urlsafe_b64encode(payload_json.encode()).decode().rstrip("=")
    sig = hmac.new(TOKEN_SECRET.encode(), payload_b64.encode(), hashlib.sha256).hexdigest()
    token = f"{payload_b64}.{sig}"
    _active_tokens[token] = time.time()
    return token


def decode_token(token: str) -> Optional[dict]:
    if token not in _active_tokens:
        return None
    try:
        payload_b64, sig = token.split(".", 1)
        expected_sig = hmac.new(TOKEN_SECRET.encode(), payload_b64.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected_sig):
            return None
        payload_json = urlsafe_b64decode(payload_b64 + "==")
        payload = json.loads(payload_json)
        if payload.get("exp", 0) < time.time():
            _active_tokens.pop(token, None)
            return None
        return payload
    except Exception:
        return None


def validate_token(token: str) -> bool:
    return decode_token(token) is not None


def get_current_user(request: Request) -> Optional[dict]:
    token = request.cookies.get("auth_token") or request.headers.get("Authorization", "").replace("Bearer ", "")
    if not token:
        return None
    payload = decode_token(token)
    if not payload:
        return None
    return {
        "id": payload["uid"],
        "username": payload["un"],
        "user_type": payload["ut"],
    }


def require_admin(request: Request):
    return require_permission(request, "users", "read")


def require_permission(request: Request, resource: str, action: str):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    if not check_permission(user["user_type"], resource, action):
        raise HTTPException(status_code=403, detail=f"权限不足：需要 {resource}:{action}")
    return user


def require_login(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    return user


@router.post("/login", response_model=LoginResponse)
def login(req: LoginRequest, request: Request):
    ip = _client_ip(request)
    key = f"{ip}:{req.username}"
    allowed, wait = _login_allowed(key)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=f"登录失败次数过多，请 {wait} 秒后再试",
        )

    user = authenticate(req.username, req.password)
    if not user:
        _record_login_failure(key)
        logger.warning(f"用户登录失败: {req.username} (密码错误) from {ip}")
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    _reset_login_failures(key)
    token = _generate_token(user["id"], user["username"], user["user_type"])
    logger.info(f"用户登录成功: {req.username} (user_id={user['id']}, type={user['user_type']}) from {ip}")
    return LoginResponse(
        token=token,
        message="登录成功",
        user_type=user["user_type"],
        username=user["username"],
        must_change_password=user.get("must_change_password", False),
    )


@router.post("/logout")
def logout():
    return {"message": "已登出"}


@router.put("/password")
def update_password(req: ChangePasswordRequest, request: Request):
    user = require_login(request)

    ok, reason = validate_password_strength(req.new_password)
    if not ok:
        raise HTTPException(status_code=400, detail=f"新密码强度不足：{reason}。要求：密码至少 8 位，且必须包含大写字母、小写字母、数字、特殊符号")

    if req.new_password != req.confirm_password:
        raise HTTPException(status_code=400, detail="两次输入的新密码不一致")

    try:
        change_password(user["id"], req.old_password, req.new_password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if user["user_type"] == "admin":
        pw_file = os.path.join(os.path.dirname(DB_PATH), ".db_web_password")
        from server.database import _hash_password
        with open(pw_file, "w") as f:
            f.write(_hash_password(req.new_password))
        try:
            os.chmod(pw_file, 0o600)
        except Exception:
            pass

    logger.info(f"用户修改密码: {user['username']} (user_id={user['id']})")
    return {"message": "密码修改成功"}


@router.get("/me", response_model=CurrentUserResponse)
def get_me(request: Request):
    user = require_login(request)
    db_user = get_user_by_id(user["id"])
    if not db_user:
        raise HTTPException(status_code=404, detail="用户不存在")
    return CurrentUserResponse(
        id=db_user["id"],
        username=db_user["username"],
        user_type=db_user["user_type"],
        description=db_user.get("description", ""),
    )


@router.get("/permissions")
def get_my_permissions(request: Request):
    user = require_login(request)
    permissions = get_role_permissions(user["user_type"])
    return {
        "role": user["user_type"],
        "permissions": permissions,
    }