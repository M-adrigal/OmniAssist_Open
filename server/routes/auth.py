import hashlib
import hmac
import json
import os
import secrets
import time
from base64 import b64encode as std_b64encode, urlsafe_b64encode, urlsafe_b64decode
from collections import defaultdict
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Response
from server.models import LoginRequest, LoginResponse, ChangePasswordRequest, CurrentUserResponse
from server.database import (
    authenticate,
    change_password,
    get_user_by_id,
    get_user_by_public_id,
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

# ---- 图形验证码（市面通用字符型图形验证码；单进程内存态，多 worker 时改用 Redis）----
# 验证码一次性使用、5 分钟过期，避免被复用或离线爆破。
_CAPTCHA_TTL = 5 * 60
_CAPTCHA_LEN = 4
# 去除易混淆字符（0/O/1/l/I）的字符集
_CAPTCHA_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_captcha_store: dict[str, dict] = {}


def _purge_expired_captchas() -> None:
    now = time.time()
    expired = [k for k, v in _captcha_store.items() if v["expires"] <= now]
    for k in expired:
        _captcha_store.pop(k, None)


def _gen_captcha_image(code: str) -> str:
    """用 Pillow 生成带噪点和干扰线的验证码图片，返回 data:image/png;base64 字符串。

    设计参考市面通用图形验证码：浅色背景 + 随机彩色字符（旋转/位移/大小随机）+
    多条干扰线 + 密集噪点 + 轻微波浪扭曲。
    在「可被人类轻松识别」与「增加 OCR 难度」之间取平衡。Pillow 缺失时退化为纯文字 code 返回。
    """
    try:
        from PIL import Image, ImageDraw, ImageFont, ImageFilter
    except Exception:
        logger.warning("Pillow 未安装，图形验证码降级为纯文本模式")
        return code

    import io
    import random

    width, height = 150, 52
    bg_r = random.randint(240, 250)
    bg_g = random.randint(243, 252)
    bg_b = random.randint(248, 255)
    img = Image.new("RGB", (width, height), (bg_r, bg_g, bg_b))
    draw = ImageDraw.Draw(img)

    # 干扰线（更多、颜色更深）
    for _ in range(random.randint(6, 9)):
        x1 = random.randint(0, width)
        y1 = random.randint(0, height)
        x2 = random.randint(0, width)
        y2 = random.randint(0, height)
        c = random.randint(100, 190)
        draw.line(
            [(x1, y1), (x2, y2)],
            fill=(c, c + random.randint(-20, 20), c + random.randint(-10, 30)),
            width=random.randint(1, 2),
        )

    # 加载可用字体（跨平台：macOS 中文字体 → Linux DejaVu → 内置默认）
    _font_path = None
    for _cand in (
        "/System/Library/Fonts/STHeiti Light.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        try:
            ImageFont.truetype(_cand, 28)
            _font_path = _cand
            break
        except Exception:
            continue
    font_base = ImageFont.load_default() if _font_path is None else ImageFont.truetype(_font_path, 28)

    char_w = width // len(code) + 2
    for i, ch in enumerate(code):
        # 每个字符随机字号 / 颜色 / Y 偏移
        font_size = random.randint(24, 32)
        try:
            font = ImageFont.truetype(_font_path, font_size) if _font_path else font_base
        except Exception:
            font = font_base
        color = (
            random.randint(20, 100),
            random.randint(20, 110),
            random.randint(100, 210),
        )
        layer_h = height + 8
        layer = Image.new("RGBA", (char_w + 12, layer_h), (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        x_off = random.randint(2, 7)
        y_off = random.randint(4, 12)
        ld.text((x_off, y_off), ch, font=font, fill=color)
        angle = random.uniform(-28, 28)
        layer = layer.rotate(angle, resample=Image.BICUBIC, center=(layer.width // 2, layer.height // 2))
        paste_x = i * char_w + random.randint(-3, 3)
        paste_y = random.randint(-4, 4)
        img.paste(layer, (paste_x, paste_y), layer)

    # 噪点（更密集）
    for _ in range(random.randint(120, 180)):
        x = random.randint(0, width - 1)
        y = random.randint(0, height - 1)
        v = random.randint(160, 230)
        draw.point((x, y), fill=(v, v + random.randint(-15, 15), v + random.randint(-5, 20)))

    # 轻微模糊让边缘更自然
    img = img.filter(ImageFilter.SMOOTH_MORE)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = std_b64encode(buf.getvalue()).decode()
    return f"data:image/png;base64,{b64}"


@router.get("/captcha")
def get_captcha(response: Response):
    """获取图形验证码：返回一次性 captcha_id 与 base64 图片，登录时需回传对应 code。"""
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    _purge_expired_captchas()
    import random
    code = "".join(random.choice(_CAPTCHA_ALPHABET) for _ in range(_CAPTCHA_LEN))
    captcha_id = secrets.token_hex(16)
    _captcha_store[captcha_id] = {"code": code.lower(), "expires": time.time() + _CAPTCHA_TTL}
    image = _gen_captcha_image(code)
    return {"captcha_id": captcha_id, "image": image, "ttl": _CAPTCHA_TTL}


def _verify_captcha(captcha_id: str, captcha_code: str) -> tuple[bool, str]:
    """校验图形验证码（一次性、大小写不敏感）。返回 (是否通过, 失败原因)。"""
    _purge_expired_captchas()
    if not captcha_id or not captcha_code:
        return False, "请填写图形验证码"
    rec = _captcha_store.pop(captcha_id, None)
    if rec is None:
        return False, "验证码已失效，请刷新"
    if rec["expires"] <= time.time():
        return False, "验证码已过期，请刷新"
    if rec["code"] != captcha_code.strip().lower():
        return False, "图形验证码错误"
    return True, ""


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
    # 对外仅暴露不透明 public_id，不暴露自增主键
    pid = None
    try:
        _u = get_user_by_id(user_id)
        pid = _u.get("public_id") if _u else None
    except Exception:
        pid = None
    if not pid:
        pid = str(user_id)  # 兜底（迁移后所有用户均有 public_id）
    payload = {
        "uid": pid,
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
    pid = payload.get("uid")
    # 从 public_id 解析出内部整数 id；失败安全：解析不到视为未登录
    db_user = get_user_by_public_id(pid) if pid else None
    if not db_user:
        return None
    return {
        "id": pid,                      # 对外不透明 id（字符串），前端不直接感知自增主键
        "db_id": db_user["id"],         # 内部整数 id，仅用于 DB 关联与权限判定
        "username": db_user["username"],
        "user_type": db_user["user_type"],
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

    # 图形验证码校验（市面通用：登录前先过图形验证，拦截自动化爆破）
    ok, reason = _verify_captcha(req.captcha_id, req.captcha_code)
    if not ok:
        # 验证码错误也计入失败次数，避免绕过限流反复尝试
        _record_login_failure(key)
        raise HTTPException(status_code=400, detail=reason)

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
        raise HTTPException(status_code=400, detail=f"新密码强度不足：{reason}。要求：密码至少 8 位，且需包含大写字母、小写字母、数字、特殊符号中至少两类")

    if req.new_password != req.confirm_password:
        raise HTTPException(status_code=400, detail="两次输入的新密码不一致")

    try:
        change_password(user["db_id"], req.old_password, req.new_password)
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

    logger.info(f"用户修改密码: {user['username']} (user_id={user['db_id']})")
    return {"message": "密码修改成功"}


@router.get("/me", response_model=CurrentUserResponse)
def get_me(request: Request):
    user = require_login(request)
    db_user = get_user_by_id(user["db_id"])
    if not db_user:
        raise HTTPException(status_code=404, detail="用户不存在")
    return CurrentUserResponse(
        public_id=db_user["public_id"],
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