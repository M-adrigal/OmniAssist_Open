import uuid
import time
from fastapi import APIRouter, HTTPException
from server.models import SessionCreate, SessionRename

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


def get_session_store():
    from server.main import get_session_store as gss
    return gss()


@router.get("", response_model=list[dict])
def list_sessions():
    store = get_session_store()
    sessions = []
    for sid, data in store.items():
        sessions.append({
            "id": sid,
            "title": data.get("title", "新对话"),
            "created_at": data.get("created_at", 0),
            "message_count": len(data.get("messages", [])),
        })
    sessions.sort(key=lambda s: s["created_at"], reverse=True)
    return sessions


@router.post("", response_model=dict)
def create_session(body: SessionCreate = None):
    store = get_session_store()
    sid = str(uuid.uuid4())
    title = (body.title if body and body.title else None) or "新对话"
    store[sid] = {
        "title": title,
        "created_at": time.time(),
        "messages": [],
    }
    return {"id": sid, "title": title, "created_at": store[sid]["created_at"]}


@router.get("/{session_id}", response_model=dict)
def get_session(session_id: str):
    store = get_session_store()
    if session_id not in store:
        raise HTTPException(status_code=404, detail="会话不存在")
    data = store[session_id]
    return {
        "id": session_id,
        "title": data.get("title", "新对话"),
        "created_at": data.get("created_at", 0),
        "messages": data.get("messages", []),
    }


@router.put("/{session_id}", response_model=dict)
def rename_session(session_id: str, body: SessionRename):
    store = get_session_store()
    if session_id not in store:
        raise HTTPException(status_code=404, detail="会话不存在")
    store[session_id]["title"] = body.title
    return {"id": session_id, "title": body.title}


@router.delete("/{session_id}", response_model=dict)
def delete_session(session_id: str):
    store = get_session_store()
    if session_id not in store:
        raise HTTPException(status_code=404, detail="会话不存在")
    del store[session_id]
    return {"success": True, "message": "会话已删除"}