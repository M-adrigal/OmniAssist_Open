import os
import shutil
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


def _load_owned_session(session_id: str, user: dict) -> dict:
    """取会话并校验归属。

    会话 ID 是 UUID，但「猜不到」不等于「有权限」——
    任何登录用户拿到别人的 ID 都不该能读写。统一返回 404（而非 403），
    避免把「这个 ID 存在」这个事实泄漏出去。
    """
    s = db_get_session(session_id)
    if not s or s.get("user_id") != user["db_id"]:
        raise HTTPException(status_code=404, detail="会话不存在")
    return s


@router.get("", response_model=list[dict])
def list_sessions(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    return db_list_sessions(user["db_id"])


@router.get("/search", response_model=list[dict])
def search_sessions(request: Request, q: str = Query(..., min_length=1)):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    return db_search_sessions(user["db_id"], q.strip())


@router.post("", response_model=dict)
def create_session(body: SessionCreate = None, request: Request = None):
    user = get_current_user(request) if request else None
    user_id = user["db_id"] if user else 1
    sid = str(uuid.uuid4())
    title = (body.title if body and body.title else None) or "新对话"
    # 标题长度限制
    if len(title) > MAX_TITLE_LENGTH:
        raise HTTPException(status_code=400, detail=f"标题过长，最大允许 {MAX_TITLE_LENGTH} 字符")
    s = db_create_session(sid, user_id, title)
    return {"id": s["id"], "title": s["title"], "created_at": s["created_at"]}


@router.get("/{session_id}", response_model=dict)
def get_session(session_id: str, response: Response, request: Request = None):
    user = get_current_user(request) if request else None
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    s = _load_owned_session(session_id, user)
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
        "model_entry_id": s.get("model_entry_id"),
        "messages": messages,
    }


@router.put("/{session_id}/model", response_model=dict)
def switch_session_model(session_id: str, body: dict, request: Request):
    """切换本会话使用的模型。

    只写「会话 → 模型条目」的指向，不改用户默认模型：
    用户在这个会话里试一下 A，不该影响他所有新对话。
    entry_id 传 null 表示回落到用户默认 / 平台默认。
    """
    from server import model_store as ms
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    _load_owned_session(session_id, user)

    entry_id = body.get("entry_id")
    if entry_id is not None:
        try:
            entry_id = int(entry_id)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="entry_id 不合法")
        entry = ms.get_entry(entry_id, owner_user_id=user["db_id"])
        if not entry:
            raise HTTPException(status_code=404, detail="模型不存在或无权使用")
        if not entry.get("enabled"):
            raise HTTPException(status_code=400, detail="该模型已停用")

    if not ms.set_session_entry(session_id, entry_id):
        raise HTTPException(status_code=404, detail="会话不存在")

    from server.database import resolve_model_config
    cfg = resolve_model_config(user["db_id"], session_id=session_id)
    return {
        "success": True,
        "model_entry_id": entry_id,
        "resolved": {
            "entry_id": cfg.get("entry_id"),
            "display_name": cfg.get("display_name") or cfg.get("model_id") or "",
            "model_id": cfg.get("model_id", ""),
            "provider_name": cfg.get("provider_name", ""),
            "owner": cfg.get("entry_owner", ""),
        },
    }


@router.put("/{session_id}", response_model=dict)
def rename_session(session_id: str, body: SessionRename, request: Request = None):
    user = get_current_user(request) if request else None
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    _load_owned_session(session_id, user)
    if len(body.title) > MAX_TITLE_LENGTH:
        raise HTTPException(status_code=400, detail=f"标题过长，最大允许 {MAX_TITLE_LENGTH} 字符")
    s = db_rename_session(session_id, body.title)
    if not s:
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"id": s["id"], "title": s["title"]}


@router.delete("/{session_id}", response_model=dict)
def delete_session(session_id: str, request: Request = None):
    user = get_current_user(request) if request else None
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    _load_owned_session(session_id, user)
    if not db_delete_session(session_id):
        raise HTTPException(status_code=404, detail="会话不存在")

    # 连带删除该会话上传的图片等附件。
    # 图片只存于磁盘（会话历史里没有副本），会话删了不清理就会变成孤儿文件：
    # 用户以为删干净了，实际还躺在服务器上。
    try:
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        upload_dir = os.path.join(project_root, "document_output", str(user["id"]), "uploads", session_id)
        # 归属校验必须以「本人目录」为界，只判「在 document_output 内」不够：
        # 若该目录被替换成指向他人产出目录的符号链接，rmtree 会顺着链接删掉他人整个目录。
        user_root = os.path.realpath(os.path.join(project_root, "document_output", str(user["id"])))
        if os.path.islink(upload_dir):
            # 是链接就只摘链接，绝不递归删链接目标
            os.unlink(upload_dir)
        else:
            real_dir = os.path.realpath(upload_dir)
            if real_dir.startswith(user_root + os.sep) and os.path.isdir(real_dir):
                shutil.rmtree(real_dir)
    except Exception as e:
        # 清理失败不阻断会话删除（会话已删成功），但要留痕便于排查
        import logging
        logging.getLogger(__name__).warning(f"[SESSION] 删除会话附件失败 session={session_id}: {e}")

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
        # task.user_id 存的是整数 db_id，必须传 user["db_id"] 而非 public_id
        return get_all_task_statuses(user["db_id"])
    except Exception:
        return {}