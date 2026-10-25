"""每位用户一份的「记忆与进化」接口。

设计前提（四条，改动前请先读）：

1. **记忆是第三份用户数据**。它必须能被完整读走（GET）、整份覆盖（PUT）、
   整体清空（POST /reset）、当成文本导出和导入（text 字段 / POST /parse）。
   少任何一条，用户都会以为存上了但其实拿不回来。
2. **模型只提议，人不点头就不算数**。
   POST /extract 只返回整理结果，**绝不落库**；写入永远走 PUT（人来保存）。
   这是 Cursor 那条路线：自动生成的记忆要人批准才保存。与其事后去猜
   「这条记忆是模型记错了还是我记错了」，不如在入库前就把它拦下。
3. **失误必须看得见，不能静默**。
   超出上限 → 400 且一条都不写；列表段超条数 → 挤掉最旧的并在 warnings 里
   说出来；导入认不出的行 → 原样还给用户（unmatched），不偷偷丢掉。
4. **每次改动都留档，且可恢复**。
   保存 / 导入 / 重置 / 单条增删都会先存一份快照（保留最近 30 版），
   删得多的改动会主动提醒「已留档，可恢复」。重置因此第一次变成可撤销的操作。

段是可扩展的：界面要的段定义从 GET 的 `registry` 里拿，不要写死四段。
新增一个记忆段只改 `memory_store.SECTION_REGISTRY`，接口与界面自动跟上。

权限：只认自己那份。管理员也看不到别人的记忆（与会话、文件同口径）；
组织级开关由管理员在别处控制记忆功能是否开放，不在这里。
"""
import uuid

from fastapi import APIRouter, HTTPException, Request

from server.models import (
    MemoryEnabledUpdate,
    MemoryEvolveRequest,
    MemoryEvolveResponse,
    MemoryExtractRequest,
    MemoryExtractResponse,
    MemoryItemRequest,
    MemoryParseRequest,
    MemoryParseResponse,
    MemoryProposal,
    MemoryResponse,
    MemoryUpdate,
    MemoryVersion,
)
from server.routes.auth import require_login

router = APIRouter(prefix="/api/memory", tags=["memory"])


def _store():
    from server import memory_store
    return memory_store


def _respond(mem: dict, with_versions: bool = False) -> MemoryResponse:
    ms = _store()
    flat = mem.get("sections") or {k: (mem.get(k) or "") for k in ms.SECTION_KEYS}
    return MemoryResponse(
        work=flat.get("work", ""),
        personal=flat.get("personal", ""),
        focus=flat.get("focus", ""),
        recent=flat.get("recent", ""),
        sections=flat,
        items=mem.get("items") or {},
        registry=ms.SECTION_REGISTRY,
        enabled=mem.get("enabled", 1),
        updated_at=mem.get("updated_at"),
        text=ms.render_text(flat),
        total_chars=sum(len(str(v or "").strip()) for v in flat.values()),
        warnings=mem.get("warnings", []),
        limits={
            "total_chars": ms.MAX_TOTAL_CHARS,
            "recent_items": ms.MAX_RECENT_ITEMS,
        },
        version_count=mem.get("version_count", 0),
        versions=mem.get("versions", []),
    )


@router.get("", response_model=MemoryResponse)
def get_memory(request: Request):
    from server import memory_store
    user = require_login(request)
    mem = memory_store.get_memory(user["db_id"])
    mem["versions"] = [
        MemoryVersion(**v) for v in memory_store.list_versions(user["db_id"])
    ]
    return _respond(mem)


@router.put("", response_model=MemoryResponse)
def put_memory(body: MemoryUpdate, request: Request):
    """整份覆盖各段内容。不带 enabled —— 开关走单独接口。"""
    from server import memory_store
    user = require_login(request)
    sections = dict(body.sections or {})
    # 老字段优先合并进来：老前端还在发 work/personal/...，不能让它静默失效
    for key in ("work", "personal", "focus", "recent"):
        val = getattr(body, key, None)
        if val is not None and key not in sections:
            sections[key] = val
    try:
        saved = memory_store.save_memory(user["db_id"], sections)
    except memory_store.MemoryTooLong as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _respond(saved)


@router.post("/item", response_model=MemoryResponse)
def add_memory_item(body: MemoryItemRequest, request: Request):
    """往某个段追加一条。只动这一段，其它段不受影响。"""
    from server import memory_store
    user = require_login(request)
    try:
        saved = memory_store.add_entry(user["db_id"], body.section_key, body.content or "")
    except KeyError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _respond(saved)


