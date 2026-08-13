import asyncio
import json
import os
import re
import time
import threading
from functools import partial
from datetime import datetime
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from server.models import ChatRequest
from agent.intent_keywords import select_tools_by_intent
from agent.model_gateway import ModelGateway
from agent.logger import get_logger, set_context, clear_context
from agent.parallel_executor import ParallelToolExecutor

router = APIRouter(prefix="/api/chat", tags=["chat"])
logger = get_logger("chat")

# 安全限制
MAX_MESSAGE_LENGTH = 10000      # 单条消息最大字符数
MAX_SESSION_TITLE_LENGTH = 200  # 会话标题最大字符数

from server.routes.auth import get_current_user
from server.approval_store import approval_store
from server.trust_store import trust_store, audit_log
from server.database import resolve_model_config


def _is_role_exempt(user_id: int) -> bool:
    """角色级免确认：持有 tools:execute_sensitive 权限的角色可跳过敏感操作确认。

    通过数据库 permissions 表判断（role = user_type）。默认无此权限。
    """
    try:
        from server.database import get_user_role, check_permission
        role = get_user_role(user_id)
        return bool(role) and check_permission(role, "tools", "execute_sensitive")
    except Exception:
        return False

# 参数脱敏：密钥类字段不展示明文，超长字符串截断
_SECRET_KEY_HINTS = ("password", "secret", "key", "token", "api_key", "apikey", "credential")


def _mask_tool_args(name: str, args: dict) -> dict:
    masked = {}
    for k, v in (args or {}).items():
        if any(h in str(k).lower() for h in _SECRET_KEY_HINTS):
            masked[k] = "******"
        elif isinstance(v, str) and len(v) > 300:
            masked[k] = v[:300] + f"... (截断，共 {len(v)} 字符)"
        else:
            masked[k] = v
    return masked


# ===== 会话消息脱敏与截断（持久化前）=====
# 防止明文密钥写入会话表（sessions.messages 以明文 JSON 存储），并控制 messages 体积
# （thought / 工具结果 / 搜索结果可能极长，既泄露敏感信息又撑大数据库）。
_MAX_THOUGHT = 2000
_MAX_TOOL_RESULT = 3000
_MAX_ANSWER = 20000
_MAX_SEARCH = 1500

_SECRET_PATTERNS = [
    re.compile(r'(?i)(api[_-]?key|secret|token|access[_-]?key|private[_-]?key|authorization|bearer|password|passwd)\s*[:=]\s*["\']?([^\s"\',}{]{6,})'),
    re.compile(r'(?i)\b(sk-[A-Za-z0-9]{16,}|gh[pousr]_[A-Za-z0-9]{16,}|glpat-[0-9a-zA-Z_-]{16,}|AKIA[0-9A-Z]{16})\b'),
    re.compile(r'(?i)\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b'),
]


def _redact_secrets(text):
    """将常见密钥形态替换为 ***REDACTED***（幂等，多次调用安全）。"""
    if not isinstance(text, str):
        return text
    for pat in _SECRET_PATTERNS:
        text = pat.sub("***REDACTED***", text)
    return text


def _sanitize_tool_call(t):
    if not isinstance(t, dict):
        return t
    nt = dict(t)
    name = nt.get("name", "")
    if "arguments" in nt:
        nt["arguments"] = _mask_tool_args(name, nt["arguments"]) if isinstance(nt.get("arguments"), dict) else nt["arguments"]
    if "result" in nt:
        nt["result"] = _redact_secrets(str(nt["result"]))[:_MAX_TOOL_RESULT]
    return nt


def _sanitize_search(s):
    if not isinstance(s, dict):
        return s
    ns = dict(s)
    if ns.get("results"):
        ns["results"] = _redact_secrets(str(ns["results"]))[:_MAX_SEARCH]
    if ns.get("query"):
        ns["query"] = _redact_secrets(str(ns["query"]))[:_MAX_SEARCH]
    return ns


def _sanitize_messages(messages):
    """返回脱敏 + 截断后的消息副本，不修改入参（内存中的原始消息保持完整用于渲染）。"""
    out = []
    for m in messages or []:
        if not isinstance(m, dict):
            out.append(m)
            continue
        nm = dict(m)
        role = m.get("role")
        if role == "assistant":
            if m.get("thought"):
                nm["thought"] = _redact_secrets(str(m["thought"]))[:_MAX_THOUGHT]
            if m.get("reasoning_content"):
                nm["reasoning_content"] = _redact_secrets(str(m["reasoning_content"]))[:_MAX_THOUGHT]
            if m.get("content"):
                nm["content"] = _redact_secrets(str(m["content"]))[:_MAX_ANSWER]
            if m.get("tools"):
                nm["tools"] = [_sanitize_tool_call(t) for t in m["tools"]]
        elif role == "tool":
            if m.get("content"):
                nm["content"] = _redact_secrets(str(m["content"]))[:_MAX_TOOL_RESULT]
        elif role == "user":
            if m.get("content"):
                nm["content"] = _redact_secrets(str(m["content"]))[:_MAX_ANSWER]
        if m.get("search"):
            nm["search"] = _sanitize_search(m["search"])
        out.append(nm)
    return out


def _persist_messages(session_id, messages, title=None, user_id=None):
    """脱敏 + 截断后持久化会话消息（内存中的原始消息不受影响）。"""
    _save_session_messages(session_id, _sanitize_messages(messages), title, user_id)


# ===== SessionTask: 后台任务管理（解耦客户端连接） =====

