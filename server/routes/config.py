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
    from server.database import resolve_model_config
    user = require_login(request)
    cfg = resolve_model_config(user["id"])
    return ModelConfigResponse(
        model_name=cfg.get("model_name", ""),
        base_url=cfg.get("base_url", ""),
        api_key_masked=cfg.get("api_key_masked", "(未设置)"),
        context_limit=cfg.get("context_limit", ""),
        config_type=cfg.get("config_type", "none"),
        show_thought=cfg.get("show_thought", False),
    )


@router.put("", response_model=ModelConfigResponse)
def update_config(body: ModelConfigUpdate, request: Request):
    from server.database import save_model_config
    user = require_login(request)

    kwargs = {}
    if body.api_key is not None:
        kwargs["api_key"] = body.api_key
    if body.base_url is not None:
        kwargs["base_url"] = body.base_url
    if body.model_name is not None:
        kwargs["model_name"] = body.model_name
    if body.context_limit is not None:
        kwargs["context_limit"] = body.context_limit
    if body.show_thought is not None:
        kwargs["show_thought"] = body.show_thought

    cfg = save_model_config(user["id"], **kwargs)

    from server.main import update_agent_context_limit, update_agent_show_thought
    update_agent_context_limit(cfg.get("context_limit", ""))
    update_agent_show_thought(cfg.get("show_thought", False))

    return ModelConfigResponse(
        model_name=cfg.get("model_name", ""),
        base_url=cfg.get("base_url", ""),
        api_key_masked=cfg.get("api_key_masked", "(未设置)"),
        context_limit=cfg.get("context_limit", ""),
        config_type="personal",
        show_thought=cfg.get("show_thought", False),
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
            show_thought=False,
        )
    return ModelConfigResponse(
        model_name=cfg.get("model_name", ""),
        base_url=cfg.get("base_url", ""),
        api_key_masked=cfg.get("api_key_masked", "(未设置)"),
        context_limit=cfg.get("context_limit", ""),
        config_type="global",
        show_thought=cfg.get("show_thought", False),
    )


@router.put("/global", response_model=ModelConfigResponse)
def update_global_config(body: ModelConfigUpdate, request: Request):
    from server.database import save_model_config
    require_permission(request, "model_config_global", "write")

    kwargs = {}
    if body.api_key is not None:
        kwargs["api_key"] = body.api_key
    if body.base_url is not None:
        kwargs["base_url"] = body.base_url
    if body.model_name is not None:
        kwargs["model_name"] = body.model_name
    if body.context_limit is not None:
        kwargs["context_limit"] = body.context_limit
    if body.show_thought is not None:
        kwargs["show_thought"] = body.show_thought

    cfg = save_model_config(None, **kwargs)

    from server.main import update_agent_context_limit, refresh_global_llm, update_agent_show_thought
    update_agent_context_limit(cfg.get("context_limit", ""))
    update_agent_show_thought(cfg.get("show_thought", False))
    refresh_global_llm()

    return ModelConfigResponse(
        model_name=cfg.get("model_name", ""),
        base_url=cfg.get("base_url", ""),
        api_key_masked=cfg.get("api_key_masked", "(未设置)"),
        context_limit=cfg.get("context_limit", ""),
        config_type="global",
        show_thought=cfg.get("show_thought", False),
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