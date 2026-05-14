import uuid
import time
from fastapi import APIRouter, HTTPException, Request, Query
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

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


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
    s = db_create_session(sid, user_id, title)
    return {"id": s["id"], "title": s["title"], "created_at": s["created_at"]}


@router.get("/{session_id}", response_model=dict)
def get_session(session_id: str):
    s = db_get_session(session_id)
    if not s:
        raise HTTPException(status_code=404, detail="会话不存在")
    return {
        "id": s["id"],
        "title": s.get("title", "新对话"),
        "created_at": s.get("created_at", 0),
        "messages": s.get("messages", []),
    }


@router.put("/{session_id}", response_model=dict)
def rename_session(session_id: str, body: SessionRename):
    s = db_rename_session(session_id, body.title)
    if not s:
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"id": s["id"], "title": s["title"]}


@router.delete("/{session_id}", response_model=dict)
def delete_session(session_id: str):
    if not db_delete_session(session_id):
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"success": True, "message": "会话已删除"}