class SessionTask:
    """跟踪一个会话的后台聊天任务，缓冲 SSE 事件，支持多订阅者"""

    def __init__(self, session_id: str, user_message: str, user_id: int):
        self.session_id = session_id
        self.user_message = user_message
        self.user_id = user_id
        self.status = "running"          # running / completed / failed
        self.events: list[str] = []      # 全量 SSE 事件缓冲
        self.subscribers: list[asyncio.Queue] = []
        self.started_at = time.time()
        self.completed_at: float | None = None
        self.answer = ""
        self._bg_task: asyncio.Task | None = None

    def add_event(self, event_str: str):
        """添加一个 SSE 事件到缓冲区，并推送给所有活跃订阅者"""
        self.events.append(event_str)
        # 解析 done 事件，提取 answer
        try:
            if '"type": "done"' in event_str or '"type":"done"' in event_str:
                pass  # done 事件仅标记结束
        except Exception:
            pass
        for q in self.subscribers:
            try:
                q.put_nowait(event_str)
            except asyncio.QueueFull:
                pass

    def mark_completed(self):
        """标记任务完成，通知所有订阅者结束"""
        if self.status != "running":
            return
        self.status = "completed"
        self.completed_at = time.time()
        for q in self.subscribers:
            try:
                q.put_nowait(None)  # None = 流结束信号
            except asyncio.QueueFull:
                pass

    def mark_failed(self, error: str):
        """标记任务失败"""
        if self.status != "running":
            return
        self.status = "failed"
        self.completed_at = time.time()
        for q in self.subscribers:
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                pass

    async def subscribe(self) -> asyncio.Queue:
        """订阅任务事件：先回放历史事件，再等待新事件"""
        q: asyncio.Queue = asyncio.Queue(maxsize=10000)
        # 回放已有事件
        for evt in self.events:
            q.put_nowait(evt)
        # 如果任务已结束，立即发送结束信号
        if self.status in ("completed", "failed"):
            q.put_nowait(None)
        else:
            self.subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        """取消订阅"""
        if q in self.subscribers:
            self.subscribers.remove(q)


# 全局任务注册表
_running_tasks: dict[str, SessionTask] = {}


def get_running_task(session_id: str) -> SessionTask | None:
    return _running_tasks.get(session_id)


def set_running_task(session_id: str, task: SessionTask):
    _running_tasks[session_id] = task


def remove_running_task(session_id: str):
    _running_tasks.pop(session_id, None)


def get_all_task_statuses(user_id: int) -> dict:
    """获取指定用户的所有任务状态"""
    cleanup_old_tasks()
    result = {}
    for sid, task in _running_tasks.items():
        if task.user_id != user_id:
            continue
        result[sid] = {
            "status": task.status,
            "user_message": task.user_message[:100],
            "started_at": task.started_at,
            "completed_at": task.completed_at,
        }
    return result


def cleanup_old_tasks():
    """清理超过 5 分钟的已完成任务"""
    now = time.time()
    to_remove = [
        sid for sid, t in _running_tasks.items()
        if t.status in ("completed", "failed")
        and t.completed_at
        and now - t.completed_at > 300
    ]
    for sid in to_remove:
        _running_tasks.pop(sid, None)


async def _run_sync(func, *args, **kwargs):
    """在线程池中运行同步函数，避免阻塞 asyncio 事件循环"""
    loop = asyncio.get_running_loop()
    if kwargs:
        return await loop.run_in_executor(None, partial(func, *args, **kwargs))
    return await loop.run_in_executor(None, func, *args)


def get_dependencies():
    try:
        from __main__ import get_agent, get_llm_client, get_tool_registry, get_config, get_skill_registry
    except ImportError:
        from server.main import get_agent, get_llm_client, get_tool_registry, get_config, get_skill_registry
    return get_agent(), get_llm_client(), get_tool_registry(), get_config(), get_skill_registry()


def get_session_store():
    try:
        from __main__ import get_session_store as gss
    except ImportError:
        from server.main import get_session_store as gss
    return gss()


def _get_disabled_skills(user_id: int) -> set:
    """获取当前用户禁用的技能名称集合（失败时返回空集，即不禁用任何技能）"""
    from server.database import get_disabled_system_skills
    try:
        return get_disabled_system_skills(user_id) or set()
    except Exception as e:
        logger.debug(f"获取禁用技能失败 (user={user_id}): {e}")
        return set()


def _build_skill_context(user_id: int, skill_registry) -> str:
    """构建用户技能上下文，注入系统提示词

    Args:
        user_id: 用户 ID
        skill_registry: SkillRegistry 实例

    Returns:
        str: 技能上下文字符串
    """
    if skill_registry is None:
        return ""

    # 加载用户技能（从文件系统扫描，补充数据库技能）
    try:
        skill_registry.load_user_skills_from_fs(user_id)
    except Exception as e:
        logger.debug(f"加载文件系统用户技能失败 (user={user_id}): {e}")

    # 加载全部数据库用户技能（含被禁用者）：禁用状态交给 disabled_names 在
    # 上下文与工具层统一过滤，避免"禁用后工具仍可调用"的遗漏。
    from server.database import get_user_skills
    try:
        skill_registry.load_user_skills(
            user_id, get_user_skills(user_id)
        )
    except Exception as e:
        logger.debug(f"加载数据库用户技能失败 (user={user_id}): {e}")

    # 注意：build_context 的第二个参数是「被禁用」的技能集合，
    # 不是「启用」的集合。传错会导致上下文里的技能被整体反选。
    return skill_registry.build_context(user_id, _get_disabled_skills(user_id))


def _filter_disabled_tools(tool_specs: list, user_id: int, skill_registry) -> list:
    """从工具列表中剔除已禁用技能的脚本

    仅从提示词里拿掉技能说明是不够的 —— 工具在服务启动时被全量注册，
    模型依然可以直接调用被禁用技能的脚本。这里在下发工具列表前做一次过滤。
    """
    if not tool_specs or skill_registry is None:
        return tool_specs

    disabled = _get_disabled_skills(user_id)
    if not disabled:
        return tool_specs

    try:
        blocked = skill_registry.get_script_names_of(disabled, user_id)
    except Exception as e:
        logger.debug(f"解析禁用技能脚本失败 (user={user_id}): {e}")
        return tool_specs

    if not blocked:
        return tool_specs

    filtered = [
        spec for spec in tool_specs
        if spec.get("function", {}).get("name") not in blocked
    ]
    if len(filtered) != len(tool_specs):
        logger.info(
            f"已屏蔽 {len(tool_specs) - len(filtered)} 个禁用技能的工具 "
            f"(user={user_id}, skills={sorted(disabled)})"
        )
    return filtered


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


