import os
import json
try:
    import readline
except ImportError:
    pass
from openai import AuthenticationError
from tools import ToolRegistry
from llm import LLMClient
try:
    from agent.agent import SimpleAgent
except ImportError:
    from agent import SimpleAgent
from tool_builder import ToolBuilder, create_simulated_executor, create_local_executor, create_http_executor
from config import AgentConfig


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


def _parse_tool_command(user_input: str) -> tuple:
    """解析 /tool 命令

    支持格式：
        /tool list
        /tool add
        /tool update <tool_name>
        /tool delete <tool_name>

    Args:
        user_input: 用户输入的原始字符串

    Returns:
        tuple: (action: str, tool_name: str or None)
               action 为 "list"、"add"、"update"、"delete" 或 None（非 tool 命令）
    """
    stripped = user_input.strip()
    if not stripped.startswith("/tool"):
        return None, None

    parts = stripped.split(maxsplit=2)
    if len(parts) < 2:
        return "invalid", None

    action = parts[1].lower()
    if action in ("list", "add"):
        return action, None
    elif action in ("update", "delete"):
        tool_name = parts[2] if len(parts) > 2 else None
        return action, tool_name

    return "invalid", None


def _create_executor(tool_name: str, execution_prompt: str,
                     execution_mode: str, execution_code: str,
                     http_config: dict, llm_client: LLMClient,
                     dependencies: list = None):
    """根据 execution_mode 创建对应的执行器

    Args:
        tool_name: 工具名称
        execution_prompt: 执行提示词模板
        execution_mode: 执行模式，"llm_simulated"、"local_execution" 或 "http_request"
        execution_code: 预生成的 Python 代码（仅 local_execution 使用）
        http_config: HTTP 请求配置（仅 http_request 使用）
        llm_client: LLMClient 实例
        dependencies: pip 依赖包列表（仅 local_execution 使用）

    Returns:
        callable: 执行函数
    """
    if execution_mode == "local_execution":
        return create_local_executor(tool_name, execution_code, dependencies)
    if execution_mode == "http_request":
        return create_http_executor(tool_name, http_config, execution_prompt, llm_client)
    return create_simulated_executor(tool_name, execution_prompt, llm_client)


def _install_dependencies_interactive(dependencies: list):
    """交互式安装依赖：询问用户确认后安装，显示安装过程

    安装失败或用户 Ctrl+C 中断时自动清理已安装的包。

    Args:
        dependencies: 需要安装的 pip 包列表

    Returns:
        bool: 安装是否成功
    """
    if not dependencies:
        return True

    from sandbox import ToolSandbox

    print(f"\n该工具需要安装以下依赖: {', '.join(dependencies)}")
    print("是否安装？(Y/n)：", end="")
    try:
        confirm = input().strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\n已取消安装。")
        return False

    if confirm and confirm not in ("y", "yes"):
        print("已跳过依赖安装，工具首次调用时将自动尝试安装。")
        return False

    sandbox = ToolSandbox()
    try:
        sandbox.install_verbose(dependencies)
        return True
    except KeyboardInterrupt:
        print("\n用户中断安装，正在清理...")
        sandbox._cleanup_failed_install(dependencies)
        return False
    except Exception as e:
        print(f"依赖安装失败: {e}")
        return False


def _ensure_output_dir(tool_json: dict):
    """确保文件输出类工具的输出目录存在

    如果工具定义了 output_dir 字段，则在项目根目录下创建该目录。

    Args:
        tool_json: 工具 JSON 定义
    """
    output_dir = tool_json.get("output_dir")
    if not output_dir:
        return

    if output_dir.startswith("..") or output_dir.startswith("/"):
        print(f"[输出目录] 警告：output_dir '{output_dir}' 包含不安全的路径，已跳过创建。")
        return

    base_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(base_dir)
    full_path = os.path.join(project_root, output_dir)

    if not os.path.exists(full_path):
        os.makedirs(full_path, exist_ok=True)
        print(f"[输出目录] 已创建: {output_dir}/")
    else:
        print(f"[输出目录] 目录已存在: {output_dir}/")


