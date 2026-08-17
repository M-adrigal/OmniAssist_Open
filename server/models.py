from pydantic import BaseModel, field_validator
from typing import Optional, List, Dict, Any
import datetime as _dt


def _coerce_timestamp(v):
    """把数据库返回的 TIMESTAMP 统一成可读字符串。

    SQLite 的 TIMESTAMP 列在不同写入路径下可能是 float/int 时间戳
    （如 time.time()）或字符串（如 CURRENT_TIMESTAMP / strftime），
    这里做容错转换，避免 Pydantic 校验失败导致整个响应 500。
    """
    if v is None:
        return None
    if isinstance(v, (int, float)):
        try:
            return _dt.datetime.fromtimestamp(v).strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, OSError, OverflowError):
            return str(v)
    return str(v)


class ChatRequest(BaseModel):
    session_id: Optional[str] = None
    message: str
    web_search: str = "off"
    show_thought: bool = False


class SessionCreate(BaseModel):
    title: Optional[str] = None


class SessionRename(BaseModel):
    title: str


class ModelConfigUpdate(BaseModel):
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    model_name: Optional[str] = None
    context_limit: Optional[str] = None
    thinking_mode: Optional[str] = None  # off/low/high，控制思考强度与是否展示
    max_iterations: Optional[int] = None
    temperature_mode: Optional[str] = None  # static/auto，温度策略
    temperature: Optional[float] = None  # 基准温度 0-2


class ModelConfigResponse(BaseModel):
    model_name: str
    base_url: str
    api_key_masked: str
    context_limit: str
    config_type: str = "none"
    thinking_mode: str = "low"
    max_iterations: int = 10
    temperature_mode: str = "auto"
    temperature: float = 0.7


class SearchConfigResponse(BaseModel):
    tavily_api_key: Optional[str] = None
    tavily_api_key_masked: str = "(未设置)"


class ToolCreate(BaseModel):
    description: str


class ToolUpdate(BaseModel):
    description: str


class ToolInfo(BaseModel):
    name: str
    description: str
    parameters: Dict[str, Any]
    execution_mode: str
    output_dir: Optional[str] = None
    dependencies: Optional[List[str]] = None


class FileItem(BaseModel):
    name: str
    path: str
    type: str
    size: int
    children: Optional[List["FileItem"]] = None


class CommandItem(BaseModel):
    command: str
    description: str
    category: str


class LoginRequest(BaseModel):
    username: str
    password: str
    captcha_id: Optional[str] = None
    captcha_code: Optional[str] = None


class LoginResponse(BaseModel):
    token: str
    message: str
    user_type: str
    username: str
    must_change_password: bool = False


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str
    confirm_password: str


class UserCreateRequest(BaseModel):
    username: str
    password: str
    user_type: str = "user"
    description: str = ""


class UserUpdateRequest(BaseModel):
    password: Optional[str] = None
    user_type: Optional[str] = None
    description: Optional[str] = None


class UserResponse(BaseModel):
    public_id: str
    username: str
    user_type: str
    description: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    @field_validator("created_at", "updated_at", mode="before")
    @classmethod
    def _fmt_timestamp(cls, v):
        return _coerce_timestamp(v)


class CurrentUserResponse(BaseModel):
    public_id: str
    username: str
    user_type: str
    description: str


class SkillCreate(BaseModel):
    name: str
    content: str = ""  # SKILL.md 内容
    scripts: str = "[]"  # JSON 字符串，脚本数组


class SkillUpdate(BaseModel):
    content: Optional[str] = None
    scripts: Optional[str] = None
    enabled: Optional[int] = None


class SkillToggle(BaseModel):
    name: str
    enabled: bool