def _save_session_messages(session_id: str, messages: list, title: str = None, user_id: int = None):
    from server.database import update_session_messages
    update_session_messages(session_id, messages, title, user_id)


def _save_user_message_immediate(session_id: str, message: str, user_id: int):
    """立即将用户消息持久化到 DB（流开始时调用，不等待 LLM 响应完成）。

    刷新/断连后用户消息不丢失。后续 _post_process_done 会追加助手消息并更新标题。
    """
    try:
        store = get_session_store()
        existing_msgs = _load_session_messages(session_id)
        # 防重：如果最后一条已是同内容用户消息，跳过
        if existing_msgs and existing_msgs[-1].get("role") == "user" and existing_msgs[-1].get("content") == message:
            logger.debug(f"[IMMEDIATE_SAVE] 用户消息已存在，跳过 (session={session_id})")
            return
        existing_msgs.append({"role": "user", "content": message})
        _persist_messages(session_id, existing_msgs, user_id=user_id)
        logger.info(f"[IMMEDIATE_SAVE] 用户消息已立即持久化 (session={session_id}, len={len(existing_msgs)})")
    except Exception as e:
        logger.warning(f"[IMMEDIATE_SAVE] 用户消息立即持久化失败 (session={session_id}): {e}")


def _save_assistant_message_immediate(
    session_id: str, answer: str, full_content: str,
    search_info: dict = None, all_thoughts: list = None,
    all_tool_calls: list = None, user_id: int = None,
):
    """立即将助手回复持久化到 DB（流结束时、发 done 之前同步调用）。

    确保用户刷新后助手消息不丢失。标题生成等非关键后处理仍由 _post_process_done 异步完成。
    返回 True 表示已保存（_post_process_done 应跳过重复写入）。
    """
    try:
        existing_msgs = _load_session_messages(session_id)
        # 防重：如果最后一条已是同内容助手消息，跳过
        content = answer or full_content
        if existing_msgs and existing_msgs[-1].get("role") == "assistant" and existing_msgs[-1].get("content") == content:
            logger.debug(f"[IMMEDIATE_SAVE_AST] 助手消息已存在，跳过 (session={session_id})")
            return True
        assistant_msg = {"role": "assistant", "content": content}
        if search_info:
            assistant_msg["search"] = search_info
        if all_thoughts:
            assistant_msg["thought"] = "\n\n".join(all_thoughts)
        if all_tool_calls:
            assistant_msg["tools"] = all_tool_calls
        existing_msgs.append(assistant_msg)
        _persist_messages(session_id, existing_msgs, user_id=user_id)
        logger.info(f"[IMMEDIATE_SAVE_AST] 助手消息已立即持久化 (session={session_id}, len={len(existing_msgs)})")
        return True
    except Exception as e:
        logger.warning(f"[IMMEDIATE_SAVE_AST] 助手消息立即持久化失败 (session={session_id}): {e}")
        return False


def _post_process_done(
    session_id: str, store: dict, message: str,
    answer: str, full_content: str,
    search_info: dict, all_thoughts: list,
    all_tool_calls: list, llm, user_id: int, iteration: int,
    _assistant_already_saved: bool = False,
):
    """后台任务：保存消息、生成标题、记录任务日志

    若 _assistant_already_saved=True（助手消息已在发 done 前同步写入），
    则仅做标题生成和日志，不再重复写消息。
    """
    if session_id:
        _store_msgs = store[session_id]["messages"]
        # 防重：检查用户消息是否已被 _save_user_message_immediate 持久化
        _last_user_idx = None
        for _ri in range(len(_store_msgs) - 1, -1, -1):
            if _store_msgs[_ri].get("role") == "user":
                _last_user_idx = _ri
                break
        if _last_user_idx is not None and _store_msgs[_last_user_idx].get("content") == message:
            # 用户消息已存在（立即保存写入的），不再重复追加
            pass
        else:
            _store_msgs.append({"role": "user", "content": message})

        if not _assistant_already_saved:
            # 助手消息尚未同步保存，在此追加并写库
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
            for i, m in enumerate(store[session_id]["messages"]):
                if m.get("role") == "assistant":
                    logger.debug(f"保存消息 msg[{i}] has_thought={bool(m.get('thought'))} has_tools={bool(m.get('tools'))} thought_len={len(m.get('thought',''))} content_len={len(m.get('content',''))}")
            _persist_messages(session_id, store[session_id]["messages"], title, user_id)
        else:
            # 助手消息已同步保存，仅做标题生成（首两条消息时）
            if len(store[session_id]["messages"]) <= 2:
                title = _generate_title(message, answer or full_content, llm)
                store[session_id]["title"] = title
                _persist_messages(session_id, store[session_id]["messages"], title, user_id)

        if all_tool_calls:
            from agent.task_reviewer import log_task_execution
            failed_tools = [t["name"] for t in all_tool_calls if t.get("error")]
            success = len(failed_tools) == 0
            log_task_execution(
                user_id, message, success,
                tools_used=[t["name"] for t in all_tool_calls],
                tools_failed=failed_tools,
                iterations=iteration + 1,
            )


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


def _is_obvious_no_search(message: str) -> bool:
    """自动联网搜索的『明显不需要搜索』启发式：命中即跳过，零额外开销。

    覆盖问候、闲聊、自我介绍、情绪安抚、讲笑话、纯算术、纯标点/表情等
    明显无需联网的场景，避免为每个此类消息都发起一次 LLM 判别调用。
    """
    m = (message or "").strip()
    if not m:
        return True
    # 极短消息（你好 / 嗨 / 在吗 / 嗯 ...）
    if len(m) <= 2:
        return True
    low = m.lower()
    # 去掉所有标点/空白后再做问候匹配，兼容「你好！」「在吗？」等带标点的写法
    norm = re.sub(r"[\s\W_]+", "", low)
    _greet = (
        "你好", "您好", "嗨", "哈喽", "在吗", "在不在", "有人吗",
        "谢谢", "感谢", "多谢", "再见", "拜拜", "晚安", "早安", "午安",
        "hello", "hi", "hey", "thanks", "thank you", "bye", "goodbye",
        "你是谁", "你叫什么", "你是什么", "你能做什么", "你能干啥", "你会什么",
        "讲个笑话", "说个笑话", "陪我聊聊", "在的", "嗯", "哦", "好的", "好", "ok", "okay",
    )
    if norm in _greet:
        return True
    # 纯数字 / 运算符（简单算术）
    if re.fullmatch(r"[\d\s\+\-\*/\(\)\.\^=×÷%，。、]+", m):
        return True
    # 纯标点 / 表情 / 空白
    if re.fullmatch(r"[\s\W_]+", m):
        return True
    return False


