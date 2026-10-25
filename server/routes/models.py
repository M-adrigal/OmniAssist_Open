"""模型供应商与模型的配置接口。

分两组，权限与数据边界不同：

**平台组**（管理员，需 `model_config_global`）
- 供应商登记：地址由管理员决定，普通用户没有填地址的入口，白名单因此天然成立
- 模型池：挂在平台凭据下的模型，决定哪些对全体用户可见

**个人组**（任何登录用户，需 `model_config_personal`）
- 只能从管理员已登记的供应商里挑一家，填**自己的 key**
- 所有写库操作强制带 `owner_user_id = 当前用户 db_id`，
  绝不允许只按 id 取记录 —— 这是用户间数据隔离的第一道闸
"""

from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from server.routes.auth import require_login, require_permission, get_current_user
from server import model_store as ms

router = APIRouter(prefix="/api/config", tags=["model-config"])

MAX_MODEL_ID_LEN = 200
MAX_NAME_LEN = 60


# ===== 请求体 =====

class ProviderCreate(BaseModel):
    name: Optional[str] = None
    base_url: str
    api_key: Optional[str] = None
    builtin_key: Optional[str] = None


class ProviderUpdate(BaseModel):
    name: Optional[str] = None
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    enabled: Optional[bool] = None


class PoolEntryCreate(BaseModel):
    provider_id: int
    model_id: str
    display_name: Optional[str] = None
    context_limit: Optional[str] = None
    visible: bool = True
    is_default: bool = False
    # 单位：元/百万 token。留空表示「未标价」（不等于免费）
    price_in: Optional[float] = None
    price_out: Optional[float] = None
    price_cached: Optional[float] = None


class PoolEntryUpdate(BaseModel):
    model_id: Optional[str] = None
    display_name: Optional[str] = None
    context_limit: Optional[str] = None
    visible: Optional[bool] = None
    enabled: Optional[bool] = None
    is_default: Optional[bool] = None
    price_in: Optional[float] = None
    price_out: Optional[float] = None
    price_cached: Optional[float] = None


class MyModelCreate(BaseModel):
    """用户自配模型。两条路径二选一：

    - `platform_provider_id`：选一家平台已登记的供应商，地址由后端复制（白名单路径）
    - `base_url` + `provider_name`：自己填一家（自定义路径，用自己的 Key）
    """

    platform_provider_id: Optional[int] = None
    api_key: str
    model_id: str
    display_name: Optional[str] = None
    context_limit: Optional[str] = None
    provider_name: Optional[str] = None
    base_url: Optional[str] = None
    # 单位：元/百万 token（自付模型也要填，否则自己的花费也算不出来）
    price_in: Optional[float] = None
    price_out: Optional[float] = None
    price_cached: Optional[float] = None


class MyModelUpdate(BaseModel):
    model_id: Optional[str] = None
    display_name: Optional[str] = None
    context_limit: Optional[str] = None
    api_key: Optional[str] = None
    price_in: Optional[float] = None
    price_out: Optional[float] = None
    price_cached: Optional[float] = None


class CurrentModelUpdate(BaseModel):
    entry_id: Optional[int] = None


class TestRequest(BaseModel):
    entry_id: Optional[int] = None
    provider_id: Optional[int] = None
    model_id: Optional[str] = None


_PRICE_KEYS = ("price_in", "price_out", "price_cached")


def price_kwargs(body: BaseModel) -> dict:
    """提取请求体里**实际传了**的单价字段，供 update_entry(**kwargs) 展开。

    必须用 `exclude_unset`：不传和传空是两种意图 —— 前者是「这次不碰价格」，
    后者是「把这个模型的标价清掉」。若图省事直接读 body.price_in，用户改个显示名
    就会把已填的单价静默清空，这类「改 A 丢 B」的问题最难排查。
    """
    if not isinstance(body, BaseModel):
        return {}
    data = body.model_dump(exclude_unset=True)
    return {k: v for k, v in data.items() if k in _PRICE_KEYS}


def _clean(value: Optional[str], field: str, max_len: int = MAX_NAME_LEN) -> Optional[str]:
    if value is None:
        return None
    v = str(value).strip()
    if len(v) > max_len:
        raise HTTPException(status_code=400, detail=f"{field}过长，最多 {max_len} 字符")
    return v


