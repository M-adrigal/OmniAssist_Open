"""Token 用量记录与统计查询。

数据来源分两种，落库时以 is_estimated 区分：

- **精确轨**：服务商在响应里回传的 usage（prompt/completion/cached/reasoning tokens），
  与它的计费口径一致，可用于成本核算与账单对账。`is_estimated = 0`。
- **估算轨**：上游未回传 usage（网关不支持、流式中断、客户端断连）时按字符估算。
  `is_estimated = 1`，仅用于「不要记成 0」，不参与成本结论。

一次模型调用落一行（而不是一个会话一行），这样才拆得出「消耗来源」：
用户可见的对话 / 用户看不见的标题生成与上下文压缩 / 子 Agent。

三个「特征值」字段与两条轨的关系
--------------------------------
`prompt_chars` / `completion_chars` / `image_count` **两条轨都写**，而不是只在
估算时记。原因：精确轨的每一行天然就是一个训练样本 —— 它同时有「我们算出来的
字符数」和「服务商认定的真实 token 数」，两者配对即可回归出本项目自己的折算系数，
替换掉 `estimate_tokens` 里那个拍脑袋的经验值。详见 scripts/calibrate_tokens.py --fit。

只存数值、**绝不存原文**：原文里可能混着密钥、他人的会话内容，落库等于给数据
多开一份副本并放大泄露半径。
"""

from datetime import date, datetime, timedelta
from typing import Optional

from agent.logger import get_logger

logger = get_logger("usage")

# 用量来源标签（与 LLMClient 的 source 参数一致）
SOURCE_LABELS = {
    "chat": "对话",
    "title": "标题生成",
    "compress": "上下文压缩",
    "subagent": "子 Agent",
    "skill": "技能执行",
    "memory": "记忆整理",
}

