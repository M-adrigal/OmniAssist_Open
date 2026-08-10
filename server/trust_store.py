"""会话级「权限模式」状态机（进程内，单事件循环安全）。

两种权限模式：
- "request"（请求批准，默认）：敏感操作前需用户在聊天框逐项确认。
- "full"（完全访问权限）：敏感操作直接执行、不再弹确认卡片。

授权范围（完全访问权限覆盖）：
- 联网（web 搜索 / 抓取）、可操作工具（技能 / 诊断）、沙箱命令、
  用户上传文件、生成文件。
边界：软件本体（agent/、server/ 源码）与系统配置（密钥、数据库）受
沙箱保护，任何模式下均不可被 Agent 控制（见 agent/sandbox.py）。

设计要点：
- 以 session_id 隔离每个会话的权限模式，多用户并发互不影响。
- 仅会话拥有者（owner_id 匹配）可切换/查询，防越权。
- 完全访问权限是「提权」，默认 request，且前端需持续提示。
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
    # 允许的权限模式
    MODE_REQUEST = "request"   # 请求批准（默认）
    MODE_FULL = "full"         # 完全访问权限
    _VALID_MODES = (MODE_REQUEST, MODE_FULL)

    def __init__(self):
        # session_id -> {"owner_id", "mode", "enabled", "updated_at"}
        self._trust: dict[str, dict] = {}

    def set(self, session_id: str, owner_id: int, enabled: bool = None,
            mode: str = None) -> bool:
        """设置会话权限模式，校验 owner。

        Args:
            enabled: 兼容旧调用，True 映射为 full、False 映射为 request。
            mode: 显式模式 "request" / "full"。若同时给出以 mode 为准。
        """
        if mode is None:
            mode = self.MODE_FULL if enabled else self.MODE_REQUEST
        if mode not in self._VALID_MODES:
            return False
        cur = self._trust.get(session_id)
        if cur and cur["owner_id"] != owner_id:
            return False
        self._trust[session_id] = {
            "owner_id": owner_id,
            "mode": mode,
            "enabled": (mode == self.MODE_FULL),
            "updated_at": time.time(),
        }
        return True

    def is_trusted(self, session_id: str, user_id: int = None) -> bool:
        """查询会话是否处于完全访问权限；若传入 user_id 则同时校验归属。"""
        cur = self._trust.get(session_id)
        if not cur:
            return False
        if user_id is not None and cur["owner_id"] != user_id:
            return False
        return bool(cur.get("enabled", False))

    def get(self, session_id: str, user_id: int = None) -> dict:
        cur = self._trust.get(session_id)
        if not cur:
            return {"enabled": False, "mode": self.MODE_REQUEST}
        if user_id is not None and cur["owner_id"] != user_id:
            return {"enabled": False, "mode": self.MODE_REQUEST}
        return {"enabled": bool(cur.get("enabled", False)), "mode": cur.get("mode", self.MODE_REQUEST)}

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
