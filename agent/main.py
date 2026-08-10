import os
import sys
import json

# 将项目根目录加入 sys.path，使 agent 包可被导入
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import readline
except ImportError:
    pass
from openai import AuthenticationError
from agent.tools import ToolRegistry
from agent.llm import LLMClient
from agent.agent import SimpleAgent
from agent.config import AgentConfig
from agent.user_secrets import resolve_user_secrets_in_template


# ==================== 执行器工厂函数 ====================

def create_simulated_executor(tool_name: str, execution_prompt: str,
                              llm_client: LLMClient):
    """创建 LLM 模拟执行器

    Args:
        tool_name: 工具名称
        execution_prompt: 执行提示词模板
        llm_client: LLMClient 实例

    Returns:
        callable: 执行函数
    """
    def executor(**kwargs):
        prompt = execution_prompt
        for key, value in kwargs.items():
            prompt = prompt.replace(f"{{{{{key}}}}}", str(value))

        try:
            response = llm_client.chat(
                [{"role": "user", "content": prompt}],
                temperature=0.1
            )
            return response.get("content", "")
        except Exception as e:
            return f"模拟执行失败: {str(e)}"

    return executor


def create_local_executor(tool_name: str, execution_code: str,
                          dependencies: list = None, sandbox_pool=None):
    """创建本地执行器

    Args:
        tool_name: 工具名称
        execution_code: Python 代码字符串
        dependencies: pip 依赖包列表
        sandbox_pool: 可选的 SandboxPool 实例

    Returns:
        callable: 执行函数
    """
    def executor(**kwargs):
        nonlocal sandbox_pool
        if sandbox_pool is None:
            from sandbox import SandboxPool
            sandbox_pool = SandboxPool()

        if dependencies:
            # 提取 user_id 以获取用户专属沙箱
            user_id = kwargs.get("_user_id", 0)
            sandbox = sandbox_pool.get(user_id)
            install_ok = sandbox.install(dependencies)
            if not install_ok:
                return f"[工具执行异常] 依赖安装失败: {dependencies}。请检查网络连接或手动安装。"

        # 提取 user_id 并移除，避免传入工具参数
        user_id = kwargs.pop("_user_id", 0)
        sandbox = sandbox_pool.get(user_id)

        try:
            return sandbox.execute(execution_code, kwargs, timeout=60, user_id=user_id, tool_name=tool_name)
        except Exception as e:
            return f"[工具执行异常] {type(e).__name__}: {str(e)}"

    return executor


def create_http_executor(tool_name: str, http_config: dict,
                         execution_prompt: str, llm_client: LLMClient,
                         response_formatter: str = None):
    """创建 HTTP 请求执行器

    Args:
        tool_name: 工具名称
        http_config: HTTP 请求配置 {url, method, headers, body}
        execution_prompt: 执行提示词模板
        llm_client: LLMClient 实例
        response_formatter: 可选的 Python 格式化代码

    Returns:
        callable: 执行函数
    """
    import urllib.request
    import urllib.error

    def executor(**kwargs):
        # 按用户解析 {secret:xxx} 占位符（多用户各自密钥，回退全局）
        user_id = kwargs.get("_user_id", 0)
        url = resolve_user_secrets_in_template(http_config.get("url", ""), user_id)
        method = http_config.get("method", "GET").upper()
        headers = {
            k: resolve_user_secrets_in_template(v, user_id)
            for k, v in http_config.get("headers", {}).items()
        }
        body_template = resolve_user_secrets_in_template(http_config.get("body", ""), user_id)

        for key, value in kwargs.items():
            if key == "_user_id":
                continue
            url = url.replace(f"{{{key}}}", str(value))
            body_template = body_template.replace(f"{{{key}}}", str(value))

        req_data = None
        if method in ("POST", "PUT", "PATCH") and body_template:
            req_data = body_template.encode("utf-8")

        req = urllib.request.Request(url, data=req_data, method=method)
        for k, v in headers.items():
            req.add_header(k, v)

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            return f"HTTP请求失败: {e.code} {e.reason}"
        except Exception as e:
            return f"HTTP请求失败: {str(e)}"

        if response_formatter:
            try:
                local_vars = {"response_data": raw, "json": json}
                exec(response_formatter, {}, local_vars)
                if "format_response" in local_vars:
                    # 透传调用参数，使格式化器能拿到 location/unit 等字段
                    return local_vars["format_response"](raw, **kwargs)
            except Exception as e:
                pass

        prompt = execution_prompt
        prompt = prompt.replace("{{response_data}}", raw[:5000])
        for key, value in kwargs.items():
            prompt = prompt.replace(f"{{{{{key}}}}}", str(value))

        try:
            response = llm_client.chat(
                [{"role": "user", "content": prompt}],
                temperature=0.1
            )
            return response.get("content", "")
        except Exception as e:
            return raw[:2000]

    return executor


