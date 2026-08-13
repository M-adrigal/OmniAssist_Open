import os
import sys
import json
import subprocess
import signal
import struct
import atexit
import logging
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
from agent.user_secrets import set_user_secret, list_user_secrets_masked, delete_user_secret


def install_skill_template(template_name: str, _user_id=None):
    """把技能模板安装到当前用户的技能仓库（复制目录 + 立即加载）。

    模板位于 agent/skill_templates/，需用户自带 API Key 的技能（如 weather）。
    安装后需先用 set_user_secret 设置该技能所需的 key/host 才能使用。
    """
    import os
    import shutil

    base = os.path.dirname(os.path.abspath(__file__))
    templates_dir = os.path.normpath(os.path.join(base, "..", "agent", "skill_templates"))
    src = os.path.join(templates_dir, template_name)
    if not os.path.isdir(src):
        return {"success": False, "message": f"模板不存在: {template_name}"}

    user_dir = os.path.normpath(
        os.path.join(base, "..", "agent", "skills", "user", str(_user_id), template_name)
    )
    if os.path.exists(user_dir):
        return {"success": False, "message": f"你已安装过 {template_name}，更新请先 delete_user_skill 再装"}

    try:
        shutil.copytree(src, user_dir)
    except Exception as e:
        return {"success": False, "message": f"复制失败: {e}"}

    try:
        get_skill_registry().load_user_skills_from_fs(_user_id)
    except Exception:
        pass

    return {
        "success": True,
        "message": f"已安装模板 {template_name} 到你的技能仓库；使用前请先 set_user_secret 设置该技能所需的凭据",
    }

from server.routes import routers
from server.database import init_db, DB_PATH, _generate_random_password

logger = get_logger("server")


def _attach_uvicorn_logging():
    """将 uvicorn 的内部日志接入应用日志系统（logs/app.log + logs/error.log）。

    uvicorn 启动时会给 uvicorn / uvicorn.error / uvicorn.access 三个 logger
    挂上 stdout handler 并设 propagate=False，导致所有启动/访问日志打到标准输出，
    一旦启动命令带了 `> serverN.log` 重定向就会在项目根目录散落一堆文件。

    本函数在应用 lifespan 启动事件中调用（此时 uvicorn 已完成自身日志初始化），
    清除其 stdout handler、改向 root logger 传播，由 agent.logger 已配置好的
    logs/ 文件 handler 统一接管，从而不再产生任何 serverN.log。
    """
    try:
        get_logger("server")  # 确保 logging 已初始化（写入 logs/）
    except Exception:
        pass
    for _name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        _lg = logging.getLogger(_name)
        _lg.handlers = []
        _lg.propagate = True
    # 访问日志仅 WARNING 及以上进入 app.log，避免每个请求刷屏
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

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


