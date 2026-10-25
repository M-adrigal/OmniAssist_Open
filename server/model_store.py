"""模型供应商 + 模型条目的两层存储。

为什么必须拆两层
----------------
改造前 `model_configs` 一行里同时塞了「凭据」（base_url + api_key）和「模型」（model_name），
结果是：想加第二个模型只能再加一个用户，结构上根本不支持。

现实里它们的基数是 1:N —— 一家供应商一个地址一把 key，下面挂多个模型。
更关键的是 **上下文长度必须跟着模型走**：同一家的 flash 与 pro 窗口大小不同，
而上下文压缩阈值按这个值算，挂在凭据上一定会算错（填大了请求超限、填小了白扔历史）。

所以：
- `model_providers` = 凭据层。一行 = 一个「地址 + 密钥」的组合，key 只存一份。
- `model_entries`   = 模型层。一行 = 一个可被选中的模型。

三层归属（数据隔离的核心）
--------------------------
`model_providers.owner_user_id`：
- `NULL`      → 平台登记。**地址由管理员控制**，普通用户只能引用这些行。
- 整数(uid)   → 用户自配。地址仍来自平台登记的行（用户不能自己填地址），
                区别只是用**用户自己的 key**。

所有读写用户自带数据的函数都必须带 owner 条件，绝不允许只按 id 取。
"""

import re
import sqlite3
from datetime import datetime
from typing import Optional

from agent.logger import get_logger

logger = get_logger("model_store")


