import os
import sys
import json
import subprocess
import signal
import struct
import atexit
from contextlib import asynccontextmanager
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
from agent.agent import SimpleAgent
from agent.main import _create_executor
from agent.sandbox import SandboxPool
from agent.skills import SkillRegistry
from agent.agent_pool import AgentPool
from agent.logger import get_logger

from server.routes import routers
from server.database import init_db, DB_PATH, _generate_random_password

logger = get_logger("server")

_config: AgentConfig = None
_llm_client: LLMClient = None
_tool_registry: ToolRegistry = None
_agent: SimpleAgent = None
_sandbox_pool: SandboxPool = None
_skill_registry: SkillRegistry = None
_agent_pool: AgentPool = None
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
            logger.info("sqlite-web 子进程已终止")
        except Exception as e:
            logger.error(f"终止 sqlite-web 子进程失败: {e}")


def _signal_handler(signum, frame):
    logger.info(f"收到信号 {signum}，正在优雅关闭...")
    _cleanup()
    sys.exit(0)


signal.signal(signal.SIGTERM, _signal_handler)
signal.signal(signal.SIGINT, _signal_handler)
atexit.register(_cleanup)


def init_services():
    global _config, _llm_client, _tool_registry, _agent, _sandbox_pool, _skill_registry, _agent_pool

    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    config_path = os.path.join(base_dir, "data", ".agent_config")
    skills_dir = os.path.join(base_dir, "agent", "skills")

    _config = AgentConfig(config_path)

    from agent.tool_secrets import get_tool_secrets
    get_tool_secrets()

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
        _sandbox_pool = SandboxPool()
    except Exception as e:
        logger.error(f"沙箱初始化失败: {e}")
        logger.warning("工具执行功能将不可用，但对话功能不受影响")
        _sandbox_pool = None

    # 初始化技能注册中心
    _skill_registry = SkillRegistry()
    system_skills = _skill_registry.load_system_skills(skills_dir)
    logger.info(f"已加载 {len(system_skills)} 个系统技能: {system_skills}")

    _tool_registry = ToolRegistry()

    # 从技能注册中心加载所有脚本，注册为工具
    skill_scripts = _skill_registry.get_all_scripts()
    for script in skill_scripts:
        _tool_registry.register_tool(
            name=script.name,
            description=script.description,
            parameters=script.parameters,
            func=_create_executor(
                script.name, script.description, script.execution_mode,
                script.source, script.http_config, _llm_client,
                script.dependencies, script.response_formatter, sandbox_pool=_sandbox_pool
            )
        )
    logger.info(f"从技能中注册了 {len(skill_scripts)} 个工具脚本")

    # 初始化多 Agent 池
    _agent_pool = AgentPool(_llm_client, _sandbox_pool, _skill_registry)
    profiles_dir = os.path.join(base_dir, "agent", "profiles")
    pool_agents = _agent_pool.load_profiles(profiles_dir)
    if pool_agents:
        _agent_pool.register_as_tools(_tool_registry)
        logger.info(f"已加载 {len(pool_agents)} 个子 Agent: {pool_agents}")

    # 注册 Skill 编辑器工具（用户级 Skill CRUD）
    from agent.skill_editor import create_user_skill, update_user_skill, delete_user_skill, list_user_skills
    _tool_registry.register_tool(
        name="create_user_skill",
        description="为用户创建一个新的自定义 Skill。Skill 包含 SKILL.md（技能描述）和可选的工具脚本。",
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Skill 名称（英文标识，如 my_analyzer）"},
                "skill_md": {"type": "string", "description": "SKILL.md 内容（Markdown 格式的技能描述和使用说明）"},
                "tools": {"type": "array", "description": "工具脚本列表，每项含 name 和 content（JSON Schema）", "items": {"type": "object"}},
            },
            "required": ["name", "skill_md"]
        },
        func=lambda name, skill_md, tools=None, _user_id=None: json.dumps(
            create_user_skill(_user_id, name, skill_md, tools), ensure_ascii=False
        )
    )
    _tool_registry.register_tool(
        name="update_user_skill",
        description="更新用户已有的自定义 Skill。可修改 SKILL.md 内容和/或工具脚本。",
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "要更新的 Skill 名称"},
                "skill_md": {"type": "string", "description": "新的 SKILL.md 内容（不传则不更新）"},
                "tools": {"type": "array", "description": "新的工具脚本列表（不传则不更新，传空数组清空所有工具）", "items": {"type": "object"}},
            },
            "required": ["name"]
        },
        func=lambda name, skill_md=None, tools=None, _user_id=None: json.dumps(
            update_user_skill(_user_id, name, skill_md, tools), ensure_ascii=False
        )
    )
    _tool_registry.register_tool(
        name="delete_user_skill",
        description="删除用户的一个自定义 Skill（仅限用户级 Skill，系统 Skill 不可删除）。",
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "要删除的 Skill 名称"},
            },
            "required": ["name"]
        },
        func=lambda name, _user_id=None: json.dumps(
            delete_user_skill(_user_id, name), ensure_ascii=False
        )
    )
    _tool_registry.register_tool(
        name="list_user_skills",
        description="列出当前用户的所有自定义 Skill。",
        parameters={"type": "object", "properties": {}, "required": []},
        func=lambda _user_id=None: json.dumps(
            list_user_skills(_user_id), ensure_ascii=False
        )
    )
    logger.info("已注册 Skill 编辑器工具（create/update/delete/list）")

    # 注册任务复盘工具
    from agent.task_reviewer import log_task_execution, review_recent_tasks, analyze_and_suggest, clear_reviews
    _tool_registry.register_tool(
        name="review_recent_tasks",
        description="查看最近的任务执行记录，包括成功/失败任务、工具使用情况、错误信息。用于复盘和发现优化点。",
        parameters={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "最多返回条数，默认 20"},
            },
            "required": []
        },
        func=lambda limit=20, _user_id=None: json.dumps(
            review_recent_tasks(_user_id, limit), ensure_ascii=False
        )
    )
    _tool_registry.register_tool(
        name="analyze_and_suggest",
        description="分析任务执行日志，自动发现失败模式并生成 Skill 优化建议。可用于复盘时自动优化。",
        parameters={"type": "object", "properties": {}, "required": []},
        func=lambda _user_id=None: json.dumps(
            analyze_and_suggest(_user_id), ensure_ascii=False
        )
    )
    logger.info("已注册任务复盘工具（review/analyze）")

    # 注册意图关键词更新工具
    from agent.intent_keywords import update_user_keywords
    _tool_registry.register_tool(
        name="update_intent_keywords",
        description="更新当前用户的意图关键词配置。关键词用于按需选择工具，减少 LLM 每次请求的 token 开销。"
                    "当发现某些查询未能匹配到正确的工具时，可通过此工具添加新关键词。",
        parameters={
            "type": "object",
            "properties": {
                "keywords": {"type": "object", "description": "关键词配置 {类别: [关键词模式列表]}，如 {\"weather\": [\"天气\", \"温度\"]}"},
            },
            "required": ["keywords"]
        },
        func=lambda keywords, _user_id=None: json.dumps(
            {"success": update_user_keywords(_user_id, keywords)}, ensure_ascii=False
        )
    )
    logger.info("已注册意图关键词更新工具")

    show_thought = False
    context_limit = ""

    if global_cfg:
        show_thought = global_cfg.get("show_thought", False)
        context_limit = global_cfg.get("context_limit", "")
    else:
        show_thought = _config.get("show_thought", False)
        context_limit = _config.get("context_limit", "")

    # 构建技能上下文
    skill_context = _skill_registry.build_context()

    _agent = SimpleAgent(
        _llm_client, _tool_registry,
        context_limit=context_limit,
        show_thought=show_thought,
        skill_context=skill_context
    )


