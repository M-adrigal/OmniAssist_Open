from pydantic import BaseModel
from typing import Optional, List, Dict, Any


class ChatRequest(BaseModel):
    session_id: Optional[str] = None
    message: str
    web_search: str = "off"


class SessionCreate(BaseModel):
    title: Optional[str] = None


class SessionRename(BaseModel):
    title: str


class ModelConfigUpdate(BaseModel):
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    model_name: Optional[str] = None
    context_limit: Optional[str] = None
    show_thought: Optional[bool] = None


class ModelConfigResponse(BaseModel):
    model_name: str
    base_url: str
    api_key_masked: str
    context_limit: str
    config_type: str = "none"
    show_thought: bool = False


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


class LoginResponse(BaseModel):
    token: str
    message: str
    user_type: str
    username: str


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
    id: int
    username: str
    user_type: str
    description: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class CurrentUserResponse(BaseModel):
    id: int
    username: str
    user_type: str
    description: str