def _check_duplicate_tool(description: str, existing_tools: list,
                          llm_client: LLMClient) -> dict:
    """检测用户描述的新工具是否与已有工具功能重复

    Args:
        description: 用户对新工具的描述
        existing_tools: 已有工具列表，每项为 (name, description)
        llm_client: LLMClient 实例

    Returns:
        dict: {"is_duplicate": bool, "matched_tool": str or None, "reason": str}
    """
    if not existing_tools:
        return {"is_duplicate": False, "matched_tool": None, "reason": ""}

    tools_text = "\n".join(
        f"- {name}: {desc}" for name, desc in existing_tools
    )

    prompt = f"""你是一个工具重复检测器。判断用户描述的新工具是否与已有工具功能重复。

已有工具列表：
{tools_text}

用户对新工具的描述：
{description}

判断标准：
- 如果新工具的核心功能与某个已有工具高度重叠（如都是生成Word文档、都是查询天气），则判定为重复。
- 如果只是部分相关但核心功能不同（如一个生成Word、一个生成Excel），则不算重复。
- 如果新工具是已有工具的超集或子集，也算重复。

请严格输出一个 JSON 对象：
{{"is_duplicate": true或false, "matched_tool": "匹配到的工具名（仅is_duplicate为true时需要）", "reason": "简要说明判断理由"}}"""

    try:
        response = llm_client.chat(
            [{"role": "user", "content": prompt}],
            temperature=0.1
        )
        raw = response.get("content", "")
        import re as _re
        match = _re.search(r'\{.*\}', raw, _re.DOTALL)
        if match:
            result = json.loads(match.group())
            return result
    except Exception:
        pass

    return {"is_duplicate": False, "matched_tool": None, "reason": ""}


