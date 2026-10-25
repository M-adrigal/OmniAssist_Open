from fastapi import APIRouter, HTTPException, Request
from server.models import ModelConfigUpdate, ModelConfigResponse, SearchConfigResponse
from server.routes.auth import require_permission, require_login

router = APIRouter(prefix="/api/config", tags=["config"])


def _mask_key(key: str) -> str:
    if not key:
        return "(未设置)"
    if len(key) <= 8:
        return "*" * len(key)
    return key[:4] + "*" * (len(key) - 8) + key[-4:]


@router.get("", response_model=ModelConfigResponse)
def get_config(request: Request):
    """当前生效的模型 + 个人偏好。

    模型部分现在由「凭据 + 模型」两层结构解析（见 server/model_store.py），
    返回的 model_name / base_url / config_type 等旧字段保留，供旧界面过渡使用。
    """
    from server.database import resolve_model_config
    user = require_login(request)
    cfg = resolve_model_config(user["db_id"])
    return ModelConfigResponse(
        model_name=cfg.get("model_name", ""),
        base_url=cfg.get("base_url", ""),
        api_key_masked=cfg.get("api_key_masked", "(未设置)"),
        context_limit=cfg.get("context_limit", ""),
        config_type=cfg.get("config_type", "none"),
        thinking_mode=cfg.get("thinking_mode", "low"),
        max_iterations=cfg.get("max_iterations", 10),
        temperature_mode=cfg.get("temperature_mode", "auto"),
        temperature=cfg.get("temperature", 0.7),
        entry_id=cfg.get("entry_id"),
        display_name=cfg.get("display_name") or "",
        model_id=cfg.get("model_id") or "",
        provider_name=cfg.get("provider_name") or "",
        entry_owner=cfg.get("entry_owner") or "",
        resolved_from=cfg.get("resolved_from") or "",
    )


@router.put("", response_model=ModelConfigResponse)
def update_config(body: ModelConfigUpdate, request: Request):
    """保存个人偏好。

    模型端点（地址 / key / 模型 ID）**不再从这里写入** —— 它们已拆到
    /api/config/my-models 与 /api/config/current-model，避免「一个人保存配置
    改到全平台单例」以及「任意用户填任意地址」两个问题。
    这里传上来的模型字段一律忽略。
    """
    from server.database import save_model_config, resolve_model_config
    user = require_permission(request, "model_config_personal", "write")

    kwargs = {}
    if body.thinking_mode is not None:
        kwargs["thinking_mode"] = body.thinking_mode
    if body.max_iterations is not None:
        kwargs["max_iterations"] = body.max_iterations
    if body.temperature_mode is not None:
        kwargs["temperature_mode"] = body.temperature_mode
    if body.temperature is not None:
        kwargs["temperature"] = body.temperature

    if kwargs:
        save_model_config(user["db_id"], clear_api_key=False, **kwargs)

    cfg = resolve_model_config(user["db_id"])
    return ModelConfigResponse(
        model_name=cfg.get("model_name", ""),
        base_url=cfg.get("base_url", ""),
        api_key_masked=cfg.get("api_key_masked", "(未设置)"),
        context_limit=cfg.get("context_limit", ""),
        config_type=cfg.get("config_type", "none"),
        thinking_mode=cfg.get("thinking_mode", "low"),
        max_iterations=cfg.get("max_iterations", 10),
        temperature_mode=cfg.get("temperature_mode", "auto"),
        temperature=cfg.get("temperature", 0.7),
        entry_id=cfg.get("entry_id"),
        display_name=cfg.get("display_name") or "",
        model_id=cfg.get("model_id") or "",
        provider_name=cfg.get("provider_name") or "",
        entry_owner=cfg.get("entry_owner") or "",
        resolved_from=cfg.get("resolved_from") or "",
    )


