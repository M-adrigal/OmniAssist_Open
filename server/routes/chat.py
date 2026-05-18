import json
import asyncio
import re
from datetime import datetime
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from server.models import ChatRequest
from agent.model_gateway import ModelGateway

router = APIRouter(prefix="/api/chat", tags=["chat"])


from server.routes.auth import get_current_user


def get_dependencies():
    try:
        from __main__ import get_agent, get_llm_client, get_tool_registry, get_tool_builder, get_config
    except ImportError:
        from server.main import get_agent, get_llm_client, get_tool_registry, get_tool_builder, get_config
    return get_agent(), get_llm_client(), get_tool_registry(), get_tool_builder(), get_config()


def get_session_store():
    try:
        from __main__ import get_session_store as gss
    except ImportError:
        from server.main import get_session_store as gss
    return gss()


def _resolve_llm_client(user_id: int):
    from server.database import resolve_model_config
    from agent.llm import LLMClient
    cfg = resolve_model_config(user_id)
    if not cfg.get("api_key"):
        return None, cfg
    llm = LLMClient(
        api_key=cfg["api_key"],
        base_url=cfg.get("base_url", ""),
        model=cfg.get("model_name", ""),
    )
    return llm, cfg


def _load_session_messages(session_id: str) -> list:
    from server.database import get_session as db_get_session
    s = db_get_session(session_id)
    if s and s.get("messages"):
        return s["messages"]
    return []


def _save_session_messages(session_id: str, messages: list, title: str = None):
    from server.database import update_session_messages
    update_session_messages(session_id, messages, title)


def _generate_title(user_message: str, assistant_response: str, llm_client) -> str:
    prompt = (
        "请根据以下对话内容，生成一个简短的标题（不超过15个字），"
        "直接返回标题文本，不要加引号或其他修饰：\n\n"
        f"用户: {user_message[:200]}\n"
        f"助手: {assistant_response[:200]}"
    )
    try:
        resp = llm_client.chat([{"role": "user", "content": prompt}], tools=None)
        title = resp.get("content", "").strip().strip('"').strip("'").strip("。").strip("，")
        if not title:
            return user_message[:30] + ("..." if len(user_message) > 30 else "")
        return title[:30]
    except Exception:
        return user_message[:30] + ("..." if len(user_message) > 30 else "")


def _compress_if_needed(messages: list, llm_client, config) -> list:
    from agent.agent import SimpleAgent
    context_limit_str = config.get("context_limit", "")
    context_limit_tokens = SimpleAgent._parse_context_limit(context_limit_str)
    if context_limit_tokens == 0:
        return messages
    return SimpleAgent.compress_messages(messages, llm_client, context_limit_tokens)


SCENARIO_CONFIG = {
    "realtime": {
        "label": "实时信息",
        "search_depth": "advanced",
        "max_results": 3,
        "append_date": True,
        "instruction": (
            "用户正在查询实时信息（如天气、股价、新闻等），时效性至关重要。\n"
            "1. 请先使用 get_current_datetime 工具获取当前准确日期和时间。\n"
            "2. 严格基于搜索结果回答，并注明每条信息的来源和发布时间。\n"
            "3. 如果搜索结果中的日期与当前日期不一致，请明确指出并告知用户数据可能已过时。\n"
            "4. 优先采用发布时间最新的结果。"
        ),
    },
    "factual": {
        "label": "事实知识",
        "search_depth": "basic",
        "max_results": 3,
        "append_date": False,
        "instruction": (
            "用户正在查询事实性知识。\n"
            "1. 将搜索结果作为补充参考，可以结合你自己的知识综合回答。\n"
            "2. 如果搜索结果与你的知识一致，直接给出准确答案。\n"
            "3. 如果搜索结果与你的知识有冲突，优先采用搜索结果并注明来源。\n"
            "4. 回答应简洁准确，不需要过度展开。"
        ),
    },
    "latest": {
        "label": "最新动态",
        "search_depth": "advanced",
        "max_results": 5,
        "append_date": True,
        "instruction": (
            "用户正在查询最新动态、版本更新或近期发展。\n"
            "1. 请先使用 get_current_datetime 工具获取当前准确日期。\n"
            "2. 重点关注搜索结果中的时间信息，按时间倒序整理。\n"
            "3. 明确标注每条信息的发布时间或版本号。\n"
            "4. 区分'已发布'和'即将发布'的内容。\n"
            "5. 如果搜索结果不够新，请如实告知用户。"
        ),
    },
    "howto": {
        "label": "教程指南",
        "search_depth": "basic",
        "max_results": 3,
        "append_date": False,
        "instruction": (
            "用户正在寻求操作指南或教程。\n"
            "1. 基于搜索结果整理出清晰的操作步骤，按顺序编号。\n"
            "2. 每个步骤应具体可执行，必要时补充注意事项。\n"
            "3. 如果搜索结果中有多种方法，列出并说明各自的适用场景。\n"
            "4. 注明信息来源，方便用户深入了解。"
        ),
    },
    "comparison": {
        "label": "对比分析",
        "search_depth": "advanced",
        "max_results": 5,
        "append_date": False,
        "instruction": (
            "用户正在对比多个事物。\n"
            "1. 基于搜索结果，从多个维度（功能、性能、价格、适用场景等）进行系统对比。\n"
            "2. 使用对比表格或分点列出各自的优缺点。\n"
            "3. 给出综合建议，说明在什么情况下选择哪个。\n"
            "4. 注明信息来源，确保对比的公平客观。"
        ),
    },
    "local": {
        "label": "本地化信息",
        "search_depth": "basic",
        "max_results": 3,
        "append_date": True,
        "instruction": (
            "用户正在查询与特定地点相关的信息。\n"
            "1. 请先使用 get_current_datetime 工具获取当前准确日期。\n"
            "2. 确认搜索结果中的地点与用户查询的地点一致。\n"
            "3. 注意信息的时效性，标注发布时间。\n"
            "4. 如果涉及天气、交通等实时数据，优先采用最新结果。"
        ),
    },
    "general": {
        "label": "通用搜索",
        "search_depth": "basic",
        "max_results": 3,
        "append_date": False,
        "instruction": (
            "以下是通过联网搜索获取的信息。\n"
            "1. 请参考搜索结果回答用户问题。\n"
            "2. 如果搜索结果与问题无关或不充分，可以基于你自己的知识回答。\n"
            "3. 在回答中适当引用信息来源。"
        ),
    },
}