def _handle_tool_add(builder: ToolBuilder, registry: ToolRegistry,
                     llm_client: LLMClient, tools_dir: str, agent,
                     config: AgentConfig):
    """处理 /tool add 命令：通过自然语言新增工具（交互式）

    Args:
        builder: ToolBuilder 实例
        registry: ToolRegistry 实例
        llm_client: LLMClient 实例
        tools_dir: Tool 存储目录路径
        agent: SimpleAgent 实例，新增工具后重置对话上下文
        config: AgentConfig 实例
    """
    print("请通过自然语言描述新增的工具：")
    try:
        description = input("> ").strip()
    except (EOFError, KeyboardInterrupt):
        return

    if description.lower() == "exit":
        print("已取消新增操作，返回对话模式。")
        return

    if not description:
        print("描述不能为空，已取消新增操作。")
        return

    existing = [(name, info.get("description", ""))
                for name, info in registry.list_tools().items()]
    dup_check = _check_duplicate_tool(description, existing, llm_client)
    if dup_check.get("is_duplicate"):
        matched = dup_check.get("matched_tool", "未知")
        reason = dup_check.get("reason", "")
        print(f"\n[重复检测] {reason}")
        print(f"检测到与已有工具 '{matched}' 功能相似。")
        print("请选择操作：")
        print("  1. 仍要创建新工具")
        print("  2. 优化已有工具 '{matched}'")
        print("  3. 取消")
        try:
            choice = input("请输入选项 (1/2/3)：").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if choice == "2":
            _handle_tool_update(builder, registry, llm_client, tools_dir,
                                matched, agent, config)
            return
        elif choice == "3":
            print("已取消新增操作。")
            return
        elif choice != "1":
            print("无效选项，已取消。")
            return
        print("将继续创建新工具...\n")

    print("正在智能分析并生成工具...")
    try:
        smart_result = builder.smart_generate(description)
    except AuthenticationError as e:
        _prompt_reconfigure(config, llm_client, str(e))
        return
    except ValueError as e:
        print(f"智能生成失败：{e}，回退到传统流程...")
        try:
            analysis = builder.analyze_requirements(description)
        except AuthenticationError as e2:
            _prompt_reconfigure(config, llm_client, str(e2))
            return
        except ValueError as e2:
            print(f"需求分析失败：{e2}")
            return
        smart_result = {"success": False, "need_info": analysis.get("need_info", False),
                        "questions": analysis.get("questions", []),
                        "reason": analysis.get("reason", "")}

    if smart_result.get("success"):
        tool_json = smart_result["tool"]
    else:
        if smart_result.get("need_info"):
            questions = smart_result.get("questions", [])
            partial_tool = smart_result.get("partial_tool", {})
            reason = smart_result.get("reason", "")
            if reason:
                print(f"\n{reason}")
            if questions:
                print("需要以下信息：")
                for i, q in enumerate(questions, 1):
                    print(f"  {i}. {q}")
                print()
                print("请依次回答（输入 exit 可随时取消）：")
                answers = []
                for i, q in enumerate(questions, 1):
                    try:
                        ans = input(f"  [{i}] {q}\n  > ").strip()
                    except (EOFError, KeyboardInterrupt):
                        return
                    if ans.lower() == "exit":
                        print("已取消新增操作，返回对话模式。")
                        return
                    if not ans:
                        ans = "（未提供）"
                    answers.append(f"Q: {q}\nA: {ans}")
                extra_info = "\n用户提供的额外信息：\n" + "\n".join(answers)
            else:
                extra_info = ""

            full_description = description
            if extra_info:
                full_description = f"{description}\n{extra_info}"
            if partial_tool:
                full_description = (
                    f"原始需求：{description}\n{extra_info}\n\n"
                    f"已部分确定的工具定义（请在此基础上补全）：\n"
                    f"{json.dumps(partial_tool, ensure_ascii=False, indent=2)}"
                )
        else:
            full_description = description

        print("正在生成工具定义...")
        try:
            tool_json = builder.generate_tool(full_description)
        except AuthenticationError as e:
            _prompt_reconfigure(config, llm_client, str(e))
            return
        except ValueError as e:
            print(f"生成失败：{e}")
            return

    valid, msg = builder.validate_tool_json(tool_json)
    if not valid:
        print(f"工具定义校验失败：{msg}，正在自动修复...")
        repair_desc = f"校验失败原因：{msg}。请修复此问题，确保输出完整的工具 JSON，包含所有必须字段。"
        try:
            tool_json = builder.repair_tool(tool_json, repair_desc)
        except AuthenticationError as e:
            _prompt_reconfigure(config, llm_client, str(e))
            return
        except ValueError as e:
            print(f"自动修复失败：{e}")
            return
        tool_json["name"] = tool_json.get("name", "")
        valid, msg = builder.validate_tool_json(tool_json)
        if not valid:
            print(f"自动修复后校验仍失败：{msg}")
            print("建议重新描述工具需求后再次尝试。")
            return
        print("自动修复成功。")

    try:
        builder.save_tool_to_file(tool_json, tools_dir=tools_dir)
    except (ValueError, IOError) as e:
        print(f"保存失败：{e}")
        return

    _ensure_output_dir(tool_json)

    dependencies = tool_json.get("dependencies")
    if dependencies:
        _install_dependencies_interactive(dependencies)

    executor = _create_executor(
        tool_json["name"], tool_json["execution_prompt"],
        tool_json["execution_mode"],
        tool_json.get("execution_code", ""),
        tool_json.get("http_config", {}),
        llm_client,
        tool_json.get("dependencies")
    )
    registry.register_tool(
        name=tool_json["name"],
        description=tool_json["description"],
        parameters=tool_json["parameters"],
        func=executor
    )
    print(f"工具 '{tool_json['name']}' 已新增并注册成功！")

    _self_test_tool(tool_json, executor)

    agent.reset()
    print("对话上下文已重置，新工具立即可用。")