def _llm_decide_search(message: str, llm) -> bool:
    """自动联网搜索的 LLM 判别：判断是否需要联网搜索最新/实时/外部信息。

    仅用于启发式无法确定的模糊消息。失败（含推理模型等异常）时保守返回 True，
    即默认按『需要搜索』处理，避免漏搜导致回答失准。
    """
    if llm is None or getattr(llm, "client", None) is None:
        return True
    _prompt = [
        {"role": "system", "content": (
            "你是联网搜索需求判别器。判断用户的这条消息是否需要『联网搜索最新/实时/外部信息』"
            "才能准确回答。只回答 YES 或 NO。\n"
            "不需要搜索：闲聊问候、自我介绍、情绪安抚、讲笑话、写诗、纯观点/创作、"
            "简单计算、你自身能力说明、仅凭常识即可回答的通用问题。\n"
            "需要搜索：新闻/快讯、实时数据(天气/股价/汇率/赛事/交通)、最新版本或发布信息、"
            "特定事实查证、地点相关、需最新资料的操作步骤/教程等。"
        )},
        {"role": "user", "content": message},
    ]
    try:
        resp = llm.client.chat.completions.create(
            model=llm.model,
            messages=_prompt,
            temperature=0,
            max_tokens=4,
            stream=False,
        )
        text = (resp.choices[0].message.content or "").strip().upper()
        return text.startswith("Y")
    except Exception as e:
        logger.warning(f"联网搜索需求判别失败，保守按需要搜索处理: {e}")
        return True


async def _handle_command(message: str, session_id: str, user_id: int):
    agent, llm, registry, config, skill_registry = get_dependencies()
    store = get_session_store()

    if agent is None:
        yield f"data: {json.dumps({'type': 'error', 'content': '服务正在初始化中，请稍后再试'})}\n\n"
        yield f"data: {json.dumps({'type': 'done'})}\n\n"
        return

    msg = message.strip()

    if msg == "/help" or msg == "help":
        help_text = """**可用命令：**

| 命令 | 说明 |
|------|------|
| `/help` | 显示此帮助信息 |
| `/reset` | 重置当前对话上下文 |

> 技能管理（创建/更新/删除/查看）已支持自然语言交互，直接在对话中描述需求即可。"""
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


async def _stream_llm_async(llm, chat_messages, tools, api_params):
    """在线程池中运行 LLM 流式调用，避免阻塞事件循环

    每次 next(iterator) 在独立线程中执行，让出事件循环给其他请求。
    """
    loop = asyncio.get_running_loop()
    stream = await loop.run_in_executor(
        None,
        lambda: llm.chat_stream(chat_messages, tools=tools, **api_params)
    )
    try:
        it = iter(stream)
        _SENTINEL = object()
        while True:
            chunk = await loop.run_in_executor(None, next, it, _SENTINEL)
            if chunk is _SENTINEL:
                break
            yield chunk
    finally:
        if hasattr(stream, 'close'):
            await loop.run_in_executor(None, stream.close)


def _snapshot_output_files(user_id):
    """递归收集 document_output/{user_id}/ 下所有文件的相对路径集合（相对该目录）。"""
    root = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "document_output", str(user_id),
    )
    result = set()
    if not os.path.isdir(root):
        return result
    for dirpath, dirnames, filenames in os.walk(root):
        # 排除隐藏目录（如 .DS_Store 所在目录）与隐藏文件
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in filenames:
            if fn.startswith("."):
                continue
            result.add(os.path.relpath(os.path.join(dirpath, fn), root))
    return result


def _collect_new_output_files(user_id, baseline):
    """对比基线，返回本轮对话新增的文件列表（path 为相对项目根，供前端预览/下载）。"""
    root = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "document_output", str(user_id),
    )
    if not os.path.isdir(root):
        return []
    current = _snapshot_output_files(user_id)
    new = sorted(current - baseline)
    files = []
    for rel in new:
        full = os.path.join(root, rel)
        try:
            size = os.path.getsize(full)
        except OSError:
            size = 0
        files.append({
            "name": os.path.basename(rel),
            "path": os.path.join("document_output", str(user_id), rel),
            "size": size,
            "ext": os.path.splitext(rel)[1].lower().lstrip("."),
        })
    return files


