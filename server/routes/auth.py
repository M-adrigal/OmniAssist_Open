import hashlib
import secrets
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/auth", tags=["auth"])

VALID_USERNAME = "admin"
VALID_PASSWORD = "123456"

_active_tokens: dict[str, float] = {}


def _generate_token() -> str:
    raw = f"{secrets.token_hex(32)}:{time.time()}"
    return hashlib.sha256(raw.encode()).hexdigest()


def validate_token(token: str) -> bool:
    if token in _active_tokens:
        return True
    return False


class LoginRequest(BaseModel):
    username: str
    password: str


@router.post("/login")
def login(req: LoginRequest):
    if req.username != VALID_USERNAME or req.password != VALID_PASSWORD:
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    token = _generate_token()
    _active_tokens[token] = time.time()

    return {"token": token, "message": "登录成功"}


@router.post("/logout")
def logout():
    return {"message": "已登出"}