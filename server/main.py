import os
import sys
import subprocess
import signal
import atexit
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
from agent.sandbox import ToolSandbox

from server.routes import routers
from server.database import init_db, DB_PATH, _generate_random_password

_config: AgentConfig = None
_llm_client: LLMClient = None
_tool_registry: ToolRegistry = None
_tool_builder: ToolBuilder = None
_agent: SimpleAgent = None
_sandbox: ToolSandbox = None
_session_store: dict = {}
_db_process: subprocess.Popen = None
_db_user = "root"


def _cleanup():
    global _db_process
    if _db_process is not None and _db_process.poll() is None:
        try:
            _db_process.terminate()
            try:
                _db_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _db_process.kill()
                _db_process.wait()
            print("[清理] sqlite-web 子进程已终止")
        except Exception as e:
            print(f"[清理] 终止 sqlite-web 子进程失败: {e}")


def _signal_handler(signum, frame):
    print(f"\n[信号] 收到信号 {signum}，正在优雅关闭...")
    _cleanup()
    sys.exit(0)


signal.signal(signal.SIGTERM, _signal_handler)
signal.signal(signal.SIGINT, _signal_handler)
atexit.register(_cleanup)


def _prewarm_sandbox(tools_dir: str):
    """扫描所有工具定义，预热沙箱依赖

    在服务启动时一次性安装所有工具的依赖包，
    避免首次调用工具时的冷启动延迟。
    """
    import json

    if _sandbox is None:
        return
    if not os.path.isdir(tools_dir):
        return

    all_deps = set()
    for fname in sorted(os.listdir(tools_dir)):
        if not fname.endswith(".json"):
            continue
        fpath = os.path.join(tools_dir, fname)
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                tool_def = json.load(f)
            deps = tool_def.get("dependencies", [])
            if deps:
                for d in deps:
                    all_deps.add(d)
        except Exception:
            pass

    if not all_deps:
        print("[沙箱预热] 没有需要安装的依赖")
        return

    print(f"[沙箱预热] 检测到 {len(all_deps)} 个依赖: {sorted(all_deps)}")
    try:
        _sandbox.install_verbose(sorted(all_deps))
        print("[沙箱预热] 依赖安装完成")
    except Exception as e:
        print(f"[沙箱预热] 部分依赖安装失败: {e}")
        print("[沙箱预热] 工具首次调用时将自动重试安装")


def init_services():
    global _config, _llm_client, _tool_registry, _tool_builder, _agent, _sandbox

    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    config_path = os.path.join(base_dir, "data", ".agent_config")
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

    try:
        _sandbox = ToolSandbox()
    except Exception as e:
        print(f"[启动] 沙箱初始化失败: {e}")
        print("[启动] 工具执行功能将不可用，但对话功能不受影响")
        _sandbox = None

    _tool_registry = ToolRegistry()
    _tool_builder = ToolBuilder(_llm_client)

    _tool_registry.load_tools_from_dir(
        tools_dir,
        func_factory=lambda name, prompt, mode, code, http_cfg, deps, fmt=None:
            _create_executor(name, prompt, mode, code, http_cfg, _llm_client, deps, fmt, sandbox=_sandbox)
    )

    if _sandbox is not None:
        import threading
        threading.Thread(target=_prewarm_sandbox, args=(tools_dir,), daemon=True).start()
    else:
        print("[沙箱预热] 沙箱不可用，跳过依赖预热")

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

    if _llm_client is None:
        return

    cfg = get_model_config(None)
    if not cfg or not cfg.get("api_key"):
        return

    _llm_client._api_key = cfg["api_key"]
    _llm_client._base_url = cfg.get("base_url", "")
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


@app.middleware("http")
async def global_exception_handler(request: Request, call_next):
    try:
        return await call_next(request)
    except Exception as e:
        import traceback
        print(f"[错误] 未捕获的异常: {e}")
        traceback.print_exc()
        return JSONResponse(
            status_code=500,
            content={"detail": f"服务内部错误: {str(e)}"}
        )


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
    pw_file = os.path.join(os.path.dirname(DB_PATH), ".db_web_password")

    if admin_pw:
        print(f"\n{'='*60}")
        print(f"  OmniAssist 已启动")
        print(f"  访问地址: http://localhost:17520")
        print(f"{'='*60}")
        print(f"  默认管理员账号: admin")
        print(f"  默认管理员密码: admin123")
        print(f"  ⚠️  首次登录需修改密码后方可使用！")
        print(f"{'='*60}\n")

        from server.database import _hash_password
        with open(pw_file, "w") as f:
            f.write(_hash_password(admin_pw))
        try:
            os.chmod(pw_file, 0o600)
        except Exception:
            pass
    else:
        print(f"\n{'='*60}")
        print(f"  OmniAssist 已启动")
        print(f"  访问地址: http://localhost:17520")
        print(f"{'='*60}\n")

        if not os.path.isfile(pw_file):
            new_pw = _generate_random_password(8)
            from server.database import _hash_password, _get_connection
            conn = _get_connection()
            now = __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                "UPDATE users SET password_hash = ?, must_change_password = 1, updated_at = ? WHERE username = 'admin'",
                (_hash_password(new_pw), now)
            )
            conn.commit()
            with open(pw_file, "w") as f:
                f.write(_hash_password(new_pw))
            try:
                os.chmod(pw_file, 0o600)
            except Exception:
                pass
            print(f"[启动] 密码文件丢失，已重置管理员密码")
            print(f"  新密码: {new_pw}")
            print(f"  ⚠️  请使用新密码登录并及时修改！")

    try:
        init_services()
        print("[启动] 服务初始化完成")
    except Exception as e:
        print(f"[启动] 服务初始化失败: {e}")
        import traceback
        traceback.print_exc()
        print("[启动] 请检查模型配置和工具定义，服务将继续启动但部分功能可能不可用")

    refresh_global_llm()

    _start_sqlite_web()