# 内置供应商目录（候选地址清单）。
# 这些地址是各家官方公开的 OpenAI 兼容端点，格式毫无规律（/v1、/v3、/v4、compatible-mode），
# 让用户手抄地址是最容易出错的一步 —— 所以地址一律由这里或管理员提供，用户只选「哪一家」。
BUILTIN_PROVIDERS = [
    {"key": "volc_ark", "name": "火山方舟", "base_url": "https://ark.cn-beijing.volces.com/api/v3"},
    {"key": "deepseek", "name": "DeepSeek 官方", "base_url": "https://api.deepseek.com"},
    {"key": "dashscope", "name": "通义千问", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"},
    {"key": "moonshot", "name": "Kimi（月之暗面）", "base_url": "https://api.moonshot.cn/v1"},
    {"key": "zhipu", "name": "智谱 GLM", "base_url": "https://open.bigmodel.cn/api/paas/v4"},
    {"key": "hunyuan", "name": "腾讯混元", "base_url": "https://api.hunyuan.cloud.tencent.com/v1"},
    {"key": "qianfan", "name": "百度文心", "base_url": "https://qianfan.baidubce.com/v2"},
    {"key": "minimax", "name": "MiniMax", "base_url": "https://api.minimaxi.com/v1"},
]

BUILTIN_BY_KEY = {p["key"]: p for p in BUILTIN_PROVIDERS}
BUILTIN_BY_URL = {p["base_url"].rstrip("/"): p for p in BUILTIN_PROVIDERS}


_PROVIDERS_DDL = """
CREATE TABLE IF NOT EXISTS model_providers (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id     INTEGER,
    name              TEXT NOT NULL DEFAULT '',
    base_url          TEXT NOT NULL DEFAULT '',
    api_key_encrypted TEXT DEFAULT '',
    builtin_key       TEXT DEFAULT '',
    is_builtin        INTEGER DEFAULT 0,
    enabled           INTEGER DEFAULT 1,
    sort_order        INTEGER DEFAULT 0,
    created_at        TIMESTAMP,
    updated_at        TIMESTAMP,
    FOREIGN KEY (owner_user_id) REFERENCES users(id) ON DELETE CASCADE
)
"""

_ENTRIES_DDL = """
CREATE TABLE IF NOT EXISTS model_entries (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_id   INTEGER NOT NULL,
    model_id      TEXT NOT NULL,
    display_name  TEXT NOT NULL DEFAULT '',
    context_limit TEXT DEFAULT '',
    visible       INTEGER DEFAULT 1,
    is_default    INTEGER DEFAULT 0,
    enabled       INTEGER DEFAULT 1,
    sort_order    INTEGER DEFAULT 0,
    price_in      REAL,
    price_out     REAL,
    price_cached  REAL,
    created_at    TIMESTAMP,
    updated_at    TIMESTAMP,
    FOREIGN KEY (provider_id) REFERENCES model_providers(id) ON DELETE CASCADE
)
"""

# 价格单位与字段含义（**写入前必须理解，否则成本会算错一个数量级**）
# ------------------------------------------------------------------
# 单位统一为「元 / 百万 token」（国内厂商的公开报价口径），不是「元 / 千 token」、
# 也不是「元 / token」。填 2 表示每百万 token 2 元，单次 8000 token 的请求约 0.016 元。
#
# price_in      未命中缓存的输入单价
# price_out     输出单价（reasoning_tokens 已含在 completion_tokens 内，不单独计价）
# price_cached  命中缓存的输入单价，留空表示「与 price_in 相同」
#
# 三个字段都允许 NULL —— **NULL 表示「没给价」，与「免费（0 元）」是两回事**。
# 区分这两者的唯一办法就是允许空值：模型没标价时成本按「未知」处理并显示「—」，
# 绝不能默认成 0 元把成本凭空抹掉。
_PRICE_FIELDS = ("price_in", "price_out", "price_cached")

# 同一归属下的同一个地址只允许一行（平台级 owner 为 NULL，用 COALESCE 折成 0 才能唯一约束）
_PROVIDERS_INDEX = [
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_provider_owner_url "
    "ON model_providers(COALESCE(owner_user_id, 0), base_url)",
    "CREATE INDEX IF NOT EXISTS idx_provider_owner ON model_providers(owner_user_id)",
]

# 同一凭据下的同一模型只允许一行
_ENTRIES_INDEX = [
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_entry_provider_model "
    "ON model_entries(provider_id, model_id)",
    "CREATE INDEX IF NOT EXISTS idx_entry_provider ON model_entries(provider_id)",
]


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _conn():
    """复用 database 模块的连接（含 WAL / busy_timeout 等 PRAGMA）。延迟导入避免循环依赖。"""
    from server.database import _get_connection
    return _get_connection()


def _enc(plaintext: str) -> str:
    from server.database import _encrypt_db
    return _encrypt_db(plaintext or "")


def _dec(ciphertext: str) -> str:
    from server.database import _decrypt_db
    if not ciphertext:
        return ""
    try:
        return _decrypt_db(ciphertext)
    except Exception as e:
        logger.warning(f"供应商 key 解密失败（按空处理）: {e}")
        return ""


def _mask(key: str) -> str:
    from server.database import _mask_key
    return _mask_key(key)


def guess_display_name(model_id: str) -> str:
    """从模型 ID 猜一个能看的显示名。

    方舟这类平台的模型 ID 带版本日期（deepseek-v4-flash-ga-260731），
    直接展示给用户毫无意义，所以要有一条「ID → 显示名」的默认推导，用户可再改。
    """
    mid = (model_id or "").strip()
    if not mid:
        return ""
    # 去掉末尾的版本日期：-260731 / -ga-260731 / -20260731
    base = re.sub(r"-(?:ga-)?(?:20)?\d{6}$", "", mid, flags=re.I)
    # 去掉常见的推理接入点前缀（豆包 ep-xxxx），这种只能原样显示
    if base.lower().startswith("ep-"):
        return mid
    pretty = re.sub(r"[-_]+", " ", base).strip()
    # 品牌名不按首字母大写处理，否则会出现 Deepseek / Gpt 这种难看写法
    _BRAND = {
        "deepseek": "DeepSeek", "gpt": "GPT", "claude": "Claude", "gemini": "Gemini",
        "qwen": "Qwen", "glm": "GLM", "kimi": "Kimi", "moonshot": "Moonshot",
        "doubao": "豆包", "hunyuan": "混元", "ernie": "文心", "minimax": "MiniMax",
        "llama": "Llama", "mistral": "Mistral", "yi": "Yi", "step": "Step",
    }
    words = []
    for w in pretty.split():
        low = w.lower()
        if low in _BRAND:
            words.append(_BRAND[low])
        elif re.fullmatch(r"\d+(\.\d+)?[bkKmM]?", w):
            words.append(w)
        elif w.isupper() or re.fullmatch(r"v?\d+(\.\d+)*", w, flags=re.I):
            words.append(w.upper() if len(w) <= 3 else w.capitalize())
        else:
            words.append(w.capitalize())
    return " ".join(words) or mid


# ===== 建表 =====

def ensure_tables(conn) -> None:
    """建表 + 索引 + 补齐新增列（幂等，可在启动时反复调用）。"""
    conn.execute(_PROVIDERS_DDL)
    conn.execute(_ENTRIES_DDL)
    for ddl in _PROVIDERS_INDEX + _ENTRIES_INDEX:
        conn.execute(ddl)

    # model_configs 从「凭据+模型混装」退化为「用户偏好 + 当前选中的模型」
    for ddl in (
        "ALTER TABLE model_configs ADD COLUMN current_entry_id INTEGER DEFAULT NULL",
    ):
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError:
            pass

    # 会话级模型：会话中途换模型后必须记得住「从哪句开始换的」
    try:
        conn.execute("ALTER TABLE sessions ADD COLUMN model_entry_id INTEGER DEFAULT NULL")
    except sqlite3.OperationalError:
        pass

    # 单价（元/百万 token）。幂等：老库补列，新库在建表时已含，重复 ADD 会报错并被忽略。
    for _col in _PRICE_FIELDS:
        try:
            conn.execute(f"ALTER TABLE model_entries ADD COLUMN {_col} REAL")
        except sqlite3.OperationalError:
            pass

    conn.commit()


# ===== 供应商（凭据层）=====

def _row_to_provider(row, include_key: bool = False) -> Optional[dict]:
    if not row:
        return None
    d = dict(row)
    if include_key:
        d["api_key"] = _dec(d.get("api_key_encrypted", ""))
    else:
        d["api_key"] = ""
    d["api_key_masked"] = _mask(_dec(d.get("api_key_encrypted", "")))
    d.pop("api_key_encrypted", None)
    d["has_key"] = bool(d["api_key_masked"] and d["api_key_masked"] != "(未设置)")
    d["enabled"] = bool(d.get("enabled", 1))
    d["is_builtin"] = bool(d.get("is_builtin", 0))
    d["owner_user_id"] = d.get("owner_user_id")
    d["scope"] = "platform" if d.get("owner_user_id") is None else "personal"
    return d


def list_providers(owner_user_id: Optional[int] = None, include_platform: bool = False,
                   only_platform: bool = False) -> list[dict]:
    """列出供应商。

    - `only_platform=True`：只给平台登记的行（普通用户可选供应商的来源）。
    - `owner_user_id=<uid>`：该用户自配的行。
    - 两个都不传：全平台所有行（仅管理员监管场景）。
    """
    conn = _conn()
    if only_platform:
        rows = conn.execute(
            "SELECT * FROM model_providers WHERE owner_user_id IS NULL ORDER BY sort_order, id"
        ).fetchall()
    elif owner_user_id is not None and include_platform:
        rows = conn.execute(
            "SELECT * FROM model_providers WHERE owner_user_id IS NULL OR owner_user_id = ? "
            "ORDER BY (owner_user_id IS NOT NULL), sort_order, id",
            (owner_user_id,),
        ).fetchall()
    elif owner_user_id is not None:
        rows = conn.execute(
            "SELECT * FROM model_providers WHERE owner_user_id = ? ORDER BY sort_order, id",
            (owner_user_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM model_providers ORDER BY (owner_user_id IS NOT NULL), sort_order, id"
        ).fetchall()
    return [_row_to_provider(r) for r in rows]


def get_provider(provider_id: int, owner_user_id: Optional[int] = None,
                 allow_platform: bool = True) -> Optional[dict]:
    """按 id 取供应商，**必须带归属校验**。

    `allow_platform=True` 时平台行（owner IS NULL）对所有用户可见；
    用户自配行只有 owner 本人能取到 —— 这是数据隔离的第一道闸。
    """
    conn = _conn()
    row = conn.execute("SELECT * FROM model_providers WHERE id = ?", (provider_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    owner = d.get("owner_user_id")
    if owner is None:
        if not allow_platform:
            return None
    elif owner_user_id is None or owner != owner_user_id:
        return None
    return _row_to_provider(row)


def create_provider(name: str, base_url: str, api_key: str = "",
                    owner_user_id: Optional[int] = None,
                    builtin_key: str = "", sort_order: int = 0) -> dict:
    """新建供应商。同一归属 + 同一地址已存在时复用并只更新 key（避免同一把 key 存两份）。"""
    base_url = (base_url or "").strip().rstrip("/")
    if not base_url:
        raise ValueError("供应商地址不能为空")
    if not (base_url.startswith("http://") or base_url.startswith("https://")):
        raise ValueError("供应商地址必须以 http:// 或 https:// 开头")

    existing = find_provider_by_url(base_url, owner_user_id)
    if existing:
        if api_key:
            update_provider(existing["id"], owner_user_id=owner_user_id, api_key=api_key,
                            name=name or None, enabled=True)
        return get_provider(existing["id"], owner_user_id=owner_user_id) or existing

    now = _now()
    conn = _conn()
    cur = conn.execute(
        "INSERT INTO model_providers "
        "(owner_user_id, name, base_url, api_key_encrypted, builtin_key, is_builtin, "
        " enabled, sort_order, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
        (owner_user_id, name or base_url, base_url, _enc(api_key),
         builtin_key or "", 1 if builtin_key else 0, sort_order, now, now),
    )
    conn.commit()
    return get_provider(cur.lastrowid, owner_user_id=owner_user_id) or {}


def find_provider_by_url(base_url: str, owner_user_id: Optional[int]) -> Optional[dict]:
    conn = _conn()
    base_url = (base_url or "").strip().rstrip("/")
    if owner_user_id is None:
        row = conn.execute(
            "SELECT * FROM model_providers WHERE owner_user_id IS NULL AND base_url = ?",
            (base_url,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM model_providers WHERE owner_user_id = ? AND base_url = ?",
            (owner_user_id, base_url),
        ).fetchone()
    return _row_to_provider(row)


def update_provider(provider_id: int, owner_user_id: Optional[int] = None,
                    **fields) -> Optional[dict]:
    """更新供应商。`owner_user_id` 传 None 表示操作平台行。

    可改：name / api_key / base_url / enabled / sort_order。
    base_url 仅开放给「改错地址」的补救场景：同归属下地址唯一，改到已存在的地址会报错。
    """
    # 先确认这一行确实属于该归属，避免越权改别人的 key
    target = get_provider(provider_id, owner_user_id=owner_user_id, allow_platform=(owner_user_id is None))
    if not target:
        return None
    if target["scope"] != ("platform" if owner_user_id is None else "personal"):
        return None

    updates = {}
    if "name" in fields and fields["name"] is not None:
        updates["name"] = str(fields["name"]).strip()
    if "api_key" in fields and fields["api_key"] is not None:
        updates["api_key_encrypted"] = _enc(fields["api_key"])
    if "base_url" in fields and fields["base_url"] is not None:
        v = str(fields["base_url"]).strip().rstrip("/")
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("供应商地址必须以 http:// 或 https:// 开头")
        updates["base_url"] = v
    if "enabled" in fields and fields["enabled"] is not None:
        updates["enabled"] = 1 if fields["enabled"] else 0
    if "sort_order" in fields and fields["sort_order"] is not None:
        updates["sort_order"] = int(fields["sort_order"])

    if updates:
        updates["updated_at"] = _now()
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        conn = _conn()
        try:
            conn.execute(f"UPDATE model_providers SET {set_clause} WHERE id = ?",
                         list(updates.values()) + [provider_id])
            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback()
            raise ValueError("同一归属下已存在使用该地址的供应商")
    return get_provider(provider_id, owner_user_id=owner_user_id)


def delete_provider(provider_id: int, owner_user_id: Optional[int] = None) -> bool:
    target = get_provider(provider_id, owner_user_id=owner_user_id, allow_platform=(owner_user_id is None))
    if not target:
        return False
    if target["scope"] != ("platform" if owner_user_id is None else "personal"):
        return False
    conn = _conn()
    # 模型条目随凭据级联删除（外键 ON DELETE CASCADE）
    cur = conn.execute("DELETE FROM model_providers WHERE id = ?", (provider_id,))
    conn.commit()
    return cur.rowcount > 0


# ===== 模型条目（模型层）=====

def _norm_price(value):
    """把外部传入的单价归一化为 float 或 None。

    三种取值必须分得清，否则成本会被算错一个数量级或者直接消失：
      - 不传（key 不在 fields 里）      → 不改
      - None / ''                      → 清空，表示「这个模型没有标价」
      - 数字或数字字符串                → 按 元/百万 token 存

    特别拦 NaN：它是 float，能通过 isinstance 检查，但存进 SQLite 后任何比较
    都返回假，会让「有没有价格」的判断彻底失灵 —— 宁可直接报错。
    """
    if value is None or value == "":
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        raise ValueError("单价必须是数字")
    if f != f:  # NaN 的唯一可靠判据
        raise ValueError("单价不能为 NaN")
    if f < 0:
        raise ValueError("单价不能为负数")
    if f > 1_000_000:
        raise ValueError("单价超出合理范围（单位：元/百万 token）")
    return f


def _row_to_entry(row) -> Optional[dict]:
    if not row:
        return None
    d = dict(row)
    d["visible"] = bool(d.get("visible", 1))
    d["enabled"] = bool(d.get("enabled", 1))
    d["is_default"] = bool(d.get("is_default", 0))
    d["model_name"] = d.get("model_id", "")   # 兼容旧字段名
    d["label"] = d.get("display_name") or d.get("model_id", "")
    # 单价原样透出：None 必须保持 None —— 前端靠它判断「未标价」，
    # 与「免费（0 元）」是两种完全不同的状态，不能在这里折成 0。
    for k in _PRICE_FIELDS:
        v = d.get(k)
        d[k] = float(v) if v is not None else None
    return d


_ENTRY_SELECT = """
SELECT e.*, p.name AS provider_name, p.base_url AS base_url,
       p.owner_user_id AS provider_owner, p.enabled AS provider_enabled
FROM model_entries e JOIN model_providers p ON p.id = e.provider_id
"""


def list_entries(owner_user_id: Optional[int] = None, only_platform: bool = False,
                 include_platform_pool: bool = False) -> list[dict]:
    """列出模型条目。

    - `only_platform=True`：平台模型池里「对用户可见」的条目（普通用户能选的）。
    - `owner_user_id=<uid>`：该用户自配的条目（凭据归属为该用户）。
    - 都不传：全部（管理员视图）。
    """
    conn = _conn()
    if only_platform:
        rows = conn.execute(
            _ENTRY_SELECT + " WHERE p.owner_user_id IS NULL AND e.visible = 1 "
            "ORDER BY e.sort_order, e.id"
        ).fetchall()
    elif owner_user_id is not None and include_platform_pool:
        rows = conn.execute(
            _ENTRY_SELECT + " WHERE (p.owner_user_id IS NULL AND e.visible = 1) "
            "OR p.owner_user_id = ? ORDER BY (p.owner_user_id IS NOT NULL), e.sort_order, e.id",
            (owner_user_id,),
        ).fetchall()
    elif owner_user_id is not None:
        rows = conn.execute(
            _ENTRY_SELECT + " WHERE p.owner_user_id = ? ORDER BY e.sort_order, e.id",
            (owner_user_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            _ENTRY_SELECT + " ORDER BY (p.owner_user_id IS NOT NULL), e.sort_order, e.id"
        ).fetchall()
    return [_row_to_entry(r) for r in rows]


def get_entry(entry_id: int, owner_user_id: Optional[int] = None) -> Optional[dict]:
    """按 id 取模型条目，带归属校验：平台条目对所有用户可见，个人条目仅本人可见。"""
    if not entry_id:
        return None
    conn = _conn()
    row = conn.execute(_ENTRY_SELECT + " WHERE e.id = ?", (entry_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    powner = d.get("provider_owner")
    if powner is not None and (owner_user_id is None or powner != owner_user_id):
        return None
    return _row_to_entry(row)


def create_entry(provider_id: int, model_id: str, display_name: str = "",
                 context_limit: str = "", visible: bool = True, is_default: bool = False,
                 sort_order: int = 0, owner_user_id: Optional[int] = None,
                 price_in=None, price_out=None, price_cached=None) -> dict:
    """在指定供应商下新增模型。provider 的归属必须与调用方一致（防越权挂到别人凭据下）。"""
    model_id = (model_id or "").strip()
    if not model_id:
        raise ValueError("模型 ID 不能为空")

    provider = get_provider(provider_id, owner_user_id=owner_user_id)
    if not provider:
        raise ValueError("供应商不存在或无权使用")
    # 归属必须严格对齐：管理员只能操作平台池，用户只能操作自己凭据下的模型。
    # 若允许用户在平台供应商下挂模型，该条目的归属会变成「平台」→ 其他用户也会看到，属于串号。
    if owner_user_id is not None and provider["scope"] == "platform":
        raise ValueError("请先添加自己的凭据，再挂模型")
    if owner_user_id is None and provider["scope"] != "platform":
        raise ValueError("供应商不存在或无权使用")

    now = _now()
    conn = _conn()
    existing = conn.execute(
        "SELECT id FROM model_entries WHERE provider_id = ? AND model_id = ?",
        (provider_id, model_id),
    ).fetchone()
    if existing:
        update_entry(existing["id"], owner_user_id=owner_user_id,
                     display_name=display_name or None, context_limit=context_limit,
                     visible=visible, is_default=is_default, enabled=True)
        return get_entry(existing["id"], owner_user_id=owner_user_id) or {}

    prices = {
        "price_in": _norm_price(price_in),
        "price_out": _norm_price(price_out),
        "price_cached": _norm_price(price_cached),
    }

    cur = conn.execute(
        "INSERT INTO model_entries "
        "(provider_id, model_id, display_name, context_limit, visible, is_default, "
        " enabled, sort_order, price_in, price_out, price_cached, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)",
        (provider_id, model_id, display_name or guess_display_name(model_id),
         context_limit or "", 1 if visible else 0, 1 if is_default else 0, sort_order,
         prices["price_in"], prices["price_out"], prices["price_cached"], now, now),
    )
    conn.commit()
    # 只有平台池的默认模型需要互斥；用户自配的「默认」由 model_configs.current_entry_id 表达
    if is_default and owner_user_id is None:
        set_default_entry(cur.lastrowid)
    return get_entry(cur.lastrowid, owner_user_id=owner_user_id) or {}


def update_entry(entry_id: int, owner_user_id: Optional[int] = None, **fields) -> Optional[dict]:
    target = get_entry(entry_id, owner_user_id=owner_user_id)
    if not target:
        return None
    if owner_user_id is None and target.get("provider_owner") is not None:
        return None

    updates = {}
    if fields.get("model_id"):
        updates["model_id"] = str(fields["model_id"]).strip()
    if fields.get("display_name") is not None:
        updates["display_name"] = str(fields["display_name"]).strip()
    if fields.get("context_limit") is not None:
        updates["context_limit"] = str(fields["context_limit"]).strip()
    if fields.get("visible") is not None:
        updates["visible"] = 1 if fields["visible"] else 0
    if fields.get("enabled") is not None:
        updates["enabled"] = 1 if fields["enabled"] else 0
    if fields.get("sort_order") is not None:
        updates["sort_order"] = int(fields["sort_order"])

    # 单价用「key 是否存在」判断要不要改，而不是「值是否非空」—— 因为 None 本身
    # 是一个有意义的输入（清空标价），不能与「没传」混为一谈。
    for k in _PRICE_FIELDS:
        if k in fields:
            updates[k] = _norm_price(fields.get(k))

    if updates:
        updates["updated_at"] = _now()
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        conn = _conn()
        try:
            conn.execute(f"UPDATE model_entries SET {set_clause} WHERE id = ?",
                         list(updates.values()) + [entry_id])
            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback()
            # 改模型 ID 时撞了唯一索引（同供应商下同模型只允许一条）
            raise ValueError("同一供应商下已存在相同模型 ID")
    if fields.get("is_default") and owner_user_id is None:
        set_default_entry(entry_id)
    return get_entry(entry_id, owner_user_id=owner_user_id)


def delete_entry(entry_id: int, owner_user_id: Optional[int] = None) -> bool:
    target = get_entry(entry_id, owner_user_id=owner_user_id)
    if not target:
        return False
    if owner_user_id is None and target.get("provider_owner") is not None:
        return False
    conn = _conn()
    cur = conn.execute("DELETE FROM model_entries WHERE id = ?", (entry_id,))
    conn.commit()
    return cur.rowcount > 0


def set_default_entry(entry_id: int, owner_user_id: Optional[int] = None) -> None:
    """把某个平台模型设为平台默认（同作用域内互斥）。"""
    conn = _conn()
    conn.execute(
        "UPDATE model_entries SET is_default = 0 WHERE id != ? AND provider_id IN "
        "(SELECT id FROM model_providers WHERE owner_user_id IS NULL)",
        (entry_id,),
    )
    conn.execute("UPDATE model_entries SET is_default = 1 WHERE id = ?", (entry_id,))
    conn.commit()


def get_default_entry() -> Optional[dict]:
    """平台默认模型：平台池里标了默认的那条，没有就取平台池第一条可见的。"""
    conn = _conn()
    row = conn.execute(
        _ENTRY_SELECT + " WHERE p.owner_user_id IS NULL AND e.enabled = 1 AND e.visible = 1 "
        "ORDER BY e.is_default DESC, e.sort_order, e.id LIMIT 1"
    ).fetchone()
    return _row_to_entry(row)


# ===== 用户当前选中的模型 =====

def get_user_current_entry(user_id: int) -> Optional[int]:
    conn = _conn()
    row = conn.execute(
        "SELECT current_entry_id FROM model_configs WHERE user_id = ?", (user_id,)
    ).fetchone()
    if not row:
        return None
    try:
        return int(row["current_entry_id"]) if row["current_entry_id"] else None
    except (TypeError, ValueError):
        return None


def set_user_current_entry(user_id: int, entry_id: Optional[int]) -> None:
    conn = _conn()
    now = _now()
    existing = conn.execute("SELECT id FROM model_configs WHERE user_id = ?", (user_id,)).fetchone()
    if existing:
        conn.execute("UPDATE model_configs SET current_entry_id = ?, updated_at = ? WHERE user_id = ?",
                     (entry_id, now, user_id))
    else:
        conn.execute("INSERT INTO model_configs (user_id, current_entry_id, created_at, updated_at) "
                     "VALUES (?, ?, ?, ?)", (user_id, entry_id, now, now))
    conn.commit()


# ===== 解析「本次请求用哪个模型」 =====

def _entry_usable(entry: dict, include_hidden: bool = False) -> bool:
    """这条模型现在能不能真的用来跑请求。

    - `enabled=0`：停用，谁都不能用。
    - 平台条目 `visible=0`：管理员从「对用户开放」里撤下来了 —— 普通用户不可用；
      管理员带着 include_hidden 时仍可用（管理端要能自己试）。
    - 用户自配条目没有「对谁可见」的概念，只看 enabled。
    """
    if not entry.get("enabled", True):
        return False
    is_platform = entry.get("provider_owner") is None
    if is_platform and not entry.get("visible", True) and not include_hidden:
        return False
    return True


def resolve_active_model(user_id: int, entry_id: Optional[int] = None,
                         session_id: Optional[str] = None,
                         include_hidden: bool = False) -> Optional[dict]:
    """四级取值，找到本次对话真正要用的模型凭据。

    优先级（高 → 低）：
      1. 调用方显式指定（会话内刚切换过模型）
      2. 会话上记住的模型（刷新页面后仍用同一个）
      3. 用户自己选的默认模型
      4. 平台默认模型

    每一级都必须过归属校验：用户自配的条目只有本人能用；
    引用别人的 entry_id 会逐级失败并落到下一级，最终回落平台默认 —— 不会报错也不会越权。
    """
    candidates = []
    if entry_id:
        candidates.append(("request", entry_id))
    if session_id:
        cand = _session_entry_id(session_id)
        if cand:
            candidates.append(("session", cand))
    user_entry = get_user_current_entry(user_id) if user_id else None
    if user_entry:
        candidates.append(("user_default", user_entry))

    for source, cand_id in candidates:
        entry = get_entry(cand_id, owner_user_id=user_id)
        if not entry:
            continue
        # 「管理员隐藏了模型」必须在**这里**生效，不能只靠下拉列表不显示。
        # 早先只过滤列表，于是隐藏后用户仍然能用（界面看不到，但请求照跑）。
        # include_hidden 只给管理员自己用（否则管理员隐藏完把自己也锁死）。
        if not _entry_usable(entry, include_hidden):
            continue
        provider = get_provider(entry["provider_id"], owner_user_id=user_id)
        if not provider or not provider.get("enabled"):
            continue
        return _compose(entry, provider, source)

    default = get_default_entry()
    if default and _entry_usable(default, include_hidden):
        provider = get_provider(default["provider_id"], owner_user_id=user_id)
        if provider and provider.get("enabled"):
            return _compose(default, provider, "platform_default")
    return None


def _compose(entry: dict, provider: dict, source: str) -> dict:
    """把条目 + 凭据拼成一次请求需要的完整配置（字段名保持与旧 resolve_model_config 兼容）。"""
    from server.database import _get_connection, _decrypt_db
    row = _get_connection().execute(
        "SELECT api_key_encrypted, base_url, name, owner_user_id FROM model_providers WHERE id = ?",
        (provider["id"],),
    ).fetchone()
    raw_key = ""
    base_url = provider.get("base_url", "")
    pname = provider.get("name", "")
    powner = provider.get("owner_user_id")
    if row:
        try:
            raw_key = _decrypt_db(row["api_key_encrypted"] or "") or ""
        except Exception:
            raw_key = ""
        base_url = row["base_url"] or base_url
        pname = row["name"] or pname
        powner = row["owner_user_id"]
    return {
        "entry_id": entry["id"],
        "provider_id": provider["id"],
        "provider_name": pname,
        "model_id": entry.get("model_id", ""),
        "model_name": entry.get("model_id", ""),          # 兼容旧字段
        "display_name": entry.get("display_name") or entry.get("model_id", ""),
        "base_url": base_url,
        "api_key": raw_key,
        "api_key_masked": _mask(raw_key),
        "context_limit": entry.get("context_limit", "") or "",
        "entry_owner": "platform" if powner is None else "user",
        "resolved_from": source,
        "config_type": "platform" if powner is None else "personal",
    }


def _session_entry_id(session_id: str) -> Optional[int]:
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT model_entry_id FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row:
        return None
    try:
        return int(row["model_entry_id"]) if row["model_entry_id"] else None
    except (TypeError, ValueError):
        return None


def set_session_entry(session_id: str, entry_id: Optional[int]) -> bool:
    conn = _conn()
    cur = conn.execute("UPDATE sessions SET model_entry_id = ? WHERE id = ?",
                       (entry_id, session_id))
    conn.commit()
    return cur.rowcount > 0


# ===== 存量迁移 =====

def migrate_from_model_configs(conn=None) -> dict:
    """把旧的单模型配置迁移成 两层结构。幂等，可重复执行。

    两种情形需要分开处理，否则会给同一个 key 存出两份凭据：

    1. **平台行**（`model_configs.user_id IS NULL`）：建 owner=NULL 的 provider + entry，
       并设为平台默认。这是最重要的一步 —— 现有对话立刻跑在新结构上。
    2. **用户行**：如果它的 base_url 与 model_id 跟平台行完全一致（说明用户其实在用平台配置），
       只把 `current_entry_id` 指向平台那条 entry，**不复制凭据**；
       否则才建该用户自己的 provider + entry。
    """
    from server.database import _get_connection
    conn = conn or _get_connection()
    ensure_tables(conn)

    stats = {"providers_created": 0, "entries_created": 0, "users_linked": 0, "skipped": 0}

    rows = conn.execute("SELECT * FROM model_configs").fetchall()
    platform_row = None
    platform_entry_id = None

    # 先处理平台行，用户行要靠它做比对
    for r in rows:
        d = dict(r)
        if d.get("user_id") is not None:
            continue
        base_url = (d.get("base_url") or "").strip().rstrip("/")
        model_id = (d.get("model_name") or "").strip()
        if not base_url and not model_id:
            continue
        try:
            key = _dec(d.get("api_key_encrypted", ""))
        except Exception:
            key = ""
        provider = _migrate_one_provider(conn, base_url, key, None, stats)
        if model_id:
            entry = _migrate_one_entry(conn, provider["id"], model_id, d.get("context_limit", ""), stats)
            platform_entry_id = entry["id"]
            conn.execute("UPDATE model_entries SET is_default = 1 WHERE id = ?", (entry["id"],))
        platform_row = d

    p_base = ((platform_row or {}).get("base_url") or "").strip().rstrip("/")
    p_model = ((platform_row or {}).get("model_name") or "").strip()

    for r in rows:
        d = dict(r)
        uid = d.get("user_id")
        if uid is None:
            continue
        base_url = (d.get("base_url") or "").strip().rstrip("/")
        model_id = (d.get("model_name") or "").strip()
        try:
            key = _dec(d.get("api_key_encrypted", ""))
        except Exception:
            key = ""
        if not base_url and not model_id:
            stats["skipped"] += 1
            continue

        same_as_platform = (base_url == p_base and model_id == p_model) or (
            base_url == p_base and not model_id
        )
        if same_as_platform and platform_entry_id:
            # 用户原本用的就是平台配置（自己没单独配过），**不要**给他写 current_entry_id。
            # 让他继续跟随平台默认：以后平台换模型他也跟着换，这才符合「我没有自己配」的
            # 实际状态；如果写成「用户默认」，他会变成被锁死在迁移那一刻的模型上。
            stats["skipped"] += 1
            continue

        provider = _migrate_one_provider(conn, base_url, key, uid, stats)
        if model_id:
            entry = _migrate_one_entry(conn, provider["id"], model_id, d.get("context_limit", ""), stats)
            conn.execute("UPDATE model_configs SET current_entry_id = ? WHERE user_id = ?",
                         (entry["id"], uid))
            stats["users_linked"] += 1

    # 纠正历史误写：早期迁移会给「本来就在用平台配置」的用户写入 current_entry_id，
    # 使其看似「自己选了默认模型」。这里把「指向平台默认、且没有自配凭据」的行清空，
    # 让其回落为「跟随平台默认」—— 解析结果完全相同，但语义正确（平台换模型时他会跟着换）。
    default = get_default_entry()
    if default:
        conn.execute(
            "UPDATE model_configs SET current_entry_id = NULL "
            "WHERE current_entry_id = ? AND COALESCE(user_id, 0) NOT IN "
            "(SELECT owner_user_id FROM model_providers WHERE owner_user_id IS NOT NULL)",
            (default["id"],),
        )
    conn.commit()
    logger.info(f"模型配置迁移完成: {stats}")
    return stats


def _migrate_one_provider(conn, base_url: str, api_key: str, owner_user_id, stats) -> dict:
    if owner_user_id is None:
        row = conn.execute(
            "SELECT * FROM model_providers WHERE owner_user_id IS NULL AND base_url = ?",
            (base_url,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM model_providers WHERE owner_user_id = ? AND base_url = ?",
            (owner_user_id, base_url),
        ).fetchone()
    if row:
        # 已存在时不能直接返回：内置供应商目录（seed_builtin_providers）会先占位建出
        # 一条无 key 的行，若不把旧配置里的 key 补进去，迁移后平台 key 会「凭空消失」，
        # 表现为所有对话 401 —— 这是迁移最容易踩的坑。
        d = dict(row)
        if api_key and not (d.get("api_key_encrypted") or ""):
            conn.execute(
                "UPDATE model_providers SET api_key_encrypted = ?, updated_at = ? WHERE id = ?",
                (_enc(api_key), _now(), d["id"]),
            )
            d["api_key_encrypted"] = "(patched)"
        return d

    builtin = BUILTIN_BY_URL.get(base_url)
    now = _now()
    cur = conn.execute(
        "INSERT INTO model_providers "
        "(owner_user_id, name, base_url, api_key_encrypted, builtin_key, is_builtin, "
        " enabled, sort_order, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 1, 0, ?, ?)",
        (owner_user_id,
         builtin["name"] if builtin else (base_url or "我的模型"),
         base_url, _enc(api_key),
         builtin["key"] if builtin else "", 1 if builtin else 0, now, now),
    )
    stats["providers_created"] += 1
    return dict(conn.execute("SELECT * FROM model_providers WHERE id = ?", (cur.lastrowid,)).fetchone())


def _migrate_one_entry(conn, provider_id: int, model_id: str, context_limit: str, stats) -> dict:
    row = conn.execute(
        "SELECT * FROM model_entries WHERE provider_id = ? AND model_id = ?",
        (provider_id, model_id),
    ).fetchone()
    if row:
        return dict(row)
    now = _now()
    cur = conn.execute(
        "INSERT INTO model_entries "
        "(provider_id, model_id, display_name, context_limit, visible, is_default, "
        " enabled, sort_order, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 1, 0, 1, 0, ?, ?)",
        (provider_id, model_id, guess_display_name(model_id), context_limit or "", now, now),
    )
    stats["entries_created"] += 1
    return dict(conn.execute("SELECT * FROM model_entries WHERE id = ?", (cur.lastrowid,)).fetchone())


def seed_builtin_providers(conn=None) -> int:
    """把内置供应商目录登记进表（不填 key，仅登记地址）。

    这样管理员不必手抄地址就能「开通」一家供应商给用户用；
    用户自配时也只能从管理员登记过的地址里选，白名单天然成立。
    """
    from server.database import _get_connection
    conn = conn or _get_connection()
    ensure_tables(conn)
    created = 0
    for p in BUILTIN_PROVIDERS:
        row = conn.execute(
            "SELECT id FROM model_providers WHERE owner_user_id IS NULL AND base_url = ?",
            (p["base_url"],),
        ).fetchone()
        if row:
            continue
        now = _now()
        conn.execute(
            "INSERT INTO model_providers "
            "(owner_user_id, name, base_url, api_key_encrypted, builtin_key, is_builtin, "
            " enabled, sort_order, created_at, updated_at) "
            "VALUES (NULL, ?, ?, '', ?, 1, 1, 0, ?, ?)",
            (p["name"], p["base_url"], p["key"], now, now),
        )
        created += 1
    conn.commit()
    return created