def _public_entry(e: dict) -> dict:
    """对外返回的模型条目：不暴露内部凭据归属的数字 id，只给便于展示的语义字段。"""
    out = {
        "entry_id": e["id"],
        "model_id": e.get("model_id", ""),
        "display_name": e.get("display_name") or e.get("model_id", ""),
        "label": e.get("display_name") or e.get("model_id", ""),
        "provider_id": e.get("provider_id"),
        "provider_name": e.get("provider_name", ""),
        "context_limit": e.get("context_limit", "") or "",
        "owner": "platform" if e.get("provider_owner") is None else "user",
        "visible": bool(e.get("visible", 1)),
        "enabled": bool(e.get("enabled", 1)),
        "is_default": bool(e.get("is_default", 0)),
    }
    # 单价原样透出（None 表示未标价，与 0 元免费是两回事，前端据此显示「—」）
    for k in _PRICE_KEYS:
        v = e.get(k)
        out[k] = float(v) if v is not None else None
    return out


def _public_provider(p: dict, include_key_state: bool = True) -> dict:
    d = {
        "id": p["id"],
        "name": p.get("name", ""),
        "base_url": p.get("base_url", ""),
        "builtin_key": p.get("builtin_key", "") or "",
        "is_builtin": bool(p.get("is_builtin", 0)),
        "enabled": bool(p.get("enabled", 1)),
        "owner": p.get("scope", "platform"),
    }
    if include_key_state:
        d["api_key_masked"] = p.get("api_key_masked", "(未设置)")
        d["has_key"] = bool(p.get("has_key"))
    return d


# ================= 个人组 =================

@router.get("/models")
def list_selectable_models(request: Request, session_id: Optional[str] = None):
    """当前用户可选的模型：平台池（对用户可见的）+ 自己配的。供下拉选择与切换使用。

    带 session_id 时，`resolved` 反映「这个会话当前用的模型」而不是用户默认值 ——
    会话中途切过模型后，界面必须显示会话实际在用的那个。
    """
    user = require_permission(request, "model_config_personal", "read")
    uid = user["db_id"]
    if session_id:
        from server.database import get_session
        s = get_session(session_id)
        # 别人的会话当作没传：既不报错也不泄漏「这个会话存在且用了什么模型」
        if not s or s.get("user_id") != uid:
            session_id = None

    entries = ms.list_entries(uid, include_platform_pool=True)
    resolved = _current_summary(user, session_id)
    cur = resolved.get("entry_id")

    items = []
    for e in entries:
        if not e.get("enabled") or not e.get("provider_enabled", True):
            continue
        item = _public_entry(e)
        item["selected"] = (e["id"] == cur)
        items.append(item)
    return {"models": items, "current_entry_id": cur, "resolved": resolved}


def _current_summary(user: dict, session_id: Optional[str] = None) -> dict:
    from server.database import resolve_model_config
    # 管理员视角保留「隐藏但仍是他选中的那条」（管理端要显示自己在用哪个）；
    # 普通用户看不到 —— 否则输入框右下角还会显示一个已经下架的模型名。
    cfg = resolve_model_config(user["db_id"], session_id=session_id,
                               include_hidden=(user.get("user_type") == "admin"))
    return {
        "entry_id": cfg.get("entry_id"),
        "display_name": cfg.get("display_name") or cfg.get("model_id") or "",
        "model_id": cfg.get("model_id", ""),
        "provider_name": cfg.get("provider_name", ""),
        "owner": cfg.get("entry_owner", ""),
        "context_limit": cfg.get("context_limit", ""),
        "resolved_from": cfg.get("resolved_from", ""),
    }


@router.get("/available-providers")
def list_available_providers(request: Request):
    """用户自配时可选的供应商 = 管理员登记且启用的平台供应商。

    注意只返回地址，不返回平台 key 的明文，只需让用户知道「这家平台已经可用」。
    """
    require_login(request)
    rows = ms.list_providers(only_platform=True)
    return [
        {
            "id": p["id"],
            "name": p.get("name", ""),
            "base_url": p.get("base_url", ""),
            "builtin_key": p.get("builtin_key", "") or "",
            "has_platform_key": bool(p.get("has_key")),
            "enabled": bool(p.get("enabled", 1)),
        }
        for p in rows if p.get("enabled")
    ]