@app.get("/api/health")
def health():
    return {"status": "ok"}

app.mount("/static", StaticFiles(directory=static_dir, html=False), name="static")


def _check_and_free_port(port: int):
    import signal

    pids = _find_port_pids(port)
    if not pids:
        return

    for pid in pids:
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


def _find_port_pids(port: int) -> list:
    methods = [
        ["lsof", "-ti", f":{port}"],
        ["fuser", f"{port}/tcp"],
        ["ss", "-tlnp"],
    ]

    for cmd in methods:
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=5
            )
            if result.returncode != 0:
                continue

            if cmd[0] == "ss":
                pids = _parse_ss_output(result.stdout, port)
            elif cmd[0] == "fuser":
                pids = _parse_fuser_output(result.stdout)
            else:
                pids = [int(p) for p in result.stdout.strip().split("\n") if p.strip().isdigit()]

            if pids:
                return pids
        except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
            continue

    return []


def _parse_fuser_output(output: str) -> list:
    pids = []
    for part in output.strip().split():
        pid_str = part.rstrip("km")
        if pid_str.isdigit():
            pids.append(int(pid_str))
    return pids


def _parse_ss_output(output: str, target_port: int) -> list:
    import re
    pids = []
    port_pattern = re.compile(rf":{target_port}\b")
    pid_pattern = re.compile(r"pid=(\d+)")
    for line in output.split("\n"):
        if port_pattern.search(line):
            match = pid_pattern.search(line)
            if match:
                pids.append(int(match.group(1)))
    return pids


def _start_sqlite_web():
    global _db_process
    DB_PROXY_PORT = 17521
    DB_BACKEND_PORT = 17523

    _check_and_free_port(DB_PROXY_PORT)
    _check_and_free_port(DB_BACKEND_PORT)

    password_file = os.path.join(os.path.dirname(DB_PATH), ".db_web_password")
    if os.path.isfile(password_file):
        with open(password_file, "r") as f:
            web_password_hash = f.read().strip()
    else:
        from server.database import _hash_password, _get_connection
        web_password = _generate_random_password(8)
        web_password_hash = _hash_password(web_password)
        conn = _get_connection()
        now = __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "UPDATE users SET password_hash = ?, must_change_password = 1, updated_at = ? WHERE username = 'admin'",
            (_hash_password(web_password), now)
        )
        conn.commit()
        with open(password_file, "w") as f:
            f.write(web_password_hash)
        try:
            os.chmod(password_file, 0o600)
        except Exception:
            pass
        print(f"[数据库管理] 密码文件丢失，已重置管理员密码: {web_password}")

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

    _start_db_auth_proxy(DB_PROXY_PORT, DB_BACKEND_PORT, web_password_hash)

    print(f"[数据库管理] 已启动: http://0.0.0.0:{DB_PROXY_PORT}")
    print(f"[数据库管理] 使用管理员账号(admin)登录即可访问")
    print(f"[数据库管理] 数据库文件: {DB_PATH}")


def _start_db_auth_proxy(proxy_port: int, backend_port: int, password_hash: str):
    import http.server
    import urllib.request
    import urllib.error
    import base64
    import threading

    password_file = os.path.join(os.path.dirname(DB_PATH), ".db_web_password")

    class ProxyHandler(http.server.BaseHTTPRequestHandler):
        def _check_auth(self):
            from server.database import verify_password
            try:
                with open(password_file, "r") as f:
                    stored = f.read().strip()
            except Exception:
                stored = password_hash
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Basic "):
                try:
                    credentials = base64.b64decode(auth.split(" ", 1)[1]).decode()
                    username, _, password = credentials.partition(":")
                    if username == "admin":
                        if ":" in stored:
                            if verify_password(password, stored):
                                return True
                        elif password == stored:
                            return True
                except Exception:
                    pass
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

    try:
        server = http.server.ThreadingHTTPServer(("0.0.0.0", proxy_port), ProxyHandler)
    except OSError as e:
        print(f"[数据库管理] 代理端口 {proxy_port} 绑定失败: {e}")
        return

    def _run_proxy():
        try:
            server.serve_forever()
        except Exception as e:
            print(f"[数据库管理] 代理服务异常退出: {e}")

    threading.Thread(target=_run_proxy, daemon=True).start()


if __name__ == "__main__":
    import uvicorn

    PORT = 17520
    _check_and_free_port(PORT)
    uvicorn.run(app, host="0.0.0.0", port=PORT)