# ==================== 辅助函数 ====================

def _prompt_reconfigure(config: AgentConfig, llm_client: LLMClient, error_msg: str):
    """模型认证失败时，提示用户重新配置

    Args:
        config: AgentConfig 实例
        llm_client: LLMClient 实例
        error_msg: 错误信息
    """
    print(f"\n模型调用失败：{error_msg}")
    print("可能是 API Key 未配置或已失效。")
    try:
        input("按回车键进入模型配置页面...")
    except (EOFError, KeyboardInterrupt):
        return
    _handle_model_set(config, llm_client)


def _create_executor(tool_name: str, execution_prompt: str,
                     execution_mode: str, execution_code: str,
                     http_config: dict, llm_client: LLMClient,
                     dependencies: list = None,
                     response_formatter: str = None,
                     sandbox_pool=None):
    """根据 execution_mode 创建对应的执行器

    Args:
        tool_name: 工具名称
        execution_prompt: 执行提示词模板
        execution_mode: 执行模式，"llm_simulated"、"local_execution" 或 "http_request"
        execution_code: 预生成的 Python 代码（仅 local_execution 使用）
        http_config: HTTP 请求配置（仅 http_request 使用）
        llm_client: LLMClient 实例
        dependencies: pip 依赖包列表（仅 local_execution 使用）
        response_formatter: 可选的 Python 格式化代码（仅 http_request 使用）
        sandbox_pool: 可选的 SandboxPool 实例

    Returns:
        callable: 执行函数
    """
    if execution_mode == "local_execution":
        return create_local_executor(tool_name, execution_code, dependencies, sandbox_pool=sandbox_pool)
    if execution_mode == "http_request":
        return create_http_executor(tool_name, http_config, execution_prompt, llm_client,
                                    response_formatter=response_formatter)
    return create_simulated_executor(tool_name, execution_prompt, llm_client)


# ==================== 模型命令处理 ====================

def _parse_model_command(user_input: str) -> tuple:
    """解析 /model 命令

    支持格式：
        /model set
        /model show
        /model update

    Args:
        user_input: 用户输入的原始字符串

    Returns:
        tuple: (action: str or None, arg: str or None)
    """
    stripped = user_input.strip()
    if not stripped.startswith("/model"):
        return None, None

    parts = stripped.split(maxsplit=2)
    if len(parts) < 2:
        return "invalid", None

    action = parts[1].lower()
    if action in ("set", "show", "update"):
        return action, None

    return "invalid", None


def _handle_model_set(config: AgentConfig, llm_client: LLMClient):
    """处理 /model set 命令：配置模型参数

    Args:
        config: AgentConfig 实例
        llm_client: LLMClient 实例，配置保存后刷新
    """
    print("请依次输入模型配置（输入 exit 可随时取消）：")
    print("-" * 40)

    try:
        api_key = input("API Key: ").strip()
    except (EOFError, KeyboardInterrupt):
        return
    if api_key.lower() == "exit":
        print("已取消配置。")
        return
    if not api_key:
        print("API Key 不能为空，已取消配置。")
        return

    current_base_url = config.get('base_url') or ''
    current_model = config.get('model_name') or ''
    base_url_prompt = f"Base URL{f' [{current_base_url}]' if current_base_url else ''}: "
    model_prompt = f"Model Name{f' [{current_model}]' if current_model else ''}: "

    try:
        base_url = input(base_url_prompt).strip()
    except (EOFError, KeyboardInterrupt):
        return
    if base_url.lower() == "exit":
        print("已取消配置。")
        return
    if not base_url:
        base_url = current_base_url

    try:
        model_name = input(model_prompt).strip()
    except (EOFError, KeyboardInterrupt):
        return
    if model_name.lower() == "exit":
        print("已取消配置。")
        return
    if not model_name:
        model_name = current_model

    current_limit = config.get('context_limit') or ''
    if current_limit:
        limit_hint = f" [当前: {current_limit}]"
    else:
        limit_hint = " [如 32k、64k、128k，留空则使用模型最大上下文]"
    limit_prompt = f"上下文限制{limit_hint}: "

    try:
        limit_input = input(limit_prompt).strip()
    except (EOFError, KeyboardInterrupt):
        return
    if limit_input.lower() == "exit":
        print("已取消配置。")
        return

    context_limit = limit_input if limit_input else ""

    config.set_model(api_key, base_url, model_name)
    config.set('context_limit', context_limit)
    llm_client.refresh()
    print(f"\n配置已保存并生效！")
    print(f"  Model: {model_name}")
    print(f"  Base URL: {base_url}")
    print(f"  API Key: {config.get_masked_api_key()}")
    if context_limit:
        print(f"  上下文限制: {context_limit}")
    else:
        print(f"  上下文限制: 使用模型最大上下文")