def _classify_query(query: str) -> str:
    """根据用户问题内容分类场景"""
    q = query.lower()

    realtime_keywords = [
        "天气", "气温", "温度", "下雨", "刮风", "雾霾", "空气质量",
        "股价", "股票", "汇率", "金价", "油价", "比特币", "eth", "btc",
        "新闻", "快讯", "最新消息", "突发", "刚刚",
        "今天", "现在", "当前", "实时", "此刻", "今日",
        "直播", "比分", "赛程",
    ]
    if any(kw in q for kw in realtime_keywords):
        return "realtime"

    latest_keywords = [
        "最新版", "最新版本", "更新", "发布", "上线", "推出",
        "latest", "new version", "recent", "最近",
        "新功能", "新特性", "changelog", "release",
        "趋势", "动态", "进展", "前沿",
    ]
    if any(kw in q for kw in latest_keywords):
        return "latest"

    howto_keywords = [
        "怎么", "如何", "怎样", "教程", "步骤", "方法", "指南",
        "how to", "how do", "tutorial", "guide",
        "操作", "配置", "安装", "部署", "搭建", "设置",
        "入门", "上手",
    ]
    if any(kw in q for kw in howto_keywords):
        return "howto"

    comparison_keywords = [
        "对比", "比较", "区别", "差异", "哪个好", "哪个更好",
        "vs", "versus", "compare", "difference",
        "优缺点", "优劣", "选哪个", "推荐哪个",
        "和", "与", "还是",
    ]
    if any(kw in q for kw in comparison_keywords):
        if any(kw in q for kw in ["哪个", "选", "推荐", "对比", "比较", "区别", "差异", "vs"]):
            return "comparison"

    local_keywords = [
        "附近", "周边", "本地", "当地", "这里",
        "北京", "上海", "广州", "深圳", "杭州", "成都", "武汉", "南京",
        "天气", "交通", "限行", "地铁", "公交",
    ]
    if any(kw in q for kw in local_keywords):
        return "local"

    factual_keywords = [
        "什么是", "是谁", "定义", "解释", "含义", "概念",
        "what is", "who is", "define", "explain",
        "百科", "简介", "介绍",
    ]
    if any(kw in q for kw in factual_keywords):
        return "factual"

    return "general"


def _do_web_search(query: str, api_key: str, scenario: str = "general") -> str:
    if not api_key:
        return ""

    cfg = SCENARIO_CONFIG.get(scenario, SCENARIO_CONFIG["general"])
    search_query = query

    if cfg["append_date"]:
        today_str = datetime.now().strftime("%Y年%m月%d日")
        search_query = f"{query} {today_str}"

    try:
        from tavily import Client
        client = Client(api_key=api_key)
        response = client.search(
            query=search_query,
            search_depth=cfg["search_depth"],
            max_results=cfg["max_results"],
        )

        if not response.get("results"):
            return ""

        parts = []
        answer = response.get("answer", "")
        if answer:
            parts.append(f"摘要: {answer}")

        for i, r in enumerate(response["results"], 1):
            title = r.get("title", "无标题")
            url = r.get("url", "")
            content = r.get("content", "无内容")
            if len(content) > 300:
                content = content[:300] + "..."
            parts.append(f"{i}. {title}\n   来源: {url}\n   {content}")

        return "\n\n".join(parts)
    except ImportError:
        return ""
    except Exception as e:
        return f"搜索出错: {str(e)}"


