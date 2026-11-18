"""会话级「权限模式」状态机（跨进程：进程内 / Redis 二选一）。

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
- **状态放在 server/kv.py**：默认进程内，配了 REDIS_URL 就是 Redis。
  多 worker 下"A 进程开了完全访问权限、请求落到 B 进程又不生效"是原来最脏的问题
  —— 用户以为已提权，实际换了个进程又开始弹确认，且没有任何提示。

同时提供 audit_log() 供审批/信任模块复用，统一写入 logs/audit.log 以便追溯。
"""
import os
import time

from agent.logger import get_logger
from server import kv

log = get_logger(__name__)

TRUST_TTL = 86400   # 会话权限模式保留 24 小时；会话本身有 TTL，到期自然清


class TrustStore:
    # 允许的权限模式
    MODE_REQUEST = "request"   # 请求批准（默认）
    MODE_FULL = "full"         # 完全访问权限
    _VALID_MODES = (MODE_REQUEST, MODE_FULL)

    @staticmethod
    def _key(session_id: str) -> str:
        return f"trust:session:{session_id}"

    def _load(self, session_id: str):
        rec = kv.get_backend().get_json(self._key(session_id))
        return rec if isinstance(rec, dict) else None

    def set(self, session_id: str, owner_id: int, enabled: bool = None,
            mode: str = None) -> bool:
        """设置会话权限模式，校验 owner。

        Args:
            enabled: 兼容旧调用，True 映射为 full、False 映射为 request。
            mode: 显式模式 "request" / "full"。若同时给出以 mode 为准。

        ⚠️ **`mode` 只能用关键字传**。写成 `set(sid, owner, "full")` 会把模式字符串
        塞进 `enabled` 位置，而非空字符串在 Python 里是 truthy → 静默变成"已开启，
        且模式按 enabled 推导"，也就是看起来成功了、实际语义全靠巧合。
        这种"看起来对"的错最难查（写测试时我自己就踩了一次），所以这里不再用
        truthy 兜着，直接拒绝并说清原因。
        """
        if enabled is not None and not isinstance(enabled, bool):
            log.error(
                f"trust_store.set 的 enabled 必须是 bool，收到 "
                f"{type(enabled).__name__}={enabled!r}（session={session_id}）。"
                f"疑似把 mode 传到了 enabled 位置 —— 请改用 mode= 关键字。已按失败处理。"
            )
            return False
        if mode is None:
            mode = self.MODE_FULL if enabled else self.MODE_REQUEST
        if mode not in self._VALID_MODES:
            return False
        cur = self._load(session_id)
        if cur and cur.get("owner_id") != owner_id:
            return False
        kv.get_backend().set_json(self._key(session_id), {
            "owner_id": owner_id,
            "mode": mode,
            "enabled": (mode == self.MODE_FULL),
            "updated_at": time.time(),
        }, ttl=TRUST_TTL)
        return True

    def is_trusted(self, session_id: str, user_id: int = None) -> bool:
        """查询会话是否处于完全访问权限；若传入 user_id 则同时校验归属。"""
        cur = self._load(session_id)
        if not cur:
            return False
        if user_id is not None and cur.get("owner_id") != user_id:
            return False
        return bool(cur.get("enabled", False))

    def get(self, session_id: str, user_id: int = None) -> dict:
        cur = self._load(session_id)
        if not cur:
            return {"enabled": False, "mode": self.MODE_REQUEST}
        if user_id is not None and cur.get("owner_id") != user_id:
            return {"enabled": False, "mode": self.MODE_REQUEST}
        return {"enabled": bool(cur.get("enabled", False)),
                "mode": cur.get("mode", self.MODE_REQUEST)}

    def clear(self, session_id: str):
        """会话删除/结束时清理。"""
        kv.get_backend().delete(self._key(session_id))


# 全局单例（状态在后端，实例本身无状态）
trust_store = TrustStore()


def audit_log(event_type, **fields):
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