def _handle_model_show(config: AgentConfig):
    """处理 /model show 命令：显示当前配置

    Args:
        config: AgentConfig 实例
    """
    info = config.show_config()
    print("当前模型配置：")
    print(f"  Model Name: {info['model_name']}")
    print(f"  Base URL:   {info['base_url']}")
    print(f"  API Key:    {info['api_key']}")
    limit = info['context_limit']
    if limit:
        print(f"  上下文限制: {limit}")
    else:
        print(f"  上下文限制: 使用模型最大上下文")


def _handle_model_update(config: AgentConfig, llm_client: LLMClient):
    """处理 /model update 命令：更新单个配置项

    Args:
        config: AgentConfig 实例
        llm_client: LLMClient 实例，配置保存后刷新
    """
    info = config.show_config()
    print("当前配置：")
    print(f"  1) Model Name: {info['model_name']}")
    print(f"  2) Base URL:   {info['base_url']}")
    print(f"  3) API Key:    {info['api_key']}")
    limit_display = info['context_limit'] if info['context_limit'] else "使用模型最大上下文"
    print(f"  4) 上下文限制: {limit_display}")
    print()
    print("输入序号选择要修改的项（输入 exit 取消）：")

    try:
        choice = input("> ").strip()
    except (EOFError, KeyboardInterrupt):
        return

    if choice.lower() == "exit":
        print("已取消更新。")
        return

    if choice == "1":
        try:
            new_value = input("新的 Model Name: ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if new_value.lower() == "exit":
            print("已取消更新。")
            return
        if not new_value:
            print("Model Name 不能为空，已取消更新。")
            return
        config.set('model_name', new_value)
        print(f"Model Name 已更新为：{new_value}")

    elif choice == "2":
        try:
            new_value = input("新的 Base URL: ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if new_value.lower() == "exit":
            print("已取消更新。")
            return
        if not new_value:
            print("Base URL 不能为空，已取消更新。")
            return
        config.set('base_url', new_value)
        print(f"Base URL 已更新为：{new_value}")

    elif choice == "3":
        try:
            new_value = input("新的 API Key: ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if new_value.lower() == "exit":
            print("已取消更新。")
            return
        if not new_value:
            print("API Key 不能为空，已取消更新。")
            return
        config.set_api_key(new_value)
        print(f"API Key 已更新为：{config.get_masked_api_key()}")

    elif choice == "4":
        try:
            new_value = input("新的上下文限制（如 32k、64k、128k，留空使用模型最大上下文）: ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if new_value.lower() == "exit":
            print("已取消更新。")
            return
        config.set('context_limit', new_value if new_value else "")
        if new_value:
            print(f"上下文限制已更新为：{new_value}")
        else:
            print("上下文限制已更新为：使用模型最大上下文")

    else:
        print("无效的序号，已取消更新。")
        return

    llm_client.refresh()
    print("配置已更新并生效。")


# ==================== Agent 命令处理 ====================

def _parse_agent_command(user_input: str) -> tuple:
    """解析 /agent 命令

    支持格式：
        /agent thought on
        /agent thought off

    Args:
        user_input: 用户输入的原始字符串

    Returns:
        tuple: (action: str or None, arg: str or None)
    """
    stripped = user_input.strip()
    if not stripped.startswith("/agent"):
        return None, None

    parts = stripped.split(maxsplit=3)
    if len(parts) < 2:
        return "invalid", None

    action = parts[1].lower()
    if action == "thought":
        arg = parts[2].lower() if len(parts) > 2 else None
        return "thought", arg

    return "invalid", None


def _handle_agent_thought(config: AgentConfig, agent: SimpleAgent, arg: str):
    """处理 /agent thought on|off 命令

    Args:
        config: AgentConfig 实例
        agent: SimpleAgent 实例
        arg: "on" 或 "off"
    """
    if arg not in ("on", "off"):
        print("用法：/agent thought on  或  /agent thought off")
        return

    enabled = (arg == "on")
    config.set('show_thought', enabled)
    agent.set_show_thought(enabled)
    status = "开启" if enabled else "关闭"
    print(f"思考过程显示已{status}。")


def _show_help():
    """显示帮助信息，列出所有可用命令"""
    print("""
可用命令：
  /help                    显示此帮助信息
  exit                     退出程序
  reset                    重置对话上下文

  /model set               配置模型参数（API Key 加密存储）
  /model show              查看当前模型配置
  /model update            修改单个配置项

  /agent thought on|off    开启/关闭 Agent 思考过程显示

  直接输入自然语言即可与 Agent 对话。""")


def main():
    """主函数：初始化组件并启动命令行交互"""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(base_dir)

    config = AgentConfig()

    from agent.tool_secrets import get_tool_secrets
    get_tool_secrets()

    tool_registry = ToolRegistry()

    context_limit = config.get('context_limit') or ''
    llm_client = LLMClient(config=config)

    # 初始化沙箱池
    from agent.sandbox import SandboxPool
    try:
        sandbox_pool = SandboxPool()
    except Exception as e:
        print(f"[启动] 沙箱初始化失败: {e}")
        print("[启动] 工具执行功能将不可用")
        sandbox_pool = None

    # 初始化技能注册中心
    from agent.skill_registry import SkillRegistry
    skill_registry = SkillRegistry()
    skills_dir = os.path.join(base_dir, "skills")
    system_skills = skill_registry.load_system_skills(skills_dir)
    print(f"[启动] 已加载 {len(system_skills)} 个系统技能: {system_skills}")

    # 从技能注册中心加载所有脚本，注册为工具
    skill_scripts = skill_registry.get_all_scripts()
    for script in skill_scripts:
        tool_registry.register_tool(
            name=script.name,
            description=script.description,
            parameters=script.parameters,
            func=_create_executor(
                script.name, script.description, script.execution_mode,
                script.source, script.http_config, llm_client,
                script.dependencies, script.response_formatter, sandbox_pool=sandbox_pool
            )
        )
    print(f"[启动] 从技能中注册了 {len(skill_scripts)} 个工具脚本")

    # 初始化多 Agent 池
    from agent.agent_pool import AgentPool
    profiles_dir = os.path.join(base_dir, "profiles")
    agent_pool = AgentPool(llm_client, sandbox_pool, skill_registry)
    pool_agents = agent_pool.load_profiles(profiles_dir)
    if pool_agents:
        agent_pool.register_as_tools(tool_registry)
        print(f"[启动] 已加载 {len(pool_agents)} 个子 Agent: {pool_agents}")

    # 构建技能上下文
    skill_context = skill_registry.build_context()

    agent = SimpleAgent(llm_client, tool_registry, context_limit=context_limit,
                        show_thought=config.get('show_thought', False),
                        skill_context=skill_context)

    print("\n轻量级 AI Agent 底座已启动（输入 /help 查看可用命令）")
    print("-" * 50)

    while True:
        try:
            user_input = input("你: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见！")
            break

        if not user_input:
            continue

        if user_input.lower() == "exit":
            break

        if user_input.lower() == "reset":
            agent.reset()
            print("对话已重置")
            print("-" * 50)
            continue

        if user_input.lower() == "/help" or user_input.lower() == "help":
            _show_help()
            print("-" * 50)
            continue

        model_action, _ = _parse_model_command(user_input)
        if model_action == "set":
            _handle_model_set(config, llm_client)
            print("-" * 50)
            continue
        elif model_action == "show":
            _handle_model_show(config)
            print("-" * 50)
            continue
        elif model_action == "update":
            _handle_model_update(config, llm_client)
            print("-" * 50)
            continue
        elif model_action == "invalid":
            print("无效的 /model 命令。可用子命令：set、show、update")
            print("输入 /help 查看完整帮助。")
            print("-" * 50)
            continue

        agent_action, agent_arg = _parse_agent_command(user_input)
        if agent_action == "thought":
            _handle_agent_thought(config, agent, agent_arg)
            print("-" * 50)
            continue
        elif agent_action == "invalid":
            print("无效的 /agent 命令。可用子命令：thought on|off")
            print("输入 /help 查看完整帮助。")
            print("-" * 50)
            continue

        api_key = config.get_api_key()
        if not api_key:
            print("未配置 API Key，无法调用大模型。")
            print("请使用 /model set 命令配置模型参数。")
            print("输入 /help 查看完整帮助。")
            print("-" * 50)
            continue

        try:
            response = agent.chat(user_input)
        except AuthenticationError as e:
            _prompt_reconfigure(config, llm_client, str(e))
            print("-" * 50)
            continue
        print("\n" + "-" * 50)


if __name__ == "__main__":
    main()