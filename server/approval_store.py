"""敏感操作审批状态机（进程内，单事件循环安全）。

设计要点：
- 以 session_id + group_id 隔离每个会话的审批请求，多用户并发互不影响。
- 每个待确认分组持有一个 asyncio.Future；Agent 循环在工具执行前 await 该 Future，
  POST /approve 端点（同一事件循环内）set_result 后即恢复执行 —— 不阻塞事件循环。
- 多进程部署（如 gunicorn -w N）注意：本实现为单进程内存态，跨进程不共享；
  若启用多 worker，需替换为 Redis 等跨进程共享存储（接口保持一致即可）。
"""
import asyncio
import time
import uuid


class ApprovalStore:
    def __init__(self):
        # group_id -> {"future", "session_id", "owner_id", "items", "created_at"}
        self._groups: dict[str, dict] = {}

    def create(self, session_id: str, owner_id: int, items: list) -> tuple[str, asyncio.Future]:
        """创建一组待确认请求，返回 (group_id, future)。"""
        loop = asyncio.get_running_loop()
        gid = "g_" + uuid.uuid4().hex[:12]
        fut = loop.create_future()
        self._groups[gid] = {
            "future": fut,
            "session_id": session_id,
            "owner_id": owner_id,
            "items": items,
            "created_at": time.time(),
            "decisions": {},  # 逐项累积的决议，凑齐全部项后释放 future
        }
        return gid, fut

    async def wait(self, group_id: str) -> dict:
        """等待该分组的决议（decisions 字典）。组不存在则抛 KeyError。"""
        g = self._groups.get(group_id)
        if not g:
            raise KeyError("not_found")
        return await g["future"]

    def resolve(self, group_id: str, decisions: dict, session_id: str, owner_id: int) -> bool:
        """兼容旧路径：一次性写入整组决议并释放 future。"""
        g = self._groups.get(group_id)
        if not g:
            raise KeyError("not_found")
        if g["session_id"] != session_id or g["owner_id"] != owner_id:
            raise PermissionError("forbidden")
        if g["future"].done():
            return False
        g["decisions"].update(decisions)
        g["future"].set_result(dict(g["decisions"]))
        return True

    def resolve_item(self, group_id: str, item_id: str, decision: str, session_id: str, owner_id: int) -> bool:
        """增量写入单项决议；当全部项均已决策时释放 future。

        支持 decision: approve | reject | skip。
        """
        g = self._groups.get(group_id)
        if not g:
            raise KeyError("not_found")
        if g["session_id"] != session_id or g["owner_id"] != owner_id:
            raise PermissionError("forbidden")
        if g["future"].done():
            return False
        g["decisions"][item_id] = decision
        remaining = {it["item_id"] for it in g["items"]} - set(g["decisions"].keys())
        if not remaining:
            g["future"].set_result(dict(g["decisions"]))
        return True

    def cancel_session(self, session_id: str):
        """任务被停止/取消时，拒绝该会话所有待确认请求并清理。"""
        for gid, g in list(self._groups.items()):
            if g["session_id"] == session_id:
                if not g["future"].done():
                    g["future"].set_result(
                        {it["item_id"]: "reject" for it in g["items"]}
                    )
                self._groups.pop(gid, None)

    def cleanup(self, group_id: str):
        """请求处理完后清理记录。"""
        self._groups.pop(group_id, None)

    def pending_count(self, session_id: str) -> int:
        return sum(1 for g in self._groups.values() if g["session_id"] == session_id)


# 全局单例（单进程内共享）
approval_store = ApprovalStore()