@router.put("/current-model")
def set_current_model(body: CurrentModelUpdate, request: Request):
    """设置自己的默认模型。传 null 表示跟随平台默认。"""
    user = require_permission(request, "model_config_personal", "write")
    uid = user["db_id"]
    if body.entry_id is not None:
        entry = ms.get_entry(body.entry_id, owner_user_id=uid)
        if not entry:
            raise HTTPException(status_code=404, detail="模型不存在或无权使用")
        if not entry.get("enabled"):
            raise HTTPException(status_code=400, detail="该模型已停用")
    ms.set_user_current_entry(uid, body.entry_id)
    return {"success": True, "resolved": _current_summary(user)}


@router.get("/my-models")
def list_my_models(request: Request):
    user = require_permission(request, "model_config_personal", "read")
    uid = user["db_id"]
    entries = ms.list_entries(uid)          # 只取 owner=自己的，绝不返回他人自配
    providers = ms.list_providers(uid)      # 同上
    return {
        "models": [_public_entry(e) for e in entries],
        "providers": [_public_provider(p) for p in providers],
    }


@router.post("/my-models")
def create_my_model(body: MyModelCreate, request: Request):
    """新增一个自配模型。

    两条路径：
    1. `platform_provider_id`：地址不从请求体来，只接受平台已登记的供应商 id，
       后端复制地址。这样用户无论如何提交参数，都不可能把对话内容导向一个未登记地址。
    2. `base_url`：用户自建供应商。地址是用户自己填的，用的是用户自己的 Key，
       成本与风险都由用户自担；平台 Key 永远不会出现在个人行上。
    """
    user = require_permission(request, "model_config_personal", "write")
    uid = user["db_id"]

    model_id = _clean(body.model_id, "模型 ID", MAX_MODEL_ID_LEN)
    if not model_id:
        raise HTTPException(status_code=400, detail="请填写模型 ID")
    api_key = (body.api_key or "").strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="请填写自己的 API Key")

    if body.platform_provider_id:
        platform = ms.get_provider(body.platform_provider_id)
        if not platform or platform.get("scope") != "platform":
            raise HTTPException(status_code=404, detail="供应商不存在")
        if not platform.get("enabled"):
            raise HTTPException(status_code=400, detail="该供应商已停用")
        builtin_key = platform.get("builtin_key", "")
        if not builtin_key:
            # 平台自建代理不开放给用户自带 Key（Key 归属说不清）
            raise HTTPException(status_code=400, detail="该供应商不支持用户自带 Key")
        # 在自己的归属下建一条同地址凭据（同一家只存一份 key，重复添加即更新）
        mine = ms.create_provider(
            name=platform.get("name", ""),
            base_url=platform.get("base_url", ""),
            api_key=api_key,
            owner_user_id=uid,
            builtin_key=builtin_key,
        )
    else:
        base_url = (body.base_url or "").strip()
        if not base_url:
            raise HTTPException(status_code=400, detail="请选择平台供应商或填写自定义地址")
        if not (base_url.startswith("http://") or base_url.startswith("https://")):
            raise HTTPException(status_code=400, detail="供应商地址必须以 http:// 或 https:// 开头")
        name = _clean(body.provider_name, "供应商名称") or base_url
        try:
            mine = ms.create_provider(
                name=name, base_url=base_url, api_key=api_key, owner_user_id=uid,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    try:
        created = ms.create_entry(
            provider_id=mine["id"],
            model_id=model_id,
            display_name=_clean(body.display_name, "显示名称") or ms.guess_display_name(model_id),
            context_limit=_clean(body.context_limit, "上下文长度", 32) or "",
            visible=False,      # 用户自配模型只对自己可见
            is_default=False,
            owner_user_id=uid,
            price_in=body.price_in,
            price_out=body.price_out,
            price_cached=body.price_cached,
        )
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))
    return {"success": True, "model": _public_entry(created), "provider": _public_provider(mine)}


@router.put("/my-models/{entry_id}")
def update_my_model(entry_id: int, body: MyModelUpdate, request: Request):
    user = require_permission(request, "model_config_personal", "write")
    uid = user["db_id"]
    entry = ms.get_entry(entry_id, owner_user_id=uid)
    if not entry or entry.get("provider_owner") is None:
        raise HTTPException(status_code=404, detail="模型不存在或无权修改")

    try:
        updated = ms.update_entry(
            entry_id, owner_user_id=uid,
            model_id=_clean(body.model_id, "模型 ID", MAX_MODEL_ID_LEN),
            display_name=_clean(body.display_name, "显示名称"),
            context_limit=_clean(body.context_limit, "上下文长度", 32),
            **price_kwargs(body),   # 只带上本次实际提交的单价字段
        )
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))
    if body.api_key is not None and body.api_key.strip():
        ms.update_provider(entry["provider_id"], owner_user_id=uid, api_key=body.api_key.strip())
    return {"success": True, "model": _public_entry(updated or entry)}