def _classify_skill_risk(script) -> tuple:
    """根据技能执行模式与来源判定工具的审批风险。

    设计要点（纵深防御）：
    - 系统内置技能（is_system=True）视为可信，在已加固的沙箱中执行，免审批；
      local_execution 系统技能标为 read 级（展示用），不会触发确认弹窗。
    - 用户自建技能（is_system=False）不可信，local_execution 标为 exec、
      http_request 标为 write，且 require_approval=True —— 必须经过聊天框确认，
      防止 LLM 自动执行用户自定义的危险代码。

    Returns:
        (risk_level, require_approval, risk_description)
    """
    mode = getattr(script, "execution_mode", "local_execution")
    is_sys = getattr(script, "is_system", True)
    if mode == "local_execution":
        if is_sys:
            return ("read", False, "系统技能：在沙箱中执行自定义代码（已隔离）")
        return ("exec", True, "执行你自定义的 Python 代码（沙箱隔离，执行前需你确认）")
    if mode == "http_request":
        if is_sys:
            return ("read", False, "系统技能：向外部接口发起请求")
        return ("write", True, "向外部地址发起自定义请求（执行前需你确认）")
    return ("safe", False, "")


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
        _risk_level, _require_approval, _risk_desc = _classify_skill_risk(script)
        _tool_registry.register_tool(
            name=script.name,
            description=script.description,
            parameters=script.parameters,
            func=_create_executor(
                script.name, script.description, script.execution_mode,
                script.source, script.http_config, _llm_client,
                script.dependencies, script.response_formatter, sandbox_pool=_sandbox_pool
            ),
            risk_level=_risk_level,
            require_approval=_require_approval,
            risk_description=_risk_desc,
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
        ),
        risk_level="write", risk_description="新建自定义技能：{name}",
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
        ),
        risk_level="write", risk_description="更新自定义技能：{name}",
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
        ),
        risk_level="write", risk_description="删除自定义技能：{name}",
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

    # 注册用户级密钥管理工具（仅操作调用者自身 _user_id 的密钥，无 admin 门禁）
    _tool_registry.register_tool(
        name="set_user_secret",
        description="保存当前用户私有的密钥/配置（如第三方 API Key、私有 host）。"
                    "用于用户自带凭据的技能（天气、金价等）。密钥按用户加密隔离，其他用户不可见。",
        parameters={
            "type": "object",
            "properties": {
                "key_name": {"type": "string", "description": "密钥名称，如 qweather_api_key / qweather_api_host"},
                "value": {"type": "string", "description": "密钥明文"},
            },
            "required": ["key_name", "value"]
        },
        func=lambda key_name, value, _user_id=None: json.dumps(
            {"success": set_user_secret(_user_id, key_name, value)}, ensure_ascii=False
        )
    )
    _tool_registry.register_tool(
        name="list_user_secrets",
        description="列出当前用户已保存的私有密钥名称及脱敏值（不返回明文）。",
        parameters={"type": "object", "properties": {}, "required": []},
        func=lambda _user_id=None: json.dumps(
            {"secrets": list_user_secrets_masked(_user_id)}, ensure_ascii=False
        )
    )
    _tool_registry.register_tool(
        name="delete_user_secret",
        description="删除当前用户的一个私有密钥。",
        parameters={
            "type": "object",
            "properties": {
                "key_name": {"type": "string", "description": "要删除的密钥名称"},
            },
            "required": ["key_name"]
        },
        func=lambda key_name, _user_id=None: json.dumps(
            {"success": delete_user_secret(_user_id, key_name)}, ensure_ascii=False
        )
    )
    logger.info("已注册用户密钥管理工具（set/list/delete_user_secret）")

    # 注册技能模板安装工具
    _tool_registry.register_tool(
        name="install_skill_template",
        description="把官方技能模板（需自带 API Key 的，如 weather）安装到当前用户的技能仓库。"
                    "安装后需先用 set_user_secret 设置该技能需要的 key/host 才能使用。",
        parameters={
            "type": "object",
            "properties": {
                "template_name": {"type": "string", "description": "模板名称，如 weather"},
            },
            "required": ["template_name"]
        },
        func=lambda template_name, _user_id=None: json.dumps(
            install_skill_template(template_name, _user_id), ensure_ascii=False
        )
    )
    logger.info("已注册技能模板安装工具（install_skill_template）")

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

    # ============ 注册诊断与管理工具（admin 限定，见 agent/diagnostics.py） ============
    from agent.diagnostics import (
        diag_read_logs, diag_service_status, diag_restart_service,
        diag_check_env, diag_db_query, diag_read_file,
        diag_list_files, diag_delete_file, diag_rename_file,
        diag_validate_skill,
    )

    def _reg(name, desc, params, fn, risk_level="safe", risk_description=None):
        _tool_registry.register_tool(
            name=name, description=desc, parameters=params, func=fn,
            risk_level=risk_level, risk_description=risk_description,
        )

    _reg(
        "diag_read_logs",
        "【管理员】读取应用运行日志或错误日志尾部（只读）。参数 log_name: 'app'/'error' 或带日期的归档名；lines: 返回行数。用于排查工具报错时直接查看堆栈。",
        {"type": "object", "properties": {
            "log_name": {"type": "string", "description": "日志名：app / error / app.log.2026-08-06"},
            "lines": {"type": "integer", "description": "返回尾部行数，默认200，上限2000"},
        }, "required": ["log_name"]},
        lambda log_name, lines=200, _user_id=None: diag_read_logs(log_name, lines, _user_id),
    )
    _reg(
        "diag_service_status",
        "【管理员】查询服务运行端口(17520/17521/17523)的监听状态、进程 PID、Python 与 pip 版本（只读）。用于判断服务是否存活。",
        {"type": "object", "properties": {}},
        lambda _user_id=None: diag_service_status(_user_id),
    )
    _reg(
        "diag_restart_service",
        "【管理员】受控重启主服务。会先停掉旧进程再拉起新服务，当前请求可能短暂中断，稍后刷新即可。用于修复依赖/代码更新后需重启的场景。",
        {"type": "object", "properties": {}},
        lambda _user_id=None: diag_restart_service(_user_id),
        risk_level="exec", risk_description="重启主服务（当前连接将短暂中断）",
    )
    _reg(
        "diag_check_env",
        "【管理员】结构化只读环境检查（不开放自由 shell）。check_type 取值：python_version / pip_package(需 value=包名) / file_exists(需 value=相对路径) / port_listen(需 value=端口)。",
        {"type": "object", "properties": {
            "check_type": {"type": "string", "description": "python_version | pip_package | file_exists | port_listen"},
            "value": {"type": "string", "description": "配合 check_type 使用：包名/相对路径/端口号"},
        }, "required": ["check_type"]},
        lambda check_type, value="", _user_id=None: diag_check_env(check_type, value, _user_id),
    )
    _reg(
        "diag_db_query",
        "【管理员】数据库只读查询（SELECT-only，限白名单表 user_skills/sessions/users/permissions/model_configs/search_configs）。用于排查数据层问题，如确认 user_skills 表的禁用标记。",
        {"type": "object", "properties": {
            "sql": {"type": "string", "description": "单条 SELECT 语句，禁止 INSERT/UPDATE/DELETE/DROP 等写操作"},
        }, "required": ["sql"]},
        lambda sql, _user_id=None: diag_db_query(sql, _user_id),
    )
    _reg(
        "diag_read_file",
        "【管理员】读取项目内的配置/技能定义/沙箱脚本（白名单目录+扩展名，禁止密钥文件）。用于直接查看代码定位语法错误等问题。",
        {"type": "object", "properties": {
            "path": {"type": "string", "description": "相对项目根目录的路径，如 agent/skills/weather/SKILL.md"},
        }, "required": ["path"]},
        lambda path, _user_id=None: diag_read_file(path, _user_id),
    )
    _reg(
        "diag_list_files",
        "【管理员】列出 document_output 下已生成的文件（只读展示）。target_user_id 为空则列出所有用户。",
        {"type": "object", "properties": {
            "target_user_id": {"type": "integer", "description": "可选，按用户过滤；不传则列出全部"},
        }, "required": []},
        lambda target_user_id=None, _user_id=None: diag_list_files(target_user_id, _user_id),
    )
    _reg(
        "diag_delete_file",
        "【管理员】删除 document_output 下的某个生成文件（写操作，带审计）。",
        {"type": "object", "properties": {
            "rel_path": {"type": "string", "description": "相对项目根目录的文件路径，必须位于 document_output 下"},
        }, "required": ["rel_path"]},
        lambda rel_path, _user_id=None: diag_delete_file(rel_path, _user_id),
        risk_level="write", risk_description="删除文件：{rel_path}",
    )
    _reg(
        "diag_rename_file",
        "【管理员】重命名 document_output 下的某个生成文件（写操作，带审计）。new_name 仅限文件名。",
        {"type": "object", "properties": {
            "old_rel_path": {"type": "string", "description": "原文件相对路径"},
            "new_name": {"type": "string", "description": "新文件名（不含路径）"},
        }, "required": ["old_rel_path", "new_name"]},
        lambda old_rel_path, new_name, _user_id=None: diag_rename_file(old_rel_path, new_name, _user_id),
        risk_level="write", risk_description="重命名文件：{old_rel_path} → {new_name}",
    )
    _reg(
        "diag_validate_skill",
        "【管理员】校验 SKILL.md 格式是否合法、字段(name/description)是否完整、正文是否非空。保存前可先调用此工具自检。",
        {"type": "object", "properties": {
            "skill_md": {"type": "string", "description": "待校验的 SKILL.md 全文"},
        }, "required": ["skill_md"]},
        lambda skill_md, _user_id=None: diag_validate_skill(skill_md, _user_id),
    )
    logger.info("已注册诊断与管理工具（9个，admin限定）")

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
    # 将 uvicorn 内部日志接入 logs/ 体系，避免散落 serverN.log
    _attach_uvicorn_logging()

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