async def _stream_chat(message: str, session_id: str = None, web_search: str = "off", user_id: int = None, show_thought: bool = False):
    set_context(user_id=user_id, session_id=session_id)
    # 角色级免确认（tools:execute_sensitive），整个对话只判定一次
    _role_exempt = _is_role_exempt(user_id)
    agent, _, registry, _, skill_registry = get_dependencies()
    store = get_session_store()

    # 截断消息用于日志
    msg_preview = message[:80] + "..." if len(message) > 80 else message
    logger.info(f"收到消息: \"{msg_preview}\" (len={len(message)})")


    # 统一出口标志：确保 done 事件和 clear_context 在任何代码路径都执行
    _done_sent = False
    _messages_saved = False
    _assistant_content_produced = False  # 是否已向用户产出过真实助手内容（用于 fallback 去污染）
    _llm_attempts = 0  # LLM 调用重试计数（应对网络抖动/临时错误）

    try:
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
                _done_sent = True  # _handle_command 已发送 done
                return

        llm, cfg = await _run_sync(_resolve_llm_client, user_id)
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
            messages = await _run_sync(_load_session_messages, session_id)
            # 数据库中没有消息但内存中有，则使用内存中的（避免覆盖已有的会话）
            if not messages and session_id in store:
                messages = store[session_id].get("messages", [])
            if messages:
                compressed = await _run_sync(_compress_if_needed, messages, llm, cfg)
                if len(compressed) < len(messages):
                    _persist_messages(session_id, compressed, user_id=user_id)
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

            # 是否真正需要搜索：仅 auto 模式按需判断；on 模式强制搜索
            need_search = True
            if not tavily_key:
                if web_search == "on":
                    user = get_user_by_id(user_id)
                    is_admin = user and user.get("user_type") == "admin"
                    if is_admin:
                        yield f"data: {json.dumps({'type': 'error', 'content': '联网搜索功能尚未配置，请到设置中配置 Tavily API Key'})}\n\n"
                    else:
                        yield f"data: {json.dumps({'type': 'error', 'content': '联网搜索功能尚未配置，请联系管理员进行联网搜索配置'})}\n\n"
                    yield f"data: {json.dumps({'type': 'done'})}\n\n"
                    _done_sent = True
                    return
                else:
                    # 自动模式：未配置搜索 key 时优雅降级，直接走普通对话
                    need_search = False
            elif web_search == "auto":
                if _is_obvious_no_search(message):
                    need_search = False
                else:
                    need_search = await _run_sync(_llm_decide_search, message, llm)

            if need_search and tavily_key:
                search_scenario = _classify_query(message)
                scenario_label = SCENARIO_CONFIG[search_scenario]["label"]
                yield f"data: {json.dumps({'type': 'status', 'content': f'正在联网搜索（{scenario_label}）...'})}\n\n"
                search_context = await _run_sync(_do_web_search, message, tavily_key, search_scenario)
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

        if session_id and user_id:
            upload_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                "document_output", str(user_id), "uploads", session_id
            )
            if os.path.isdir(upload_dir):
                uploaded = [os.path.join(upload_dir, f) for f in sorted(os.listdir(upload_dir))
                            if os.path.isfile(os.path.join(upload_dir, f)) and not f.startswith(".")]
                if uploaded:
                    from agent.file_parser import parse_files, build_context_prompt
                    parsed = await _run_sync(parse_files, uploaded)
                    file_context = await _run_sync(build_context_prompt, parsed)
                    system_prompt = file_context + "\n\n" + system_prompt

        # 注入技能上下文
        if skill_registry and user_id:
            skill_context = await _run_sync(_build_skill_context, user_id, skill_registry)
            if skill_context:
                system_prompt += "\n" + skill_context

        chat_messages = [{"role": "system", "content": system_prompt}]

        for msg in messages:
            chat_messages.append(msg)

        chat_messages.append({"role": "user", "content": message})

        cached_hint = _extract_cached_tool_context(messages)
        if cached_hint:
            chat_messages[0]["content"] = chat_messages[0]["content"] + "\n\n" + cached_hint

        # 先剔除被禁用技能的工具，再按意图筛选，确保开关在工具层真正生效
        _all_specs = await _run_sync(
            _filter_disabled_tools, registry.get_all_openai_specs(), user_id, skill_registry
        )
        tool_specs = await _run_sync(select_tools_by_intent, message, _all_specs, user_id)
        # 最大迭代次数：优先读取模型配置（个人 > 全局），未配置则用默认 10
        try:
            _mc = resolve_model_config(user_id)
            max_iterations = int(_mc.get("max_iterations", 10) or 10)
        except Exception:
            max_iterations = 10
        if max_iterations < 1:
            max_iterations = 10
        all_tool_calls = []
        all_thoughts = []
        _consecutive_failures = {}  # {tool_name: count} — 连续失败计数器
        # 本轮对话开始前的产出文件基线，用于结束前 diff 出新增文件并推送给前端
        _output_baseline = _snapshot_output_files(user_id)

        user_ctx = {
            "user_id": user_id,
            "username": "",
            "user_type": "user",
            "session_id": session_id or "",
        }
        try:
            from server.database import get_user_by_id as _gbu
            db_user = _gbu(user_id)
            if db_user:
                user_ctx["username"] = db_user.get("username", "")
                user_ctx["user_type"] = db_user.get("user_type", "user")
        except Exception:
            pass

        try:
            for iteration in range(max_iterations):
                logger.debug(f"迭代开始 iteration={iteration + 1}/{max_iterations} (user={user_id}, session={session_id or '-'})")
                if iteration == 0:
                    yield f"data: {json.dumps({'type': 'status', 'content': '正在处理...'})}\n\n"

                full_content = ""
                full_reasoning = ""
                tool_calls = None
                _stream_buf = ""
                _in_thinking = False

                try:
                    # 防御：工具返回结果过长可能触发上游 413，发送前对 role=tool 内容做硬性截断
                    for _m in chat_messages:
                        if _m.get("role") == "tool" and isinstance(_m.get("content"), str) and len(_m["content"]) > 4000:
                            _orig_len = len(_m["content"])
                            _m["content"] = _m["content"][:4000] + f"\n... [工具输出过长已截断，原长 {_orig_len} 字符]"

                    async for chunk in _stream_llm_async(llm, chat_messages, tool_specs, api_params):
                        if chunk.get("reasoning_content"):
                            reasoning_text = chunk["reasoning_content"]
                            full_reasoning += reasoning_text
                            _assistant_content_produced = True
                            if show_thought:
                                yield f"data: {json.dumps({'type': 'thought', 'content': reasoning_text})}\n\n"

                        if chunk.get("content"):
                            content = chunk["content"]
                            full_content += content
                            _assistant_content_produced = True

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
                    _llm_attempts += 1
                    logger.error(
                        f"LLM 流式调用失败 (user={user_id}, session={session_id}, "
                        f"iteration={iteration + 1}, attempt={_llm_attempts}): {e}",
                        exc_info=True,
                    )
                    if _llm_attempts <= 1:
                        yield f"data: {json.dumps({'type': 'status', 'content': '模型调用异常，正在重试...'})}\n\n"
                        continue  # 重试当前迭代（应对网络抖动 / 临时上游错误）
                    yield f"data: {json.dumps({'type': 'error', 'content': f'模型调用失败: {str(e)[:200]}'})}\n\n"
                    return

                if reasoning_field is None and _stream_buf.strip():
                    if _in_thinking:
                        # 流结束时有未闭合的 <thinking> 标签，作为思考内容输出
                        yield f"data: {json.dumps({'type': 'thought', 'content': _stream_buf})}\n\n"
                    else:
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

                    # 结束前推送本轮新增的产出文件，让前端在对话内可见可下载
                    _new_files = _collect_new_output_files(user_id, _output_baseline)
                    if _new_files:
                        yield f"data: {json.dumps({'type': 'files_created', 'files': _new_files}, ensure_ascii=False)}\n\n"

                    # 同步保存助手回复到 DB（在发 done 之前，确保刷新不丢）
                    _ast_saved = _save_assistant_message_immediate(
                        session_id, answer, full_content,
                        search_info, all_thoughts, all_tool_calls, user_id,
                    )

                    yield f"data: {json.dumps({'type': 'done'})}\n\n"
                    _done_sent = True

                    # 后处理（标题生成等非关键操作）放到后台任务，避免阻塞流关闭
                    _captured = {
                        "session_id": session_id, "store": store, "message": message,
                        "answer": answer, "full_content": full_content,
                        "search_info": search_info, "all_thoughts": all_thoughts,
                        "all_tool_calls": all_tool_calls, "llm": llm,
                        "user_id": user_id, "iteration": iteration,
                        "_assistant_already_saved": _ast_saved,
                    }
                    threading.Thread(target=_post_process_done, kwargs=_captured, daemon=True).start()
                    _messages_saved = True
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

                # ===== 工具执行（含敏感操作审批门）=====
                # 1) 划分无需审批 / 需要审批
                _safe, _pending = [], []
                for tc in tool_calls:
                    _tname = tc["function"]["name"]
                    _targs = json.loads(tc["function"]["arguments"])
                    if registry.needs_approval(_tname, _targs):
                        _pending.append({"tc": tc, "name": _tname, "args": _targs})
                    else:
                        _safe.append({"tc": tc, "name": _tname, "args": _targs})

                # 2) 敏感工具审批门（逐项否决；会话信任 / 角色免确认则跳过）
                _results_by_id = {}
                if _pending:
                    _trusted = trust_store.is_trusted(session_id, user_id)
                    _skip = _trusted or _role_exempt
                    if _skip:
                        # 免确认：直接执行，前端展示「已免确认」提示而非确认卡片
                        _reason = (
                            "本会话为「完全访问权限」，敏感操作将直接执行"
                            if _trusted
                            else "账号已授权免确认 (tools:execute_sensitive)，敏感操作将直接执行"
                        )
                        _items = [{
                            "item_id": p["tc"].get("id", ""),
                            "tool": p["name"],
                            "args": _mask_tool_args(p["name"], p["args"]),
                            "risk": registry.get_risk_level(p["name"]),
                            "desc": registry.describe_tool_risk(p["name"], p["args"]),
                        } for p in _pending]
                        audit_log("APPROVAL_SKIP", session=session_id, user=user_id,
                                   reason=_reason, tools=[i["tool"] for i in _items])
                        logger.info(f"敏感操作免确认直接执行 (session={session_id}, reason={_reason})")
                        yield f"data: {json.dumps({'type': 'approval_skipped', 'session_id': session_id, 'reason': _reason, 'items': _items}, ensure_ascii=False)}\n\n"
                        for p in _pending:
                            _results_by_id[p["tc"].get("id", "")] = None  # 待执行
                            yield f"data: {json.dumps({'type': 'tool_call', 'name': p['name'], 'arguments': p['args']}, ensure_ascii=False)}\n\n"
                    else:
                        _items = [{
                            "item_id": p["tc"].get("id", ""),
                            "tool": p["name"],
                            "args": _mask_tool_args(p["name"], p["args"]),
                            "risk": registry.get_risk_level(p["name"]),
                            "desc": registry.describe_tool_risk(p["name"], p["args"]),
                        } for p in _pending]
                        _gid, _ = approval_store.create(session_id, user_id, _items)
                        yield f"data: {json.dumps({'type': 'approval_required', 'group_id': _gid, 'session_id': session_id, 'items': _items}, ensure_ascii=False)}\n\n"
                        try:
                            _decisions = await asyncio.wait_for(approval_store.wait(_gid), timeout=600)
                        except asyncio.TimeoutError:
                            _decisions = {it["item_id"]: "reject" for it in _items}
                            logger.warning(f"审批等待超时，自动拒绝 (session={session_id}, group={_gid})")
                        yield f"data: {json.dumps({'type': 'approval_resolved', 'group_id': _gid, 'decisions': _decisions}, ensure_ascii=False)}\n\n"
                        approval_store.cleanup(_gid)
                        for p in _pending:
                            _iid = p["tc"].get("id", "")
                            _dec = _decisions.get(_iid)
                            if _dec == "approve":
                                _results_by_id[_iid] = None  # 待执行
                                yield f"data: {json.dumps({'type': 'tool_call', 'name': p['name'], 'arguments': p['args']}, ensure_ascii=False)}\n\n"
                            elif _dec == "skip":
                                # 跳过：不执行该工具，返回中性提示，流程继续
                                _results_by_id[_iid] = {
                                    "name": p["name"], "arguments": p["args"],
                                    "result": "⏭️ 用户已跳过此操作（未执行）", "skipped": True,
                                    "tool_call_id": _iid,
                                }
                                yield f"data: {json.dumps({'type': 'tool_call', 'name': p['name'], 'arguments': p['args']}, ensure_ascii=False)}\n\n"
                            else:
                                _results_by_id[_iid] = {
                                    "name": p["name"], "arguments": p["args"],
                                    "result": "Error: 用户已拒绝执行该敏感操作", "error": True,
                                    "tool_call_id": _iid,
                                }
                                yield f"data: {json.dumps({'type': 'tool_call', 'name': p['name'], 'arguments': p['args']}, ensure_ascii=False)}\n\n"

                # 3) 无需审批的工具：标记执行并发送 tool_call
                for p in _safe:
                    _results_by_id[p["tc"].get("id", "")] = None
                    yield f"data: {json.dumps({'type': 'tool_call', 'name': p['name'], 'arguments': p['args']}, ensure_ascii=False)}\n\n"

                # 4) 汇总待执行列表，按 并行/串行 执行
                _to_exec = [p for p in (_safe + _pending) if _results_by_id.get(p["tc"].get("id", "")) is None]
                if len(_to_exec) > 0:
                    if len(_to_exec) == 1:
                        yield f"data: {json.dumps({'type': 'status', 'content': f'正在调用工具: {_to_exec[0]["name"]}...'})}\n\n"
                    else:
                        yield f"data: {json.dumps({'type': 'status', 'content': f'正在并行调用 {len(_to_exec)} 个工具...'})}\n\n"
                if len(_to_exec) == 0:
                    batch_results = [_results_by_id[tc.get("id", "")] for tc in tool_calls]
                elif len(_to_exec) == 1:
                    p = _to_exec[0]
                    try:
                        result = await _run_sync(registry.execute, p["name"], p["args"], user_id=user_id)
                        if isinstance(result, dict):
                            result_str = json.dumps(result, ensure_ascii=False)
                        else:
                            result_str = str(result)
                        error = (
                            result_str.startswith("Error")
                            or result_str.startswith("[沙箱执行失败]")
                            or result_str.startswith("[沙箱执行超时]")
                            or result_str.startswith("[沙箱异常]")
                            or result_str.startswith("[工具执行异常]")
                        )
                    except Exception as e:
                        result_str = f"工具执行错误: {str(e)}"
                        error = True
                    _results_by_id[p["tc"].get("id", "")] = {
                        "name": p["name"], "arguments": p["args"],
                        "result": result_str, "error": error,
                        "tool_call_id": p["tc"].get("id", ""),
                    }
                    batch_results = [_results_by_id[tc.get("id", "")] for tc in tool_calls]
                else:
                    _executor = ParallelToolExecutor()
                    _exec_res = await _run_sync(_executor.execute_batch, [p["tc"] for p in _to_exec], registry, user_id=user_id)
                    _map = {r["tool_call_id"]: r for r in _exec_res}
                    for p in _to_exec:
                        _results_by_id[p["tc"].get("id", "")] = _map.get(p["tc"].get("id", ""), {
                            "name": p["name"], "arguments": p["args"],
                            "result": "Error: 工具执行结果缺失", "error": True,
                            "tool_call_id": p["tc"].get("id", ""),
                        })
                    batch_results = [_results_by_id[tc.get("id", "")] for tc in tool_calls]

                for i, tool_call in enumerate(tool_calls):
                    r = batch_results[i]
                    yield f"data: {json.dumps({'type': 'tool_result', 'name': r['name'], 'content': r['result']})}\n\n"

                    all_tool_calls.append({
                        "name": r["name"],
                        "arguments": r["arguments"],
                        "result": r["result"],
                        "error": r["error"],
                    })

                    chat_messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call["id"],
                        "name": r["name"],
                        "content": r["result"],
                    })

                    # 连续失败检测：同一工具连续失败 2 次则终止
                    if r["error"]:
                        _consecutive_failures[r["name"]] = _consecutive_failures.get(r["name"], 0) + 1
                        if _consecutive_failures[r["name"]] >= 2:
                            _fail_name = r["name"]
                            yield f"data: {json.dumps({'type': 'status', 'content': f'工具 {_fail_name} 连续失败，停止重试'})}\n\n"
                            if all_tool_calls:
                                yield f"data: {json.dumps({'type': 'tool_summary', 'tools': all_tool_calls})}\n\n"
                            yield f"data: {json.dumps({'type': 'error', 'content': f'工具 {_fail_name} 连续执行失败，已停止重试。请检查该功能是否正常。'})}\n\n"
                            _done_sent = True
                            # 同步保存已有对话内容（含错误回复）
                            _fail_answer = f"工具 {_fail_name} 连续执行失败，已停止重试。"
                            _save_assistant_message_immediate(
                                session_id, _fail_answer, "",
                                search_info, all_thoughts, all_tool_calls, user_id,
                            )
                            # 保存已有的对话内容
                            _captured = {
                                "session_id": session_id, "store": store, "message": message,
                                "answer": _fail_answer, "full_content": "",
                                "search_info": search_info, "all_thoughts": all_thoughts,
                                "all_tool_calls": all_tool_calls, "llm": llm,
                                "user_id": user_id, "iteration": iteration,
                                "_assistant_already_saved": True,
                            }
                            threading.Thread(target=_post_process_done, kwargs=_captured, daemon=True).start()
                            _messages_saved = True
                            return
                    else:
                        _consecutive_failures[r["name"]] = 0

            if all_tool_calls:
                yield f"data: {json.dumps({'type': 'tool_summary', 'tools': all_tool_calls})}\n\n"
            logger.info(
                f"已达最大迭代次数 max_iterations={max_iterations} "
                f"(user={user_id}, session={session_id or '-'}, "
                f"使用工具={[t['name'] for t in all_tool_calls]})"
            )
            yield f"data: {json.dumps({'type': 'error', 'content': '已达到最大迭代次数'})}\n\n"
        except Exception as e:
            logger.error(f"对话处理异常 (user={user_id}, session={session_id}): {e}", exc_info=True)
            yield f"data: {json.dumps({'type': 'error', 'content': f'对话处理异常: {str(e)[:200]}'})}\n\n"


    finally:
        if not _done_sent:
            # 异常/中断退出时仍推送本轮新增的产出文件，保证用户可见
            _new_files = _collect_new_output_files(user_id, _output_baseline)
            if _new_files:
                yield f"data: {json.dumps({'type': 'files_created', 'files': _new_files}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
        # Fallback: 异常退出时兜底保存。仅当已产出过真实助手内容才写入助手消息，
        # 否则只保留用户消息，避免"(任务执行中断)"等虚假内容污染对话上下文、导致下一轮继续失败。
        if not _messages_saved and session_id and message:
            try:
                if session_id not in store:
                    store[session_id] = {"title": "新对话", "created_at": time.time(), "messages": []}
                store[session_id]["messages"].append({"role": "user", "content": message})
                if _assistant_content_produced:
                    store[session_id]["messages"].append({"role": "assistant", "content": "(任务执行中断)"})
                _persist_messages(session_id, store[session_id]["messages"], None, user_id)
                logger.warning(f"消息通过 fallback 保存 (session={session_id}, has_content={_assistant_content_produced})")
            except Exception as e:
                logger.error(f"Fallback 保存消息失败 (session={session_id}): {e}")
        clear_context()


