import os
import sys
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _project_root)
sys.path.append(os.path.join(_project_root, "agent"))

from agent.config import AgentConfig
from agent.llm import LLMClient
from agent.tools import ToolRegistry
from agent.tool_builder import ToolBuilder
from agent.agent import SimpleAgent
from agent.main import _create_executor

from server.routes import routers

_config: AgentConfig = None
_llm_client: LLMClient = None
_tool_registry: ToolRegistry = None
_tool_builder: ToolBuilder = None
_agent: SimpleAgent = None
_session_store: dict = {}


def init_services():
    global _config, _llm_client, _tool_registry, _tool_builder, _agent

    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    config_path = os.path.join(base_dir, "agent", ".agent_config")
    tools_dir = os.path.join(base_dir, "agent", "agent_tools")

    _config = AgentConfig(config_path)
    _llm_client = LLMClient(config=_config)
    _tool_registry = ToolRegistry()
    _tool_builder = ToolBuilder(_llm_client)

    _tool_registry.load_tools_from_dir(
        tools_dir,
        func_factory=lambda name, prompt, mode, code, http_cfg, deps:
            _create_executor(name, prompt, mode, code, http_cfg, _llm_client, deps)
    )

    show_thought = _config.get("show_thought", False)
    max_rounds = _config.get("max_history_rounds", 10)
    _agent = SimpleAgent(_llm_client, _tool_registry, max_history_rounds=max_rounds, show_thought=show_thought)


def get_config() -> AgentConfig:
    return _config


def get_llm_client() -> LLMClient:
    return _llm_client


def get_tool_registry() -> ToolRegistry:
    return _tool_registry


def get_tool_builder() -> ToolBuilder:
    return _tool_builder


def get_agent() -> SimpleAgent:
    return _agent


def get_session_store() -> dict:
    return _session_store


app = FastAPI(title="Agent Framework API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

AUTH_WHITELIST = {"/api/auth/login", "/api/health", "/login.html", "/favicon.ico"}


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path

    if path in AUTH_WHITELIST or path.startswith("/static/"):
        return await call_next(request)

    token = request.cookies.get("auth_token") or request.headers.get("Authorization", "").replace("Bearer ", "")

    from server.routes.auth import validate_token
    if not token or not validate_token(token):
        if path.startswith("/api/"):
            return JSONResponse(status_code=401, content={"detail": "未登录"})
        return RedirectResponse(url="/login.html")

    return await call_next(request)


for router in routers:
    app.include_router(router)

static_dir = os.path.join(_project_root, "static")
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/")
def serve_index():
    return FileResponse(os.path.join(static_dir, "index.html"))


@app.get("/login.html")
def serve_login():
    return FileResponse(os.path.join(static_dir, "login.html"))


@app.on_event("startup")
def startup():
    pass


@app.get("/api/health")
def health():
    return {"status": "ok"}


init_services()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=17520)