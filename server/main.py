import os
import sys
import subprocess
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
from server.database import init_db, DB_PATH, _generate_random_password

_config: AgentConfig = None
_llm_client: LLMClient = None
_tool_registry: ToolRegistry = None
_tool_builder: ToolBuilder = None
_agent: SimpleAgent = None
_session_store: dict = {}
_db_process: subprocess.Popen = None
_db_user = "root"
_db_password: str = None


def init_services():
    global _config, _llm_client, _tool_registry, _tool_builder, _agent

    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    config_path = os.path.join(base_dir, "agent", ".agent_config")
    tools_dir = os.path.join(base_dir, "agent", "agent_tools")

    _config = AgentConfig(config_path)

    from server.database import get_model_config, save_model_config, get_search_config, save_search_config

    global_cfg = get_model_config(None)

    if global_cfg and global_cfg.get("api_key"):
        _llm_client = LLMClient(
            api_key=global_cfg["api_key"],
            base_url=global_cfg.get("base_url", ""),
            model=global_cfg.get("model_name", ""),
            config=_config,
        )
    else:
        file_api_key = _config.get_api_key()
        if file_api_key:
            save_model_config(
                None,
                api_key=file_api_key,
                base_url=_config.get("base_url", ""),
                model_name=_config.get("model_name", ""),
                context_limit=_config.get("context_limit", ""),
                show_thought=_config.get("show_thought", False),
            )
            tavily_encrypted = _config._data.get("tavily_api_key_encrypted", "")
            if tavily_encrypted:
                try:
                    from agent.config import _decrypt as _file_decrypt
                    tavily_key = _file_decrypt(tavily_encrypted, _config.config_dir)
                    if tavily_key:
                        save_search_config(tavily_api_key=tavily_key)
                except Exception:
                    pass
            global_cfg = get_model_config(None)

        _llm_client = LLMClient(config=_config)

    _tool_registry = ToolRegistry()
    _tool_builder = ToolBuilder(_llm_client)

    _tool_registry.load_tools_from_dir(
        tools_dir,
        func_factory=lambda name, prompt, mode, code, http_cfg, deps:
            _create_executor(name, prompt, mode, code, http_cfg, _llm_client, deps)
    )

    show_thought = False
    context_limit = ""

    if global_cfg:
        show_thought = global_cfg.get("show_thought", False)
        context_limit = global_cfg.get("context_limit", "")
    else:
        show_thought = _config.get("show_thought", False)
        context_limit = _config.get("context_limit", "")

    _agent = SimpleAgent(_llm_client, _tool_registry, context_limit=context_limit, show_thought=show_thought)


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


def update_agent_context_limit(context_limit: str):
    global _agent
    if _agent:
        _agent.update_context_limit(context_limit)


def update_agent_show_thought(show_thought: bool):
    global _agent
    if _agent:
        _agent.set_show_thought(show_thought)


def refresh_global_llm():
    global _llm_client, _agent
    from server.database import get_model_config

    cfg = get_model_config(None)
    if not cfg or not cfg.get("api_key"):
        return

    _llm_client.client = __import__("openai").OpenAI(
        api_key=cfg["api_key"],
        base_url=cfg.get("base_url", "")
    )
    _llm_client.model = cfg.get("model_name", "")

    context_limit = cfg.get("context_limit", "")
    show_thought = cfg.get("show_thought", False)
    if _agent:
        _agent.update_context_limit(context_limit)
        _agent.set_show_thought(show_thought)


def get_session_store() -> dict:
    return _session_store


app = FastAPI(title="OmniAssist API", version="1.0.0")

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


@app.get("/")
def serve_index():
    return FileResponse(os.path.join(static_dir, "index.html"))


@app.get("/login.html")
def serve_login():
    return FileResponse(os.path.join(static_dir, "login.html"))


@app.get("/favicon.ico")
def serve_favicon():
    return FileResponse(os.path.join(static_dir, "favicon.svg"), media_type="image/svg+xml")


@app.on_event("startup")
def startup():
    global _db_password

    admin_pw = init_db()
    if admin_pw:
        print(f"\n[用户体系] 数据库已初始化")
        print(f"[用户体系] 默认管理员账号: admin")
        print(f"[用户体系] 默认管理员密码: {admin_pw}")
        print(f"[用户体系] 请登录后及时修改密码！\n")

    refresh_global_llm()

    _db_password = _generate_random_password()
    _start_sqlite_web()


@app.get("/api/health")
def health():
    return {"status": "ok"}

app.mount("/static", StaticFiles(directory=static_dir, html=False), name="static")


init_services()


def _check_and_free_port(port: int):
    import subprocess
    import signal

    try:
        result = subprocess.run(
            ["lsof", "-ti", f":{port}"],
            capture_output=True, text=True, timeout=5
        )
        pids = [pid for pid in result.stdout.strip().split("\n") if pid]
        if not pids:
            return

        for pid_str in pids:
            pid = int(pid_str)
            if pid == os.getpid():
                continue
            try:
                os.kill(pid, signal.SIGTERM)
                print(f"[启动] 已终止占用端口 {port} 的进程 (PID: {pid})")
            except ProcessLookupError:
                pass
            except PermissionError:
                print(f"[启动] 警告：无权限终止进程 (PID: {pid})，端口 {port} 可能仍被占用")

        import time
        time.sleep(0.5)
    except Exception as e:
        print(f"[启动] 端口检测异常: {e}")


def _start_sqlite_web():
    global _db_process
    DB_PORT = 17521

    _check_and_free_port(DB_PORT)

    try:
        _db_process = subprocess.Popen(
            [
                sys.executable, "-m", "sqlite_web",
                "--host", "127.0.0.1",
                "--port", str(DB_PORT),
                "--no-browser",
                DB_PATH,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print(f"[数据库管理] sqlite-web 已启动: http://127.0.0.1:{DB_PORT}")
        print(f"[数据库管理] 仅限本机访问，数据库文件: {DB_PATH}")
    except Exception as e:
        print(f"[数据库管理] sqlite-web 启动失败: {e}")
        print(f"[数据库管理] 请先安装: pip install sqlite-web")


if __name__ == "__main__":
    import uvicorn

    PORT = 17520
    _check_and_free_port(PORT)
    uvicorn.run(app, host="0.0.0.0", port=PORT)