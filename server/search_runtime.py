"""联网搜索的运行时支撑：结果缓存 + 调用计数。

为什么这两件事必须跟搜索调用放在一起：

- **缓存**：同一 query 短期内重复搜会重复花钱。Tavily 按次、DeepSeek 按「成功请求」，
  两者都算钱；模型还经常为同一个问题连发两三次搜索。缓存是唯一能省下这笔的环节。
- **计数**：Tavily 每月 1000 次免费额度，用完了要么报错要么开始计费。没有计数，
  用户只能去厂商后台对账 —— 出了事也说不清是「没配好」还是「额度用光了」。

缓存是**进程内**的：单 worker 部署够用；多 worker 时每个进程各存一份，
代价只是少省一点钱，不会算错账。计数落库，所以多 worker 也是准的。
"""
import time
from datetime import datetime

# 搜索结果的时效性自己会衰减，10 分钟足够覆盖"同一轮里反复搜"；
# 网页抓取的内容更稳定，但也不能太久，否则用户改完页面看不到变化。
SEARCH_TTL = 600
FETCH_TTL = 1800

_cache = {}          # key -> (expire_at, value)
_MAX_ENTRIES = 500   # 兜底：防止进程长期运行把内存撑起来


def cache_key(*parts) -> str:
    """把请求参数拼成缓存键。空值一律忽略，避免 None 与 '' 产生两份缓存。"""
    return "|".join(str(p) for p in parts if p not in (None, ""))


def cache_get(key: str):
    """命中返回缓存值，未命中或已过期返回 None。"""
    item = _cache.get(key)
    if not item:
        return None
    expire_at, value = item
    # 用 <= 而不是 <：ttl=0 时 expire_at 等于当前时间，用 < 会"刚好不过期"，
    # 在快机器上表现为随机命中 —— 这种边界别留给浮点运气。
    if expire_at <= time.time():
        _cache.pop(key, None)
        return None
    return value


def cache_put(key: str, value, ttl: int):
    if len(_cache) >= _MAX_ENTRIES:
        # 先清过期项；还满就丢掉最早写入的一批（不需要精确 LRU）
        now = time.time()
        for k in [k for k, v in _cache.items() if v[0] < now]:
            _cache.pop(k, None)
        if len(_cache) >= _MAX_ENTRIES:
            for k in list(_cache)[: _MAX_ENTRIES // 5]:
                _cache.pop(k, None)
    _cache[key] = (time.time() + ttl, value)


def clear_cache():
    """给测试用，也让"想立刻重搜"有个明确出口。"""
    _cache.clear()


# ---------------- 计数（落库） ----------------

_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS search_usage (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        day TEXT NOT NULL,
        provider TEXT NOT NULL,
        kind TEXT NOT NULL,
        count INTEGER NOT NULL DEFAULT 0,
        -- 本项目全库统一本地时间（见 database.py 表定义注释）；这里曾用
        -- CURRENT_TIMESTAMP（UTC），与同一行里本地日期的 day 差 8 小时。
        updated_at TIMESTAMP DEFAULT (datetime('now','localtime')),
        UNIQUE (day, provider, kind)
    )
"""


def ensure_table(conn) -> None:
    conn.execute(_TABLE_SQL)


def bump(provider: str, kind: str = "search", conn=None) -> None:
    """记一次成功调用。kind: search / fetch。

    只记**成功**的：失败不计费（厂商口径也是"成功请求才计费"），
    把失败也记进去会让"本月已用"虚高，用户反而更慌。
    """
    if conn is None:
        from server.database import _get_connection
        conn = _get_connection()
    day = datetime.now().strftime("%Y-%m-%d")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ensure_table(conn)
    # 显式传本地时间，不吃默认值 —— 默认 CURRENT_TIMESTAMP 是 UTC，会让
    # updated_at 与同行的 day（本地日期）差 8 小时（2026-10-27 数据审计发现）。
    conn.execute(
        "INSERT INTO search_usage (day, provider, kind, count, updated_at) "
        "VALUES (?, ?, ?, 1, ?) "
        "ON CONFLICT(day, provider, kind) DO UPDATE SET "
        "count = count + 1, updated_at = ?",
        (day, provider, kind, now, now),
    )
    conn.commit()


def summary(conn=None) -> dict:
    """返回今日与本月各供应商各类型的调用次数。

    Returns:
        dict: {"month": "2026-10", "month_total": {provider: n}, "today": {provider: n},
               "by_kind": {"search": n, "fetch": n}}
    """
    if conn is None:
        from server.database import _get_connection
        conn = _get_connection()
    ensure_table(conn)
    today = datetime.now().strftime("%Y-%m-%d")
    month = today[:7]
    rows = conn.execute(
        "SELECT day, provider, kind, count FROM search_usage WHERE day >= ?",
        (month + "-01",),
    ).fetchall()
    month_total, today_total, by_kind = {}, {}, {}
    for r in rows:
        # sqlite3.Row 与 tuple 都支持下标，不用管 row_factory
        day, provider, kind, count = r[0], r[1], r[2], r[3] or 0
        month_total[provider] = month_total.get(provider, 0) + count
        by_kind[kind] = by_kind.get(kind, 0) + count
        if day == today:
            today_total[provider] = today_total.get(provider, 0) + count
    return {
        "month": month,
        "month_total": month_total,
        "today": today_total,
        "by_kind": by_kind,
    }