# ===== 个人供应商（自建凭据的管理）=====

class MyProviderUpdate(BaseModel):
    name: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None


@router.get("/my-providers")
def list_my_providers(request: Request):
    """当前用户自己的供应商（含平台路径复制的与自建的）。"""
    user = require_permission(request, "model_config_personal", "read")
    rows = ms.list_providers(user["db_id"])
    return [_public_provider(p) for p in rows]


@router.put("/my-providers/{provider_id}")
def update_my_provider(provider_id: int, body: MyProviderUpdate, request: Request):
    """修改自己的供应商（名称 / 地址 / Key）。归属校验在 store 内做。"""
    user = require_permission(request, "model_config_personal", "write")
    uid = user["db_id"]
    target = ms.get_provider(provider_id, owner_user_id=uid)
    if not target or target.get("scope") != "personal":
        raise HTTPException(status_code=404, detail="供应商不存在或无权修改")
    try:
        updated = ms.update_provider(
            provider_id, owner_user_id=uid,
            name=_clean(body.name, "供应商名称"),
            base_url=(body.base_url.strip() if body.base_url else None),
            api_key=(body.api_key.strip() if body.api_key else None),
        )
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))
    if not updated:
        raise HTTPException(status_code=404, detail="供应商不存在")
    return {"success": True, "provider": _public_provider(updated)}


@router.delete("/my-providers/{provider_id}")
def delete_my_provider(provider_id: int, request: Request):
    """删除自己的供应商，其下模型一并删除（外键级联）。

    若当前默认模型或某会话正在用其中条目，先把用户默认值清回「跟随平台」，
    避免留下指向已删模型的悬空引用。
    """
    user = require_permission(request, "model_config_personal", "write")
    uid = user["db_id"]
    target = ms.get_provider(provider_id, owner_user_id=uid)
    if not target or target.get("scope") != "personal":
        raise HTTPException(status_code=404, detail="供应商不存在或无权删除")

    entry_ids = [e["id"] for e in ms.list_entries(uid) if e["provider_id"] == provider_id]
    if ms.get_user_current_entry(uid) in entry_ids:
        ms.set_user_current_entry(uid, None)
    # 正在用这些条目的会话交由 resolve 时自然回落（条目没了 → 平台默认），无需逐个改
    if not ms.delete_provider(provider_id, owner_user_id=uid):
        raise HTTPException(status_code=500, detail="删除失败")
    return {"success": True, "removed_entries": len(entry_ids)}


@router.delete("/my-models/{entry_id}")
def delete_my_model(entry_id: int, request: Request):
    user = require_permission(request, "model_config_personal", "write")
    uid = user["db_id"]
    entry = ms.get_entry(entry_id, owner_user_id=uid)
    if not entry or entry.get("provider_owner") is None:
        raise HTTPException(status_code=404, detail="模型不存在或无权删除")

    if ms.get_user_current_entry(uid) == entry_id:
        ms.set_user_current_entry(uid, None)
    if not ms.delete_entry(entry_id, owner_user_id=uid):
        raise HTTPException(status_code=500, detail="删除失败")

    # 若这条凭据下已无模型，连同凭据一起删掉（避免留下孤立的 key）
    remain = ms.list_entries(uid)
    still = [e for e in remain if e["provider_id"] == entry["provider_id"]]
    if not still:
        ms.delete_provider(entry["provider_id"], owner_user_id=uid)
    return {"success": True}


# ================= 平台组（管理员）=================

@router.get("/builtin-providers")
def list_builtin_providers(request: Request):
    """内置供应商目录（地址清单）。

    管理员从这里选「开通哪一家」，地址用目录里的规范值，避免手抄地址出错。
    普通用户不调这个接口 —— 他们只能看到管理员已开通的那几家。
    """
    require_permission(request, "model_config_global", "read")
    return ms.BUILTIN_PROVIDERS


@router.get("/providers")
def admin_list_providers(request: Request):
    """平台供应商列表（全局模型配置的数据源）。

    **只返回平台级（owner IS NULL）行**。用户自配凭据是个人私有数据，
    混进来会造成两个问题：① 别人的 Key 出现在公共管理界面，隐私越界；
    ② 「已开通 N 家」把个人行算进去，平台可用供应商数量失真。
    个人配置各人在「个人模型」tab 只能看到自己的。
    """
    require_permission(request, "model_config_global", "read")
    rows = [p for p in ms.list_providers() if p.get("scope") == "platform"]
    return [_public_provider(p) for p in rows]


