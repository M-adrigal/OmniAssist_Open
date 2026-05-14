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
        func_factory=lambda name, prompt, mode, code, http_cfg, deps, fmt=None:
            _create_executor(name, prompt, mode, code, http_cfg, _llm_client, deps, fmt)
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
    admin_pw = init_db()
    if admin_pw:
        print(f"\n[用户体系] 数据库已初始化")
        print(f"[用户体系] 默认管理员账号: admin")
        print(f"[用户体系] 默认管理员密码: {admin_pw}")
        print(f"[用户体系] 请登录后及时修改密码！\n")

        pw_file = os.path.join(os.path.dirname(DB_PATH), ".db_web_password")
        with open(pw_file, "w") as f:
            f.write(admin_pw)
        try:
            os.chmod(pw_file, 0o600)
        except Exception:
            pass

    refresh_global_llm()

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
    DB_PROXY_PORT = 17521
    DB_BACKEND_PORT = 17523

    _check_and_free_port(DB_PROXY_PORT)
    _check_and_free_port(DB_BACKEND_PORT)

    password_file = os.path.join(os.path.dirname(DB_PATH), ".db_web_password")
    if os.path.isfile(password_file):
        with open(password_file, "r") as f:
            web_password = f.read().strip()
    else:
        web_password = _generate_random_password(12)
        with open(password_file, "w") as f:
            f.write(web_password)
        try:
            os.chmod(password_file, 0o600)
        except Exception:
            pass
        print(f"[数据库管理] 未找到密码文件，已生成临时密码，请通过平台修改管理员密码以同步")

    try:
        _db_process = subprocess.Popen(
            [
                sys.executable, "-m", "sqlite_web",
                "--host", "127.0.0.1",
                "--port", str(DB_BACKEND_PORT),
                "--no-browser",
                DB_PATH,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        print(f"[数据库管理] sqlite-web 启动失败: {e}")
        print(f"[数据库管理] 请先安装: pip install sqlite-web")
        return

    import time
    time.sleep(1)

    _start_db_auth_proxy(DB_PROXY_PORT, DB_BACKEND_PORT, web_password)

    print(f"[数据库管理] 已启动: http://127.0.0.1:{DB_PROXY_PORT}")
    print(f"[数据库管理] 使用管理员账号(admin)登录即可访问")
    print(f"[数据库管理] 仅限本机访问，数据库文件: {DB_PATH}")


def _start_db_auth_proxy(proxy_port: int, backend_port: int, password: str):
    import http.server
    import urllib.request
    import urllib.error
    import base64
    import threading

    password_file = os.path.join(os.path.dirname(DB_PATH), ".db_web_password")

    class ProxyHandler(http.server.BaseHTTPRequestHandler):
        def _check_auth(self):
            try:
                with open(password_file, "r") as f:
                    current_pw = f.read().strip()
            except Exception:
                current_pw = password
            expected = base64.b64encode(f"admin:{current_pw}".encode()).decode()
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Basic ") and auth.split(" ", 1)[1] == expected:
                return True
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="Database Management"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False

        def _proxy(self):
            if not self._check_auth():
                return
            url = f"http://127.0.0.1:{backend_port}{self.path}"
            body = None
            content_length = self.headers.get("Content-Length")
            if content_length:
                body = self.rfile.read(int(content_length))
            try:
                req = urllib.request.Request(url, data=body, method=self.command)
                skip_headers = {"host", "authorization", "content-length"}
                for key, val in self.headers.items():
                    if key.lower() not in skip_headers:
                        req.add_header(key, val)
                with urllib.request.urlopen(req) as resp:
                    self.send_response(resp.status)
                    for key, val in resp.headers.items():
                        if key.lower() not in ("transfer-encoding", "connection", "set-cookie", "vary"):
                            self.send_header(key, val)
                    self.end_headers()
                    self.wfile.write(resp.read())
            except urllib.error.HTTPError as e:
                self.send_response(e.code)
                for key, val in e.headers.items():
                    if key.lower() not in ("transfer-encoding", "connection", "set-cookie", "vary"):
                        self.send_header(key, val)
                self.end_headers()
                self.wfile.write(e.read())
            except Exception:
                self.send_response(502)
                self.end_headers()

        do_GET = _proxy
        do_POST = _proxy
        do_PUT = _proxy
        do_DELETE = _proxy
        do_HEAD = _proxy
        do_OPTIONS = _proxy
        do_PATCH = _proxy

        def log_message(self, format, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", proxy_port), ProxyHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()


if __name__ == "__main__":
    import uvicorn

    PORT = 17520
    _check_and_free_port(PORT)
    uvicorn.run(app, host="0.0.0.0", port=PORT)