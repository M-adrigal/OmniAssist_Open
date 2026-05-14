from fastapi import APIRouter, HTTPException
from server.models import ModelConfigUpdate, ModelConfigResponse

router = APIRouter(prefix="/api/config", tags=["config"])


def get_config_dependency():
    from server.main import get_config
    return get_config()


def get_llm_dependency():
    from server.main import get_llm_client
    return get_llm_client()


@router.get("", response_model=ModelConfigResponse)
def get_config():
    config = get_config_dependency()
    return ModelConfigResponse(
        model_name=config.get("model_name", ""),
        base_url=config.get("base_url", ""),
        api_key_masked=config.get_masked_api_key(),
        context_limit=config.get("context_limit", ""),
        tavily_api_key_masked=_mask_key(config.get("tavily_api_key", "")),
    )


def _mask_key(key: str) -> str:
    if not key:
        return "(未设置)"
    if len(key) <= 8:
        return "*" * len(key)
    return key[:4] + "*" * (len(key) - 8) + key[-4:]


@router.put("", response_model=ModelConfigResponse)
def update_config(body: ModelConfigUpdate):
    config = get_config_dependency()
    llm = get_llm_dependency()

    if body.api_key is not None:
        config.set_api_key(body.api_key)
    if body.base_url is not None:
        config.set("base_url", body.base_url)
    if body.model_name is not None:
        config.set("model_name", body.model_name)
    if body.context_limit is not None:
        config.set("context_limit", body.context_limit)
        from server.main import update_agent_context_limit
        update_agent_context_limit(body.context_limit)
    if body.tavily_api_key is not None:
        config.set("tavily_api_key", body.tavily_api_key)

    llm.refresh()

    return ModelConfigResponse(
        model_name=config.get("model_name", ""),
        base_url=config.get("base_url", ""),
        api_key_masked=config.get_masked_api_key(),
        context_limit=config.get("context_limit", ""),
        tavily_api_key_masked=_mask_key(config.get("tavily_api_key", "")),
    )