def _handle_tool_update(builder: ToolBuilder, registry: ToolRegistry,
                        llm_client: LLMClient, tools_dir: str, tool_name: str, agent,
                        config: AgentConfig):
    """处理 /tool update 命令：通过自然语言修改已有工具

    Args:
        builder: ToolBuilder 实例
        registry: ToolRegistry 实例
        llm_client: LLMClient 实例
        tools_dir: Tool 存储目录路径
        tool_name: 要修改的工具名称
        agent: SimpleAgent 实例，修改工具后重置对话上下文
        config: AgentConfig 实例
    """
    if not tool_name:
        print("请指定要修改的工具名称，格式：/tool update <工具名>")
        return

    filepath = os.path.join(tools_dir, f"{tool_name}.json")
    if not os.path.isfile(filepath):
        print(f"工具 '{tool_name}' 不存在，请先使用 /tool add 创建。")
        return

    print("请输入修改内容：")
    try:
        description = input("> ").strip()
    except (EOFError, KeyboardInterrupt):
        return

    if description.lower() == "exit":
        print("已取消修改操作，返回对话模式。")
        return

    if not description:
        print("描述不能为空，已取消修改操作。")
        return

    print("正在分析并修复工具...")
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            original_tool = json.load(f)
    except Exception:
        original_tool = {}

    try:
        tool_json = builder.repair_tool(original_tool, description)
    except AuthenticationError as e:
        _prompt_reconfigure(config, llm_client, str(e))
        return
    except ValueError as e:
        print(f"修复失败：{e}")
        return

    tool_json["name"] = tool_name

    valid, msg = builder.validate_tool_json(tool_json)
    if not valid:
        print(f"首次修复校验失败：{msg}，正在重试...")
        retry_desc = f"{description}\n\n重要提醒：上次修复输出的 JSON 缺少必要字段（{msg}），请确保输出完整的工具 JSON，包含所有必须字段。"
        try:
            tool_json = builder.repair_tool(original_tool, retry_desc)
        except AuthenticationError as e:
            _prompt_reconfigure(config, llm_client, str(e))
            return
        except ValueError as e:
            print(f"重试失败：{e}")
            return
        tool_json["name"] = tool_name
        valid, msg = builder.validate_tool_json(tool_json)
        if not valid:
            print(f"重试后校验仍失败：{msg}")
            print("建议使用 /tool add 重新创建该工具。")
            return

    try:
        builder.save_tool_to_file(tool_json, tools_dir=tools_dir)
    except (ValueError, IOError) as e:
        print(f"保存失败：{e}")
        return

    registry.unregister_tool(tool_name)

    _ensure_output_dir(tool_json)

    dependencies = tool_json.get("dependencies")
    if dependencies:
        _install_dependencies_interactive(dependencies)

    executor = _create_executor(
        tool_name, tool_json["execution_prompt"],
        tool_json["execution_mode"],
        tool_json.get("execution_code", ""),
        tool_json.get("http_config", {}),
        llm_client,
        tool_json.get("dependencies")
    )
    registry.register_tool(
        name=tool_name,
        description=tool_json["description"],
        parameters=tool_json["parameters"],
        func=executor
    )
    print(f"工具 '{tool_name}' 已更新并重新注册成功！")

    _self_test_tool(tool_json, executor)

    agent.reset()
    print("对话上下文已重置，更新后的工具立即可用。")


