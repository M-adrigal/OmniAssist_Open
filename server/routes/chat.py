import asyncio
import base64
import json
import os
import re
import time
import threading
import uuid
from functools import partial
from datetime import datetime
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from server.models import ChatRequest
from agent.intent_keywords import select_tools_by_intent
from agent.model_gateway import ModelGateway, thinking_mode_to_flags
from agent.logger import get_logger, set_context, clear_context
from agent.parallel_executor import ParallelToolExecutor
from agent.loop_guard import new_loop_state, evaluate as _eval_loop_guard, _call_signature

router = APIRouter(prefix="/api/chat", tags=["chat"])
logger = get_logger("chat")

# 安全限制
MAX_MESSAGE_LENGTH = 10000      # 单条消息最大字符数
MAX_SESSION_TITLE_LENGTH = 200  # 会话标题最大字符数

from server.routes.auth import get_current_user
from server.approval_store import approval_store
from server.trust_store import trust_store, audit_log
from server.database import resolve_model_config
from server.agent_audit import agent_audit


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


def _content_to_text(content) -> str:
    """把消息 content 归一成纯文本（用于脱敏落库与历史渲染）。

    多模态消息的 content 是块数组（[{"type":"text",...},{"type":"image_url",...}]），
    若直接 str(content) 会把整个结构压成一个字符串再被截断——既破坏图片，
    又会生成一堆无意义文本。这里只取文本块；**图片块一律不落库**：
    图片每次请求都从磁盘实时重建，库里不需要留第二份副本。
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for seg in content:
            if isinstance(seg, dict) and isinstance(seg.get("text"), str):
                parts.append(seg["text"])
        return "\n".join(parts)
    return str(content)


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
                nm["content"] = _redact_secrets(_content_to_text(m["content"]))[:_MAX_ANSWER]
            if m.get("tools"):
                nm["tools"] = [_sanitize_tool_call(t) for t in m["tools"]]
        elif role == "tool":
            if m.get("content"):
                nm["content"] = _redact_secrets(_content_to_text(m["content"]))[:_MAX_TOOL_RESULT]
        elif role == "user":
            if m.get("content"):
                nm["content"] = _redact_secrets(_content_to_text(m["content"]))[:_MAX_ANSWER]
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


def _collect_selected_skills(skill_registry, user_id: int, selected_skills: list) -> list:
    """把用户勾选的技能名解析成 Skill 对象列表（供置顶注入）。

    过滤规则：
      - 不存在的名字直接跳过（前端可能残留已卸载的技能）；
      - 已被用户禁用的不注入（勾选不能越过禁用开关）；
      - 最多取 5 个，防止一次勾选太多把系统提示撑爆。
    """
    if not selected_skills or not skill_registry:
        return []
    try:
        disabled = _get_disabled_skills(user_id)
    except Exception:
        disabled = set()
    try:
        user_skills = skill_registry.get_user_skills(user_id) or {}
    except Exception:
        return []
    chosen = []
    for name in selected_skills[:5]:
        if not isinstance(name, str) or not name.strip():
            continue
        skill = user_skills.get(name.strip())
        if skill is None or name in disabled:
            continue
        chosen.append(skill)
    return chosen


def _select_round_documents(dir_doc_names: list, uploaded_files: list,
                            last_round_files: list, message_text: str):
    """按需选择要注入文本上下文的文档（替代原来的全量注入）。

    背景：会话上传目录跨轮累积、从不清空。旧实现每轮把所有历史文档全文
    注入系统提示词，token 随文档数线性增长，大文档会话越来越慢。
    文档与图片不同 —— 文档有「基于我刚上传的文档…」的跨轮引用需求，
    不能简单按轮隔离，故按四类信号按需注入：

      1. **本轮上传/引用的文档**：uploaded_files 里的文档（含用户用
         「@ 引用对话中的文件」主动引用的历史文件 —— 它走同一通道）；
      2. **消息文本提到文件名的历史文档**：用户说「基于 report.docx 改成表格」，
         按文件名子串匹配（兼容不带扩展名的口语，如「report」）；
      3. **最近一轮上传的文档**：覆盖「刚才那个文档」这类无名称指代；
      4. **老会话降级**：历史消息里没有上传清单（本次改造前的会话），
         无法判断轮次 → 保持全量注入，兼容优先。

    返回 (要注入文本上下文的文件名 set, 是否需要按需模式)。
    """
    dir_doc_names = list(dir_doc_names or [])
    this_round = {str(f).strip() for f in (uploaded_files or []) if f}
    last_round = {str(f).strip() for f in (last_round_files or []) if f}
    text = (message_text or "").lower()

    if last_round_files is None:
        # 老会话：无历史上传清单 → 全量注入（旧行为）
        return set(dir_doc_names), False

    selected = set()
    # 1. 本轮上传/引用（目录里真实存在的才算，防前端乱传名字）
    selected |= {f for f in this_round if f in dir_doc_names}
    # 2. 消息文本提及（完整文件名直接匹配；去扩展名的主名需 ≥2 字符，
    #    单字母主名误匹配率太高，宁可漏匹配也不乱注入）
    for name in dir_doc_names:
        if name in selected:
            continue
        if name.lower() in text:
            selected.add(name)
            continue
        base = os.path.splitext(name)[0].lower()
        if len(base) >= 2 and base in text:
            selected.add(name)
    # 3. 最近一轮上传的文档（无名称指代兜底）
    selected |= {f for f in last_round if f in dir_doc_names}
    return selected, True


def _build_doc_index_prompt(all_names: list, injected: set, on_demand: bool) -> str:
    """构建「会话文档清单」提示词片段。

    让模型知道本会话传过哪些文档、哪些的内容当前可见。用户引用了
    未注入的文档时，引导用户重新上传或用「@ 引用对话中的文件」，
    而不是让模型凭空猜测内容。
    """
    if not all_names:
        return ""
    lines = ["\n\n【本会话已上传的文档】（按上传时间倒序；标注「内容已注入」的其正文已包含在下方上下文中）"]
    for name in all_names:
        tag = "，内容已注入" if name in injected else "，内容未注入"
        lines.append(f"- {name}{tag}")
    if on_demand and len(injected) < len(all_names):
        lines.append(
            "若用户引用了「内容未注入」的文档，请提示用户重新上传该文件，"
            "或在输入框用「@ 引用对话中的文件」选中它 —— 不要凭空猜测未注入文档的内容。"
        )
    return "\n".join(lines)


def _context_messages(messages: list) -> list:
    """裁出「这次要送给模型的那一段」历史。

    两条规则：
    1. `/reset` 是**上下文边界**：标记之前的消息仍留在会话里（用户能往回翻），
       但不再送给模型。边界 = 最后一条带 `context_reset` 的消息之后。
    2. 斜杠命令本身（`/reset`、`/help`）永远不送模型 —— 它们是指令，不是对话内容。

    为什么不做成"清空 messages"：那会把用户的对话记录一起删掉（2026-10-24 反馈
    「重置上下文后，之前的上下文丢失」）。记录该留，记忆该断，是两件事。
    """
    _start = 0
    for i, m in enumerate(messages or []):
        if isinstance(m, dict) and m.get("context_reset"):
            _start = i + 1
    out = []
    for m in (messages or [])[_start:]:
        if isinstance(m, dict) and (m.get("content") or "").strip().lower() in (
                "/reset", "reset", "/help", "help"):
            continue
        out.append(m)
    return out


def _select_round_images(dir_images: list, uploaded_files: list):
    """从会话目录的图片里挑出「本轮上传」的那些，并算出本轮被限额跳过的张数。

    为什么需要它：会话上传目录（document_output/{uid}/uploads/{session_id}/）
    跨轮累积、从不清空。若把目录里所有图片都送进请求，第 N 轮会带上前 N-1 轮
    的全部图片（叠加）—— 视觉 token 按张计费，模型也分不清哪张对应当前问题。
    文档文本仍走全目录（跨轮引用是文档场景的特性，有版本标注），只限图片。

    Args:
        dir_images: build_context_parts 返回的本会话目录图片列表
            （已按其 max_images / max_total_bytes 限额剔除超限的那批）
        uploaded_files: 前端上报的本轮上传文件名

    Returns:
        (本轮图片列表, 本轮被跳过的张数)。skipped 按本轮口径算：
        本轮上传的图片总数（按扩展名判定，含被限额剔除的）− 实际放行的。
        不能沿用 build_context_parts 的全局 skipped —— 那里面混着历史图，
        会把「历史图超限」错报成本轮超限。
    """
    from agent.file_parser import IMAGE_EXTENSIONS

    dir_images = dir_images or []
    this_round = {str(f).strip() for f in (uploaded_files or []) if f}
    if not this_round:
        # 未上报本轮文件（旧客户端 / 重新生成）：本轮没有新图，不发历史图。
        # 图片从不写入会话历史，重发只会叠加与浪费。
        return [], 0
    # 本轮上传且确实躺在目录里的图片（两侧空白都 strip 后再比，防止前后端
    # 有一侧带了空格导致漏匹配 —— 漏匹配的后果是图被当历史图静默丢弃）
    kept = [im for im in dir_images if (im.get("filename") or "").strip() in this_round]
    n_round_images = sum(
        1 for f in this_round if os.path.splitext(f)[1].lower() in IMAGE_EXTENSIONS
    )
    skipped = max(0, n_round_images - len(kept))
    return kept, skipped


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


def _filter_disabled_tools(tool_specs: list, user_id: int, skill_registry,
                           disable_web_search: bool = False) -> list:
    """从工具列表中剔除已禁用技能的脚本，以及（可选）联网搜索工具

    仅从提示词里拿掉技能说明是不够的 —— 工具在服务启动时被全量注册，
    模型依然可以直接调用被禁用技能的脚本。这里在下发工具列表前做一次过滤。
    disable_web_search=True 时，即使已配置 Tavily，也完全不把 web_search 工具
    交给模型（比仅靠 prompt 约束更强，对齐『关闭即彻底不可用』的开关语义）。
    """
    if not tool_specs:
        return tool_specs

    blocked = set()

    # 1) 用户级禁用技能对应的脚本
    if skill_registry is not None:
        disabled = _get_disabled_skills(user_id)
        if disabled:
            try:
                blocked |= skill_registry.get_script_names_of(disabled, user_id)
            except Exception as e:
                logger.debug(f"解析禁用技能脚本失败 (user={user_id}): {e}")

    # 2) 联网搜索关闭时剔除 web_search 工具
    if disable_web_search:
        blocked.add("web_search")

    if not blocked:
        return tool_specs

    filtered = [
        spec for spec in tool_specs
        if spec.get("function", {}).get("name") not in blocked
    ]
    if len(filtered) != len(tool_specs):
        logger.info(
            f"已屏蔽 {len(tool_specs) - len(filtered)} 个工具 "
            f"(user={user_id}, web_search_off={disable_web_search}, "
            f"skills={sorted(disabled) if skill_registry is not None else []})"
        )
    return filtered


def _resolve_llm_client(user_id: int, session_id: str = None):
    """按「请求指定 → 会话记录 → 用户默认 → 平台默认」解析出本次要用的模型与凭据。

    传入 session_id 是必需的：会话中途换过模型后，后续每轮必须继续用同一个，
    否则用户切到 A 模型，下一句又被平台默认的 B 模型接走了。
    """
    from server.database import resolve_model_config, get_user_by_id
    from agent.llm import LLMClient
    # 管理员可以用被隐藏的模型（他要在管理端自己验证能不能跑），
    # 普通用户不行 —— 管理员一旦把模型全部隐藏，普通用户就必须用不了。
    _u = get_user_by_id(user_id) or {}
    _is_admin = _u.get("user_type") == "admin"
    cfg = resolve_model_config(user_id, session_id=session_id,
                               include_hidden=_is_admin)
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


def _save_user_message_immediate(session_id: str, message: str, user_id: int,
                                 uploaded_files: list = None):
    """立即将用户消息持久化到 DB（流开始时调用，不等待 LLM 响应完成）。

    刷新/断连后用户消息不丢失。后续 _post_process_done 会追加助手消息并更新标题。

    uploaded_files：本轮上传/引用的文件名清单，随消息落库。
    它是「按需注入文档」的依据 —— 没有这份清单，后端无法区分
    哪份文档是哪一轮传的，只能全量注入所有历史文档（token 随文档数线性增长）。
    _sanitize_messages 用 dict(m) 拷贝，该字段会被保留。
    """
    try:
        store = get_session_store()
        existing_msgs = _load_session_messages(session_id)
        # 防重：如果最后一条已是同内容用户消息，跳过
        if existing_msgs and existing_msgs[-1].get("role") == "user" and existing_msgs[-1].get("content") == message:
            logger.debug(f"[IMMEDIATE_SAVE] 用户消息已存在，跳过 (session={session_id})")
            return
        msg = {"role": "user", "content": message}
        _round_files = [str(f).strip() for f in (uploaded_files or []) if f]
        if _round_files:
            msg["uploaded_files"] = _round_files
        existing_msgs.append(msg)
        _persist_messages(session_id, existing_msgs, user_id=user_id)
        logger.info(f"[IMMEDIATE_SAVE] 用户消息已立即持久化 (session={session_id}, len={len(existing_msgs)}, files={_round_files})")
    except Exception as e:
        logger.warning(f"[IMMEDIATE_SAVE] 用户消息立即持久化失败 (session={session_id}): {e}")


def _save_assistant_message_immediate(
    session_id: str, answer: str, full_content: str,
    search_info: dict = None, all_thoughts: list = None,
    all_tool_calls: list = None, user_id: int = None, files: list = None,
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
        if files:
            # 本轮产出的文件（name/path/size/ext）随消息持久化，刷新/重登录后可在对话中还原下载卡片
            assistant_msg["files"] = files
        existing_msgs.append(assistant_msg)
        _persist_messages(session_id, existing_msgs, user_id=user_id)
        logger.info(f"[IMMEDIATE_SAVE_AST] 助手消息已立即持久化 (session={session_id}, len={len(existing_msgs)}, files={len(files or [])})")
        return True
    except Exception as e:
        logger.warning(f"[IMMEDIATE_SAVE_AST] 助手消息立即持久化失败 (session={session_id}): {e}")
        return False


def _run_deferred_tools(deferred: list, session_id: str, user_id: int,
                        iteration: int = None, pub_id: str = None,
                        registry=None, usage_sink=None):
    """在本轮流式响应完全结束、消息已落库之后，执行被推迟的自伤型工具。

    背景（2026-10-23 事故）：Agent 在流式生成途中调用 diag_restart_service，
    服务在输出到一半时把自己关掉 → 生成器损坏（generator already executing），
    该轮助手消息一条都没落库 → 用户刷新后上下文只剩自己的提问。

    推迟到此刻执行的意义：
      · 助手消息已写入 DB → 即使进程随即退出，本轮内容不会丢；
      · done 事件已发出 → 前端不会卡在"生成中"；
      · 审批仍走正常流程（该工具 risk_level=exec，本就需要用户确认）。
    """
    if not deferred:
        return
    for p in deferred:
        name = p.get("name")
        args = p.get("args") or {}
        _t0 = time.time()
        try:
            result = registry.execute(name, args, user_id=user_id, public_id=pub_id,
                                      usage_sink=usage_sink)
        except Exception as e:
            result = f"Error executing tool '{name}': {e}"
        _dur = int((time.time() - _t0) * 1000)
        agent_audit.record_tool(
            session_id, user_id, name, arguments=args, result=result,
            error=isinstance(result, str) and result.startswith("Error"),
            duration_ms=_dur, iteration=iteration,
            risk_level=registry.get_risk_level(name) if registry else None,
            deferred=True,
        )
        logger.info(f"延迟工具执行完成 (session={session_id}, tool={name}, {_dur}ms)")


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
        # 以 DB 为权威来源，避免内存 store 与 DB 不同步导致回写时覆盖已保存的助手消息（刷新丢内容）。
        # 即时保存（_save_assistant_message_immediate）只写 DB、不更新内存 store，
        # 故后台后处理绝不能直接用 store[session_id]["messages"] 整体覆盖写回。
        _db_msgs = _load_session_messages(session_id)
        if _db_msgs is None:
            _db_msgs = []

        # 助手消息兜底追加：仅当即时保存未写入 DB 时（避免重复）
        _assistant_content = answer or full_content
        _last = _db_msgs[-1] if _db_msgs else None
        _need_persist = False
        if not _assistant_already_saved and not (
            _last and _last.get("role") == "assistant" and _last.get("content") == _assistant_content
        ):
            assistant_msg = {"role": "assistant", "content": _assistant_content}
            if search_info:
                assistant_msg["search"] = search_info
            if all_thoughts:
                assistant_msg["thought"] = "\n\n".join(all_thoughts)
            if all_tool_calls:
                assistant_msg["tools"] = all_tool_calls
            _db_msgs.append(assistant_msg)
            _need_persist = True

        # 标题生成（首两条消息时）并落库
        title = None
        if len(_db_msgs) <= 2:
            title = _generate_title(message, answer or full_content, llm)
            store[session_id]["title"] = title
            _need_persist = True
        if _need_persist:
            for i, m in enumerate(_db_msgs):
                if m.get("role") == "assistant":
                    logger.debug(f"保存消息 msg[{i}] has_thought={bool(m.get('thought'))} has_tools={bool(m.get('tools'))} thought_len={len(m.get('thought',''))} content_len={len(m.get('content',''))}")
            _persist_messages(session_id, _db_msgs, title, user_id)

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
        resp = llm_client.chat(
            [{"role": "user", "content": prompt}], tools=None, source="title"
        )
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
    return SimpleAgent.compress_messages(
        messages, llm_client, context_limit_tokens, source="compress"
    )


SCENARIO_CONFIG = {
    "realtime": {
        "label": "实时信息",
        "search_depth": "advanced",
        "max_results": 3,
        "append_date": True,
        "instruction": (
            "用户正在查询实时信息（如股价、新闻等），时效性至关重要。\n"
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
            "4. 如果涉及交通等实时数据，优先采用最新结果。"
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
        "交通", "限行", "地铁", "公交",
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
        # 新版本 tavily SDK 使用 TavilyClient(api_key=...)；
        # 老版本仅提供 Client(api_key=...)，此处做兼容兜底。
        try:
            from tavily import TavilyClient
            client = TavilyClient(api_key=api_key)
        except (ImportError, AttributeError, TypeError):
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
        # 【不删历史】早期实现是 store[...]["messages"] = [] 直接清空 —— 用户的
        # 对话记录会连同上下文一起消失，翻不回去（2026-10-24 反馈「重置后之前的
        # 上下文丢失」）。现在改成在最后一条 /reset 消息上打标记：界面保留完整历史，
        # 送模型时只取最后一个标记之后的消息（见 below 的 _ctx_start）。
        if session_id:
            try:
                _msgs = _load_session_messages(session_id) or []
                _marked = False
                for _m in reversed(_msgs):
                    if (isinstance(_m, dict) and _m.get("role") == "user"
                            and (_m.get("content") or "").strip().lower() in ("/reset", "reset")):
                        _m["context_reset"] = True
                        _marked = True
                        break
                if not _marked:
                    # 立即持久化线程可能还没把 /reset 这条写进去，兜底加一条边界标记
                    _msgs.append({"role": "system", "content": "上下文已重置",
                                  "context_reset": True})
                _persist_messages(session_id, _msgs, user_id=user_id)
                if session_id in store:
                    store[session_id]["messages"] = _msgs
            except Exception as e:
                logger.warning(f"/reset 标记上下文边界失败 (session={session_id}): {e}")
        yield f"data: {json.dumps({'type': 'token', 'content': '上下文已重置。历史记录仍保留在会话里，之后的对话不会再引用它。'})}\n\n"
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
            mode = "high" if arg == "on" else "low"
            agent.set_thinking_mode(mode)
            try:
                from server.database import save_model_config
                save_model_config(user_id, thinking_mode=mode)
            except Exception:
                pass
            status = "开启" if arg == "on" else "关闭"
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


def _build_image_blocks(images: list) -> list:
    """把图片元信息转成 OpenAI 兼容的 image_url 内容块（base64 data URL）。

    只在发请求前读取磁盘并编码：图片字节**不写入会话历史**，避免 base64 入库
    把 sessions.messages 撑爆；每次请求按磁盘现状实时重建。

    读取失败或文件已被清理的图片静默跳过，不中断整轮对话。
    """
    blocks = []
    for im in images or []:
        path = im.get("path") or ""
        try:
            with open(path, "rb") as _f:
                b64 = base64.b64encode(_f.read()).decode("ascii")
        except Exception as e:
            logger.warning(f"[IMAGE] 读取图片失败，已跳过: {im.get('filename')} ({e})")
            continue
        blocks.append({
            "type": "image_url",
            "image_url": {"url": f"data:{im.get('mime') or 'image/png'};base64,{b64}"},
        })
    return blocks


async def _stream_chat(message: str, session_id: str = None, web_search: str = "off",
                    user_id: int = None, public_id: str = None, show_thought: bool = False,
                    selected_skills: list = None, uploaded_files: list = None):
    # 文件输出隔离使用对外不透明 public_id；缺失时回退到整数 user_id（兼容旧调用）
    pub_id = public_id if public_id is not None else user_id
    set_context(user_id=user_id, session_id=session_id)
    # 角色级免确认（tools:execute_sensitive），整个对话只判定一次
    _role_exempt = _is_role_exempt(user_id)
    agent, _, registry, _, skill_registry = get_dependencies()
    store = get_session_store()

    # 仅记录元数据，不记录用户输入内容（避免敏感信息落入终端/日志文件）
    logger.info(f"收到消息 (user={user_id}, session={session_id or '-'}, len={len(message)})")
    # ---- 审计：对话开始 ----
    agent_audit.record_turn(
        session_id, user_id, "start",
        message_len=len(message),
        web_search=web_search,
    )


    # 统一出口标志：确保 done 事件和 clear_context 在任何代码路径都执行
    _done_sent = False
    _messages_saved = False
    _assistant_content_produced = False  # 是否已向用户产出过真实助手内容（用于 fallback 去污染）
    _llm_attempts = 0  # LLM 调用重试计数（应对网络抖动/临时错误）
    # 延迟执行队列：会终止当前服务进程的工具（如 diag_restart_service）。
    # 绝不能在流式生成期间执行——否则服务会在自己输出到一半时被杀，
    # 生成器损坏（generator already executing）且本轮上下文全部丢失。
    # 事故记录：2026-10-23 Agent 在流式中调 diag_restart_service，11:16:25 自杀。
    _deferred_tools: list = []
    # 这两个收集器初始化在 try 之外：finally 兜底会引用它们，
    # 若在 try 内初始化，异常早于初始化点时 finally 会 NameError 而二次崩溃。
    all_tool_calls: list = []
    all_thoughts: list = []

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

        llm, cfg = await _run_sync(_resolve_llm_client, user_id, session_id)
        if llm is None:
            from server.database import get_user_by_id
            user = get_user_by_id(user_id)
            is_admin = user and user.get("user_type") == "admin"
            if is_admin:
                yield f"data: {json.dumps({'type': 'error', 'content': '模型尚未配置，请到设置中配置模型 API Key'})}\n\n"
            else:
                yield f"data: {json.dumps({'type': 'error', 'content': '当前没有可用的模型：管理员未开放任何模型，请联系管理员开通，或在设置中配置自己的模型'})}\n\n"
            return

        model_name = cfg.get("model_name", "").strip()
        gateway = ModelGateway(model_name)

        # Token 用量埋点：把本次请求的 sink 挂到 LLM 客户端上。
        # 该 client 由 _resolve_llm_client 每次新建，故按请求天然隔离；
        # 之后链路里所有模型调用（对话/标题/压缩/子 Agent）都会自动落库。
        _run_id = uuid.uuid4().hex[:12]
        _usage_sink = None
        try:
            from server.usage_store import make_sink as _make_usage_sink
            _usage_sink = _make_usage_sink(
                user_id=user_id, session_id=session_id, run_id=_run_id,
                provider_name=cfg.get("provider_name", ""),
                entry_owner=cfg.get("entry_owner", ""),
            )
            llm.usage_sink = _usage_sink
        except Exception as _e:
            logger.warning(f"token 用量埋点初始化失败（不影响对话）: {_e}")

        # 思考模式：关(off)/低(low)/高(high)。低=轻量思考且不展示（省 token）；
        # 高=强思考并展示。display_thought 额外叠加本次请求的展示开关（body.show_thought）。
        _mc_mode = cfg.get("thinking_mode", "low")
        if _mc_mode not in ("off", "low", "high"):
            _mc_mode = "low"
        _thinking_enabled, _effort = thinking_mode_to_flags(_mc_mode)
        _display_thought = (_mc_mode == "high") or bool(show_thought)

        gateway_cfg = gateway.build_params(_thinking_enabled, temperature=0, reasoning_effort=_effort)
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

        # 市面对齐（纯 Agentic）：不做「前置一次性检索 + 注入 system 提示」。
        # 是否联网、搜什么、是否基于结果追问，完全交给模型通过 web_search 工具自主决定。
        # web_search=off 时由下方 system prompt 禁用该工具。
        search_info = None

        system_prompt = (
            "你是一个智能助手，能够根据用户需求选择合适的工具。\n\n"
            "工具使用原则：\n"
            "1. 仔细阅读每个工具的 description（描述），判断是否与用户需求匹配\n"
            "2. 只有当用户明确需要工具的功能时才调用工具，不要随意调用\n"
            "3. 如果用户只是提问或聊天，直接回答即可，不需要调用任何工具\n"
            "4. 如果用户说'放到word里'、'保存为文档'、'生成word'等，应使用 save_to_word 工具\n"
            "5. 如果用户问时间日期，使用 get_current_datetime 工具\n"
            "6. 如果用户需要计算，使用 simple_calculator 工具\n"
            "7. 如果用户需要搜索最新/实时/外部信息（新闻、股价、最新版本、事实查证、教程、对比分析等），使用 web_search 工具；搜索结果会附带『来源: url』，请在回答末尾用『信息来源：』段落统一列出所有参考来源，每条格式为【标题】(完整URL)，例如：\n信息来源：\n- 【Macworld 新品路线图】(https://www.macworld.com/article/...)\n- 【新浪财经前瞻】(https://finance.sina.com.cn/...)\n"
            "8. 如果需要读取某个已知具体网址的页面内容，使用 web_fetch 工具（该工具已在沙箱内放行网络，可正常抓取网页；搜索最新/实时信息仍优先用 web_search）\n"
            "9. 如果用户需要农历转换，使用 convert_gregorian_to_lunar 工具\n"
            "10. 调用工具前先确认参数是否齐全，参数不齐时向用户询问\n\n"
            "工具复用原则（重要）：\n"
            "11. 调用工具前，先检查对话历史中是否已有该工具的执行结果\n"
            "12. 如果之前的工具调用已经获取了所需数据，直接引用历史结果，不要重复调用\n"
            "13. 只有以下情况才需要重新调用工具：\n"
            "    - 之前没有相关数据\n"
            "    - 用户明确要求重新查询（如'重新查一下'、'再查一下'、'刷新'）\n"
            "    - 数据范围超出已有结果（如已有某范围数据但用户问范围外的内容）\n"
            "    - 数据可能已过期（时间敏感数据，如股市行情、实时路况等）\n"
            "14. 例如：之前已查到某数据，用户再问其中一部分，直接引用已有数据回答即可\n\n"
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
            "5. 对于简单问题，用1-3句话回答即可，不要展开成段落\n\n"
            "文件输出路径原则（重要）：\n"
            "1. 当用户请求生成文件（Word/Excel/图片/文本等）并询问结果位置时，只需告知「文件已生成：文件名」，"
            "只写文件名本身（如 苹果2026秋季发布会新品汇总.docx），严禁输出 document_output/.../ 开头的完整磁盘路径，"
            "也不要输出包含用户标识（如 u_xxx）的目录前缀。\n"
            "2. 若需提示用户文件已保存，使用类似「文件已生成：苹果2026秋季发布会新品汇总.docx」的表述即可。\n\n"
            "上传文件版本原则（重要）：\n"
            "1. 当用户上传了文件（尤其是再次上传同名或新版本文档）并要求基于文件优化/生成新文档时，"
            "必须以「最新上传版本」的文件内容为准，忽略对话历史中基于旧版本文件得出的结论。\n"
            "2. 文件上下文注入时每个文件都带有「上传于 时间」与「最新/较早版本」标记，请优先参考标注为最新上传版本的内容。"
        )

        if web_search == "off":
            system_prompt += (
                "\n\n注意：当前会话已关闭联网搜索功能，请勿调用 web_search 工具，"
                "直接基于你的知识回答用户即可。"
            )

        if _thinking_enabled:
            if needs_prompt_fallback:
                system_prompt += (
                    "\n\n思考过程格式（重要）：\n"
                    "在给出最终回答之前，请用 <thinking>...</thinking> 标签包裹你的思考过程。\n"
                    "思考过程请用自然流畅的独白形式书写，像自己在心里默默分析一样，不要使用列表或标签格式。\n"
                    "应自然覆盖以下内容：先理解用户真正想问什么，然后把问题拆解成几个小步骤，\n"
                    "判断需要哪些知识或工具，一步步推理出结论，最后检查一下有没有遗漏，规划好怎么组织回答。\n\n"
                    "格式示例：\n"
                    "<thinking>\n"
                    "用户想让我把这段会议纪要整理成一份 Word 文档。我得先理清纪要里的关键决议和待办事项，然后按标题、背景、决议、行动项的结构组织内容，最后调用 save_word 工具生成。让我确认一下：需要保留原文的要点顺序，行动项要标注负责人和截止时间，语气正式但简洁。\n"
                    "</thinking>\n\n"
                    "然后给出你的正式回答。\n"
                    "注意：<thinking> 标签内的内容是你的内部思考，标签外的内容才是给用户的正式回答。\n"
                    "每次回复中只能使用一次 <thinking> 标签，放在正式回答之前。"
                )
        else:
            system_prompt += (
                "\n\n重要：请直接给出最终回答，不要输出思考过程、分析过程或任何前置说明。"
            )

        # 图片不随文本上下文注入：单独收集，待确认模型支持视觉后再以多模态块送入
        _pending_images = []
        _skipped_images = 0
        _dir_image_count = 0

        if session_id and user_id:
            upload_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                "document_output", str(pub_id), "uploads", session_id
            )
            if os.path.isdir(upload_dir):
                _all_in_dir = [os.path.join(upload_dir, f) for f in os.listdir(upload_dir)
                               if os.path.isfile(os.path.join(upload_dir, f)) and not f.startswith(".")]
                if _all_in_dir:
                    # 按修改时间倒序，同名文件只保留最新版本，避免模型沿用旧文档内容
                    _all_in_dir.sort(key=lambda p: os.path.getmtime(p), reverse=True)
                    _seen = set()
                    uploaded = []
                    for p in _all_in_dir:
                        _base = os.path.basename(p)
                        if _base in _seen:
                            continue
                        _seen.add(_base)
                        uploaded.append(p)
                    _all_names = [os.path.basename(p) for p in uploaded]
                    # 文件名 -> 上传时间（mtime），用于标注版本/时间戳与「最新优先」
                    _file_meta = {os.path.basename(p): os.path.getmtime(p) for p in uploaded}

                    # ---- 按需选择要注入的文档（详见 _select_round_documents）----
                    # 历史上传清单：从会话消息的 uploaded_files 字段还原（本轮不计入），
                    # 取「最近一轮」作为无名称指代（"刚才那个文档"）的兜底。
                    _last_round_files = None
                    try:
                        for _m in reversed(messages or []):
                            if _m.get("role") == "user" and _m.get("uploaded_files"):
                                _last_round_files = _m["uploaded_files"]
                                break
                    except Exception:
                        _last_round_files = None
                    from agent.file_parser import IMAGE_EXTENSIONS as _IMG_EXT
                    _dir_docs = [n for n in _all_names
                                 if os.path.splitext(n)[1].lower() not in _IMG_EXT]
                    _dir_image_count = len(_all_names) - len(_dir_docs)
                    _selected_docs, _on_demand = _select_round_documents(
                        _dir_docs, uploaded_files, _last_round_files, message,
                    )

                    from agent.file_parser import parse_files, build_context_parts
                    # 只解析选中的文档 + 本轮图片（图片按张计费，仍受 build_context_parts 限额）；
                    # 未选中的历史文档不再 parse —— 省掉每轮全量解析的 CPU 与时间。
                    _to_parse = [p for p in uploaded
                                 if os.path.basename(p) in _selected_docs
                                 or (os.path.splitext(p)[1].lower() in _IMG_EXT
                                     and os.path.basename(p) in
                                     {str(f).strip() for f in (uploaded_files or []) if f})]
                    if _to_parse:
                        parsed = await _run_sync(parse_files, _to_parse)
                        _parts = await _run_sync(build_context_parts, parsed, _file_meta)
                        # 文本类文件注入系统提示词（按需：仅选中的文档有正文）
                        if _parts.get("text"):
                            system_prompt = _parts["text"] + "\n\n" + system_prompt
                        # ---- 图片只发「本轮上传」的（详见 _select_round_images）----
                        _pending_images, _skipped_images = _select_round_images(
                            _parts.get("images") or [], uploaded_files,
                        )
                    # 会话文档清单：让模型知道传过什么、哪些内容当前可见
                    _idx_prompt = _build_doc_index_prompt(
                        _dir_docs, _selected_docs, _on_demand,
                    )
                    if _idx_prompt:
                        system_prompt += _idx_prompt

                    # 【图片口径】"这一轮到底送了几张图"必须留日志。
                    # 界面上的 chip 可能是上一次渲染残留，不能当事实（2026-10-24 用户
                    # 问"没粘贴为什么也带给模型"，只能靠这行日志回答）。
                    logger.info(
                        f"[IMAGES] session={session_id} 本轮上报 "
                        f"{len(uploaded_files or [])} 个文件；目录图片 {_dir_image_count} 张；"
                        f"本轮送入 {len(_pending_images)} 张；限额跳过 {_skipped_images} 张"
                    )

        # ---- 图片门控：仅当模型支持视觉才发图，否则降级成文字提示（绝不静默丢弃）----
        _image_blocks = []
        if _pending_images:
            if gateway.supports_vision:
                _image_blocks = _build_image_blocks(_pending_images)
            if _image_blocks:
                if _skipped_images:
                    system_prompt += (
                        f"\n\n注意：用户本次共上传 {len(_pending_images) + _skipped_images} 张图片，"
                        f"其中 {_skipped_images} 张因数量或体积超限未随本次请求发送，"
                        f"实际可见 {len(_image_blocks)} 张。"
                    )
            else:
                _names = "、".join(im.get("filename", "") for im in _pending_images)
                system_prompt += (
                    f"\n\n注意：用户上传了 {len(_pending_images)} 张图片（{_names}），"
                    f"但当前模型不支持查看图片内容。请明确告知用户这一点，"
                    f"不要凭空猜测或虚构图片内容；可建议用户切换到支持视觉的模型。"
                )

        # 注入技能上下文
        if skill_registry and user_id:
            skill_context = await _run_sync(_build_skill_context, user_id, skill_registry)
            if skill_context:
                system_prompt += "\n" + skill_context

        # ---- 用户长期记忆（每位用户一份，四段）----
        # 位置有讲究：放在技能之后、用户显式勾选之前。
        # 显式勾选的技能是「这一轮就想用它」，优先级最高；长期记忆次之；通用提示垫底。
        # 总开关关掉或四段全空时 build_prompt 返回空串，等于不注入 ——
        # 不注入的那一轮，请求里就真的不该出现这段，否则废 token 也废注意力。
        try:
            from server.memory_store import get_memory as _get_memory
            from server.memory_store import build_prompt as _build_memory_prompt
            _memory_prompt = _build_memory_prompt(_get_memory(user_id))
            if _memory_prompt:
                system_prompt += "\n\n" + _memory_prompt
                logger.debug(f"[MEMORY] 注入记忆 {len(_memory_prompt)} 字符 (user={user_id})")
        except Exception as _e:
            logger.warning(f"[MEMORY] 记忆注入失败（不影响对话）: {_e}")

        # 用户显式勾选的技能：全文置顶注入，优先于上面的自动匹配上下文。
        # 语义是「这次任务明确用它」——不靠模型猜，也不落库（每轮默认自动匹配）。
        if selected_skills and skill_registry and user_id:
            _chosen = _collect_selected_skills(skill_registry, user_id, selected_skills)
            if _chosen:
                _blocks = [
                    "\n\n【用户在本轮明确指定使用以下技能，请优先遵循其指令，"
                    "不要再自行挑选其他技能】"
                ]
                for _sk in _chosen:
                    _blocks.append(
                        f"\n## 技能：{_sk.name}\n{_sk.description}\n\n{_sk.instructions}"
                    )
                system_prompt += "".join(_blocks)
                logger.info(
                    f"用户显式指定技能 (user={user_id}): {[_sk.name for _sk in _chosen]}"
                )

        chat_messages = [{"role": "system", "content": system_prompt}]

        # 【上下文边界】/reset 之前的消息保留在会话里，但不送模型（详见 _context_messages）
        for msg in _context_messages(messages):
            chat_messages.append(msg)

        # 有图片时用户消息改为多模态内容块（文本块 + 图片块），格式与 OpenAI 兼容协议一致。
        # 注意：仅存在于本次请求的 chat_messages 中，不会写入会话历史（见 _sanitize_messages）。
        if _image_blocks:
            chat_messages.append({
                "role": "user",
                "content": [{"type": "text", "text": message}] + _image_blocks,
            })
        else:
            chat_messages.append({"role": "user", "content": message})

        cached_hint = _extract_cached_tool_context(messages)
        if cached_hint:
            chat_messages[0]["content"] = chat_messages[0]["content"] + "\n\n" + cached_hint

        # 先剔除被禁用技能的工具，以及（联网搜索关闭时）web_search 工具，
        # 再按意图筛选，确保开关在工具层真正生效（关闭即模型完全看不到该工具）
        _all_specs = await _run_sync(
            _filter_disabled_tools, registry.get_all_openai_specs(), user_id,
            skill_registry, web_search == "off"
        )
        tool_specs = await _run_sync(select_tools_by_intent, message, _all_specs, user_id)
        # 最大迭代次数：优先读取模型配置（个人 > 全局），未配置则用默认 10
        # 复用本次请求已解析出的 cfg，避免为同一份配置再查一次库（多一次 join 开销）
        try:
            max_iterations = int(cfg.get("max_iterations", 10) or 10)
        except Exception:
            max_iterations = 10
        if max_iterations < 1:
            max_iterations = 10
        all_tool_calls = []
        all_thoughts = []
        # 【任务生命周期登记】走到这里说明是「真的要调模型」的轮次（斜杠命令已在上面
        # 分流返回）。登记后才能被启动对账判出「进程被杀、这一笔没记账」。
        # ⚠️ 不能放在 chat_stream 入口：/help、/reset 这类不调模型的轮次也会被登记，
        #    终结后账本里当然没有它的行 —— 会被误判成「未记账异常」，污染看板。
        #    终结在 _run_chat_background 的 finally（成功/失败/取消都会走到）。
        try:
            from server.usage_store import mark_pending
            mark_pending(session_id, user_id)
        except Exception:
            pass
        # 本轮对话开始前的产出文件基线，用于结束前 diff 出新增文件并推送给前端
        _output_baseline = _snapshot_output_files(pub_id)
        # 迭代级防护状态（防止模型陷入“调用-失败/诊断-再调用”死循环）
        _loop_state = new_loop_state(_output_baseline)

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
                _iter_tool_names = set()    # 本轮调用的工具名集合（空转检测用）
                _iter_signatures: list = []  # 本轮每次调用的签名（工具名+参数哈希），空转检测用
                _iter_fail_signatures: list = []  # 本轮失败调用的签名，loop_guard 判"同一操作反复失败"用
                _iter_failed = False         # 本轮是否有工具报错
                _iter_perm_denied = False    # 本轮是否出现“权限不足”

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
                            if _display_thought:
                                yield f"data: {json.dumps({'type': 'thought', 'content': reasoning_text})}\n\n"

                        if chunk.get("content"):
                            content = chunk["content"]
                            full_content += content
                            _assistant_content_produced = True

                            if reasoning_field is not None:
                                yield f"data: {json.dumps({'type': 'token', 'content': content})}\n\n"
                            elif _display_thought:
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
                        if full_reasoning and _display_thought:
                            all_thoughts.append(full_reasoning)
                        answer = full_content
                    else:
                        thinking, answer = _split_thinking(full_content)
                        if thinking and _display_thought:
                            all_thoughts.append(thinking)

                    # 结束前推送本轮新增的产出文件，让前端在对话内可见可下载
                    _new_files = _collect_new_output_files(pub_id, _output_baseline)
                    if _new_files:
                        yield f"data: {json.dumps({'type': 'files_created', 'files': _new_files}, ensure_ascii=False)}\n\n"

                    # 同步保存助手回复到 DB（在发 done 之前，确保刷新不丢）
                    _ast_saved = _save_assistant_message_immediate(
                        session_id, answer, full_content,
                        search_info, all_thoughts, all_tool_calls, user_id,
                        files=_new_files if _new_files else None,
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

                    # ⛔ 延迟执行自伤型工具：此刻消息已落库、done 已发出，
                    #    即使服务随即被关闭，本轮上下文与回复也不会丢。
                    if _deferred_tools:
                        _run_deferred_tools(
                            _deferred_tools, session_id, user_id,
                            iteration=iteration, pub_id=pub_id,
                            registry=registry, usage_sink=_usage_sink,
                        )
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
                #    ⛔ 先摘出自伤型工具（会终止服务进程，如重启服务）——
                #    它们不能在本轮流式生成期间执行，否则服务会自杀：
                #    生成器损坏（generator already executing）且本轮上下文全部丢失。
                #    处理方式：移出本轮 → 整轮结束、消息落库后再执行。
                _safe, _pending, _selfharm = [], [], []
                for tc in tool_calls:
                    _tname = tc["function"]["name"]
                    _targs = json.loads(tc["function"]["arguments"])
                    if registry.blocks_during_stream(_tname):
                        _selfharm.append({"tc": tc, "name": _tname, "args": _targs})
                        continue
                    if registry.needs_approval(_tname, _targs):
                        _pending.append({"tc": tc, "name": _tname, "args": _targs})
                    else:
                        _safe.append({"tc": tc, "name": _tname, "args": _targs})

                if _selfharm:
                    _deferred_tools.extend(_selfharm)
                    _names = ", ".join(p["name"] for p in _selfharm)
                    logger.warning(
                        f"自伤型工具已延迟到本轮结束后执行 (session={session_id}, tools={_names})"
                    )
                    agent_audit.record_event(
                        "tool_deferred", session_id=session_id, user_id=user_id,
                        iteration=iteration,
                        tools=[{"name": p["name"], "arguments": p["args"]} for p in _selfharm],
                        reason="会终止服务进程，禁止在流式生成期间执行",
                    )
                    yield f"data: {json.dumps({'type': 'status', 'content': f'「{_names}」将在本轮回复完成后执行'})}\n\n"

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
                        # ---- 审计：免确认路径同样留痕（用户没看到审批，但操作确实执行了）----
                        agent_audit.record_approval_decision(
                            session_id, user_id, "skipped", {"_": "auto-approved"},
                            wait_ms=0,
                            items=[{"name": it["tool"], "args": it["args"]} for it in _items],
                            iteration=iteration,
                            skipped_reason=_reason,
                        )
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
                        # ---- 审计：记录发起了哪些敏感操作、等待用户决策 ----
                        agent_audit.record_approval_requested(
                            session_id, user_id, _gid,
                            [{"name": it["tool"], "args": it["args"]} for it in _items],
                            iteration=iteration,
                        )
                        _approval_t0 = time.time()
                        try:
                            _decisions = await asyncio.wait_for(approval_store.wait(_gid), timeout=600)
                        except asyncio.TimeoutError:
                            _decisions = {it["item_id"]: "reject" for it in _items}
                            logger.warning(f"审批等待超时，自动拒绝 (session={session_id}, group={_gid})")
                        # ---- 审计：记录用户的决策与等待时长 ----
                        agent_audit.record_approval_decision(
                            session_id, user_id, _gid, _decisions,
                            wait_ms=int((time.time() - _approval_t0) * 1000),
                            items=[{"name": it["tool"], "args": it["args"]} for it in _items],
                            iteration=iteration,
                        )
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
                        result = await _run_sync(registry.execute, p["name"], p["args"], user_id=user_id, public_id=pub_id, usage_sink=_usage_sink)
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
                    _exec_res = await _run_sync(_executor.execute_batch, [p["tc"] for p in _to_exec], registry, user_id=user_id, public_id=pub_id, usage_sink=_usage_sink)
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

                    # ---- 审计：记录每次工具调用（名称/参数/结果摘要/成败）----
                    agent_audit.record_tool(
                        session_id, user_id, r["name"],
                        arguments=r.get("arguments"),
                        result=r.get("result"),
                        error=r.get("error"),
                        iteration=iteration,
                        risk_level=registry.get_risk_level(r["name"]),
                    )
                    # 调用签名供 loop_guard 判"是否在重复同一件事"
                    _iter_signatures.append(_call_signature(r["name"], r.get("arguments")))

                    chat_messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call["id"],
                        "name": r["name"],
                        "content": r["result"],
                    })

                    # 记录本轮事实（实际决策在迭代末尾统一做，避免按工具名计数失效）
                    _iter_tool_names.add(r["name"])
                    if r["error"]:
                        _iter_failed = True
                        _iter_fail_signatures.append(
                            _call_signature(r["name"], r.get("arguments")))
                    _rr = r.get("result") or ""
                    if isinstance(_rr, str) and (
                        "权限不足" in _rr or "permission denied" in _rr.lower() or "无权限" in _rr
                    ):
                        _iter_perm_denied = True

                # === 迭代级防护：防止模型陷入“调用-失败/诊断-再调用”死循环 ===
                # 三道防护（任意工具连续失败 / 权限不足硬停 / 空转重复检测）统一在此判定
                _stop_msg, _stop_answer = _eval_loop_guard(
                    _loop_state, partial(_snapshot_output_files, pub_id),
                    _iter_failed, _iter_perm_denied, _iter_tool_names,
                    _iter_signatures, _iter_fail_signatures,
                )
                if _stop_msg is not None:
                    # 先推送本轮可能已产生的产出文件，保证用户可见
                    _stop_files = _collect_new_output_files(pub_id, _output_baseline)
                    if _stop_files:
                        yield f"data: {json.dumps({'type': 'files_created', 'files': _stop_files}, ensure_ascii=False)}\n\n"
                    yield f"data: {json.dumps({'type': 'status', 'content': '检测到工具调用异常，提前结束'})}\n\n"
                    if all_tool_calls:
                        yield f"data: {json.dumps({'type': 'tool_summary', 'tools': all_tool_calls})}\n\n"
                    if not _done_sent:
                        _save_assistant_message_immediate(
                            session_id, _stop_answer, "",
                            search_info, all_thoughts, all_tool_calls, user_id,
                            files=_stop_files if _stop_files else None,
                        )
                        _done_sent = True
                    yield f"data: {json.dumps({'type': 'error', 'content': _stop_msg})}\n\n"
                    _captured = {
                        "session_id": session_id, "store": store, "message": message,
                        "answer": _stop_answer, "full_content": "",
                        "search_info": search_info, "all_thoughts": all_thoughts,
                        "all_tool_calls": all_tool_calls, "llm": llm,
                        "user_id": user_id, "iteration": iteration,
                        "_assistant_already_saved": True,
                    }
                    threading.Thread(target=_post_process_done, kwargs=_captured, daemon=True).start()
                    _messages_saved = True
                    return

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
            _new_files = _collect_new_output_files(pub_id, _output_baseline)
            if _new_files:
                yield f"data: {json.dumps({'type': 'files_created', 'files': _new_files}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
        # Fallback: 异常/中断退出时兜底保存。
        # ⚠️ 2026-10-23 事故：原逻辑只在「已产出真实助手内容」时才写助手消息，
        #    流在 iteration=5 就断掉（还没产出文本）→ 助手消息一条没落库 →
        #    用户刷新后上下文只剩自己的提问，看不到"这一轮发生过什么"。
        # 现在改为：无论是否产出文本，都落一条**明确说明中断原因**的助手记录，
        # 并把本轮已执行的工具调用一并带上，便于回看。
        if not _messages_saved and session_id and message:
            try:
                if session_id not in store:
                    store[session_id] = {"title": "新对话", "created_at": time.time(), "messages": []}
                store[session_id]["messages"].append({"role": "user", "content": message})
                _used = [t.get("name") for t in (all_tool_calls or []) if t.get("name")]
                if _assistant_content_produced:
                    store[session_id]["messages"].append({"role": "assistant", "content": "(任务执行中断)"})
                else:
                    _detail = f"（已调用工具：{'、'.join(_used)}）" if _used else ""
                    store[session_id]["messages"].append({
                        "role": "assistant",
                        "content": f"(本轮执行中断，未产生回复{_detail})",
                    })
                if all_tool_calls:
                    store[session_id]["messages"].append({
                        "role": "assistant",
                        "content": "",
                        "tools": all_tool_calls,
                    })
                _persist_messages(session_id, store[session_id]["messages"], None, user_id)
                logger.warning(f"消息通过 fallback 保存 (session={session_id}, has_content={_assistant_content_produced}, tools={len(all_tool_calls or [])})")
            except Exception as e:
                logger.error(f"Fallback 保存消息失败 (session={session_id}): {e}")
        agent_audit.record_turn(
            session_id, user_id, "finish" if _done_sent else "abort",
            iterations=iteration + 1,
            tools_used=[t.get("name") for t in (all_tool_calls or [])],
            assistant_saved=_messages_saved,
            deferred_tools=[p.get("name") for p in (_deferred_tools or [])],
        )
        clear_context()


async def _run_chat_background(task: SessionTask, message: str, session_id: str,
                                web_search: str, user_id: int, show_thought: bool,
                                public_id: str = None, selected_skills: list = None,
                                uploaded_files: list = None):
    """后台运行聊天任务：消费 _stream_chat 生成器，将事件放入 SessionTask 缓冲区。

    与客户端连接解耦 — 即使客户端断开，任务也继续运行直到完成。
    """
    try:
        async for event in _stream_chat(
            message=message,
            session_id=session_id,
            web_search=web_search,
            user_id=user_id,
            show_thought=show_thought,
            public_id=public_id,
            selected_skills=selected_skills,
            uploaded_files=uploaded_files,
        ):
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
    finally:
        # 任务生命周期终结登记：跑了这里（完成/失败/取消）就不是「中断」。
        # 进程被杀的场景进不了 finally —— 那类留给启动对账标 interrupted。
        try:
            from server.usage_store import finalize_pending
            finalize_pending(session_id)
        except Exception:
            pass


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

    # 验证会话：必须存在【且属于本人】。
    # 只判存在会让任意登录用户拿着他人 session_id 读其全部历史、并把消息写进他人会话。
    # 不存在与非本人统一返回 404（与 sessions.py 口径一致，不泄露会话是否存在）。
    if body.session_id:
        from server.database import get_session
        _s = get_session(body.session_id)
        if not _s or _s.get("user_id") != user["db_id"]:
            raise HTTPException(status_code=404, detail="会话不存在")

    # 检查该会话是否已有正在运行的任务
    existing = get_running_task(body.session_id)
    if existing and existing.status == "running":
        raise HTTPException(status_code=409, detail="该会话有正在执行的任务，请等待完成")

    # 创建后台任务
    task = SessionTask(body.session_id, body.message, user["db_id"])
    set_running_task(body.session_id, task)

    # 【任务生命周期登记】注意：不在这里登记，改在 _stream_chat 真正要调模型之前
    # （见下方 mark_pending 调用处的注释）—— 否则 /help、/reset 这类不调模型的
    # 轮次会被启动对账判成「已终结但一笔账没记」的异常任务，造成假的未记账告警。

    # 【立即持久化用户消息】刷新/断连后不再丢失用户输入
    # 用独立线程写库，不阻塞 SSE 响应
    _sid_for_save = body.session_id
    _msg_for_save = body.message
    _uid_for_save = user["db_id"]
    _files_for_save = body.uploaded_files or []
    threading.Thread(
        target=_save_user_message_immediate,
        args=(_sid_for_save, _msg_for_save, _uid_for_save, _files_for_save),
        daemon=True,
    ).start()

    # 启动后台 asyncio.Task（与客户端连接解耦）
    task._bg_task = asyncio.create_task(
        _run_chat_background(task, body.message, body.session_id, body.web_search, user["db_id"], body.show_thought, public_id=user["id"], selected_skills=body.selected_skills, uploaded_files=body.uploaded_files)
    )

    # 返回 SSE 响应（订阅任务事件）
    return StreamingResponse(
        _subscribe_to_task(task),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


@router.post("/regenerate/{session_id}")
async def regenerate_last_turn(session_id: str, request: Request):
    """重新生成最后一轮回答。

    做法不是「再问一次」，而是先把最后一条用户消息及其之后的回复从历史里删掉，
    再把这条消息原样交回前端重新发一次 /stream —— 于是：
    - 用户消息会由 /stream 重新追加一遍，历史里不会出现两条一样的提问；
    - 助手回复、思考过程、工具调用、产出文件全部重新生成，不复用旧结果；
    - 上一轮若中途出错（回答是报错文本），重新生成也能把那条脏回复清掉。
    """
    user = get_current_user(request)
    from server.database import get_session as db_get_session
    session = db_get_session(session_id)
    # 统一 404：不把「这个 ID 存在」这个事实泄漏给非归属用户
    if not session or session.get("user_id") != user["db_id"]:
        raise HTTPException(status_code=404, detail="会话不存在")

    running = get_running_task(session_id)
    if running and running.status == "running":
        raise HTTPException(status_code=409, detail="该会话有正在执行的任务，请等待完成")

    # 直接用刚才这次读取的结果，不再多查一次库（两步之间若有新消息写入，反而会截错位置）
    messages = session.get("messages") or []
    last_user = -1
    for idx in range(len(messages) - 1, -1, -1):
        if (messages[idx] or {}).get("role") == "user":
            last_user = idx
            break
    if last_user < 0:
        raise HTTPException(status_code=400, detail="没有可重新生成的消息")

    text = (messages[last_user].get("content") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="最后一条消息为空，无法重新生成")
    # 指令（/reset 等）不是提问，重放会重复执行一次副作用
    if text.startswith("/"):
        raise HTTPException(status_code=400, detail="指令消息不支持重新生成")

    _save_session_messages(session_id, messages[:last_user], user_id=user["db_id"])
    logger.info(
        f"[REGENERATE] session={session_id} 丢弃 {len(messages) - last_user} 条消息后重跑"
        f"（user_id={user['db_id']}）"
    )
    return {"message": text, "dropped": len(messages) - last_user}


@router.get("/subscribe/{session_id}")
async def subscribe_to_stream(session_id: str, request: Request):
    """订阅正在运行的任务事件流（用于切换 session 后重连）"""
    user = get_current_user(request)
    task = get_running_task(session_id)
    if not task:
        raise HTTPException(status_code=404, detail="没有正在运行的任务")
    if task.user_id != user["db_id"]:
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
    if task.user_id != user["db_id"]:
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