async def _handle_command(message: str, session_id: str, user_id: int):
    agent, llm, registry, builder, config = get_dependencies()
    store = get_session_store()

    if agent is None:
        yield f"data: {json.dumps({'type': 'error', 'content': '服务正在初始化中，请稍后再试'})}\n\n"
        yield f"data: {json.dumps({'type': 'done'})}\n\n"
        return

    msg = message.strip()

    def _check_perm(action: str):
        from server.database import get_user_by_id, check_permission
        user = get_user_by_id(user_id)
        if not user or not check_permission(user.get("user_type", "user"), "tools", action):
            return False
        return True

    if msg == "/help" or msg == "help":
        help_text = """**可用命令：**

| 命令 | 说明 |
|------|------|
| `/help` | 显示此帮助信息 |
| `/reset` | 重置当前对话上下文 |
| `/tool list` | 查看所有已安装的工具 |
| `/tool add` | 通过自然语言新增工具 |
| `/tool update <工具名>` | 修改已有工具 |
| `/tool delete <工具名>` | 删除指定工具 |
| `/model show` | 查看当前模型配置 |
| `/agent thought on` | 开启思考过程显示 |
| `/agent thought off` | 关闭思考过程显示 |

> 提示：`/tool add`、`/tool update`、`/tool delete`、`/model set`、`/model update` 等操作也可以在左侧设置面板中完成。"""
        yield f"data: {json.dumps({'type': 'token', 'content': help_text})}\n\n"
        yield f"data: {json.dumps({'type': 'done'})}\n\n"
        return

    if msg == "/reset" or msg == "reset":
        agent.reset()
        if session_id and session_id in store:
            store[session_id]["messages"] = []
            _save_session_messages(session_id, [])
        yield f"data: {json.dumps({'type': 'token', 'content': '对话上下文已重置，可以开始新的对话。'})}\n\n"
        yield f"data: {json.dumps({'type': 'done'})}\n\n"
        return

    if msg == "/tool list":
        tools = registry.list_tools()
        if not tools:
            text = "当前没有已安装的工具。\n\n使用 `/tool add` 命令或在设置面板中通过自然语言创建工具。"
        else:
            lines = [f"已安装 {len(tools)} 个工具："]
            for name, info in tools.items():
                mode = ""
                tool_data = _load_tool_json(name)
                if tool_data:
                    mode_map = {
                        "local_execution": "本地执行",
                        "http_request": "HTTP请求",
                        "llm_simulated": "LLM模拟",
                    }
                    mode = f" ({mode_map.get(tool_data.get('execution_mode', ''), '')})"
                lines.append(f"- **{name}**{mode}: {info.get('description', '')}")
            text = "\n".join(lines)
        yield f"data: {json.dumps({'type': 'token', 'content': text})}\n\n"
        yield f"data: {json.dumps({'type': 'done'})}\n\n"
        return

    if msg.startswith("/tool add"):
        if not _check_perm("write"):
            yield f"data: {json.dumps({'type': 'error', 'content': '权限不足：仅管理员可以创建工具'})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
            return
        parts = msg.split(maxsplit=2)
        description = parts[2].strip() if len(parts) > 2 else ""
        if not description:
            yield f"data: {json.dumps({'type': 'token', 'content': '请描述你需要创建的工具功能，例如：\n`/tool add 帮我创建一个可以查询任意城市实时天气的工具`'})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
            return

        yield f"data: {json.dumps({'type': 'status', 'content': '正在分析工具需求...'})}\n\n"

        existing = [(name, info.get("description", ""))
                    for name, info in registry.list_tools().items()]

        from agent.main import _check_duplicate_tool
        dup_check = _check_duplicate_tool(description, existing, llm)
        if dup_check.get("is_duplicate"):
            matched = dup_check.get("matched_tool", "未知")
            reason = dup_check.get("reason", "")
            yield f"data: {json.dumps({'type': 'token', 'content': f'[重复检测] {reason}\n\n检测到与已有工具 **{matched}** 功能相似。\n\n如需修改已有工具，请使用 `/tool update {matched} <修改描述>`。\n如需强制创建新工具，请在描述中明确说明与已有工具的区别。'})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
            return

        yield f"data: {json.dumps({'type': 'status', 'content': '正在智能生成工具...'})}\n\n"

        try:
            smart_result = builder.smart_generate(description)
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'content': f'工具生成失败: {str(e)}'})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
            return

        if not smart_result.get("success"):
            need_info = smart_result.get("need_info", False)
            questions = smart_result.get("questions", [])
            reason = smart_result.get("reason", "")
            if need_info and questions:
                lines = [f"需要补充以下信息：\n"]
                if reason:
                    lines.append(f"{reason}\n")
                for i, q in enumerate(questions, 1):
                    lines.append(f"  {i}. {q}")
                lines.append(f"\n请使用 `/tool add 原描述 + 补充信息` 重新创建，例如：")
                lines.append(f"`/tool add {description}。补充：第1点答案是xxx，第2点答案是xxx`")
                yield f"data: {json.dumps({'type': 'token', 'content': '\n'.join(lines)})}\n\n"
            else:
                yield f"data: {json.dumps({'type': 'token', 'content': f'工具生成未成功: {reason or "请提供更详细的描述"}'})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
            return

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
                yield f"data: {json.dumps({'type': 'error', 'content': f'工具定义校验失败: {msg}'})}\n\n"
                yield f"data: {json.dumps({'type': 'done'})}\n\n"
                return

        import os as _os
        base_dir = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
        tools_dir = _os.path.join(base_dir, "agent", "agent_tools")

        try:
            builder.save_tool_to_file(tool_json, tools_dir=tools_dir, registry=registry)
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'content': f'保存失败: {str(e)}'})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
            return

        from agent.main import _ensure_output_dir, _create_executor
        _ensure_output_dir(tool_json)

        executor = _create_executor(
            tool_json["name"], tool_json["execution_prompt"],
            tool_json["execution_mode"],
            tool_json.get("execution_code", ""),
            tool_json.get("http_config", {}),
            llm,
            tool_json.get("dependencies")
        )
        registry.register_tool(
            name=tool_json["name"],
            description=tool_json["description"],
            parameters=tool_json["parameters"],
            func=executor
        )

        agent.reset()

        mode_map = {
            "local_execution": "本地执行",
            "http_request": "HTTP请求",
            "llm_simulated": "LLM模拟",
        }
        mode_label = mode_map.get(tool_json.get("execution_mode", ""), "")
        yield f"data: {json.dumps({'type': 'token', 'content': f'工具 **{tool_json["name"]}** 已创建成功！\n\n- 描述：{tool_json["description"]}\n- 执行模式：{mode_label}\n- 参数：{json.dumps(tool_json.get("parameters", {}), ensure_ascii=False)}\n\n对话上下文已重置，新工具立即可用。'})}\n\n"
        yield f"data: {json.dumps({'type': 'done'})}\n\n"
        return

    if msg.startswith("/tool update"):
        if not _check_perm("write"):
            yield f"data: {json.dumps({'type': 'error', 'content': '权限不足：仅管理员可以修改工具'})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
            return
        parts = msg.split(maxsplit=3)
        tool_name = parts[2].strip() if len(parts) > 2 else ""
        update_desc = parts[3].strip() if len(parts) > 3 else ""
        if not tool_name:
            yield f"data: {json.dumps({'type': 'token', 'content': '用法：`/tool update <工具名> <修改描述>`\n\n例如：`/tool update weather 增加湿度参数`'})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
            return
        if not update_desc:
            yield f"data: {json.dumps({'type': 'token', 'content': f'请描述对工具 **{tool_name}** 的修改内容，例如：\n`/tool update {tool_name} 增加湿度参数`'})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
            return

        import os as _os
        base_dir = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
        tools_dir = _os.path.join(base_dir, "agent", "agent_tools")
        filepath = _os.path.join(tools_dir, f"{tool_name}.json")
        if not _os.path.isfile(filepath):
            yield f"data: {json.dumps({'type': 'error', 'content': f'工具 **{tool_name}** 不存在，请先使用 `/tool add` 创建。'})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
            return

        yield f"data: {json.dumps({'type': 'status', 'content': '正在分析修改需求...'})}\n\n"

        try:
            with open(filepath, "r", encoding="utf-8") as f:
                original_tool = json.load(f)
        except Exception:
            original_tool = {}

        try:
            tool_json = builder.repair_tool(original_tool, update_desc)
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'content': f'修复失败: {str(e)}'})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
            return

        tool_json["name"] = tool_name
        valid, msg = builder.validate_tool_json(tool_json)
        if not valid:
            yield f"data: {json.dumps({'type': 'error', 'content': f'修复后校验失败: {msg}'})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
            return

        try:
            builder.save_tool_to_file(tool_json, tools_dir=tools_dir, registry=registry)
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'content': f'保存失败: {str(e)}'})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
            return

        registry.unregister_tool(tool_name)

        from agent.main import _ensure_output_dir, _create_executor
        _ensure_output_dir(tool_json)

        executor = _create_executor(
            tool_name, tool_json["execution_prompt"],
            tool_json["execution_mode"],
            tool_json.get("execution_code", ""),
            tool_json.get("http_config", {}),
            llm,
            tool_json.get("dependencies")
        )
        registry.register_tool(
            name=tool_name,
            description=tool_json["description"],
            parameters=tool_json["parameters"],
            func=executor
        )

        agent.reset()

        yield f"data: {json.dumps({'type': 'token', 'content': f'工具 **{tool_name}** 已更新成功！\n\n- 描述：{tool_json["description"]}\n- 参数：{json.dumps(tool_json.get("parameters", {}), ensure_ascii=False)}\n\n对话上下文已重置，更新后的工具立即可用。'})}\n\n"
        yield f"data: {json.dumps({'type': 'done'})}\n\n"
        return

    if msg.startswith("/tool delete"):
        if not _check_perm("delete"):
            yield f"data: {json.dumps({'type': 'error', 'content': '权限不足：仅管理员可以删除工具'})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
            return
        parts = msg.split(maxsplit=2)
        tool_name = parts[2].strip() if len(parts) > 2 else ""
        if not tool_name:
            yield f"data: {json.dumps({'type': 'token', 'content': '用法：`/tool delete <工具名>`\n\n例如：`/tool delete weather`'})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
            return

        import os as _os
        base_dir = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
        tools_dir = _os.path.join(base_dir, "agent", "agent_tools")
        filepath = _os.path.join(tools_dir, f"{tool_name}.json")
        if not _os.path.isfile(filepath):
            yield f"data: {json.dumps({'type': 'error', 'content': f'工具 **{tool_name}** 不存在。'})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
            return

        _os.remove(filepath)
        registry.unregister_tool(tool_name)
        registry.remove_from_manifest(tool_name)

        yield f"data: {json.dumps({'type': 'token', 'content': f'工具 **{tool_name}** 已删除。'})}\n\n"
        yield f"data: {json.dumps({'type': 'done'})}\n\n"
        return

    if msg == "/model show":
        from server.database import resolve_model_config
        cfg = resolve_model_config(user_id)
        text = (
            f"**当前模型配置：**\n\n"
            f"- 模型名称：`{cfg.get('model_name', '(未设置)')}`\n"
            f"- Base URL：`{cfg.get('base_url', '(未设置)')}`\n"
            f"- API Key：{cfg.get('api_key_masked', '(未设置)')}\n"
            f"- 上下文限制：{cfg.get('context_limit') or '使用模型最大上下文'}\n"
            f"- 配置类型：{cfg.get('config_type', 'none')}"
        )
        yield f"data: {json.dumps({'type': 'token', 'content': text})}\n\n"
        yield f"data: {json.dumps({'type': 'done'})}\n\n"
        return

    if msg.startswith("/model set") or msg.startswith("/model update"):
        yield f"data: {json.dumps({'type': 'token', 'content': '请在左侧设置面板 → **模型配置** 中配置模型参数。'})}\n\n"
        yield f"data: {json.dumps({'type': 'done'})}\n\n"
        return

    if msg.startswith("/agent thought"):
        parts = msg.split(maxsplit=2)
        arg = parts[2].strip().lower() if len(parts) > 2 else ""
        if arg in ("on", "off"):
            enabled = (arg == "on")
            agent.set_show_thought(enabled)
            try:
                from server.database import save_model_config
                save_model_config(user_id, show_thought=enabled)
            except Exception:
                pass
            status = "开启" if enabled else "关闭"
            yield f"data: {json.dumps({'type': 'token', 'content': f'思考过程显示已{status}。'})}\n\n"
        else:
            yield f"data: {json.dumps({'type': 'token', 'content': '用法：`/agent thought on` 或 `/agent thought off`'})}\n\n"
        yield f"data: {json.dumps({'type': 'done'})}\n\n"
        return

    return