@router.get("/global", response_model=ModelConfigResponse)
def get_global_config(request: Request):
    from server.database import get_model_config
    require_permission(request, "model_config_global", "read")
    cfg = get_model_config(None)
    if not cfg:
        return ModelConfigResponse(
            model_name="", base_url="", api_key_masked="(未设置)",
            context_limit="", config_type="global",
            thinking_mode="low", max_iterations=10,
            temperature_mode="auto", temperature=0.7,
        )
    return ModelConfigResponse(
        model_name=cfg.get("model_name", ""),
        base_url=cfg.get("base_url", ""),
        api_key_masked=cfg.get("api_key_masked", "(未设置)"),
        context_limit=cfg.get("context_limit", ""),
        config_type="global",
        thinking_mode=cfg.get("thinking_mode", "low"),
        max_iterations=cfg.get("max_iterations", 10),
        temperature_mode=cfg.get("temperature_mode", "auto"),
        temperature=cfg.get("temperature", 0.7),
    )


@router.put("/global", response_model=ModelConfigResponse)
def update_global_config(body: ModelConfigUpdate, request: Request):
    from server.database import save_model_config
    require_permission(request, "model_config_global", "write")

    kwargs = {}
    # 全局配置没有「回落」对象，未填 key 时保留原全局 key（避免误清空导致全员不可用）
    if body.api_key is not None:
        kwargs["api_key"] = body.api_key
    if body.base_url is not None:
        kwargs["base_url"] = body.base_url
    if body.model_name is not None:
        kwargs["model_name"] = body.model_name
    if body.context_limit is not None:
        kwargs["context_limit"] = body.context_limit
    if body.thinking_mode is not None:
        kwargs["thinking_mode"] = body.thinking_mode
    if body.max_iterations is not None:
        kwargs["max_iterations"] = body.max_iterations
    if body.temperature_mode is not None:
        kwargs["temperature_mode"] = body.temperature_mode
    if body.temperature is not None:
        kwargs["temperature"] = body.temperature

    cfg = save_model_config(None, clear_api_key=False, **kwargs)

    try:
        from __main__ import update_agent_context_limit, refresh_global_llm, update_agent_thinking_mode, update_agent_temperature_policy
    except ImportError:
        from server.main import update_agent_context_limit, refresh_global_llm, update_agent_thinking_mode, update_agent_temperature_policy
    update_agent_context_limit(cfg.get("context_limit", ""))
    update_agent_thinking_mode(cfg.get("thinking_mode", "low"))
    update_agent_temperature_policy(cfg.get("temperature_mode", "auto"), cfg.get("temperature", 0.7))
    try:
        refresh_global_llm()
    except Exception as e:
        import logging
        logging.getLogger("server").warning(f"刷新全局 LLM 客户端失败: {e}")

    return ModelConfigResponse(
        model_name=cfg.get("model_name", ""),
        base_url=cfg.get("base_url", ""),
        api_key_masked=cfg.get("api_key_masked", "(未设置)"),
        context_limit=cfg.get("context_limit", ""),
        config_type="global",
        thinking_mode=cfg.get("thinking_mode", "low"),
        max_iterations=cfg.get("max_iterations", 10),
        temperature_mode=cfg.get("temperature_mode", "auto"),
        temperature=cfg.get("temperature", 0.7),
    )


@router.get("/search", response_model=SearchConfigResponse)
def get_search_config(request: Request):
    from server.database import get_search_config
    require_permission(request, "search_config", "read")
    cfg = get_search_config()
    return SearchConfigResponse(
        tavily_api_key_masked=cfg.get("tavily_api_key_masked", "(未设置)"),
    )


@router.put("/search", response_model=SearchConfigResponse)
def update_search_config(body: SearchConfigResponse, request: Request):
    from server.database import save_search_config
    require_permission(request, "search_config", "write")
    cfg = save_search_config(tavily_api_key=body.tavily_api_key)
    return SearchConfigResponse(
        tavily_api_key_masked=cfg.get("tavily_api_key_masked", "(未设置)"),
    )