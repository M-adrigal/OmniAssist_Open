import json
import asyncio
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


async def _stream_chat(message: str, session_id: str = None):
    agent, llm, registry, config = get_dependencies()
    store = get_session_store()

    if session_id and session_id in store:
        session = store[session_id]
        messages = session.get("messages", [])
    else:
        if session_id:
            store[session_id] = {"title": "新对话", "created_at": __import__("time").time(), "messages": []}
        messages = []

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

            if session_id and session_id in store:
                store[session_id]["messages"].append({"role": "user", "content": message})
                store[session_id]["messages"].append({"role": "assistant", "content": content})
                if len(store[session_id]["messages"]) <= 2:
                    store[session_id]["title"] = message[:30] + ("..." if len(message) > 30 else "")
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
        _stream_chat(body.message, body.session_id),
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