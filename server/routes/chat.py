import json
import asyncio
import re
from datetime import datetime
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from server.models import ChatRequest

router = APIRouter(prefix="/api/chat", tags=["chat"])


def get_dependencies():
    from server.main import get_agent, get_llm_client, get_tool_registry, get_config
    return get_agent(), get_llm_client(), get_tool_registry(), get_config()


def get_session_store():
    from server.main import get_session_store as gss
    return gss()


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
        "max_results": 5,
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
        "max_results": 8,
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
        "max_results": 5,
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
        "max_results": 8,
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
        "max_results": 5,
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
        "max_results": 5,
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
            parts.append(f"{i}. {title}\n   来源: {url}\n   {content}")

        return "\n\n".join(parts)
    except ImportError:
        return ""
    except Exception as e:
        return f"搜索出错: {str(e)}"


async def _stream_chat(message: str, session_id: str = None, web_search: str = "off"):
    agent, llm, registry, config = get_dependencies()
    store = get_session_store()

    if not config.get_api_key():
        yield f"data: {json.dumps({'type': 'error', 'content': '请先在设置中配置模型 API Key'})}\n\n"
        return

    if session_id:
        messages = _load_session_messages(session_id)
        if messages:
            compressed = _compress_if_needed(messages, llm, config)
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
    if web_search in ("auto", "on"):
        tavily_key = config.get("tavily_api_key", "")
        if tavily_key:
            search_scenario = _classify_query(message)
            scenario_label = SCENARIO_CONFIG[search_scenario]["label"]
            yield f"data: {json.dumps({'type': 'status', 'content': f'正在联网搜索（{scenario_label}）...'})}\n\n"
            search_context = _do_web_search(message, tavily_key, search_scenario)
            if search_context:
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
        "9. 调用工具前先确认参数是否齐全，参数不齐时向用户询问"
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

    tool_specs = registry.get_all_openai_specs()
    max_iterations = 10

    for iteration in range(max_iterations):
        try:
            response = llm.chat(chat_messages, tools=tool_specs)
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'content': str(e)})}\n\n"
            return

        if "tool_calls" not in response:
            content = response.get("content", "")
            yield f"data: {json.dumps({'type': 'token', 'content': content})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"

            if session_id:
                store[session_id]["messages"].append({"role": "user", "content": message})
                store[session_id]["messages"].append({"role": "assistant", "content": content})
                title = None
                if len(store[session_id]["messages"]) <= 2:
                    title = _generate_title(message, content, llm)
                    store[session_id]["title"] = title
                _save_session_messages(session_id, store[session_id]["messages"], title)
            return

        chat_messages.append(response)

        for tool_call in response["tool_calls"]:
            tool_name = tool_call["function"]["name"]
            tool_arguments = json.loads(tool_call["function"]["arguments"])

            yield f"data: {json.dumps({'type': 'tool_call', 'name': tool_name, 'arguments': tool_arguments})}\n\n"

            try:
                tool_result = registry.execute(tool_name, tool_arguments)
            except Exception as e:
                tool_result = f"工具执行错误: {str(e)}"

            yield f"data: {json.dumps({'type': 'tool_result', 'name': tool_name, 'content': str(tool_result)})}\n\n"

            chat_messages.append({
                "role": "tool",
                "tool_call_id": tool_call["id"],
                "name": tool_name,
                "content": str(tool_result),
            })

    yield f"data: {json.dumps({'type': 'error', 'content': '已达到最大迭代次数'})}\n\n"


@router.post("/stream")
async def chat_stream(body: ChatRequest):
    if not body.message.strip():
        raise HTTPException(status_code=400, detail="消息不能为空")

    return StreamingResponse(
        _stream_chat(body.message, body.session_id, body.web_search),
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