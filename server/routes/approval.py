"""敏感操作审批端点。

POST /api/chat/{session_id}/approve
Body: {"group_id": "...", "decisions": [{"item_id": "...", "decision": "approve"|"reject"}, ...]}

- 校验会话归属（task.user_id == 当前用户），防越权确认他人会话。
- group 不存在/已过期 -> 409。
- 决议写入 approval_store，唤醒等待中的 Agent 循环。
- 每次审批写入 logs/audit.log（可追溯）。
"""
import logging
import os
from datetime import datetime

from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel

from server.approval_store import approval_store
from server.trust_store import trust_store, audit_log
from server.routes.auth import get_current_user
from server.database import get_session

router = APIRouter(prefix="/api/chat", tags=["approval"])
log = logging.getLogger("approval")


class DecisionItem(BaseModel):
    item_id: str
    decision: str  # "approve" | "reject"


class ApproveBody(BaseModel):
    group_id: str
    decisions: list[DecisionItem]


def _audit(session_id: str, group_id: str, user_id: int, decisions: dict):
    try:
        os.makedirs("logs", exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = (
            f"[{ts}] APPROVAL session={session_id} group={group_id} "
            f"user={user_id} decisions={decisions}\n"
        )
        with open("logs/audit.log", "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


@router.post("/{session_id}/approve")
async def approve(session_id: str, body: ApproveBody, request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")

    # 会话归属校验：只能确认自己会话里的请求
    from server.routes.chat import get_running_task
    task = get_running_task(session_id)
    if not task:
        raise HTTPException(status_code=404, detail="没有正在运行的任务")
    if task.user_id != user["id"]:
        raise HTTPException(status_code=403, detail="无权操作该会话")

    decisions = {d.item_id: d.decision for d in body.decisions}
    try:
        ok = approval_store.resolve(body.group_id, decisions, session_id, user["id"])
    except KeyError:
        raise HTTPException(status_code=409, detail="该确认请求已过期或不存在")
    except PermissionError:
        raise HTTPException(status_code=403, detail="无权操作该确认请求")

    if not ok:
        raise HTTPException(status_code=409, detail="该确认请求已过期")

    _audit(session_id, body.group_id, user["id"], decisions)
    log.info(f"审批已提交 session={session_id} group={body.group_id} user={user['id']} decisions={decisions}")
    return {"success": True}


class TrustBody(BaseModel):
    enabled: bool


def _check_session_owner(session_id: str, user: dict) -> dict:
    """校验当前用户对该会话的归属；返回会话记录，否则抛 HTTP 异常。"""
    s = get_session(session_id)
    if not s:
        raise HTTPException(status_code=404, detail="会话不存在")
    if s.get("user_id") != user["id"] and user.get("user_type") != "admin":
        raise HTTPException(status_code=403, detail="无权操作该会话")
    return s


@router.get("/{session_id}/trust")
def get_trust(session_id: str, request: Request):
    """查询该会话的信任模式状态。"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    _check_session_owner(session_id, user)
    return trust_store.get(session_id, user["id"])


@router.post("/{session_id}/trust")
def set_trust(session_id: str, body: TrustBody, request: Request):
    """开启/关闭该会话的信任模式（敏感操作免确认）。"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    _check_session_owner(session_id, user)
    ok = trust_store.set(session_id, user["id"], body.enabled)
    if not ok:
        raise HTTPException(status_code=409, detail="信任状态更新冲突，请刷新后重试")
    audit_log("TRUST", session=session_id, user=user["id"], enabled=body.enabled)
    log.info(f"会话信任模式变更 session={session_id} user={user['id']} enabled={body.enabled}")
    return {"success": True, "enabled": body.enabled}
