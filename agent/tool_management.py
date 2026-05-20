import os
import io
import json
from datetime import datetime

# 待确认的删除操作：{session_id: tool_name}
_pending_deletions = {}


META_TOOL_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "create_tool",
            "description": (
                "创建新的工具。\n\n"
                "【必须遵守】用户提出创建工具需求后，直接调用本函数，不要先追问！\n"
                "把用户的原始需求完整传入 description 参数，系统会自动处理数字类型、参数名、返回格式等细节。\n"
                "【例外】仅当用户需求极度模糊（如只说'帮我做个工具'）时才追问，最多1个问题。\n"
                "【权限】仅管理员可用。\n"
                "【中断】用户说'暂停/算了/取消'则停止，不要调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "description": {
                        "type": "string",
                        "description": "完整的工具需求描述，包含：工具用途、API地址（如有）、参数定义、返回数据格式、执行方式偏好等所有技术细节"
                    }
                },
                "required": ["description"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "update_tool",
            "description": (
                "更新已有工具。\n\n"
                "【用法】信息足够时直接调用此函数，传入工具名和修改描述。\n"
                "不要在调用前追问用户，把用户的原始要求作为 update_description 传入即可。\n"
                "【权限】仅管理员可用，非管理员调用会返回权限错误。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tool_name": {"type": "string", "description": "要更新的工具名称"},
                    "update_description": {"type": "string", "description": "具体的修改需求描述"}
                },
                "required": ["tool_name", "update_description"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delete_tool",
            "description": (
                "删除指定工具。\n\n"
                "【流程】\n"
                "调用本函数，传入工具名，函数返回删除确认提示。\n"
                "将提示中的确认问题展示给用户即可。\n"
                "用户回复确认后，系统将自动执行删除，无需你再次调用。\n\n"
                "【权限】仅管理员可用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tool_name": {"type": "string", "description": "要删除的工具名称"}
                },
                "required": ["tool_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_tools",
            "description": "查看所有已安装的工具列表及其详细信息。全员可用，无权限限制。",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "set_tool_secret",
            "description": (
                "设置工具密钥（API Key、Token等敏感信息）。\n"
                "当用户明确提供了某个API的密钥信息时调用此函数加密存储。\n"
                "例如'和风天气的API密钥是abc123'或'配置百度地图的key为xyz'。\n"
                "密钥将加密存储在本地，不会随代码开源泄露。\n"
                "【权限】仅管理员可用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "密钥名称，如'qweather_api_key'、'gold_price_api_key'"},
                    "value": {"type": "string", "description": "密钥明文值"}
                },
                "required": ["key", "value"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delete_tool_secret",
            "description": (
                "删除指定的工具密钥。\n"
                "【权限】仅管理员可用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "要删除的密钥名称"}
                },
                "required": ["key"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_tool_secrets",
            "description": (
                "查看所有已配置的工具密钥列表（值已脱敏，仅显示前后几位）。\n"
                "【权限】仅管理员可用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_tool_secret",
            "description": (
                "查看指定工具密钥的脱敏信息（仅显示前后几位）。\n"
                "【权限】仅管理员可用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "要查看的密钥名称"}
                },
                "required": ["key"]
            }
        }
    },
]


def _run_self_test(tool_json: dict, executor) -> dict:
    """运行工具自测并返回结构化结果

    Args:
        tool_json: 工具 JSON 定义
        executor: 工具执行函数

    Returns:
        dict: {"passed": bool, "message": str}
    """
    import sys as _sys

    capture = io.StringIO()
    original_stdout = _sys.stdout
    _sys.stdout = capture
    try:
        from agent.main import _self_test_tool
        _self_test_tool(tool_json, executor)
    except Exception as e:
        _sys.stdout = original_stdout
        return {"passed": False, "message": f"自测异常: {str(e)}"}
    finally:
        _sys.stdout = original_stdout

    output = capture.getvalue().strip()
    if output:
        lines = output.split("\n")
        for line in lines:
            if "通过" in line:
                return {"passed": True, "message": output}
            if "失败" in line or "警告" in line:
                return {"passed": False, "message": output}
        return {"passed": True, "message": output}
    return {"passed": True, "message": "自测完成（无输出）"}


def _log(log_func, operation: str, tool_name: str, user_ctx: dict, details: dict):
    """记录工具操作日志

    Args:
        log_func: log_tool_operation 函数
        operation: 操作类型
        tool_name: 工具名称
        user_ctx: 用户上下文字典
        details: 详细信息字典
    """
    if log_func is None:
        return
    try:
        log_func(
            tool_name=tool_name,
            operation=operation,
            operator=user_ctx.get("username", ""),
            operator_id=user_ctx.get("user_id", 0),
            details=json.dumps(details, ensure_ascii=False)
        )
    except Exception:
        pass


_MODE_MAP = {
    "local_execution": "本地执行",
    "http_request": "HTTP请求",
    "llm_simulated": "LLM模拟",
}


def create_tool_from_chat(
    description: str,
    builder,
    llm,
    registry,
    tools_dir: str,
    sandbox,
    agent,
    user_ctx: dict,
    log_func=None,
) -> dict:
    """从自然语言描述创建工具（完整流程）

    Args:
        description: 用户需求描述（经LLM对话补全后的完整描述）
        builder: ToolBuilder 实例
        llm: LLMClient 实例
        registry: ToolRegistry 实例
        tools_dir: 工具存储目录
        sandbox: ToolSandbox 实例
        agent: SimpleAgent 实例
        user_ctx: {"user_id", "username", "user_type"}
        log_func: log_tool_operation 函数

    Returns:
        dict: 结构化结果，供LLM解读后回复用户
    """
    if user_ctx.get("user_type") != "admin":
        _log(log_func, "create_denied", "", user_ctx, {"reason": "非管理员尝试创建", "description": description})
        return {
            "success": False,
            "permission_denied": True,
            "message": "该操作需要管理员权限。工具的新增、更新、删除仅管理员可执行。",
        }

    existing = [(name, info.get("description", ""))
                for name, info in registry.list_tools().items()]

    from agent.main import _check_duplicate_tool
    dup_check = _check_duplicate_tool(description, existing, llm)
    dup_warning = ""
    if dup_check.get("is_duplicate"):
        _log(log_func, "create_duplicate", dup_check.get("matched_tool", ""), user_ctx,
             {"description": description, "reason": dup_check.get("reason", "")})
        dup_warning = (
            f"\n\n[相似工具提醒] {dup_check.get('reason', '')}"
            f"已有工具 **{dup_check.get('matched_tool')}** 可能与此功能重叠，请注意避免重复。"
        )

    try:
        smart_result = builder.smart_generate(description)
    except Exception as e:
        return {"success": False, "message": f"工具生成失败: {str(e)}"}

    if not smart_result.get("success"):
        need_info = smart_result.get("need_info", False)
        questions = smart_result.get("questions", [])
        reason = smart_result.get("reason", "")

        if need_info and questions:
            lines = ["需要补充以下信息："]
            if reason:
                lines.append(reason)
            for i, q in enumerate(questions, 1):
                lines.append(f"  {i}. {q}")
            lines.append("\n请补充后重新创建。")
            return {"success": False, "need_info": True, "questions": questions, "message": "\n".join(lines)}
        return {"success": False, "message": reason or "工具生成未成功，请提供更详细的描述"}

    tool_json = smart_result["tool"]

    valid, msg = builder.validate_tool_json(tool_json)
    if not valid:
        try:
            tool_json = builder.repair_tool(tool_json, f"校验失败原因：{msg}")
            tool_json["name"] = tool_json.get("name", "")
            valid, msg = builder.validate_tool_json(tool_json)
        except Exception:
            pass
        if not valid:
            return {"success": False, "message": f"工具定义校验失败: {msg}"}

    try:
        builder.save_tool_to_file(tool_json, tools_dir=tools_dir, registry=registry)
    except Exception as e:
        return {"success": False, "message": f"保存失败: {str(e)}"}

    from agent.main import _ensure_output_dir, _create_executor
    _ensure_output_dir(tool_json)

    executor = _create_executor(
        tool_json["name"], tool_json["execution_prompt"],
        tool_json["execution_mode"],
        tool_json.get("execution_code", ""),
        tool_json.get("http_config", {}),
        llm,
        tool_json.get("dependencies"),
        tool_json.get("response_formatter"),
        sandbox
    )
    registry.register_tool(
        name=tool_json["name"],
        description=tool_json["description"],
        parameters=tool_json["parameters"],
        func=executor
    )

    self_test_result = _run_self_test(tool_json, executor)

    _log(log_func, "create", tool_json["name"], user_ctx, {
        "description": tool_json.get("description", ""),
        "execution_mode": tool_json.get("execution_mode", ""),
        "dependencies": tool_json.get("dependencies", []),
        "self_test_passed": self_test_result.get("passed"),
    })

    agent.reset()

    mode_label = _MODE_MAP.get(tool_json.get("execution_mode", ""), "")
    status_icon = "通过" if self_test_result.get("passed") else "未通过"
    message = (
        f"工具 **{tool_json['name']}** 已创建成功！\n\n"
        f"- 描述：{tool_json['description']}\n"
        f"- 执行模式：{mode_label}\n"
        f"- 参数：{json.dumps(tool_json.get('parameters', {}).get('properties', {}), ensure_ascii=False)}\n"
        f"- 自测状态：{status_icon}"
        f"{dup_warning}\n\n"
        f"对话上下文已重置，新工具立即可用。"
    )

    return {
        "success": True,
        "tool_name": tool_json["name"],
        "self_test": self_test_result,
        "message": message,
    }


def update_tool_from_chat(
    tool_name: str,
    update_description: str,
    builder,
    llm,
    registry,
    tools_dir: str,
    sandbox,
    agent,
    user_ctx: dict,
    log_func=None,
) -> dict:
    """从自然语言描述更新工具

    Args:
        tool_name: 要更新的工具名称
        update_description: 用户对修改需求的描述
        builder: ToolBuilder 实例
        llm: LLMClient 实例
        registry: ToolRegistry 实例
        tools_dir: 工具存储目录
        sandbox: ToolSandbox 实例
        agent: SimpleAgent 实例
        user_ctx: 用户上下文字典
        log_func: log_tool_operation 函数

    Returns:
        dict: 结构化结果
    """
    if user_ctx.get("user_type") != "admin":
        _log(log_func, "update_denied", tool_name, user_ctx, {"reason": "非管理员尝试更新"})
        return {
            "success": False,
            "permission_denied": True,
            "message": "该操作需要管理员权限。工具的新增、更新、删除仅管理员可执行。",
        }

    filepath = os.path.join(tools_dir, f"{tool_name}.json")
    if not os.path.isfile(filepath):
        from agent.main import _check_duplicate_tool
        existing = [(name, info.get("description", ""))
                    for name, info in registry.list_tools().items()]
        dup_check = _check_duplicate_tool(
            f"工具名: {tool_name}，需求: {update_description}", existing, llm
        )
        if dup_check.get("is_duplicate"):
            return {
                "success": False,
                "message": (
                    f"工具 **{tool_name}** 不存在。但检测到类似工具 **{dup_check.get('matched_tool')}**，"
                    f"是否想更新这个工具？\n\n如果没有，请先使用 create_tool 创建。"
                ),
            }
        return {
            "success": False,
            "message": f"工具 **{tool_name}** 不存在，无法更新。请先使用 create_tool 创建。",
        }

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            original_tool = json.load(f)
    except Exception:
        original_tool = {}

    try:
        tool_json = builder.repair_tool(original_tool, update_description)
    except Exception as e:
        return {"success": False, "message": f"修复失败: {str(e)}"}

    tool_json["name"] = tool_name
    valid, msg = builder.validate_tool_json(tool_json)
    if not valid:
        return {"success": False, "message": f"修复后校验失败: {msg}"}

    try:
        builder.save_tool_to_file(tool_json, tools_dir=tools_dir, registry=registry)
    except Exception as e:
        return {"success": False, "message": f"保存失败: {str(e)}"}

    registry.unregister_tool(tool_name)

    from agent.main import _ensure_output_dir, _create_executor
    _ensure_output_dir(tool_json)

    executor = _create_executor(
        tool_name, tool_json["execution_prompt"],
        tool_json["execution_mode"],
        tool_json.get("execution_code", ""),
        tool_json.get("http_config", {}),
        llm,
        tool_json.get("dependencies"),
        sandbox=sandbox
    )
    registry.register_tool(
        name=tool_name,
        description=tool_json["description"],
        parameters=tool_json["parameters"],
        func=executor
    )

    self_test_result = _run_self_test(tool_json, executor)

    _log(log_func, "update", tool_name, user_ctx, {
        "repair_description": update_description,
        "execution_mode": tool_json.get("execution_mode", ""),
        "self_test_passed": self_test_result.get("passed"),
    })

    agent.reset()

    status_icon = "通过" if self_test_result.get("passed") else "未通过"
    message = (
        f"工具 **{tool_name}** 已更新成功！\n\n"
        f"- 描述：{tool_json['description']}\n"
        f"- 参数：{json.dumps(tool_json.get('parameters', {}).get('properties', {}), ensure_ascii=False)}\n"
        f"- 自测状态：{status_icon}\n\n"
        f"对话上下文已重置，更新后的工具立即可用。"
    )

    return {
        "success": True,
        "tool_name": tool_name,
        "self_test": self_test_result,
        "message": message,
    }


def delete_tool_from_chat(
    tool_name: str,
    registry=None,
    tools_dir: str = "",
    agent=None,
    user_ctx: dict = None,
    log_func=None,
) -> dict:
    """删除工具（含二次确认机制）

    首次调用时返回确认提示，同一 session 再次调用同一工具名时执行删除。

    Args:
        tool_name: 要删除的工具名称
        registry: ToolRegistry 实例
        tools_dir: 工具存储目录
        agent: SimpleAgent 实例
        user_ctx: 用户上下文字典，需包含 session_id
        log_func: log_tool_operation 函数

    Returns:
        dict: 结构化结果
    """
    if user_ctx is None:
        user_ctx = {}

    if user_ctx.get("user_type") != "admin":
        _log(log_func, "delete_denied", tool_name, user_ctx, {"reason": "非管理员尝试删除"})
        return {
            "success": False,
            "permission_denied": True,
            "message": "该操作需要管理员权限。工具的新增、更新、删除仅管理员可执行。",
        }

    filepath = os.path.join(tools_dir, f"{tool_name}.json")
    if not os.path.isfile(filepath):
        return {"success": False, "message": f"工具 **{tool_name}** 不存在。"}

    user_id = user_ctx.get("user_id", 0)

    pending = _pending_deletions.get(user_id)
    if pending and pending == tool_name:
        _pending_deletions.pop(user_id, None)

        tool_info = {}
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                tool_info = json.load(f)
        except Exception:
            pass

        os.remove(filepath)
        registry.unregister_tool(tool_name)
        registry.remove_from_manifest(tool_name)

        _log(log_func, "delete", tool_name, user_ctx, {
            "description": tool_info.get("description", ""),
            "execution_mode": tool_info.get("execution_mode", ""),
        })

        return {
            "success": True,
            "tool_name": tool_name,
            "message": f"工具 **{tool_name}** 已删除。",
        }

    # 首次调用，记录待确认
    _pending_deletions[user_id] = tool_name
    return {
        "success": False,
        "needs_confirmation": True,
        "tool_name": tool_name,
        "message": f"确认删除工具 **{tool_name}** ？此操作不可撤销。请回复'确认'/'确定'/'好的'/'是'来确认删除。",
    }


def list_all_tools(registry) -> dict:
    """列出所有已注册的工具

    Args:
        registry: ToolRegistry 实例

    Returns:
        dict: 结构化工具列表
    """
    tools = registry.list_tools()
    if not tools:
        return {
            "success": True,
            "count": 0,
            "message": "当前没有已安装的工具。",
            "tools": [],
        }

    result = []
    for name, info in tools.items():
        item = {
            "name": name,
            "description": info.get("description", ""),
            "parameters": info.get("parameters", {}),
        }
        result.append(item)

    lines = [f"已安装 {len(result)} 个工具："]
    for item in result:
        prop_keys = list(item["parameters"].get("properties", {}).keys())
        params_str = "、".join(prop_keys) if prop_keys else "无参数"
        lines.append(f"- **{item['name']}**：{item['description']}（参数：{params_str}）")

    return {
        "success": True,
        "count": len(result),
        "tools": result,
        "message": "\n".join(lines),
    }


def set_tool_secret_from_chat(key: str, value: str, user_ctx: dict, log_func=None) -> dict:
    """从对话中设置工具密钥

    Args:
        key: 密钥名称
        value: 密钥明文值
        user_ctx: {"user_id", "username", "user_type"}
        log_func: log_tool_operation 函数

    Returns:
        dict: 结构化结果
    """
    if user_ctx.get("user_type") != "admin":
        _log(log_func, "secret_set_denied", key, user_ctx, {"reason": "非管理员尝试设置密钥"})
        return {
            "success": False,
            "permission_denied": True,
            "message": "该操作需要管理员权限。密钥的增删改查仅管理员可执行。",
        }

    from agent.tool_secrets import get_tool_secrets
    secrets = get_tool_secrets()
    try:
        secrets.set(key, value)
        masked = secrets.get_masked(key)
        _log(log_func, "secret_set", key, user_ctx, {"masked": masked})
        return {
            "success": True,
            "key": key,
            "masked": masked,
            "message": f"密钥 **{key}** 已设置成功（值：{masked}）。工具中引用 `{{secret:{key}}}` 即可自动注入。",
        }
    except Exception as e:
        return {"success": False, "message": f"密钥设置失败: {str(e)}"}


def delete_tool_secret_from_chat(key: str, user_ctx: dict, log_func=None) -> dict:
    """从对话中删除工具密钥

    Args:
        key: 密钥名称
        user_ctx: {"user_id", "username", "user_type"}
        log_func: log_tool_operation 函数

    Returns:
        dict: 结构化结果
    """
    if user_ctx.get("user_type") != "admin":
        _log(log_func, "secret_delete_denied", key, user_ctx, {"reason": "非管理员尝试删除密钥"})
        return {
            "success": False,
            "permission_denied": True,
            "message": "该操作需要管理员权限。密钥的增删改查仅管理员可执行。",
        }

    from agent.tool_secrets import get_tool_secrets
    secrets = get_tool_secrets()
    if not secrets.has(key):
        return {
            "success": False,
            "message": f"密钥 **{key}** 不存在，无需删除。",
        }
    try:
        secrets.delete(key)
        _log(log_func, "secret_delete", key, user_ctx, {})
        return {
            "success": True,
            "key": key,
            "message": f"密钥 **{key}** 已删除。",
        }
    except Exception as e:
        return {"success": False, "message": f"密钥删除失败: {str(e)}"}


def list_tool_secrets_from_chat(user_ctx: dict) -> dict:
    """从对话中列出所有工具密钥

    Args:
        user_ctx: {"user_id", "username", "user_type"}

    Returns:
        dict: 结构化结果
    """
    if user_ctx.get("user_type") != "admin":
        return {
            "success": False,
            "permission_denied": True,
            "message": "该操作需要管理员权限。密钥的增删改查仅管理员可执行。",
        }

    from agent.tool_secrets import get_tool_secrets
    secrets = get_tool_secrets()
    keys = secrets.list_keys()
    if not keys:
        return {
            "success": True,
            "count": 0,
            "message": "当前没有已配置的工具密钥。\n\n配置方式：直接告诉我要设置的密钥，如'设置和风天气的API密钥为abc123'。",
            "keys": [],
        }

    lines = [f"已配置 {len(keys)} 个密钥："]
    for k in keys:
        masked = secrets.get_masked(k)
        lines.append(f"- **{k}**：{masked}")

    lines.append("\n工具中引用 `{secret:密钥名}` 即可自动注入。")
    return {
        "success": True,
        "count": len(keys),
        "keys": [{"name": k, "masked": secrets.get_masked(k)} for k in keys],
        "message": "\n".join(lines),
    }


def get_tool_secret_from_chat(key: str, user_ctx: dict) -> dict:
    """从对话中查看指定工具密钥的脱敏信息"""
    if user_ctx.get("user_type") != "admin":
        return {
            "success": False,
            "permission_denied": True,
            "message": "该操作需要管理员权限。密钥的增删改查仅管理员可执行。",
        }

    from agent.tool_secrets import get_tool_secrets
    secrets = get_tool_secrets()
    if not secrets.has(key):
        return {
            "success": False,
            "message": f"密钥 **{key}** 尚未配置。\n\n如需设置，请直接告诉我密钥值，如'设置{key}为abc123'。",
        }
    masked = secrets.get_masked(key)
    return {
        "success": True,
        "key": key,
        "masked": masked,
        "message": f"密钥 **{key}**：{masked}",
    }