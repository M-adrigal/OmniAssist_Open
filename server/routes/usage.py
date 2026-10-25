"""Token 消耗统计接口（按用户隔离；管理员可看全平台）。

口径说明：
- total_tokens 一律取服务商回传的 usage；is_estimated=1 的记录是兜底估算，
  真实占比在接口里用 estimated_calls / calls 给出，前端需明确标注，避免误当账单。
"""

from datetime import date, timedelta

from fastapi import APIRouter, HTTPException, Query, Request

from server.routes.auth import get_current_user
from server import usage_store

router = APIRouter(prefix="/api/usage", tags=["usage"])


def _resolve_scope(request: Request, all_users: bool):
    """确定查询范围：默认只看自己；管理员可显式请求全平台。"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    scope_all = bool(all_users) and user.get("user_type") == "admin"
    return user, scope_all


def _parse_range(days: int = None, start: str = None, end: str = None):
    """解析时间范围。start/end 优先级高于 days。"""
    today = date.today()
    if start or end:
        try:
            e = date.fromisoformat(end) if end else today
            s = date.fromisoformat(start) if start else e
        except ValueError:
            raise HTTPException(status_code=400, detail="日期格式应为 YYYY-MM-DD")
        if s > e:
            s, e = e, s
        # 防御：单次查询跨度不超过 366 天
        if (e - s).days > 366:
            raise HTTPException(status_code=400, detail="查询跨度不能超过 366 天")
        return s.isoformat(), e.isoformat()

    try:
        n = int(days or 30)
    except (TypeError, ValueError):
        n = 30
    n = max(1, min(366, n))
    return (today - timedelta(days=n - 1)).isoformat(), today.isoformat()


@router.get("/summary")
def usage_summary(request: Request,
                  days: int = Query(30, ge=1, le=366),
                  start: str = None, end: str = None,
                  all_users: int = 0):
    """按天聚合的柱状图数据 + 总计 + 来源拆分 + 中断/异常任务统计。"""
    user, scope_all = _resolve_scope(request, all_users)
    s, e = _parse_range(days, start, end)
    data = usage_store.summary_by_day(user["db_id"], s, e, scope_all=scope_all)
    data["start"], data["end"] = s, e
    data["scope"] = "all" if scope_all else "self"
    # 中断/异常统计固定看近 7 天（与查询范围解耦：它回答的是「服务稳不稳」，
    # 不是「这 30 天用了多少」）。全为 0 时前端不显示，不制造噪音。
    data["pending"] = usage_store.pending_stats(
        user["db_id"], days=7, scope_all=scope_all
    )
    return data


@router.get("/sessions")
def usage_sessions(request: Request,
                   start: str = None, end: str = None,
                   days: int = Query(30, ge=1, le=366),
                   page: int = Query(1, ge=1),
                   size: int = Query(20, ge=1, le=200),
                   q: str = Query(None, max_length=100),
                   all_users: int = 0):
    """按会话聚合的分页明细。q 为会话标题关键字（模糊匹配）。"""
    user, scope_all = _resolve_scope(request, all_users)
    s, e = _parse_range(days, start, end)
    data = usage_store.list_session_usage(
        user["db_id"], s, e, page=page, size=size, scope_all=scope_all, q=q
    )
    data["start"], data["end"] = s, e
    data["q"] = (q or "").strip()
    # 模型 ID → 可读显示名。账单仍以 model ID 为准，这里只做展示映射。
    data["model_labels"] = usage_store.model_label_map()
    return data


@router.get("/session/{session_id}")
def usage_session_detail(session_id: str, request: Request, all_users: int = 0):
    """单个会话的逐次调用明细。"""
    user, scope_all = _resolve_scope(request, all_users)
    rows = usage_store.session_detail(
        user["db_id"], session_id, scope_all=scope_all
    )
    return {"session_id": session_id, "items": rows}