async def _run_chat_background(task: SessionTask, message: str, session_id: str,
                                web_search: str, user_id: int, show_thought: bool):
    """后台运行聊天任务：消费 _stream_chat 生成器，将事件放入 SessionTask 缓冲区。
    
    与客户端连接解耦 — 即使客户端断开，任务也继续运行直到完成。
    """
    try:
        async for event in _stream_chat(message, session_id, web_search, user_id, show_thought):
            task.add_event(event)
    except asyncio.CancelledError:
        # 用户主动停止任务
        approval_store.cancel_session(session_id)
        logger.info(f"后台任务被用户取消 (session={session_id})")
        try:
            task.add_event(f"data: {json.dumps({'type': 'done'})}\n\n")
        except Exception:
            pass
        task.mark_failed("cancelled")
        asyncio.get_running_loop().call_later(300, lambda: remove_running_task(session_id))
    except Exception as e:
        logger.error(f"后台聊天任务异常 (session={session_id}): {e}", exc_info=True)
        try:
            task.add_event(f"data: {json.dumps({'type': 'error', 'content': f'内部错误: {str(e)}'})}\n\n")
            task.add_event(f"data: {json.dumps({'type': 'done'})}\n\n")
        except Exception:
            pass
        task.mark_failed(str(e))
    else:
        task.mark_completed()
        # 任务完成后延迟清理（保留 5 分钟供前端查询状态）
        asyncio.get_running_loop().call_later(300, lambda: remove_running_task(session_id))