_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS token_usage (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id           INTEGER NOT NULL,
    session_id        TEXT,
    run_id            TEXT,
    source            TEXT NOT NULL DEFAULT 'chat',
    model             TEXT DEFAULT '',
    provider_name     TEXT DEFAULT '',
    entry_owner       TEXT DEFAULT '',
    prompt_tokens     INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    cached_tokens     INTEGER DEFAULT 0,
    reasoning_tokens  INTEGER DEFAULT 0,
    total_tokens      INTEGER DEFAULT 0,
    is_estimated      INTEGER DEFAULT 0,
    duration_ms       INTEGER,
    prompt_chars      INTEGER DEFAULT 0,
    completion_chars  INTEGER DEFAULT 0,
    image_count       INTEGER DEFAULT 0,
    created_at        TEXT NOT NULL
)
"""

_INDEX_DDL = [
    "CREATE INDEX IF NOT EXISTS idx_usage_user_time ON token_usage(user_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_usage_session ON token_usage(session_id)",
    "CREATE INDEX IF NOT EXISTS idx_usage_created ON token_usage(created_at)",
]


def ensure_table(conn) -> None:
    """建表 + 建索引（幂等，可在启动时反复调用）。"""
    conn.execute(_TABLE_DDL)
    for ddl in _INDEX_DDL:
        conn.execute(ddl)
    # 多模型改造新增：记下「这笔消耗走的是哪家供应商、谁的 key」，
    # 这是区分「平台成本」与「用户自付」的唯一依据（display_name 会变，不能当账本）。
    # 校准改造新增：文本长度与图片数，供「用自身数据回归折算系数」使用（两条轨都写）。
    for ddl in (
        "ALTER TABLE token_usage ADD COLUMN provider_name TEXT DEFAULT ''",
        "ALTER TABLE token_usage ADD COLUMN entry_owner TEXT DEFAULT ''",
        "ALTER TABLE token_usage ADD COLUMN prompt_chars INTEGER DEFAULT 0",
        "ALTER TABLE token_usage ADD COLUMN completion_chars INTEGER DEFAULT 0",
        "ALTER TABLE token_usage ADD COLUMN image_count INTEGER DEFAULT 0",
    ):
        try:
            conn.execute(ddl)
        except Exception:
            pass
    ensure_pending_table(conn)
    conn.commit()


# ===== 任务中断/异常对账（pending 记账，2026-10-24）=====
# 动机：服务重启/进程被杀时，正在跑的任务没机会记账 —— 它的 token 凭空消失，
# 看板「一切正常」但实际有消耗没入账。用一张独立的轻表跟踪任务生命周期：
#   任务创建 → mark_pending（status=0）
#   任务终结（完成/失败/取消，_run_chat_background 的 finally）→ finalize_pending（status=1）
#   启动对账 → 未终结且超过 stale 分钟的判「中断」（status=2）；
#              已终结但该会话在账本表里一行都没有的判「异常」（status=3）
# 为什么不给 token_usage 加 status 列：账本表的每一行语义是「一次真实调用」，
# 混进「没记账的任务」会让所有聚合查询都要多带一个过滤条件，收益不大风险不小。
# 独立表 + 启动对账，账本表零改动。

_PENDING_DDL = """
CREATE TABLE IF NOT EXISTS usage_pending (
    session_id  TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL,
    run_id      TEXT,
    status      INTEGER DEFAULT 0,   -- 0 未终结 / 1 已正常终结 / 2 中断 / 3 异常
    created_at  TEXT NOT NULL,
    finished_at TEXT
)
"""

PENDING_RUNNING = 0
PENDING_FINISHED = 1
PENDING_INTERRUPTED = 2
PENDING_ORPHAN = 3

# 未终结超过这么久才判「中断」：任务可能只是跑得慢（长文档/多轮工具），
# 15 分钟是「正常任务不可能跑这么久」的经验下限，宁肯漏判也不误判。
PENDING_STALE_MINUTES = 15


def ensure_pending_table(conn) -> None:
    conn.execute(_PENDING_DDL)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pending_status ON usage_pending(status)")


def mark_pending(session_id: str, user_id: int, run_id: str = None) -> bool:
    """任务创建时登记（幂等：重同一会话重复创建只更新时间）。"""
    if not session_id or not user_id:
        return False
    try:
        conn = _conn()
        conn.execute(
            "INSERT INTO usage_pending (session_id, user_id, run_id, status, created_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET user_id = excluded.user_id, "
            "run_id = excluded.run_id, status = 0, created_at = excluded.created_at",
            (session_id, int(user_id), run_id, PENDING_RUNNING, _now_str()),
        )
        conn.commit()
        return True
    except Exception as e:
        logger.debug(f"pending 登记失败（不影响对话）: {e}")
        return False


def finalize_pending(session_id: str) -> bool:
    """任务终结（完成/失败/取消都算）—— 只要跑了 finally 就不是「中断」。"""
    if not session_id:
        return False
    try:
        conn = _conn()
        conn.execute(
            "UPDATE usage_pending SET status = ?, finished_at = ? "
            "WHERE session_id = ? AND status = ?",
            (PENDING_FINISHED, _now_str(), session_id, PENDING_RUNNING),
        )
        conn.commit()
        return True
    except Exception as e:
        logger.debug(f"pending 终结登记失败: {e}")
        return False


def reconcile_pending(stale_minutes: int = None) -> dict:
    """启动对账：把「上次进程死掉时没终结的任务」标出来。

    两类：
      status=2 中断：任务创建后再没终结（进程被杀/崩溃），token 大概率没记账；
      status=3 异常：任务正常终结了，但该会话在账本表里一行都没有 ——
                     记错了/记丢了，比中断更严重（它是静默的）。

    Returns: {"interrupted": [...], "orphan": [...], "scanned": N}
    """
    minutes = PENDING_STALE_MINUTES if stale_minutes is None else max(1, int(stale_minutes))
    result = {"interrupted": [], "orphan": [], "scanned": 0}
    try:
        conn = _conn()
        cutoff = (datetime.now() - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")
        rows = conn.execute(
            "SELECT session_id, user_id, created_at FROM usage_pending "
            "WHERE status = ? AND created_at < ?",
            (PENDING_RUNNING, cutoff),
        ).fetchall()
        result["scanned"] = len(rows)
        for r in rows:
            conn.execute(
                "UPDATE usage_pending SET status = ?, finished_at = ? WHERE session_id = ?",
                (PENDING_INTERRUPTED, _now_str(), r["session_id"]),
            )
            result["interrupted"].append(r["session_id"])
        # 异常：已终结但账本无行
        orphans = conn.execute(
            "SELECT p.session_id, p.user_id FROM usage_pending p "
            "WHERE p.status = ? AND NOT EXISTS ("
            "  SELECT 1 FROM token_usage u WHERE u.session_id = p.session_id)"
            " AND p.created_at >= ?",
            (PENDING_FINISHED,
             (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")),
        ).fetchall()
        for r in orphans:
            conn.execute(
                "UPDATE usage_pending SET status = ? WHERE session_id = ?",
                (PENDING_ORPHAN, r["session_id"]),
            )
            result["orphan"].append(r["session_id"])
        conn.commit()
        if result["interrupted"] or result["orphan"]:
            logger.warning(
                f"用量对账：中断 {len(result['interrupted'])} 个任务，"
                f"异常（终结但无账） {len(result['orphan'])} 个"
            )
    except Exception as e:
        logger.warning(f"用量对账失败（不影响启动）: {e}")
    return result


def pending_stats(user_id: int = None, days: int = 7,
                  scope_all: bool = False) -> dict:
    """近 N 天的中断/异常统计（看板展示用）。"""
    try:
        conn = _conn()
        since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        clauses = ["created_at >= ?"]
        params = [since]
        if not scope_all:
            clauses.append("user_id = ?")
            params.append(int(user_id))
        where = " AND ".join(clauses)
        row = conn.execute(
            f"SELECT "
            f"SUM(CASE WHEN status = {PENDING_INTERRUPTED} THEN 1 ELSE 0 END) interrupted, "
            f"SUM(CASE WHEN status = {PENDING_ORPHAN} THEN 1 ELSE 0 END) orphan, "
            f"COUNT(*) total "
            f"FROM usage_pending WHERE {where}",
            params,
        ).fetchone()
        return {
            "interrupted": int((row["interrupted"] or 0) if row else 0),
            "orphan": int((row["orphan"] or 0) if row else 0),
            "total": int((row["total"] or 0) if row else 0),
        }
    except Exception as e:
        logger.debug(f"pending 统计失败: {e}")
        return {"interrupted": 0, "orphan": 0, "total": 0}


def _conn():
    from server.database import _get_connection
    return _get_connection()


def cache_rate(cached_tokens: int, prompt_exact: int):
    """缓存命中率（%），分母只取服务商回传 usage 的输入 token。

    两个口径要点：
    1. 估算行（is_estimated=1）没有缓存概念，cached 恒为 0，若计入分母会把命中率
       整体拉低，因此分子分母都只看精确行。
    2. cached_tokens 是 prompt_tokens 的子集（命中前缀缓存的那部分），
       所以命中率 = cached / prompt，不是 cached / (prompt + cached)。
    分母为 0 时返回 None，前端显示「—」，避免把「没有数据」显示成「0%」。
    """
    if not prompt_exact:
        return None
    return round(int(cached_tokens) / int(prompt_exact) * 100, 1)


def _now_str() -> str:
    """本地时间字符串。

    注意：SQLite 的 CURRENT_TIMESTAMP 是 UTC，而本项目其余表统一写入本地时间，
    这里必须显式写本地时间，否则看板的「按天」会整体偏 8 小时。
    """
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def record_usage(payload: dict, user_id: int, session_id: str = None,
                 run_id: str = None, provider_name: str = "",
                 entry_owner: str = "") -> bool:
    """写入一条用量记录。失败只记日志，绝不影响主流程。"""
    if not user_id:
        return False
    try:
        conn = _conn()
        conn.execute(
            "INSERT INTO token_usage (user_id, session_id, run_id, source, model, "
            "provider_name, entry_owner, "
            "prompt_tokens, completion_tokens, cached_tokens, reasoning_tokens, "
            "total_tokens, is_estimated, duration_ms, "
            "prompt_chars, completion_chars, image_count, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                int(user_id),
                session_id,
                run_id,
                (payload.get("source") or "chat")[:32],
                (payload.get("model") or "")[:120],
                (provider_name or "")[:60],
                (entry_owner or "")[:16],
                int(payload.get("prompt_tokens") or 0),
                int(payload.get("completion_tokens") or 0),
                int(payload.get("cached_tokens") or 0),
                int(payload.get("reasoning_tokens") or 0),
                int(payload.get("total_tokens") or 0),
                1 if payload.get("is_estimated") else 0,
                payload.get("duration_ms"),
                # 特征值：精确轨也写，用作日后回归折算系数的样本（见模块 docstring）
                int(payload.get("prompt_chars") or 0),
                int(payload.get("completion_chars") or 0),
                int(payload.get("image_count") or 0),
                _now_str(),
            ),
        )
        conn.commit()
        return True
    except Exception as e:
        logger.warning(f"token 用量落库失败（已忽略，不影响对话）: {e}")
        return False


def make_sink(user_id: int, session_id: str = None, run_id: str = None,
              provider_name: str = "", entry_owner: str = ""):
    """构造绑定到「本次请求」的用量回传函数，赋给 LLMClient.usage_sink。

    每次对话请求都新建 LLMClient，因此 sink 天然按请求隔离，不会串到别的会话。
    provider_name / entry_owner 在 sink 构造时固化 —— 它们是「这笔账算谁的」的依据，
    必须与实际调用所用的模型一致，不能事后去查（用户可能已切换或删除模型）。
    """
    def _sink(payload: dict):
        record_usage(payload, user_id=user_id, session_id=session_id, run_id=run_id,
                     provider_name=provider_name, entry_owner=entry_owner)
    return _sink


# ===== 查询 =====

def _range_clause(user_id: int = None, start: str = None, end: str = None,
                  scope_all: bool = False, alias: str = ""):
    """构造时间/归属过滤条件。alias 用于联表时加前缀（如 "u."）。"""
    p = f"{alias}." if alias else ""
    clauses = []
    params = []
    if not scope_all:
        clauses.append(f"{p}user_id = ?")
        params.append(int(user_id))
    if start:
        clauses.append(f"{p}created_at >= ?")
        params.append(f"{start} 00:00:00")
    if end:
        clauses.append(f"{p}created_at <= ?")
        params.append(f"{end} 23:59:59")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def summary_by_day(user_id: int, start: str, end: str, scope_all: bool = False) -> dict:
    """按天聚合（缺失的日期补 0，保证柱状图有完整的 N 根柱子）。"""
    where, params = _range_clause(user_id, start, end, scope_all)
    rows = _conn().execute(
        "SELECT substr(created_at, 1, 10) AS d, "
        "SUM(prompt_tokens) p, SUM(completion_tokens) c, SUM(cached_tokens) ca, "
        "SUM(reasoning_tokens) r, SUM(total_tokens) t, COUNT(*) n, "
        "SUM(is_estimated) est, "
        "SUM(CASE WHEN is_estimated = 0 THEN prompt_tokens ELSE 0 END) pe, "
        "SUM(CASE WHEN is_estimated = 0 THEN cached_tokens ELSE 0 END) cae, "
        "SUM(CASE WHEN is_estimated = 0 THEN total_tokens ELSE 0 END) te "
        f"FROM token_usage {where} GROUP BY d ORDER BY d",
        params,
    ).fetchall()

    # ---- 折算成本 ----
    # 单价跟着模型走，所以必须下探到「天 × 模型 × 供应商」这一层才能算钱；
    # 只按天聚合会丢掉「谁花的钱」这一维，加总出来的是一个没有意义的数字。
    # 顺带这一层也给出模型维度的成本，避免再查一次。
    cost_by_day: dict[str, float] = {}
    cost_by_model: dict[tuple, float] = {}
    sum_cost = 0.0
    unpriced_tokens = 0
    pmap = price_map()
    cost_rows = _conn().execute(
        "SELECT substr(created_at, 1, 10) AS d, model, provider_name, is_estimated, "
        "SUM(prompt_tokens) p, SUM(completion_tokens) c, SUM(cached_tokens) ca, "
        "SUM(total_tokens) t "
        f"FROM token_usage {where} GROUP BY d, model, provider_name, is_estimated",
        params,
    ).fetchall()
    for cr in cost_rows:
        # 只给精确轨算钱：估算轨的 token 本身就不准，折算出的金额更不可信。
        # 宁可少报并在界面注明「另有 N 次未计入」，也不要拿两个猜测相乘当结论。
        if int(cr["is_estimated"] or 0):
            continue
        prices = pmap.get(f"{cr['model'] or ''}||{cr['provider_name'] or ''}") \
            or pmap.get(cr["model"] or "")
        if prices is None:
            unpriced_tokens += int(cr["t"] or 0)
            continue
        amt = cost_of(int(cr["p"] or 0), int(cr["c"] or 0), int(cr["ca"] or 0), prices)
        if amt is None:
            unpriced_tokens += int(cr["t"] or 0)
            continue
        cost_by_day[cr["d"]] = cost_by_day.get(cr["d"], 0.0) + amt
        key = (cr["model"] or "", cr["provider_name"] or "")
        cost_by_model[key] = cost_by_model.get(key, 0.0) + amt
        sum_cost += amt
    _unpriced_tokens = unpriced_tokens

    by_day = {r["d"]: dict(r) for r in rows}
    days = []
    d0 = datetime.strptime(start, "%Y-%m-%d").date()
    d1 = datetime.strptime(end, "%Y-%m-%d").date()
    cur = d0
    while cur <= d1:
        key = cur.strftime("%Y-%m-%d")
        r = by_day.get(key)
        prompt_exact = int(r["pe"] or 0) if r else 0
        cached_exact = int(r["cae"] or 0) if r else 0
        days.append({
            "date": key,
            "prompt_tokens": int(r["p"] or 0) if r else 0,
            "completion_tokens": int(r["c"] or 0) if r else 0,
            "cached_tokens": int(r["ca"] or 0) if r else 0,
            "reasoning_tokens": int(r["r"] or 0) if r else 0,
            "total_tokens": int(r["t"] or 0) if r else 0,
            "calls": int(r["n"] or 0) if r else 0,
            "estimated_calls": int(r["est"] or 0) if r else 0,
            "prompt_exact": prompt_exact,
            "cached_exact": cached_exact,
            "cache_rate": cache_rate(cached_exact, prompt_exact),
            # 双轨分离：把「服务商认定的量」与「我们补的数量」拆开列，
            # 混在一起会让读者分不清哪个数能拿去对账。
            "total_exact": (int(r["te"] or 0) if r else 0),
            "cost": cost_by_day.get(key, 0.0),
        })
        cur += timedelta(days=1)

    total_prompt_exact = sum(x["prompt_exact"] for x in days)
    total_cached_exact = sum(x["cached_exact"] for x in days)
    total = {
        "prompt_tokens": sum(x["prompt_tokens"] for x in days),
        "completion_tokens": sum(x["completion_tokens"] for x in days),
        "cached_tokens": sum(x["cached_tokens"] for x in days),
        "reasoning_tokens": sum(x["reasoning_tokens"] for x in days),
        "total_tokens": sum(x["total_tokens"] for x in days),
        "calls": sum(x["calls"] for x in days),
        "estimated_calls": sum(x["estimated_calls"] for x in days),
        "prompt_exact": total_prompt_exact,
        "cached_exact": total_cached_exact,
        "cache_rate": cache_rate(total_cached_exact, total_prompt_exact),
        # 精确轨的量：可以拿去和服务商账单对的那个数
        "total_exact": sum(x["total_exact"] for x in days),
        # 金额只按精确轨折算，估算那部分再多标 N 次也不混进来
        "cost": round(sum_cost, 4),
        # 因为「模型没标价」而算不出钱的精确轨 token —— 前端要用它说明为什么金额偏低
        "unpriced_tokens": _unpriced_tokens,
    }

    # 来源拆分（用于「钱花在哪」）
    src_rows = _conn().execute(
        "SELECT source, COUNT(*) n, SUM(total_tokens) t, "
        "SUM(CASE WHEN is_estimated = 0 THEN prompt_tokens ELSE 0 END) pe, "
        "SUM(CASE WHEN is_estimated = 0 THEN cached_tokens ELSE 0 END) cae "
        f"FROM token_usage {where} GROUP BY source ORDER BY t DESC",
        params,
    ).fetchall()
    sources = [{
        "source": r["source"],
        "label": SOURCE_LABELS.get(r["source"], r["source"]),
        "calls": int(r["n"] or 0),
        "total_tokens": int(r["t"] or 0),
        "prompt_exact": int(r["pe"] or 0),
        "cache_rate": cache_rate(int(r["cae"] or 0), int(r["pe"] or 0)),
    } for r in src_rows]

    sessions_count = 0
    try:
        row = _conn().execute(
            f"SELECT COUNT(DISTINCT session_id) n FROM token_usage {where}",
            params,
        ).fetchone()
        sessions_count = int(row["n"] or 0) if row else 0
    except Exception:
        pass

    # 模型维度拆分：回答「token 花在哪家模型上、走的是谁的 Key」。
    # 自配模型（含用户自建供应商）与平台池模型可能同名，必须带上供应商区分；
    # entry_owner 区分平台成本（platform）与用户自付（user）。
    model_rows = _conn().execute(
        "SELECT model, provider_name, entry_owner, COUNT(*) n, "
        "SUM(total_tokens) t, SUM(prompt_tokens) p, SUM(completion_tokens) c, "
        "SUM(CASE WHEN is_estimated = 0 THEN prompt_tokens ELSE 0 END) pe, "
        "SUM(CASE WHEN is_estimated = 0 THEN cached_tokens ELSE 0 END) cae, "
        "SUM(is_estimated) est "
        f"FROM token_usage {where} "
        "GROUP BY model, provider_name, entry_owner ORDER BY t DESC",
        params,
    ).fetchall()
    models = [{
        "model": r["model"] or "（未知）",
        "provider_name": r["provider_name"] or "",
        # platform = 平台 Key（算平台成本）；user = 用户自带 Key（自付）
        "owner": r["entry_owner"] or "",
        "calls": int(r["n"] or 0),
        "total_tokens": int(r["t"] or 0),
        "prompt_tokens": int(r["p"] or 0),
        "completion_tokens": int(r["c"] or 0),
        "prompt_exact": int(r["pe"] or 0),
        "cache_rate": cache_rate(int(r["cae"] or 0), int(r["pe"] or 0)),
        "estimated_calls": int(r["est"] or 0),
        # 未标价模型的 cost 为 0 —— 但它是「没算」不是「没花」，前端须结合
        # overall 的 unpriced_tokens 提示，不能把 0 直接读成「免费」
        "cost": round(cost_by_model.get((r["model"] or "", r["provider_name"] or ""), 0.0), 4),
    } for r in model_rows]

    return {"days": days, "total": total, "sources": sources,
            "sessions": sessions_count, "models": models}


def list_session_usage(user_id: int, start: str, end: str, page: int = 1,
                       size: int = 20, scope_all: bool = False,
                       q: str = None) -> dict:
    """按会话聚合的分页明细。

    Args:
        q: 可选的会话标题关键字（模糊匹配）。标题存在 sessions 表，故用子查询先筛 id；
           会话被删除后标题已不可得，这类行不会命中关键字（属预期）。
    """
    page = max(1, int(page or 1))
    size = max(1, min(200, int(size or 20)))
    where, params = _range_clause(user_id, start, end, scope_all)
    where_u, params_u = _range_clause(user_id, start, end, scope_all, alias="u")
    conn = _conn()

    q = (q or "").strip()
    if q:
        # 转义 LIKE 通配符，避免用户输入的 % 把整表捞出来
        kw = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        title_clause = " AND session_id IN (SELECT id FROM sessions WHERE title LIKE ? ESCAPE '\\')"
        # where 可能为空串（scope_all 且未传时间范围），此时要补 WHERE 前缀
        where = (where + title_clause) if where else ("WHERE " + title_clause[5:])
        params = list(params) + [f"%{kw}%"]
        title_clause_u = title_clause.replace("session_id", "u.session_id")
        where_u = (where_u + title_clause_u) if where_u else ("WHERE " + title_clause_u[5:])
        params_u = list(params_u) + [f"%{kw}%"]

    total_row = conn.execute(
        f"SELECT COUNT(*) n FROM (SELECT session_id FROM token_usage {where} "
        "GROUP BY session_id)",
        params,
    ).fetchone()
    total = int(total_row["n"] or 0) if total_row else 0

    rows = conn.execute(
        "SELECT u.session_id AS session_id, "
        "MIN(u.created_at) AS first_at, MAX(u.created_at) AS last_at, "
        "SUM(u.prompt_tokens) AS p, SUM(u.completion_tokens) AS c, "
        "SUM(u.cached_tokens) AS ca, SUM(u.reasoning_tokens) AS r, "
        "SUM(u.total_tokens) AS t, COUNT(*) AS n, SUM(u.is_estimated) AS est, "
        "SUM(CASE WHEN u.is_estimated = 0 THEN u.prompt_tokens ELSE 0 END) AS pe, "
        "SUM(CASE WHEN u.is_estimated = 0 THEN u.cached_tokens ELSE 0 END) AS cae, "
        "MAX(u.model) AS model, "
        "(SELECT username FROM users WHERE users.id = u.user_id) AS username "
        f"FROM token_usage u {where_u} "
        "GROUP BY u.session_id ORDER BY last_at DESC LIMIT ? OFFSET ?",
        params_u + [size, (page - 1) * size],
    ).fetchall()

    items = []
    session_ids = [r["session_id"] for r in rows if r["session_id"]]
    titles = {}
    if session_ids:
        qmarks = ",".join("?" for _ in session_ids)
        try:
            for r in conn.execute(
                f"SELECT id, title FROM sessions WHERE id IN ({qmarks})", session_ids
            ).fetchall():
                titles[r["id"]] = r["title"]
        except Exception:
            pass

    # 每行的来源拆分（一次查询，避免 N+1）
    src_map = {}
    if session_ids:
        qmarks = ",".join("?" for _ in session_ids)
        try:
            for r in conn.execute(
                "SELECT session_id, source, COUNT(*) n, SUM(total_tokens) t, "
                "SUM(CASE WHEN is_estimated = 0 THEN prompt_tokens ELSE 0 END) pe, "
                "SUM(CASE WHEN is_estimated = 0 THEN cached_tokens ELSE 0 END) cae "
                f"FROM token_usage WHERE session_id IN ({qmarks}) "
                "GROUP BY session_id, source",
                session_ids,
            ).fetchall():
                src_map.setdefault(r["session_id"], []).append({
                    "source": r["source"],
                    "label": SOURCE_LABELS.get(r["source"], r["source"]),
                    "calls": int(r["n"] or 0),
                    "total_tokens": int(r["t"] or 0),
                    "prompt_exact": int(r["pe"] or 0),
                    "cache_rate": cache_rate(int(r["cae"] or 0), int(r["pe"] or 0)),
                })
        except Exception:
            pass

    # 每个会话用过哪些模型（会话内切换过模型时会有多条，按消耗降序）
    model_map = {}
    if session_ids:
        qmarks = ",".join("?" for _ in session_ids)
        try:
            for r in conn.execute(
                "SELECT session_id, model, provider_name, entry_owner, COUNT(*) n, SUM(total_tokens) t "
                f"FROM token_usage WHERE session_id IN ({qmarks}) "
                "GROUP BY session_id, model, provider_name, entry_owner ORDER BY t DESC",
                session_ids,
            ).fetchall():
                model_map.setdefault(r["session_id"], []).append({
                    "model": r["model"] or "（未知）",
                    "provider_name": r["provider_name"] or "",
                    # platform = 走平台凭据（算平台成本）；user = 用户自带 Key（自付）
                    "owner": r["entry_owner"] or "",
                    "calls": int(r["n"] or 0),
                    "total_tokens": int(r["t"] or 0),
                })
        except Exception:
            pass

    for r in rows:
        sid = r["session_id"]
        srcs = sorted(src_map.get(sid, []), key=lambda x: -x["total_tokens"])
        prompt_exact = int(r["pe"] or 0)
        cached_exact = int(r["cae"] or 0)
        items.append({
            "session_id": sid,
            "title": titles.get(sid) or ("（已删除的会话）" if sid else "（无会话）"),
            "first_at": r["first_at"],
            "last_at": r["last_at"],
            "prompt_tokens": int(r["p"] or 0),
            "completion_tokens": int(r["c"] or 0),
            "cached_tokens": int(r["ca"] or 0),
            "reasoning_tokens": int(r["r"] or 0),
            "total_tokens": int(r["t"] or 0),
            "calls": int(r["n"] or 0),
            "estimated_calls": int(r["est"] or 0),
            "prompt_exact": prompt_exact,
            "cached_exact": cached_exact,
            "cache_rate": cache_rate(cached_exact, prompt_exact),
            "model": r["model"] or "",
            "models": model_map.get(sid, []),
            "owner": r["username"] or "",
            "sources": srcs,
        })

    return {"total": total, "page": page, "size": size,
            "pages": max(1, (total + size - 1) // size), "items": items}


def price_map() -> dict:
    """model_id + 供应商 → (输入价, 输出价, 缓存价)，单位「元 / 百万 token」。

    与 `model_label_map` 同构，但它是给记账用的、这个是给算钱用的，别混。

    键为什么是「模型 ID + 供应商」：同名模型可能平台池一份、用户自配一份，
    两者的价格往往不同（自配可能走代理加价），按 model_id 单键会张冠李戴。

    没有标价的模型**不进这张表**（而不是塞 0）—— 下游据此区分「免费」与「未知」。
    """
    conn = _conn()
    out = {}
    seen = {}
    try:
        rows = conn.execute(
            "SELECT e.model_id, e.price_in, e.price_out, e.price_cached, "
            "       p.name AS pname "
            "FROM model_entries e JOIN model_providers p ON p.id = e.provider_id "
            "WHERE e.price_in IS NOT NULL OR e.price_out IS NOT NULL"
        ).fetchall()
        for r in rows:
            mid = r["model_id"] or ""
            pname = r["pname"] or ""
            prices = (r["price_in"], r["price_out"], r["price_cached"])
            out[f"{mid}||{pname}"] = prices
            seen.setdefault(mid, []).append(prices)
        # 回退键只在「这个 model_id 全平台只有一份报价」时才建。
        # 若同一 model_id 挂在不同供应商下价钱不同（自建/kate 代理常见的加价情形），
        # 用 dict.setdefault 会让先读到的那条胜出 —— 同一个模型在不同行算出两种钱，
        # 且结果取决于表的物理顺序。宁可不给回退，让这部分落到「未标价」由人工确认。
        for mid, prices_list in seen.items():
            if len(prices_list) == 1:
                out[mid] = prices_list[0]
    except Exception as e:
        logger.debug(f"模型单价读取失败（成本将显示未知）: {e}")
    return out


def cost_of(prompt_tokens: int, completion_tokens: int, cached_tokens: int,
            prices) -> Optional[float]:
    """按单价算一次调用的成本（元）。`prices` 为 None 时返回 None（未知，不是 0）。

    计费口径三条，错一条就会差出好几倍：
      1. cached_tokens **是 prompt_tokens 的子集**（命中前缀缓存的那部分），
         所以未命中部分 = prompt - cached，不能直接拿 prompt 全额乘输入价。
      2. reasoning_tokens 是 completion_tokens 的子集，已在里面，不重复计价。
      3. 缓存价未标定（None）时**回落到输入价**而不是按免费处理 ——
         厂商文档普遍写着「命中缓存更便宜」，但没标具体多少钱；当成免费会低估成本。
    """
    if prices is None:
        return None
    pin, pout, pcache = prices
    cached = max(0, int(cached_tokens or 0))
    prompt = max(0, int(prompt_tokens or 0))
    uncached = max(0, prompt - cached)
    if pcache is None:
        pcache = pin
    try:
        total = 0.0
        total += uncached / 1_000_000 * float(pin or 0)
        total += cached / 1_000_000 * float(pcache or 0)
        total += max(0, int(completion_tokens or 0)) / 1_000_000 * float(pout or 0)
        return round(total, 6)
    except (TypeError, ValueError):
        return None


def model_label_map() -> dict:
    """model_id + 供应商 → 显示名。

    账单里记的是真实模型 ID（deepseek-v4-flash-ga-260731），看板上要显示人能读的名字。
    但**不能反过来用显示名当账本主键**：显示名是给人看的、会改；模型 ID 才是给账单对账用的。
    同名模型可能同时存在（平台池一份、用户自配一份），所以键里带上供应商区分。
    """
    conn = _conn()
    out = {}
    try:
        rows = conn.execute(
            "SELECT e.model_id, e.display_name, p.name AS pname "
            "FROM model_entries e JOIN model_providers p ON p.id = e.provider_id"
        ).fetchall()
        for r in rows:
            mid = r["model_id"] or ""
            label = r["display_name"] or mid
            pname = r["pname"] or ""
            out[f"{mid}||{pname}"] = label
            # 回退键：只有一条同名模型时，前端可以只按 model_id 查
            out.setdefault(mid, label)
    except Exception as e:
        logger.debug(f"模型名称映射读取失败（看板将显示原始 ID）: {e}")
    return out


def session_detail(user_id: int, session_id: str, limit: int = 500,
                   scope_all: bool = False) -> list:
    """单个会话的逐次调用明细（点开行看细节）。"""
    clauses = ["session_id = ?"]
    params = [session_id]
    if not scope_all:
        clauses.insert(0, "user_id = ?")
        params.insert(0, int(user_id))
    sql = (
        "SELECT id, source, model, provider_name, entry_owner, prompt_tokens, "
        "completion_tokens, cached_tokens, "
        "reasoning_tokens, total_tokens, is_estimated, duration_ms, created_at "
        f"FROM token_usage WHERE {' AND '.join(clauses)} "
        "ORDER BY created_at DESC, id DESC LIMIT ?"
    )
    rows = _conn().execute(sql, params + [int(limit)]).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["source_label"] = SOURCE_LABELS.get(d.get("source"), d.get("source"))
        out.append(d)
    return out


def today_str() -> str:
    return date.today().strftime("%Y-%m-%d")