def _handle_tool_delete(registry: ToolRegistry, tools_dir: str, tool_name: str):
    """处理 /tool delete 命令：删除已有工具

    Args:
        registry: ToolRegistry 实例
        tools_dir: Tool 存储目录路径
        tool_name: 要删除的工具名称
    """
    if not tool_name:
        print("请指定要删除的工具名称，格式：/tool delete <工具名>")
        return

    filepath = os.path.join(tools_dir, f"{tool_name}.json")
    if not os.path.isfile(filepath):
        print(f"工具 '{tool_name}' 不存在。")
        return

    print(f"确认删除工具 '{tool_name}'？(yes/no)：")
    try:
        confirm = input("> ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return

    if confirm == "yes":
        ToolRegistry.delete_tool_file(tools_dir, tool_name)
        registry.unregister_tool(tool_name)
        print(f"工具 '{tool_name}' 已删除。")
    else:
        print("已取消删除操作。")


def _self_test_tool(tool_json: dict, executor):
    """工具自测：用样本参数执行工具，验证工具是否能正常工作

    覆盖所有三种执行模式：
    - local_execution：本地执行样本参数，检查是否有异常或明显错误
    - llm_simulated：LLM 模拟执行样本参数，检查返回是否合理
    - http_request：实际发起 HTTP 请求，检查是否能成功获取并解析数据

    Args:
        tool_json: 工具 JSON 定义
        executor: 工具执行函数
    """
    tool_name = tool_json.get("name", "未知")
    execution_mode = tool_json.get("execution_mode", "")
    properties = tool_json.get("parameters", {}).get("properties", {})

    if not properties:
        print(f"[自测] 工具 '{tool_name}' 无参数定义，跳过自测。")
        return

    sample_params = {}
    for param_name, param_def in properties.items():
        param_type = param_def.get("type", "string")
        enum_values = param_def.get("enum")
        if enum_values and len(enum_values) > 0:
            sample_params[param_name] = enum_values[0]
        elif param_type in ("integer", "number"):
            sample_params[param_name] = 1
        elif param_type == "boolean":
            sample_params[param_name] = True
        elif param_type == "string":
            sample_params[param_name] = "test"
        else:
            sample_params[param_name] = "test"

    if not sample_params:
        print(f"[自测] 工具 '{tool_name}' 无法构造样本参数，跳过自测。")
        return

    print(f"[自测] 正在测试工具 '{tool_name}'（模式：{execution_mode}）...")
    print(f"[自测] 样本参数：{sample_params}")

    try:
        result = executor(**sample_params)
    except Exception as e:
        print(f"[自测] 失败：工具执行抛出异常 → {type(e).__name__}: {e}")
        return

    if not result or not isinstance(result, str):
        print(f"[自测] 失败：工具返回了空结果或非字符串类型。")
        return

    result_stripped = result.strip()
    if not result_stripped:
        print(f"[自测] 失败：工具返回了空字符串。")
        return

    failure_keywords = [
        "模拟", "请提供", "无法生成", "执行失败",
        "NameError", "SyntaxError", "TypeError", "ValueError",
        "ZeroDivisionError", "AttributeError", "KeyError", "IndexError",
        "ModuleNotFoundError", "ImportError", "IndentationError",
        "HTTP请求失败", "模拟执行失败", "本地执行失败",
        "Error:", "Traceback",
    ]
    has_issue = any(kw in result for kw in failure_keywords)

    if has_issue and "ModuleNotFoundError" in result:
        import re as _re
        match = _re.search(r"No module named '(\w+)'", result)
        if match:
            missing_module = match.group(1)
            print(f"[自测] 检测到缺失依赖 '{missing_module}'，正在自动安装...")
            try:
                from sandbox import ToolSandbox
                _sb = ToolSandbox()
                _sb.install([missing_module])
                print(f"[自测] 依赖 '{missing_module}' 安装完成，正在重试自测...")
                result = executor(**sample_params)
                result_stripped = result.strip()
                has_issue = any(kw in result for kw in failure_keywords)
            except Exception as install_err:
                print(f"[自测] 自动安装依赖失败：{install_err}")

    if has_issue:
        preview = result[:200].replace('\n', ' ')
        print(f"[自测] 警告：返回结果可能包含错误 → {preview}...")
        return

    preview = result[:150].replace('\n', ' ')
    print(f"[自测] 通过：工具正常返回 → {preview}{'...' if len(result) > 150 else ''}")


def _handle_tool_list(tools_dir: str):
    """处理 /tool list 命令：列出 agent_tools/ 中所有 Tool

    Args:
        tools_dir: Tool 存储目录路径
    """
    available = ToolRegistry.list_available_tools(tools_dir)
    if not available:
        print("当前 agent_tools/ 中没有工具文件。")
        print("使用 /tool add 通过自然语言创建一个吧。")
        return

    print(f"agent_tools/ 中共有 {len(available)} 个工具：")
    for name, desc in available:
        print(f"  [{name}] {desc}")


def _parse_model_command(user_input: str) -> tuple:
    """解析 /model 命令

    支持格式：
        /model set
        /model show

    Args:
        user_input: 用户输入的原始字符串

    Returns:
        tuple: (action: str or None, None)
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

    current_rounds = config.get('max_history_rounds')
    if current_rounds is not None:
        rounds_hint = f" [当前: {current_rounds}轮，0=不限制]"
    else:
        rounds_hint = " [0=不限制，默认使用模型最大上下文]"
    rounds_prompt = f"上下文轮数{rounds_hint}: "

    try:
        rounds_input = input(rounds_prompt).strip()
    except (EOFError, KeyboardInterrupt):
        return
    if rounds_input.lower() == "exit":
        print("已取消配置。")
        return

    if rounds_input == "":
        max_history_rounds = 0
    else:
        try:
            max_history_rounds = int(rounds_input)
            if max_history_rounds < 0:
                print("上下文轮数不能为负数，已取消配置。")
                return
        except ValueError:
            print("请输入有效的数字，已取消配置。")
            return

    config.set_model(api_key, base_url, model_name)
    config.set('max_history_rounds', max_history_rounds)
    llm_client.refresh()
    print(f"\n配置已保存并生效！")
    print(f"  Model: {model_name}")
    print(f"  Base URL: {base_url}")
    print(f"  API Key: {config.get_masked_api_key()}")
    if max_history_rounds == 0:
        print(f"  上下文轮数: 不限制（使用模型最大上下文）")
    else:
        print(f"  上下文轮数: {max_history_rounds}")


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
    rounds = info['max_history_rounds']
    if rounds == 0:
        print(f"  上下文轮数: 不限制（使用模型最大上下文）")
    else:
        print(f"  上下文轮数: {rounds}")


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
    rounds_display = "不限制" if info['max_history_rounds'] == 0 else str(info['max_history_rounds'])
    print(f"  4) 上下文轮数: {rounds_display}")
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
            new_value = input("新的上下文轮数（0=不限制）: ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if new_value.lower() == "exit":
            print("已取消更新。")
            return
        if new_value == "":
            print("输入不能为空，已取消更新。")
            return
        try:
            rounds = int(new_value)
            if rounds < 0:
                print("上下文轮数不能为负数，已取消更新。")
                return
        except ValueError:
            print("请输入有效的数字，已取消更新。")
            return
        config.set('max_history_rounds', rounds)
        if rounds == 0:
            print("上下文轮数已更新为：不限制（使用模型最大上下文）")
        else:
            print(f"上下文轮数已更新为：{rounds}")

    else:
        print("无效的序号，已取消更新。")
        return

    llm_client.refresh()
    print("配置已更新并生效。")


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

  /tool list               查看 agent_tools/ 中所有工具
  /tool add                通过自然语言新增工具
  /tool update <工具名>     通过自然语言修改已有工具
  /tool delete <工具名>     删除指定工具

  /model set               配置模型参数（API Key 加密存储）
  /model show              查看当前模型配置
  /model update            修改单个配置项

  /agent thought on|off    开启/关闭 Agent 思考过程显示

  直接输入自然语言即可与 Agent 对话。""")