async def _subscribe_to_task(task: SessionTask):
    """订阅任务事件并转发给客户端 SSE"""
    q = await task.subscribe()
    try:
        while True:
            event = await q.get()
            if event is None:  # 流结束信号
                break
            yield event
    finally:
        task.unsubscribe(q)


_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


@router.post("/stream")
async def chat_stream(body: ChatRequest, request: Request):
    if not body.message.strip():
        raise HTTPException(status_code=400, detail="消息不能为空")

    # 消息长度限制
    if len(body.message) > MAX_MESSAGE_LENGTH:
        raise HTTPException(status_code=413, detail=f"消息过长，最大允许 {MAX_MESSAGE_LENGTH} 字符，当前 {len(body.message)} 字符")

    user = get_current_user(request)

    # 验证会话存在性：如果提供了 session_id，检查是否在数据库中
    if body.session_id:
        from server.database import get_session
        if not get_session(body.session_id):
            raise HTTPException(status_code=404, detail="会话不存在")

    # 检查该会话是否已有正在运行的任务
    existing = get_running_task(body.session_id)
    if existing and existing.status == "running":
        raise HTTPException(status_code=409, detail="该会话有正在执行的任务，请等待完成")

    # 创建后台任务
    task = SessionTask(body.session_id, body.message, user["id"])
    set_running_task(body.session_id, task)

    # 【立即持久化用户消息】刷新/断连后不再丢失用户输入
    # 用独立线程写库，不阻塞 SSE 响应
    _sid_for_save = body.session_id
    _msg_for_save = body.message
    _uid_for_save = user["id"]
    threading.Thread(
        target=_save_user_message_immediate,
        args=(_sid_for_save, _msg_for_save, _uid_for_save),
        daemon=True,
    ).start()

    # 启动后台 asyncio.Task（与客户端连接解耦）
    task._bg_task = asyncio.create_task(
        _run_chat_background(task, body.message, body.session_id, body.web_search, user["id"], body.show_thought)
    )

    # 返回 SSE 响应（订阅任务事件）
    return StreamingResponse(
        _subscribe_to_task(task),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


@router.get("/subscribe/{session_id}")
async def subscribe_to_stream(session_id: str, request: Request):
    """订阅正在运行的任务事件流（用于切换 session 后重连）"""
    user = get_current_user(request)
    task = get_running_task(session_id)
    if not task:
        raise HTTPException(status_code=404, detail="没有正在运行的任务")
    if task.user_id != user["id"]:
        raise HTTPException(status_code=403, detail="无权访问")

    return StreamingResponse(
        _subscribe_to_task(task),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


@router.post("/stop/{session_id}")
async def stop_task(session_id: str, request: Request):
    """停止正在运行的后台任务"""
    user = get_current_user(request)
    task = get_running_task(session_id)
    if not task:
        raise HTTPException(status_code=404, detail="没有正在运行的任务")
    if task.user_id != user["id"]:
        raise HTTPException(status_code=403, detail="无权操作")
    if task.status != "running":
        raise HTTPException(status_code=400, detail="任务已结束")

    # 取消后台 asyncio.Task
    if task._bg_task and not task._bg_task.done():
        task._bg_task.cancel()
        approval_store.cancel_session(session_id)
        logger.info(f"已取消 session {session_id} 的后台任务")

    return {"success": True, "message": "任务已停止"}


@router.get("/commands", response_model=list[dict])
def get_commands():
    return [
        {"command": "/help", "description": "显示帮助信息", "category": "通用"},
        {"command": "/reset", "description": "重置对话上下文", "category": "对话"},
    ]