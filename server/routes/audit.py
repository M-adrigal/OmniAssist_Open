"""Agent 任务执行审计查询接口（管理员限定）。

用途：事后排查"Agent 到底做了什么、用户怎么批的"。
此前只能翻 app.log 的散行反推，工具调用的参数与结果没有落盘，
导致"Agent 到底删了哪个技能"这类问题无法确认。
本接口把 server/agent_audit.py 记录的轨迹暴露出来。

权限：仅 admin。普通用户返回 403。
"""

from fastapi import APIRouter, HTTPException, Query, Request

from server.routes.auth import get_current_user
from server.agent_audit import agent_audit

router = APIRouter(prefix="/api/audit", tags=["audit"])


def _require_admin(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    if user.get("user_type") != "admin":
        raise HTTPException(status_code=403, detail="权限不足：审计查询仅限管理员")
    return user


@router.get("/session/{session_id}")
def audit_session(session_id: str, request: Request,
                  limit: int = Query(500, ge=1, le=5000)):
    """单个会话的完整执行轨迹（对话轮次 / 工具调用 / 审批决策，按时间排序）。"""
    _require_admin(request)
    items = agent_audit.session_timeline(session_id)
    if len(items) > limit:
        items = items[:limit]
    return {"session_id": session_id, "count": len(items), "items": items}


@router.get("/events")
def audit_events(request: Request,
                 session_id: str = None,
                 user_id: int = Query(None),
                 type: str = Query(None, description="按事件类型过滤：turn/tool/approval/tool_deferred"),
                 tail: int = Query(200, ge=1, le=2000),
                 since: float = Query(None, description="只看该时间戳之后的事件")):
    """按条件查询审计事件（默认返回最近 N 条）。"""
    _require_admin(request)
    types = [t.strip() for t in type.split(",")] if type else None
    items = agent_audit.query(
        session_id=session_id,
        user_id=user_id,
        types=types,
        since_ts=since,
        limit=tail,
        tail=True,
    )
    return {"count": len(items), "items": items}


@router.get("/summary")
def audit_summary(request: Request, session_id: str = None):
    """一个会话的审计概览：各类型事件计数、工具调用统计、审批统计。"""
    _require_admin(request)
    items = agent_audit.session_timeline(session_id) if session_id else agent_audit.query(limit=5000)

    by_type = {}
    tool_stats = {}
    approvals = {"requested": 0, "decided": 0, "skipped": 0}
    for it in items:
        t = it.get("type")
        by_type[t] = by_type.get(t, 0) + 1
        if t == "tool":
            name = it.get("tool") or "?"
            s = tool_stats.setdefault(name, {"count": 0, "errors": 0})
            s["count"] += 1
            if it.get("error"):
                s["errors"] += 1
        elif t == "approval":
            ev = it.get("event")
            if ev == "requested":
                approvals["requested"] += 1
            elif ev == "decided":
                if it.get("skipped_reason"):
                    approvals["skipped"] += 1
                else:
                    approvals["decided"] += 1
    return {
        "session_id": session_id,
        "total": len(items),
        "by_type": by_type,
        "tools": tool_stats,
        "approvals": approvals,
    }