def get_config() -> AgentConfig:
    return _config


def get_llm_client() -> LLMClient:
    return _llm_client


def get_tool_registry() -> ToolRegistry:
    return _tool_registry



def get_skill_registry() -> SkillRegistry:
    return _skill_registry


def get_agent_pool() -> AgentPool:
    return _agent_pool


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


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan 事件处理（替代弃用的 on_event）"""
    admin_pw = init_db()
    pw_file = os.path.join(os.path.dirname(DB_PATH), ".db_web_password")

    if admin_pw:
        logger.info("=" * 60)
        logger.info("OmniAssist 已启动")
        logger.info(f"访问地址: http://localhost:17520")
        logger.info("=" * 60)
        logger.info("默认管理员账号: admin")
        logger.info("默认管理员密码: admin123")
        logger.warning("首次登录需修改密码后方可使用！")
        logger.info("=" * 60)

        from server.database import _hash_password
        with open(pw_file, "w") as f:
            f.write(_hash_password(admin_pw))
        try:
            os.chmod(pw_file, 0o600)
        except Exception:
            pass
    else:
        logger.info("=" * 60)
        logger.info("OmniAssist 已启动")
        logger.info(f"访问地址: http://localhost:17520")
        logger.info("=" * 60)

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
            logger.warning("密码文件丢失，已重置管理员密码")
            logger.info(f"新密码: {new_pw}")
            logger.warning("请使用新密码登录并及时修改！")

    try:
        init_services()
        logger.info("服务初始化完成")
    except Exception as e:
        logger.error(f"服务初始化失败: {e}")
        import traceback
        logger.error(traceback.format_exc())
        logger.warning("请检查模型配置和工具定义，服务将继续启动但部分功能可能不可用")

    refresh_global_llm()
    _start_sqlite_web()

    yield  # 服务运行中

    # shutdown
    _cleanup()


app = FastAPI(title="OmniAssist API", version="1.0.0", lifespan=lifespan)

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
        logger.error(f"未捕获的异常: {e}\n{traceback.format_exc()}")
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


@app.get("/api/health")
def health():
    return {"status": "ok"}

app.mount("/static", StaticFiles(directory=static_dir, html=False), name="static")


def _check_and_free_port(port: int):
    import signal
    import socket
    import time

    if _is_port_available(port):
        return

    pids = _find_port_pids(port)
    if not pids:
        logger.warning(f"端口 {port} 被占用，但无法确定占用进程（可能在容器外）")
        _force_release_port(port)
        return

    for pid in pids:
        if pid == os.getpid():
            continue
        try:
            os.kill(pid, signal.SIGKILL)
            logger.info(f"已终止占用端口 {port} 的进程 (PID: {pid})")
        except ProcessLookupError:
            pass
        except PermissionError:
            logger.warning(f"无权限终止进程 (PID: {pid})，端口 {port} 可能仍被占用")

    for _ in range(10):
        time.sleep(0.3)
        if _is_port_available(port):
            return

    logger.warning(f"端口 {port} 仍被占用，尝试强制释放...")
    _force_release_port(port)


def _force_release_port(port: int):
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        try:
            s.bind(("0.0.0.0", port))
        except OSError:
            try:
                s.bind(("127.0.0.1", port))
            except OSError as e:
                logger.error(f"无法强制释放端口 {port}: {e}")
                return
        s.listen(1)
        s.close()
        logger.info(f"端口 {port} 已强制释放")
    except Exception as e:
        logger.error(f"强制释放端口 {port} 失败: {e}")


def _is_port_available(port: int) -> bool:
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", port))
            return True
    except OSError:
        return False


def _find_port_pids(port: int) -> list:
    methods = [
        ["lsof", "-ti", f":{port}"],
        ["fuser", f"{port}/tcp"],
        ["ss", "-tlnp"],
    ]

    for cmd in methods:
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=3
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
        except FileNotFoundError:
            continue
        except subprocess.TimeoutExpired:
            continue
        except ValueError:
            continue
        except Exception:
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
        logger.warning(f"密码文件丢失，已重置管理员密码: {web_password}")

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
        logger.error(f"sqlite-web 启动失败: {e}")
        logger.info("请先安装: pip install sqlite-web")
        return

    import time
    time.sleep(1)

    if _start_db_auth_proxy(DB_PROXY_PORT, DB_BACKEND_PORT, web_password_hash):
        logger.info(f"数据库管理代理已启动: http://0.0.0.0:{DB_PROXY_PORT}")
        logger.info("使用管理员账号(admin)登录即可访问")
        logger.info(f"数据库文件: {DB_PATH}")


def _start_db_auth_proxy(proxy_port: int, backend_port: int, password_hash: str) -> bool:
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
        server.socket.setsockopt(__import__("socket").SOL_SOCKET, __import__("socket").SO_REUSEADDR, 1)
        try:
            server.socket.setsockopt(__import__("socket").SOL_SOCKET, __import__("socket").SO_REUSEPORT, 1)
        except (AttributeError, OSError):
            pass
    except OSError as e:
        logger.error(f"数据库管理代理端口 {proxy_port} 绑定失败: {e}")
        return False

    def _run_proxy():
        try:
            server.serve_forever()
        except Exception as e:
            logger.error(f"数据库管理代理服务异常退出: {e}")

    threading.Thread(target=_run_proxy, daemon=True).start()
    return True


if __name__ == "__main__":
    import uvicorn

    PORT = 17520
    _check_and_free_port(PORT)
    uvicorn.run(app, host="0.0.0.0", port=PORT)