# CORS：默认仅允许本地前端域名。生产环境通过环境变量 CORS_ALLOW_ORIGINS
# 指定可信前端来源（逗号分隔），例如：https://app.example.com,https://admin.example.com
# 注意：浏览器禁止在 allow_credentials=True 时使用 "*"，故检测到 "*" 时自动关闭凭据。
_CORS_ORIGINS = [
    o.strip()
    for o in os.environ.get(
        "CORS_ALLOW_ORIGINS",
        "http://localhost:17520,http://127.0.0.1:17520,http://localhost:5173,http://127.0.0.1:5173",
    ).split(",")
    if o.strip()
]
_CORS_ALLOW_CREDENTIALS = "*" not in _CORS_ORIGINS

app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_credentials=_CORS_ALLOW_CREDENTIALS,
    allow_methods=["*"],
    allow_headers=["*"],
)

AUTH_WHITELIST = {"/api/auth/login", "/api/health", "/login.html", "/favicon.ico"}

# 处于"必须修改初始密码"状态时仍允许访问的端点（改密、登出、查自身、健康检查）
_MUST_CHANGE_ALLOW = {
    "/api/auth/password",
    "/api/auth/logout",
    "/api/auth/me",
    "/api/health",
}


@app.middleware("http")
async def no_cache_static(request: Request, call_next):
    """静态资源与页面禁用缓存，避免浏览器使用旧版 app.js/style.css。"""
    resp = await call_next(request)
    path = request.url.path
    if path.startswith("/static/") or path in ("/", "/login.html", "/favicon.ico"):
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    return resp


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path

    if path in AUTH_WHITELIST or path.startswith("/static/"):
        return await call_next(request)

    token = request.cookies.get("auth_token") or request.headers.get("Authorization", "").replace("Bearer ", "")

    from server.routes.auth import validate_token, decode_token
    from server.database import get_user_must_change
    if not token or not validate_token(token):
        if path.startswith("/api/"):
            return JSONResponse(status_code=401, content={"detail": "未登录"})
        return RedirectResponse(url="/login.html")

    # 强制修改初始密码：除改密/登出/查自身外一律拦截
    if path not in _MUST_CHANGE_ALLOW:
        payload = decode_token(token)
        if payload and get_user_must_change(int(payload.get("uid", 0))):
            if path.startswith("/api/"):
                return JSONResponse(
                    status_code=403,
                    content={"detail": "请先修改初始密码后再使用", "must_change_password": True},
                )
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
        logger.info(f"数据库管理代理已启动(仅本地回环): http://127.0.0.1:{DB_PROXY_PORT}")
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
        # 仅绑定本地回环地址，避免数据库管理界面直接暴露在公网。
        # 运维需访问时，请通过 SSH 隧道：ssh -L 17521:127.0.0.1:17521 user@your-server-ip
        server = http.server.ThreadingHTTPServer(("127.0.0.1", proxy_port), ProxyHandler)
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