def _load_tool_json(tool_name: str) -> dict:
    import os as _os
    base_dir = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    tools_dir = _os.path.join(base_dir, "agent", "agent_tools")
    filepath = _os.path.join(tools_dir, f"{tool_name}.json")
    if _os.path.isfile(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {}


def _split_thinking(content: str):
    """从内容中分离 <thinking> 标签内的思考过程和标签外的正式回答

    Args:
        content: 模型原始输出

    Returns:
        (thinking, answer): 思考内容（可为空）和正式回答
    """
    pattern = r'<thinking>\s*(.*?)\s*</thinking>'
    match = re.search(pattern, content, re.DOTALL)
    if not match:
        return '', content.strip()

    thinking = match.group(1).strip()
    before = content[:match.start()].strip()
    after = content[match.end():].strip()
    answer = (before + '\n\n' + after).strip() if before and after else (before or after)
    return thinking, answer


def _extract_cached_tool_context(messages: list) -> str:
    """从历史消息中提取最近工具调用信息，生成可复用提示

    Args:
        messages: 会话历史消息列表

    Returns:
        str: 缓存工具提示文本，无缓存则返回空字符串
    """
    tool_names = []
    seen = set()
    for msg in reversed(messages):
        if msg.get("role") == "assistant":
            for t in msg.get("tools", []):
                name = t.get("name", "")
                if name and name not in seen:
                    seen.add(name)
                    tool_names.append(name)
        if len(tool_names) >= 5:
            break

    if not tool_names:
        return ""

    return (
        f"以下工具已在本次对话中执行过：{', '.join(tool_names)}\n"
        "如果用户的新问题可以用这些工具的历史结果直接回答，请引用历史数据，不要重复调用工具。\n"
        "只有当用户明确要求重新查询、或数据范围超出已有结果时，才需要重新调用。"
    )


async def _stream_chat(message: str, session_id: str = None, web_search: str = "off", user_id: int = None, show_thought: bool = False):
    agent, _, registry, _, _ = get_dependencies()
    store = get_session_store()

    if user_id is None:
        yield f"data: {json.dumps({'type': 'error', 'content': '用户未登录'})}\n\n"
        return

    if agent is None or registry is None:
        yield f"data: {json.dumps({'type': 'error', 'content': '服务正在初始化中，请稍后再试'})}\n\n"
        return

    if message.strip().startswith("/"):
        handled = False
        async for chunk in _handle_command(message, session_id, user_id):
            if chunk is not None:
                handled = True
                yield chunk
        if handled:
            return

    llm, cfg = _resolve_llm_client(user_id)
    if llm is None:
        from server.database import get_user_by_id
        user = get_user_by_id(user_id)
        is_admin = user and user.get("user_type") == "admin"
        if is_admin:
            yield f"data: {json.dumps({'type': 'error', 'content': '模型尚未配置，请到设置中配置模型 API Key'})}\n\n"
        else:
            yield f"data: {json.dumps({'type': 'error', 'content': '模型尚未配置，请联系管理员配置全局模型，或在设置中配置个人模型'})}\n\n"
        return

    model_name = cfg.get("model_name", "").strip()
    gateway = ModelGateway(model_name)
    gateway_cfg = gateway.build_params(show_thought, temperature=0)
    reasoning_field = gateway_cfg["reasoning_field"]
    needs_prompt_fallback = gateway_cfg["needs_prompt_fallback"]
    api_params = gateway_cfg["api_params"]

    if session_id:
        messages = _load_session_messages(session_id)
        if messages:
            compressed = _compress_if_needed(messages, llm, cfg)
            if len(compressed) < len(messages):
                _save_session_messages(session_id, compressed)
                messages = compressed
        if session_id in store:
            store[session_id]["messages"] = messages
        else:
            store[session_id] = {"title": "新对话", "created_at": __import__("time").time(), "messages": messages}
    else:
        messages = []

    search_context = ""
    search_scenario = "general"
    search_info = None
    if web_search in ("auto", "on"):
        from server.database import get_search_config, get_user_by_id
        search_cfg = get_search_config()
        tavily_key = search_cfg.get("tavily_api_key", "")
        if not tavily_key:
            user = get_user_by_id(user_id)
            is_admin = user and user.get("user_type") == "admin"
            if is_admin:
                yield f"data: {json.dumps({'type': 'error', 'content': '联网搜索功能尚未配置，请到设置中配置 Tavily API Key'})}\n\n"
            else:
                yield f"data: {json.dumps({'type': 'error', 'content': '联网搜索功能尚未配置，请联系管理员进行联网搜索配置'})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
            return
        if tavily_key:
            search_scenario = _classify_query(message)
            scenario_label = SCENARIO_CONFIG[search_scenario]["label"]
            yield f"data: {json.dumps({'type': 'status', 'content': f'正在联网搜索（{scenario_label}）...'})}\n\n"
            search_context = _do_web_search(message, tavily_key, search_scenario)
            if search_context:
                search_info = {
                    "query": message,
                    "scenario": scenario_label,
                    "results": search_context,
                }
                yield f"data: {json.dumps({'type': 'web_search', 'query': message, 'scenario': scenario_label, 'results': search_context})}\n\n"
                yield f"data: {json.dumps({'type': 'status', 'content': '搜索完成，正在生成回答...'})}\n\n"

    system_prompt = (
        "你是一个智能助手，能够根据用户需求选择合适的工具。\n\n"
        "工具使用原则：\n"
        "1. 仔细阅读每个工具的 description（描述），判断是否与用户需求匹配\n"
        "2. 只有当用户明确需要工具的功能时才调用工具，不要随意调用\n"
        "3. 如果用户只是提问或聊天，直接回答即可，不需要调用任何工具\n"
        "4. 如果用户说'放到word里'、'保存为文档'、'生成word'等，应使用 save_to_word 工具\n"
        "5. 如果用户问时间日期，使用 get_current_datetime 工具\n"
        "6. 如果用户需要计算，使用 simple_calculator 工具\n"
        "7. 如果用户需要网页内容，使用 web_fetch 工具\n"
        "8. 如果用户需要农历转换，使用 convert_gregorian_to_lunar 工具\n"
        "9. 调用工具前先确认参数是否齐全，参数不齐时向用户询问\n\n"
        "工具复用原则（重要）：\n"
        "10. 调用工具前，先检查对话历史中是否已有该工具的执行结果\n"
        "11. 如果之前的工具调用已经获取了所需数据，直接引用历史结果，不要重复调用\n"
        "12. 只有以下情况才需要重新调用工具：\n"
        "    - 之前没有相关数据\n"
        "    - 用户明确要求重新查询（如'重新查一下'、'再查一下'、'刷新'）\n"
        "    - 数据范围超出已有结果（如已有7天预报但用户问第8天）\n"
        "    - 数据可能已过期（时间敏感数据，如股市行情、实时路况等）\n"
        "13. 例如：已有北京7天天气预报结果，用户再问其中某天天气，直接引用已有数据回答即可\n\n"
        "回答风格原则（重要）：\n"
        "优先使用自然段落进行回答，像人类对话一样流畅自然。只在必要时使用格式：\n"
        "- 简短问答、闲聊、一般性解释：直接用自然段落回答，不要使用任何列表或格式标记\n"
        "- 步骤说明、教程、操作指南：使用有序列表（1. 2. 3.）\n"
        "- 多个并列要点：使用无序列表（- 开头）\n"
        "- 数据对比、规格参数：使用表格（| 列1 | 列2 |）\n"
        "- 代码、命令、配置：使用代码块（```）\n"
        "- 引用、名言：使用引用块（> 开头）\n"
        "核心原则：默认用自然段落，格式只在确实能提升可读性时才使用。不要为了格式化而格式化。\n\n"
        "回答简洁原则（重要）：\n"
        "1. 直接回答用户问题，不要过度展开或添加用户未询问的额外信息\n"
        "2. 优先给出核心结论或答案，必要时再补充简要说明\n"
        "3. 如果用户没有明确要求详细分析，默认给出简洁版本\n"
        "4. 避免重复表述，每句话都应有信息增量\n"
        "5. 对于简单问题，用1-3句话回答即可，不要展开成段落"
    )

    if show_thought:
        if needs_prompt_fallback:
            system_prompt += (
                "\n\n思考过程格式（重要）：\n"
                "在给出最终回答之前，请用 <thinking>...</thinking> 标签包裹你的思考过程。\n"
                "思考过程请用自然流畅的独白形式书写，像自己在心里默默分析一样，不要使用列表或标签格式。\n"
                "应自然覆盖以下内容：先理解用户真正想问什么，然后把问题拆解成几个小步骤，\n"
                "判断需要哪些知识或工具，一步步推理出结论，最后检查一下有没有遗漏，规划好怎么组织回答。\n\n"
                "格式示例：\n"
                "<thinking>\n"
                "用户想知道北京未来三天天气，应该是为了出行做准备。要回答这个问题，我需要先查到北京的地理位置ID，然后调用天气预报接口获取未来3天的数据。拿到数据后按日期整理温度、天气状况和风力，最后给一个综合的出行建议。让我确认一下：数据要覆盖未来3天，温度单位是摄氏度，天气描述要清晰易懂。回答就按日期逐日列出，最后加一句出行提醒。\n"
                "</thinking>\n\n"
                "然后给出你的正式回答。\n"
                "注意：<thinking> 标签内的内容是你的内部思考，标签外的内容才是给用户的正式回答。\n"
                "每次回复中只能使用一次 <thinking> 标签，放在正式回答之前。"
            )
    else:
        system_prompt += (
            "\n\n重要：请直接给出最终回答，不要输出思考过程、分析过程或任何前置说明。"
        )

    if search_context:
        scenario_instruction = SCENARIO_CONFIG[search_scenario]["instruction"]
        if web_search == "on":
            system_prompt += (
                f"\n\n=== 联网搜索结果（场景：{SCENARIO_CONFIG[search_scenario]['label']}） ===\n\n"
                f"{search_context}\n\n"
                f"=== 搜索信息结束 ===\n\n"
                f"【场景指令 - 强制模式】\n{scenario_instruction}\n"
                f"请务必严格遵循以上场景指令回答用户问题。"
            )
        else:
            system_prompt += (
                f"\n\n=== 联网搜索结果（场景：{SCENARIO_CONFIG[search_scenario]['label']}） ===\n\n"
                f"{search_context}\n\n"
                f"=== 搜索信息结束 ===\n\n"
                f"【场景指令 - 自动模式】\n{scenario_instruction}\n"
                f"请参考以上场景指令，灵活判断如何最佳地回答用户问题。"
            )

    chat_messages = [{"role": "system", "content": system_prompt}]

    for msg in messages:
        chat_messages.append(msg)

    chat_messages.append({"role": "user", "content": message})

    cached_hint = _extract_cached_tool_context(messages)
    if cached_hint:
        chat_messages[0]["content"] = chat_messages[0]["content"] + "\n\n" + cached_hint

    tool_specs = registry.get_filtered_specs(message)
    max_iterations = 10
    all_tool_calls = []
    all_thoughts = []

    for iteration in range(max_iterations):
        if iteration == 0:
            yield f"data: {json.dumps({'type': 'status', 'content': '正在处理...'})}\n\n"

        try:
            stream = llm.chat_stream(chat_messages, tools=tool_specs, **api_params)
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'content': str(e)})}\n\n"
            return

        full_content = ""
        full_reasoning = ""
        tool_calls = None
        _stream_buf = ""
        _in_thinking = False

        try:
            for chunk in stream:
                if chunk.get("reasoning_content"):
                    reasoning_text = chunk["reasoning_content"]
                    full_reasoning += reasoning_text
                    if show_thought:
                        yield f"data: {json.dumps({'type': 'thought', 'content': reasoning_text})}\n\n"

                if chunk.get("content"):
                    content = chunk["content"]
                    full_content += content

                    if reasoning_field is not None:
                        yield f"data: {json.dumps({'type': 'token', 'content': content})}\n\n"
                    elif show_thought:
                        _stream_buf += content
                        while True:
                            if _in_thinking:
                                think_end = _stream_buf.find("</thinking>")
                                if think_end != -1:
                                    think_text = _stream_buf[:think_end]
                                    if think_text.strip():
                                        yield f"data: {json.dumps({'type': 'thought', 'content': think_text})}\n\n"
                                    _stream_buf = _stream_buf[think_end + len("</thinking>"):]
                                    _in_thinking = False
                                    continue
                                safe_len = len(_stream_buf)
                                for k in range(1, len("</thinking>")):
                                    if _stream_buf.endswith("</thinking>"[:k]):
                                        safe_len = len(_stream_buf) - k
                                        break
                                if safe_len > 0:
                                    if _stream_buf[:safe_len].strip():
                                        yield f"data: {json.dumps({'type': 'thought', 'content': _stream_buf[:safe_len]})}\n\n"
                                    _stream_buf = _stream_buf[safe_len:]
                                break
                            start_tag = _stream_buf.find("<thinking>")
                            if start_tag == -1:
                                safe_len = len(_stream_buf)
                                for k in range(1, len("<thinking>")):
                                    if _stream_buf.endswith("<thinking>"[:k]):
                                        safe_len = len(_stream_buf) - k
                                        break
                                if safe_len > 0:
                                    yield f"data: {json.dumps({'type': 'token', 'content': _stream_buf[:safe_len]})}\n\n"
                                    _stream_buf = _stream_buf[safe_len:]
                                break
                            else:
                                if start_tag > 0:
                                    yield f"data: {json.dumps({'type': 'token', 'content': _stream_buf[:start_tag]})}\n\n"
                                _stream_buf = _stream_buf[start_tag + len("<thinking>"):]
                                _in_thinking = True
                                continue
                    else:
                        _stream_buf += content
                        while True:
                            if "</thinking>" in _stream_buf:
                                end = _stream_buf.find("</thinking>") + len("</thinking>")
                                _stream_buf = _stream_buf[end:]
                                continue
                            start = _stream_buf.find("<thinking>")
                            if start == -1:
                                safe_len = len(_stream_buf)
                                for k in range(1, len("<thinking>")):
                                    if _stream_buf.endswith("<thinking>"[:k]):
                                        safe_len = len(_stream_buf) - k
                                        break
                                if safe_len > 0:
                                    yield f"data: {json.dumps({'type': 'token', 'content': _stream_buf[:safe_len]})}\n\n"
                                    _stream_buf = _stream_buf[safe_len:]
                                break
                            else:
                                if start > 0:
                                    yield f"data: {json.dumps({'type': 'token', 'content': _stream_buf[:start]})}\n\n"
                                _stream_buf = _stream_buf[start + len("<thinking>"):]
                if chunk.get("finish_reason"):
                    tool_calls = chunk.get("tool_calls")
                    break
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'content': str(e)})}\n\n"
            return

        if reasoning_field is None and not _in_thinking and _stream_buf.strip():
            yield f"data: {json.dumps({'type': 'token', 'content': _stream_buf})}\n\n"

        if not tool_calls:
            if all_tool_calls:
                yield f"data: {json.dumps({'type': 'tool_summary', 'tools': all_tool_calls})}\n\n"

            if reasoning_field is not None:
                if full_reasoning and show_thought:
                    all_thoughts.append(full_reasoning)
                answer = full_content
            else:
                thinking, answer = _split_thinking(full_content)
                if thinking and show_thought:
                    all_thoughts.append(thinking)

            yield f"data: {json.dumps({'type': 'done'})}\n\n"

            if session_id:
                store[session_id]["messages"].append({"role": "user", "content": message})
                assistant_msg = {"role": "assistant", "content": answer or full_content}
                if search_info:
                    assistant_msg["search"] = search_info
                if all_thoughts:
                    assistant_msg["thought"] = "\n\n".join(all_thoughts)
                if all_tool_calls:
                    assistant_msg["tools"] = all_tool_calls
                store[session_id]["messages"].append(assistant_msg)
                title = None
                if len(store[session_id]["messages"]) <= 2:
                    title = _generate_title(message, answer or full_content, llm)
                    store[session_id]["title"] = title
                import logging
                log = logging.getLogger("chat")
                for i, m in enumerate(store[session_id]["messages"]):
                    if m.get("role") == "assistant":
                        log.info(f"[SAVE] msg[{i}] role={m['role']} has_thought={bool(m.get('thought'))} has_tools={bool(m.get('tools'))} thought_len={len(m.get('thought',''))} content_len={len(m.get('content',''))}")
                _save_session_messages(session_id, store[session_id]["messages"], title)
            return

        if full_content:
            if reasoning_field is not None:
                if full_reasoning:
                    all_thoughts.append(full_reasoning)
            else:
                thinking, _ = _split_thinking(full_content)
                if thinking:
                    all_thoughts.append(thinking)

        assistant_msg = {
            "role": "assistant",
            "content": full_content,
            "tool_calls": tool_calls
        }
        if full_reasoning:
            assistant_msg["reasoning_content"] = full_reasoning
        chat_messages.append(assistant_msg)

        for tool_call in tool_calls:
            tool_name = tool_call["function"]["name"]
            tool_arguments = json.loads(tool_call["function"]["arguments"])

            yield f"data: {json.dumps({'type': 'tool_call', 'name': tool_name, 'arguments': tool_arguments})}\n\n"

            try:
                tool_result = registry.execute(tool_name, tool_arguments, user_id=user_id)
                tool_error = False
            except Exception as e:
                tool_result = f"工具执行错误: {str(e)}"
                tool_error = True

            tool_result_str = str(tool_result)
            yield f"data: {json.dumps({'type': 'tool_result', 'name': tool_name, 'content': tool_result_str})}\n\n"

            all_tool_calls.append({
                "name": tool_name,
                "arguments": tool_arguments,
                "result": tool_result_str,
                "error": tool_error or tool_result_str.startswith("[沙箱执行失败]") or tool_result_str.startswith("[沙箱执行超时]") or tool_result_str.startswith("[沙箱异常]") or tool_result_str.startswith("[工具执行异常]"),
            })

            chat_messages.append({
                "role": "tool",
                "tool_call_id": tool_call["id"],
                "name": tool_name,
                "content": tool_result_str,
            })

    if all_tool_calls:
        yield f"data: {json.dumps({'type': 'tool_summary', 'tools': all_tool_calls})}\n\n"
    yield f"data: {json.dumps({'type': 'error', 'content': '已达到最大迭代次数'})}\n\n"