@router.post("/providers")
def admin_create_provider(body: ProviderCreate, request: Request):
    require_permission(request, "model_config_global", "write")
    name = _clean(body.name, "供应商名称")
    api_key = (body.api_key or "").strip()
    builtin_key = (body.builtin_key or "").strip()

    # 从内置目录选的时候，用目录里的规范地址与名称，避免手抄地址出错
    if builtin_key:
        spec = ms.BUILTIN_BY_KEY.get(builtin_key)
        if not spec:
            raise HTTPException(status_code=400, detail="未知的内置供应商")
        name = name or spec["name"]
        base_url = spec["base_url"]
    else:
        base_url = (body.base_url or "").strip()
        if not base_url:
            raise HTTPException(status_code=400, detail="请填写供应商地址")

    try:
        p = ms.create_provider(name=name or base_url, base_url=base_url, api_key=api_key,
                               owner_user_id=None, builtin_key=builtin_key)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _public_provider(p)


@router.put("/providers/{provider_id}")
def admin_update_provider(provider_id: int, body: ProviderUpdate, request: Request):
    require_permission(request, "model_config_global", "write")
    p = ms.get_provider(provider_id)
    if not p or p.get("scope") != "platform":
        raise HTTPException(status_code=404, detail="供应商不存在或不属于平台")
    # 内置供应商的地址是各家官方规范值，改不得；管理员自建的代理地址可改
    base_url = body.base_url.strip() if body.base_url else None
    if base_url and p.get("is_builtin"):
        raise HTTPException(status_code=400, detail="内置供应商的地址不可修改")
    try:
        updated = ms.update_provider(
            provider_id, owner_user_id=None,
            name=_clean(body.name, "供应商名称"),
            api_key=(body.api_key.strip() if body.api_key else None),
            base_url=base_url,
            enabled=body.enabled,
        )
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))
    if not updated:
        raise HTTPException(status_code=404, detail="供应商不存在")
    if body.api_key:
        try:
            from server.main import refresh_global_llm
            refresh_global_llm()
        except Exception:
            pass
    return _public_provider(updated)


@router.delete("/providers/{provider_id}")
def admin_delete_provider(provider_id: int, request: Request):
    require_permission(request, "model_config_global", "write")
    p = ms.get_provider(provider_id)
    if not p or p.get("scope") != "platform":
        raise HTTPException(status_code=404, detail="供应商不存在或不属于平台")
    if not ms.delete_provider(provider_id, owner_user_id=None):
        raise HTTPException(status_code=500, detail="删除失败")
    return {"success": True}


@router.get("/pool")
def admin_list_pool(request: Request):
    """平台模型池（全局模型配置的数据源）。

    只返回挂在平台凭据下的条目；用户自配模型是个人数据，不在此出现。
    注意与 `list_entries(only_platform=True)` 的区别：那个带 visible=1 过滤，
    是给普通用户挑模型用的；管理员要连「仅管理员可见」的条目一起管，故不过滤。
    """
    require_permission(request, "model_config_global", "read")
    entries = [e for e in ms.list_entries() if e.get("provider_owner") is None]
    out = []
    for e in entries:
        d = _public_entry(e)
        d["provider_owner"] = "platform"
        out.append(d)
    return out


@router.post("/pool")
def admin_create_pool_entry(body: PoolEntryCreate, request: Request):
    require_permission(request, "model_config_global", "write")
    model_id = _clean(body.model_id, "模型 ID", MAX_MODEL_ID_LEN)
    if not model_id:
        raise HTTPException(status_code=400, detail="请填写模型 ID")
    try:
        e = ms.create_entry(
            provider_id=body.provider_id,
            model_id=model_id,
            display_name=_clean(body.display_name, "显示名称") or ms.guess_display_name(model_id),
            context_limit=_clean(body.context_limit, "上下文长度", 32) or "",
            visible=body.visible,
            is_default=body.is_default,
            price_in=body.price_in,
            price_out=body.price_out,
            price_cached=body.price_cached,
        )
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))
    return _public_entry(e)


