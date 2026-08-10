"""会话级「信任模式」状态机（进程内，单事件循环安全）。

设计要点：
- 以 session_id 隔离每个会话的信任开关，多用户并发互不影响。
- 仅会话拥有者（owner_id 匹配）可开启/查询，防越权。
- 信任模式是「提权」：开启后该会话内的敏感操作将直接执行、不再弹确认卡片。
  因此默认关闭，且前端需持续提示「当前会话已信任」。
- 多进程部署（如 gunicorn -w N）注意：本实现为单进程内存态，跨进程不共享；
  若启用多 worker，需替换为 Redis 等跨进程共享存储（接口保持一致即可）。

同时提供 audit_log() 供审批/信任模块复用，统一写入 logs/audit.log 以便追溯。
"""
import logging
import os
import time
import uuid

log = logging.getLogger("trust")


class TrustStore:
    def __init__(self):
        # session_id -> {"owner_id", "enabled", "updated_at"}
        self._trust: dict[str, dict] = {}

    def set(self, session_id: str, owner_id: int, enabled: bool) -> bool:
        """设置会话信任开关，校验 owner。"""
        cur = self._trust.get(session_id)
        if cur and cur["owner_id"] != owner_id:
            return False
        self._trust[session_id] = {
            "owner_id": owner_id,
            "enabled": bool(enabled),
            "updated_at": time.time(),
        }
        return True

    def is_trusted(self, session_id: str, user_id: int = None) -> bool:
        """查询会话是否处于信任模式；若传入 user_id 则同时校验归属。"""
        cur = self._trust.get(session_id)
        if not cur:
            return False
        if user_id is not None and cur["owner_id"] != user_id:
            return False
        return bool(cur["enabled"])

    def get(self, session_id: str, user_id: int = None) -> dict:
        cur = self._trust.get(session_id)
        if not cur:
            return {"enabled": False}
        if user_id is not None and cur["owner_id"] != user_id:
            return {"enabled": False}
        return {"enabled": bool(cur["enabled"])}

    def clear(self, session_id: str):
        """会话删除/结束时清理，避免内存泄漏。"""
        self._trust.pop(session_id, None)


# 全局单例（单进程内共享）
trust_store = TrustStore()


def audit_log(event_type: str, **fields):
    """统一审计日志：写入 logs/audit.log（可追溯）。失败静默。"""
    try:
        os.makedirs("logs", exist_ok=True)
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        parts = " ".join(f"{k}={v}" for k, v in fields.items())
        line = f"[{ts}] {event_type} {parts}\n"
        with open("logs/audit.log", "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass
