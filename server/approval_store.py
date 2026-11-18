"""敏感操作审批状态机（跨进程：进程内 / Redis 二选一）。

设计要点：
- 以 session_id + group_id 隔离每个会话的审批请求，多用户并发互不影响。
- **状态放在 server/kv.py 里**：默认进程内，配了 REDIS_URL 就是 Redis。
  多 worker 部署时，「用户在 A 进程等待 → 确认请求被负载均衡打到 B 进程」
  也能生效 —— 这是原来只存内存时最隐蔽的问题（表现为"点了确认没反应，
  600 秒后超时"，而且不报错）。
- 等待用**轮询后端**而不是 asyncio.Future 直接共享：Future 天生是进程内的，
  没法跨进程。轮询间隔 0.15s，对人点按钮的场景无感，换来的是多 worker 可用。
- 归属校验（session_id + owner_id）在每次决议时都做，防越权确认他人会话。
"""
import asyncio
import time
import uuid

from agent.logger import get_logger
from server import kv

logger = get_logger(__name__)

APPROVAL_TTL = 600          # 与前端 600 秒确认超时一致
POLL_INTERVAL = 0.15        # 轮询后端间隔（秒）
ALLOW_TTL = 86400           # 「本会话已批准」记忆保留 24 小时


def _key(group_id: str) -> str:
    return f"approval:group:{group_id}"


def _allow_key(session_id: str) -> str:
    return f"approval:allow:{session_id}"


class ApprovalStore:
    def __init__(self):
        # 本进程内正在等待的分组：group_id -> {"future": Future, "task": Task}
        self._local: dict[str, dict] = {}

    # ---------- 内部：读写后端 ----------

    @property
    def _b(self):
        return kv.get_backend()

    def _load(self, group_id: str):
        rec = self._b.get_json(_key(group_id))
        return rec if isinstance(rec, dict) else None

    def _save(self, group_id: str, rec: dict):
        self._b.set_json(_key(group_id), rec, ttl=APPROVAL_TTL)

    # ---------- 内部：轮询等待 ----------

    def _start_watch(self, group_id: str, future: asyncio.Future):
        async def _watch():
            try:
                while not future.done():
                    rec = self._load(group_id)
                    if rec is None:
                        # 记录没了（被 cleanup / 过期）：按取消处理，别让调用方永远挂着
                        future.set_result({})
                        return
                    items = rec.get("items") or []
                    decisions = rec.get("decisions") or {}
                    if rec.get("cancelled"):
                        future.set_result(
                            {it.get("item_id"): "reject" for it in items}
                        )
                        return
                    if items and len(decisions) >= len(items):
                        future.set_result(dict(decisions))
                        return
                    await asyncio.sleep(POLL_INTERVAL)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"审批等待异常（按取消处理）: {e}")
                if not future.done():
                    future.set_result({})

        task = asyncio.ensure_future(_watch())
        self._local[group_id] = {"future": future, "task": task}

    # ---------- 对外接口 ----------

    def create(self, session_id: str, owner_id: int, items: list) -> tuple[str, asyncio.Future]:
        """创建一组待确认请求，返回 (group_id, future)。"""
        loop = asyncio.get_running_loop()
        gid = "g_" + uuid.uuid4().hex[:12]
        self._save(gid, {
            "session_id": session_id,
            "owner_id": owner_id,
            "items": items,
            "decisions": {},
            "cancelled": False,
            "created_at": time.time(),
        })
        fut = loop.create_future()
        self._start_watch(gid, fut)
        return gid, fut

    async def wait(self, group_id: str) -> dict:
        """等待该分组的决议（decisions 字典）。

        本地已有等待则复用；没有（例如换了进程）就现挂一个。
        组记录根本不存在时抛 KeyError，保持原有语义。
        """
        local = self._local.get(group_id)
        if local and not local["future"].done():
            return await local["future"]
        if self._load(group_id) is None:
            raise KeyError("not_found")
        fut = asyncio.get_running_loop().create_future()
        self._start_watch(group_id, fut)
        return await fut

    def _decide(self, group_id: str, decisions: dict, session_id: str, owner_id: int) -> bool:
        rec = self._load(group_id)
        if not rec:
            raise KeyError("not_found")
        if rec.get("session_id") != session_id or rec.get("owner_id") != owner_id:
            raise PermissionError("forbidden")
        if rec.get("cancelled"):
            return False
        cur = rec.get("decisions") or {}
        cur.update(decisions)
        rec["decisions"] = cur
        self._save(group_id, rec)
        return True

    def resolve(self, group_id: str, decisions: dict, session_id: str, owner_id: int) -> bool:
        """兼容旧路径：一次性写入整组决议。"""
        return self._decide(group_id, decisions, session_id, owner_id)

    def resolve_item(self, group_id: str, item_id: str, decision: str,
                     session_id: str, owner_id: int) -> bool:
        """增量写入单项决议；当全部项均已决策时由等待方释放。

        支持 decision: approve | reject | skip。
        """
        return self._decide(group_id, {item_id: decision}, session_id, owner_id)

    def cancel_session(self, session_id: str):
        """任务被停止/取消时，拒绝该会话所有待确认请求。"""
        for gid in self._b.keys("approval:group:"):
            short = gid.rsplit(":", 1)[-1]
            rec = self._load(short)
            if rec and rec.get("session_id") == session_id:
                rec["cancelled"] = True
                rec["decisions"] = {it.get("item_id"): "reject"
                                    for it in (rec.get("items") or [])}
                self._save(short, rec)

    def cleanup(self, group_id: str):
        """请求处理完后清理记录。"""
        local = self._local.pop(group_id, None)
        if local:
            task = local.get("task")
            if task and not task.done():
                task.cancel()
        self._b.delete(_key(group_id))

    # ---------- 本会话已批准记忆（批量场景免重复打扰） ----------

    def remember_allowed(self, session_id: str, owner_id: int, signatures: list):
        """把用户已批准的「工具+参数」签名记入本会话白名单。

        批量场景的关键：同一个操作（同工具+同参数）用户批过一次，之后重复
        执行不再弹确认。签名不同（参数变了）仍会询问 —— 放行的只是
        「用户明确批过的那个操作」，不是整个工具。
        """
        if not signatures:
            return
        key = _allow_key(session_id)
        cur = self._b.get_json(key) or {}
        if cur.get("owner_id") != owner_id:
            cur = {"owner_id": owner_id, "sigs": []}
        sigs = set(cur.get("sigs") or []) | set(signatures)
        self._b.set_json(key, {"owner_id": owner_id, "sigs": sorted(sigs)}, ttl=ALLOW_TTL)

    def is_allowed(self, session_id: str, owner_id: int, signature: str) -> bool:
        cur = self._b.get_json(_allow_key(session_id))
        return bool(cur) and cur.get("owner_id") == owner_id \
            and signature in (cur.get("sigs") or [])

    def pending_count(self, session_id: str) -> int:
        n = 0
        for gid in self._b.keys("approval:group:"):
            rec = self._load(gid.rsplit(":", 1)[-1])
            if rec and rec.get("session_id") == session_id and not rec.get("cancelled"):
                items = rec.get("items") or []
                if len(rec.get("decisions") or {}) < len(items):
                    n += 1
        return n


# 全局单例（进程内缓存等待中的 Future；状态本身在后端）
approval_store = ApprovalStore()