def main():
    """主函数：初始化组件并启动命令行交互"""
    base_dir = os.path.dirname(os.path.abspath(__file__))

    config = AgentConfig()

    tool_registry = ToolRegistry()

    max_history_rounds = config.get('max_history_rounds')
    if max_history_rounds is None:
        max_history_rounds = 10
    llm_client = LLMClient(config=config)

    tools_dir = config.get('tools_dir') or os.path.join(base_dir, "agent_tools")
    tool_names = None

    tool_registry.load_tools_from_dir(
        tools_dir=tools_dir,
        tool_names=tool_names,
        func_factory=lambda name, prompt, mode, code, http_cfg, deps=None: _create_executor(name, prompt, mode, code, http_cfg, llm_client, deps)
    )

    agent = SimpleAgent(llm_client, tool_registry, max_history_rounds=max_history_rounds,
                        show_thought=config.get('show_thought', False))
    builder = ToolBuilder(llm_client)

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

        action, tool_name = _parse_tool_command(user_input)
        if action == "list":
            _handle_tool_list(tools_dir)
            print("-" * 50)
            continue
        elif action == "add":
            _handle_tool_add(builder, tool_registry, llm_client, tools_dir, agent, config)
            print("-" * 50)
            continue
        elif action == "update":
            _handle_tool_update(builder, tool_registry, llm_client, tools_dir, tool_name, agent, config)
            print("-" * 50)
            continue
        elif action == "delete":
            _handle_tool_delete(tool_registry, tools_dir, tool_name)
            print("-" * 50)
            continue
        elif action == "invalid":
            print("无效的 /tool 命令。可用子命令：list、add、update、delete")
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
        print(f"Agent: {response}")
        print("-" * 50)


if __name__ == "__main__":
    main()
