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
    # 用户在技能面板里显式勾选的技能名（本轮优先使用，覆盖自动匹配）
    selected_skills: list = []
    # 本轮新上传的文件名（发完即清）。图片只发本轮上传的 —— 会话上传目录是
    # 跨轮累积的，不限定就会把前几轮的图全部重发（叠加），既烧视觉 token
    # 又让模型分不清哪张图对应当前问题。文档文本仍走全目录（跨轮引用是特性）。
    uploaded_files: list = []


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
    # 两层模型结构下的补充字段：模型 ID 与显示名分离，切换供应商时显示名不变
    entry_id: Optional[int] = None
    display_name: str = ""
    model_id: str = ""
    provider_name: str = ""
    entry_owner: str = ""
    resolved_from: str = ""


class SearchConfigResponse(BaseModel):
    tavily_api_key: Optional[str] = None
    tavily_api_key_masked: str = "(未设置)"


# ===== 记忆（每位用户一份，四段）=====
# 四段是「整份覆盖」而不是按字段增量：记忆是一份文件，用户在编辑器里删掉一条
# 之后，服务端不该再把它拼回来。所以各段都允许缺省（缺省 = 该段清空）。

class MemoryUpdate(BaseModel):
    # 两种写法都收：
    # - `sections` 是按段 key 组织的字典（推荐，加新段不用改这里）
    # - work/personal/... 是老字段，留着让现有前端与调用方不用跟着改
    sections: Optional[Dict[str, str]] = None
    work: Optional[str] = None
    personal: Optional[str] = None
    focus: Optional[str] = None
    recent: Optional[str] = None


class MemoryEnabledUpdate(BaseModel):
    enabled: bool


class MemoryItemRequest(BaseModel):
    section_key: str
    content: Optional[str] = None
    entry_id: Optional[int] = None


class MemoryEvolveRequest(BaseModel):
    session_id: Optional[str] = None
    max_messages: Optional[int] = None


class MemoryProposal(BaseModel):
    section_key: str
    section_title: str = ""
    content: str


class MemoryEvolveResponse(BaseModel):
    # 只返回提议，落库由人勾选后走 POST /item —— 与 /extract 同一条口径
    proposals: List[MemoryProposal] = []
    session_id: Optional[str] = None
    session_title: Optional[str] = None
    message_count: int = 0
    duplicate_skipped: int = 0
    dropped: int = 0
    raw: str = ""


class MemoryVersion(BaseModel):
    id: int
    reason: str
    chars: int = 0
    created_at: Optional[str] = None


class MemoryResponse(BaseModel):
    # 老字段：段的纯文本形态（导出、老调用方都在用，加新段也不会破坏）
    work: str = ""
    personal: str = ""
    focus: str = ""
    recent: str = ""
    sections: Dict[str, str] = {}
    # 新字段：按段拆开的条目（带 id，界面逐条编辑 / 删除用）
    items: Dict[str, List[Dict[str, Any]]] = {}
    # 段定义：界面按它渲染，新增段前端不用改代码
    registry: List[Dict[str, Any]] = []
    enabled: int = 1
    updated_at: Optional[str] = None
    text: str = ""
    total_chars: int = 0
    warnings: List[str] = []
    limits: Dict[str, Any] = {"total_chars": 4000, "recent_items": 20}
    version_count: int = 0
    versions: List[MemoryVersion] = []


class MemoryExtractRequest(BaseModel):
    text: str


class MemoryExtractResponse(BaseModel):
    work: str = ""
    personal: str = ""
    focus: str = ""
    recent: str = ""
    unmatched: str = ""
    raw: str = ""


class MemoryParseRequest(BaseModel):
    text: str


class MemoryParseResponse(BaseModel):
    work: str = ""
    personal: str = ""
    focus: str = ""
    recent: str = ""
    unmatched: str = ""


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