@router.delete("/item", response_model=MemoryResponse)
def delete_memory_item(entry_id: int, request: Request):
    """删一条。删之前会留档，删错了可以恢复。

    id 走 query 而不是请求体：DELETE 带 body 在中间层（代理 / CDN）上被吞掉
    是常见事故，而这个接口只有一个参数，没必要冒这个险。
    """
    from server import memory_store
    user = require_login(request)
    if not entry_id:
        raise HTTPException(status_code=400, detail="缺少条目 id")
    try:
        saved = memory_store.delete_entry(user["db_id"], entry_id)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return _respond(saved)


@router.get("/versions", response_model=MemoryResponse)
def list_memory_versions(request: Request):
    """版本列表 + 当前内容。正文要拿某一版用下面的 /versions/{id}。"""
    from server import memory_store
    user = require_login(request)
    mem = memory_store.get_memory(user["db_id"])
    mem["versions"] = [
        MemoryVersion(**v) for v in memory_store.list_versions(user["db_id"])
    ]
    return _respond(mem)


@router.get("/versions/{version_id}")
def get_memory_version(version_id: int, request: Request):
    from server import memory_store
    user = require_login(request)
    text = memory_store.get_version(user["db_id"], version_id)
    if text is None:
        raise HTTPException(status_code=404, detail="版本不存在或不属于当前用户")
    return {"id": version_id, "text": text}


@router.post("/versions/{version_id}/restore", response_model=MemoryResponse)
def restore_memory_version(version_id: int, request: Request):
    """恢复到某一版。恢复本身也会留档（想退回恢复前有路可走）。"""
    from server import memory_store
    user = require_login(request)
    try:
        saved = memory_store.restore_version(user["db_id"], version_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="版本不存在或不属于当前用户")
    except memory_store.MemoryTooLong as e:
        raise HTTPException(status_code=400, detail=str(e))
    saved["versions"] = [
        MemoryVersion(**v) for v in memory_store.list_versions(user["db_id"])
    ]
    return _respond(saved)


@router.post("/enabled", response_model=MemoryResponse)
def set_memory_enabled(body: MemoryEnabledUpdate, request: Request):
    """总开关：关掉之后，这些内容在每轮对话里都不再注入（但不会被删）。

    关 ≠ 删。这是 ChatGPT「关闭记忆不会删除过去的对话」那条口径：
    开关是「用不用」，重置才是「清不清」。
    """
    from server import memory_store
    user = require_login(request)
    memory_store.set_enabled(user["db_id"], body.enabled)
    return _respond(memory_store.get_memory(user["db_id"]))


@router.post("/reset", response_model=MemoryResponse)
def reset_memory(request: Request):
    """清空所有段。清空前的版本会留档 —— 重置第一次变成可撤销的操作。"""
    from server import memory_store
    user = require_login(request)
    saved = memory_store.reset_memory(user["db_id"])
    saved["versions"] = [
        MemoryVersion(**v) for v in memory_store.list_versions(user["db_id"])
    ]
    return _respond(saved)


@router.post("/evolve", response_model=MemoryEvolveResponse)
def evolve_memory(body: MemoryEvolveRequest, request: Request):
    """从对话里提炼「值得长期记住的新条目」。**只返回提议，不落库。**

    这是「进化」那条写入通道：人来管记忆会越用越懒，模型从对话里替他发现
    「这句其实该记下来」。但落库仍然要人勾选 —— 与 /extract 同一条口径
    （模型只提议，人不点头就不算数）。

    隐私上必须透明：这个接口会把会话正文发给模型，所以界面上要先说清楚
    「会把这个会话最近 N 条对话发给模型」。用户不点就不会有这次调用。

    不指定 session_id 时用最近更新的那个会话。别人的会话一律读不到。
    """
    import uuid as _uuid

    from server import memory_store
    user = require_login(request)
    uid = user["db_id"]

    from server.database import get_session as _get_session, list_sessions as _list_sessions
    session = None
    if body.session_id:
        session = _get_session(body.session_id)
        # 只认自己的会话：会话 id 是可猜的，不做这步就是越权读别人的对话
        if not session or session.get("user_id") != uid:
            raise HTTPException(status_code=404, detail="会话不存在或不属于当前用户")
    else:
        sessions = _list_sessions(uid) or []
        session = sessions[0] if sessions else None
    if not session:
        raise HTTPException(status_code=400, detail="还没有可提炼的对话，先去聊几句")

    from server.routes.chat import _load_session_messages
    messages = _load_session_messages(session["id"]) or []
    conv = memory_store.render_conversation(
        messages, max_messages=body.max_messages or memory_store.MAX_EVOLVE_MESSAGES)
    if not conv:
        raise HTTPException(status_code=400, detail="这个会话还没有可提炼的对话")

    try:
        from server.routes.chat import _resolve_llm_client
        llm, cfg = _resolve_llm_client(uid)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"解析可用模型失败：{e}")
    if llm is None:
        raise HTTPException(
            status_code=400,
            detail="当前没有可用的模型：管理员未开放任何模型，请联系管理员开通，或在设置中配置自己的模型",
        )

    try:
        from server.usage_store import make_sink as _make_sink
        llm.usage_sink = _make_sink(
            user_id=uid, session_id=None, run_id=_uuid.uuid4().hex[:12],
            provider_name=cfg.get("provider_name", ""),
            entry_owner=cfg.get("entry_owner", ""),
        )
    except Exception as e:
        import logging
        logging.getLogger("server").warning(f"记忆提炼用量埋点初始化失败（不影响提炼）: {e}")

    current = memory_store.get_memory(uid)
    prompt = memory_store.build_evolve_prompt(conv, current.get("items") or {})
    try:
        resp = llm.chat([{"role": "user", "content": prompt}], tools=None, source="memory")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"模型提炼失败（{e}）。记忆没有改动。")

    raw = (resp.get("content") or "").strip()
    parsed = memory_store.parse_proposals(raw)
    fresh, dup = memory_store.filter_new_items(current.get("items") or {}, parsed["proposals"])
    return MemoryEvolveResponse(
        proposals=fresh,
        session_id=session.get("id"),
        session_title=session.get("title"),
        message_count=len([m for m in messages if isinstance(m, dict)
                           and (m.get("role") in ("user", "assistant"))]),
        duplicate_skipped=dup,
        dropped=parsed["dropped"],
        raw=raw,
    )


