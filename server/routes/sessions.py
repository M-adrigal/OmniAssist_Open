import uuid
import time
from fastapi import APIRouter, HTTPException, Request, Query, Response
from server.models import SessionCreate, SessionRename
from server.routes.auth import get_current_user
from server.database import (
    create_session as db_create_session,
    get_session as db_get_session,
    list_sessions as db_list_sessions,
    rename_session as db_rename_session,
    delete_session as db_delete_session,
    search_sessions as db_search_sessions,
)
from server.trust_store import trust_store

router = APIRouter(prefix="/api/sessions", tags=["sessions"])

# 安全限制
MAX_TITLE_LENGTH = 200  # 会话标题最大字符数


@router.get("", response_model=list[dict])
def list_sessions(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    return db_list_sessions(user["id"])


@router.get("/search", response_model=list[dict])
def search_sessions(request: Request, q: str = Query(..., min_length=1)):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    return db_search_sessions(user["id"], q.strip())


@router.post("", response_model=dict)
def create_session(body: SessionCreate = None, request: Request = None):
    user = get_current_user(request) if request else None
    user_id = user["id"] if user else 1
    sid = str(uuid.uuid4())
    title = (body.title if body and body.title else None) or "新对话"
    # 标题长度限制
    if len(title) > MAX_TITLE_LENGTH:
        raise HTTPException(status_code=400, detail=f"标题过长，最大允许 {MAX_TITLE_LENGTH} 字符")
    s = db_create_session(sid, user_id, title)
    return {"id": s["id"], "title": s["title"], "created_at": s["created_at"]}


@router.get("/{session_id}", response_model=dict)
def get_session(session_id: str, response: Response):
    s = db_get_session(session_id)
    if not s:
        raise HTTPException(status_code=404, detail="会话不存在")
    messages = s.get("messages", [])
    import logging
    log = logging.getLogger("chat")
    for i, m in enumerate(messages):
        if m.get("role") == "assistant":
            log.info(f"[LOAD] session={session_id} msg[{i}] role={m['role']} has_thought={bool(m.get('thought'))} has_tools={bool(m.get('tools'))} thought_len={len(m.get('thought',''))} content_len={len(m.get('content',''))}")
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return {
        "id": s["id"],
        "title": s.get("title", "new conversation"),
        "created_at": s.get("created_at", 0),
        "messages": messages,
    }


@router.put("/{session_id}", response_model=dict)
def rename_session(session_id: str, body: SessionRename):
    if len(body.title) > MAX_TITLE_LENGTH:
        raise HTTPException(status_code=400, detail=f"标题过长，最大允许 {MAX_TITLE_LENGTH} 字符")
    s = db_rename_session(session_id, body.title)
    if not s:
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"id": s["id"], "title": s["title"]}


@router.delete("/{session_id}", response_model=dict)
def delete_session(session_id: str):
    if not db_delete_session(session_id):
        raise HTTPException(status_code=404, detail="会话不存在")
    # 清理会话的信任模式状态
    try:
        trust_store.clear(session_id)
    except Exception:
        pass
    # 清理任务注册表中对应的运行中任务
    try:
        from server.routes.chat import remove_running_task
        remove_running_task(session_id)
    except Exception:
        pass
    return {"success": True, "message": "会话已删除"}


@router.get("/task-status/all")
def get_task_status(request: Request):
    """获取当前用户所有运行中/最近完成的任务状态"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    try:
        from server.routes.chat import get_all_task_statuses
        return get_all_task_statuses(user["id"])
    except Exception:
        return {}