@router.put("/pool/{entry_id}")
def admin_update_pool_entry(entry_id: int, body: PoolEntryUpdate, request: Request):
    require_permission(request, "model_config_global", "write")
    e = ms.get_entry(entry_id)
    if not e or e.get("provider_owner") is not None:
        raise HTTPException(status_code=404, detail="模型不存在或不属于平台")
    try:
        updated = ms.update_entry(
            entry_id,
            model_id=_clean(body.model_id, "模型 ID", MAX_MODEL_ID_LEN),
            display_name=_clean(body.display_name, "显示名称"),
            context_limit=_clean(body.context_limit, "上下文长度", 32),
            visible=body.visible,
            enabled=body.enabled,
            is_default=body.is_default,
            **price_kwargs(body),   # 只带上本次实际提交的单价字段
        )
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))
    if not updated:
        raise HTTPException(status_code=404, detail="模型不存在")
    return _public_entry(updated)


@router.delete("/pool/{entry_id}")
def admin_delete_pool_entry(entry_id: int, request: Request):
    require_permission(request, "model_config_global", "write")
    e = ms.get_entry(entry_id)
    if not e or e.get("provider_owner") is not None:
        raise HTTPException(status_code=404, detail="模型不存在或不属于平台")
    if not ms.delete_entry(entry_id):
        raise HTTPException(status_code=500, detail="删除失败")
    return {"success": True}


@router.post("/test")
def test_model(body: TestRequest, request: Request):
    """发一次极短请求验证「地址 + key + 模型 ID」三者匹配。

    模型 ID 差一个字符就是 404，而这种错误只有用户真正用的时候才会暴露，
    所以配置界面必须能当场测。
    """
    user = require_permission(request, "model_config_personal", "read")
    uid = user["db_id"]
    is_admin = bool(user.get("user_type") == "admin")

    api_key = base_url = model_id = ""
    if body.entry_id:
        entry = ms.get_entry(body.entry_id, owner_user_id=uid)
        if not entry:
            raise HTTPException(status_code=404, detail="模型不存在或无权使用")
        provider = ms.get_provider(entry["provider_id"], owner_user_id=uid)
        if not provider:
            raise HTTPException(status_code=404, detail="供应商不存在或无权使用")
        from server.database import _get_connection, _decrypt_db
        row = _get_connection().execute(
            "SELECT api_key_encrypted, base_url FROM model_providers WHERE id = ?",
            (provider["id"],)).fetchone()
        api_key = _decrypt_db(row["api_key_encrypted"] or "") if row else ""
        base_url = row["base_url"] if row else provider.get("base_url", "")
        model_id = entry.get("model_id", "")
    elif body.provider_id and body.model_id:
        provider = ms.get_provider(body.provider_id, owner_user_id=None if is_admin else uid)
        if not provider:
            raise HTTPException(status_code=404, detail="供应商不存在或无权使用")
        from server.database import _get_connection, _decrypt_db
        row = _get_connection().execute(
            "SELECT api_key_encrypted, base_url FROM model_providers WHERE id = ?",
            (provider["id"],)).fetchone()
        api_key = _decrypt_db(row["api_key_encrypted"] or "") if row else ""
        base_url = row["base_url"] if row else provider.get("base_url", "")
        model_id = body.model_id.strip()
    else:
        raise HTTPException(status_code=400, detail="缺少测试参数")

    if not api_key:
        return {"ok": False, "error": "该供应商还没有配置 API Key"}
    if not model_id:
        return {"ok": False, "error": "模型 ID 为空"}

    import time as _time
    started = _time.time()
    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key, base_url=base_url or None, timeout=20.0, max_retries=0)
        resp = client.chat.completions.create(
            model=model_id,
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=8,
        )
        real_model = getattr(resp, "model", "") or ""
        return {
            "ok": True,
            "latency_ms": int((_time.time() - started) * 1000),
            "real_model": real_model,
            "note": "" if not real_model or real_model == model_id else f"实际响应模型为 {real_model}",
        }
    except Exception as e:
        msg = str(e)
        hint = ""
        low = msg.lower()
        if "404" in msg or "not found" in low or "model_not_found" in low:
            hint = "模型 ID 可能写错了，请对照供应商控制台里的完整名称"
        elif "401" in msg or "unauthorized" in low or "invalid api key" in low:
            hint = "API Key 无效或无权限"
        elif "timeout" in low or "timed out" in low:
            hint = "连接超时，检查服务器能否访问该地址"
        return {"ok": False, "error": msg[:300], "hint": hint}
