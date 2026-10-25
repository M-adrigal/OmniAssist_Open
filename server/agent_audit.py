"""Agent 任务执行审计 —— 把「谁、在哪、做了什么、结果如何、用户怎么批的」完整留痕。

存在的理由：
之前排查「技能被删了 + 流式中断 + 上下文丢失」时，事后只能靠 app.log 的散行反推，
工具调用的**参数**与**结果**没有落盘，导致"Agent 到底删了哪个技能"无法确认。
本模块提供结构化留痕，作为事后排查的第一手依据。

记录三类事件：
  1. turn     —— 一次用户提问的开始/结束（含迭代数、耗时、最终状态）
  2. tool     —— 一次工具调用（名称、参数摘要、结果摘要、耗时、成功/失败）
  3. approval —— 一次审批交互（待确认的工具、用户决策、等待时长）

落盘位置：logs/agent_audit.log（JSONL，一行一条事件，便于 grep/jq 处理）
同时在内存保留最近 N 条，按 session_id 可查，供 API 端点读取。

隐私约束：
- **不记录用户消息原文与助手回答正文**（与既有日志口径一致，避免敏感内容落盘）。
- 工具参数/结果只记录**摘要**（长度 + 截断），并对密钥类字段做脱敏。
"""
import json
import os
import re
import threading
import time
from pathlib import Path

# ==================== 常量 ====================
_LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"
_AUDIT_FILE = _LOGS_DIR / "agent_audit.log"

# 内存保留条数（超出后丢弃最旧的；磁盘文件不截断，是完整历史）
MEM_KEEP = 5000

# 单条参数/结果的截断长度
SNAPSHOT_CHARS = 500

# 密钥类参数名（记录时替换为 ***）
_SECRET_KEY_RE = re.compile(
    r"(password|passwd|secret|token|api[_-]?key|apikey|credential|authorization|cookie)",
    re.IGNORECASE,
)


# ==================== 内部工具 ====================
def _redact(obj, depth=0):
    """把参数里的密钥类字段替换成 ***，并限制嵌套深度。"""
    if depth > 4:
        return "<...>"
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(k, str) and _SECRET_KEY_RE.search(k):
                out[k] = "***"
            else:
                out[k] = _redact(v, depth + 1)
        return out
    if isinstance(obj, (list, tuple)):
        return [_redact(v, depth + 1) for v in list(obj)[:50]]
    return obj


def _snapshot(text, limit=SNAPSHOT_CHARS):
    """文本摘要：保留头尾，中间省略。比纯截断多留一点尾部线索。"""
    if not isinstance(text, str):
        try:
            text = json.dumps(text, ensure_ascii=False)
        except Exception:
            text = str(text)
    if len(text) <= limit:
        return text
    head = text[: limit * 2 // 3]
    tail = text[-(limit // 3):]
    return f"{head}\n...[省略 {len(text) - limit} 字符]...\n{tail}"


def _args_digest(args):
    """参数摘要：脱敏后的可读形式。"""
    if args is None:
        return {}
    red = _redact(args)
    if isinstance(red, dict):
        return {k: _snapshot(v, 200) if isinstance(v, str) else v for k, v in red.items()}
    return {"_": _snapshot(red, 200)}


# ==================== 审计主体 ====================
class AgentAudit:
    """线程安全的审计记录器。"""

    def __init__(self, path: Path = None, mem_keep: int = MEM_KEEP):
        self._path = path or _AUDIT_FILE
        self._lock = threading.Lock()
        self._mem: list = []
        self._mem_keep = mem_keep

    # ---------- 落盘 ----------
    def _write(self, rec: dict):
        line = json.dumps(rec, ensure_ascii=False)
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass  # 审计失败不能影响主流程

    def _emit(self, rec: dict):
        rec.setdefault("ts", time.time())
        rec.setdefault("ts_str", time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(rec["ts"])))
        with self._lock:
            self._mem.append(rec)
            if len(self._mem) > self._mem_keep:
                # 丢弃最旧的
                del self._mem[: len(self._mem) - self._mem_keep]
            self._write(rec)

    # ---------- 公开 API ----------
    def record_turn(self, session_id, user_id, event: str, **extra):
        """记录一次对话轮次事件：start / finish / abort。

        event:
          start  - 用户提问开始
          finish - 正常结束
          abort  - 异常/中断结束
        """
        self._emit({
            "type": "turn",
            "event": event,
            "session_id": session_id,
            "user_id": user_id,
            **extra,
        })

    def record_tool(self, session_id, user_id, tool_name, arguments=None,
                    result=None, error=False, duration_ms=None, iteration=None,
                    risk_level=None, **extra):
        """记录一次工具调用完成。"""
        self._emit({
            "type": "tool",
            "session_id": session_id,
            "user_id": user_id,
            "iteration": iteration,
            "tool": tool_name,
            "risk_level": risk_level,
            "arguments": _args_digest(arguments),
            "result": _snapshot(result) if result is not None else None,
            "error": bool(error),
            "duration_ms": duration_ms,
            **extra,
        })

    def record_approval_requested(self, session_id, user_id, group_id, items,
                                  iteration=None, **extra):
        """记录发起审批（等待用户决策）。items 为 [{name, args}] 列表。"""
        self._emit({
            "type": "approval",
            "event": "requested",
            "session_id": session_id,
            "user_id": user_id,
            "iteration": iteration,
            "group_id": group_id,
            "items": [
                {"name": it.get("name"), "arguments": _args_digest(it.get("args"))}
                for it in (items or [])
            ],
            **extra,
        })

    def record_approval_decision(self, session_id, user_id, group_id, decisions,
                                 wait_ms=None, items=None, **extra):
        """记录用户对审批的决策。decisions 为 {item_id: 'approve'|'reject'|'skip'}。"""
        self._emit({
            "type": "approval",
            "event": "decided",
            "session_id": session_id,
            "user_id": user_id,
            "group_id": group_id,
            "decisions": decisions,
            "wait_ms": wait_ms,
            "items": items,
            **extra,
        })

    def record_event(self, kind: str, session_id=None, user_id=None, **extra):
        """记录其他自定义事件（如 loop_guard 触停、工具被拦截）。"""
        self._emit({
            "type": kind,
            "session_id": session_id,
            "user_id": user_id,
            **extra,
        })

    # ---------- 查询 ----------
    def query(self, session_id=None, user_id=None, types=None,
              since_ts=None, limit=200, tail=False):
        """查询审计记录。tail=True 返回最新的 limit 条（默认按时间正序）。"""
        with self._lock:
            recs = list(self._mem)
        if session_id:
            recs = [r for r in recs if r.get("session_id") == session_id]
        if user_id is not None:
            recs = [r for r in recs if r.get("user_id") == user_id]
        if types:
            if isinstance(types, str):
                types = [types]
            recs = [r for r in recs if r.get("type") in types]
        if since_ts is not None:
            recs = [r for r in recs if r.get("ts", 0) >= since_ts]
        recs.sort(key=lambda r: r.get("ts", 0))
        if tail:
            recs = recs[-limit:]
        else:
            recs = recs[:limit]
        return recs

    def session_timeline(self, session_id):
        """按时间排序列出一个会话的完整轨迹（turn/tool/approval 混合）。"""
        return self.query(session_id=session_id, limit=10000)


# 全局单例
agent_audit = AgentAudit()