@router.post("/stream")
async def chat_stream(body: ChatRequest, request: Request):
    if not body.message.strip():
        raise HTTPException(status_code=400, detail="消息不能为空")

    user = get_current_user(request)

    return StreamingResponse(
        _stream_chat(body.message, body.session_id, body.web_search, user["id"], body.show_thought),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/commands", response_model=list[dict])
def get_commands():
    return [
        {"command": "/help", "description": "显示帮助信息", "category": "通用"},
        {"command": "/reset", "description": "重置对话上下文", "category": "对话"},
        {"command": "/tool list", "description": "查看所有已安装的工具", "category": "工具"},
        {"command": "/tool add", "description": "通过自然语言新增工具", "category": "工具"},
        {"command": "/tool update", "description": "通过自然语言修改已有工具", "category": "工具"},
        {"command": "/tool delete", "description": "删除指定工具", "category": "工具"},
        {"command": "/model set", "description": "配置模型参数", "category": "模型"},
        {"command": "/model show", "description": "查看当前模型配置", "category": "模型"},
        {"command": "/model update", "description": "修改单个配置项", "category": "模型"},
        {"command": "/agent thought on", "description": "开启思考过程显示", "category": "Agent"},
        {"command": "/agent thought off", "description": "关闭思考过程显示", "category": "Agent"},
    ]