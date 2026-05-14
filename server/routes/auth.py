import hashlib
import hmac
import json
import os
import secrets
import time
from base64 import urlsafe_b64encode, urlsafe_b64decode

from fastapi import APIRouter, HTTPException, Request
from server.models import LoginRequest, LoginResponse, ChangePasswordRequest, CurrentUserResponse
from server.database import authenticate, change_password, get_user_by_id, check_permission, get_role_permissions, DB_PATH

router = APIRouter(prefix="/api/auth", tags=["auth"])

TOKEN_SECRET = secrets.token_hex(32)
TOKEN_TTL = 86400

_active_tokens: dict[str, float] = {}


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


def decode_token(token: str) -> dict | None:
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


def get_current_user(request: Request) -> dict | None:
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
def login(req: LoginRequest):
    user = authenticate(req.username, req.password)
    if not user:
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    token = _generate_token(user["id"], user["username"], user["user_type"])
    return LoginResponse(
        token=token,
        message="登录成功",
        user_type=user["user_type"],
        username=user["username"],
    )


@router.post("/logout")
def logout():
    return {"message": "已登出"}


@router.put("/password")
def update_password(req: ChangePasswordRequest, request: Request):
    user = require_login(request)

    if len(req.new_password) < 6:
        raise HTTPException(status_code=400, detail="新密码长度不能少于6位")

    if req.new_password != req.confirm_password:
        raise HTTPException(status_code=400, detail="两次输入的新密码不一致")

    try:
        change_password(user["id"], req.old_password, req.new_password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if user["user_type"] == "admin":
        pw_file = os.path.join(os.path.dirname(DB_PATH), ".db_web_password")
        with open(pw_file, "w") as f:
            f.write(req.new_password)
        try:
            os.chmod(pw_file, 0o600)
        except Exception:
            pass

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