@router.post("/parse", response_model=MemoryParseResponse)
def parse_memory(body: MemoryParseRequest, request: Request):
    """把粘贴进来的文本解析成各段（不落库，由前端填进编辑器给用户看一眼）。"""
    from server import memory_store
    require_login(request)
    parsed = memory_store.parse_text(body.text or "")
    sec = parsed["sections"]
    return MemoryParseResponse(
        work=sec.get("work", ""),
        personal=sec.get("personal", ""),
        focus=sec.get("focus", ""),
        recent=sec.get("recent", ""),
        unmatched=parsed["unmatched"],
    )


@router.post("/extract", response_model=MemoryExtractResponse)
def extract_memory(body: MemoryExtractRequest, request: Request):
    """让模型把一段自述整理成各段。**只返回建议，不落库。**

    写库的是人（PUT），模型只是帮把手。失败时原文会原样还回去 —— 用户
    敲进去的字一个都不能没，否则没人敢再往里贴东西。
    """
    from server import memory_store
    user = require_login(request)
    src = (body.text or "").strip()
    if not src:
        raise HTTPException(status_code=400, detail="先写点内容，再让模型整理")

    try:
        from server.routes.chat import _resolve_llm_client
        llm, cfg = _resolve_llm_client(user["db_id"])
    except Exception as e:  # 解析模型配置本身出问题，属于服务端故障
        raise HTTPException(status_code=500, detail=f"解析可用模型失败：{e}")

    if llm is None:
        raise HTTPException(
            status_code=400,
            detail="当前没有可用的模型：管理员未开放任何模型，请联系管理员开通，或在设置中配置自己的模型",
        )

    # 用量埋点：这次调用不在任何会话里（session_id 为空），但一样要记账。
    # 不挂 sink 的话，记忆整理花掉的 token 会凭空消失，看板上对不上账。
    try:
        from server.usage_store import make_sink as _make_sink
        llm.usage_sink = _make_sink(
            user_id=user["db_id"], session_id=None, run_id=uuid.uuid4().hex[:12],
            provider_name=cfg.get("provider_name", ""),
            entry_owner=cfg.get("entry_owner", ""),
        )
    except Exception as e:
        import logging
        logging.getLogger("server").warning(f"记忆整理用量埋点初始化失败（不影响整理）: {e}")

    prompt = memory_store.build_extract_prompt(src)
    try:
        resp = llm.chat([{"role": "user", "content": prompt}], tools=None, source="memory")
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"模型整理失败（{e}）。你输入的原文没有改动，可以手动编辑后保存。",
        )

    raw = (resp.get("content") or "").strip()
    parsed = memory_store.parse_text(raw)
    sec = parsed["sections"]
    return MemoryExtractResponse(
        work=sec.get("work", ""),
        personal=sec.get("personal", ""),
        focus=sec.get("focus", ""),
        recent=sec.get("recent", ""),
        unmatched=parsed["unmatched"],
        raw=raw,
    )
