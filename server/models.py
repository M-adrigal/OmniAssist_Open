from pydantic import BaseModel
from typing import Optional, List, Dict, Any


class ChatRequest(BaseModel):
    session_id: Optional[str] = None
    message: str


class SessionCreate(BaseModel):
    title: Optional[str] = None


class SessionRename(BaseModel):
    title: str


class ModelConfigUpdate(BaseModel):
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    model_name: Optional[str] = None
    max_history_rounds: Optional[int] = None


class ModelConfigResponse(BaseModel):
    model_name: str
    base_url: str
    api_key_masked: str
    max_history_rounds: int


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