"""跨进程共享的键值存储（审批状态、信任模式用）。

## 为什么需要

审批状态与"信任模式"原来存在进程内存里。单 worker 没问题，一旦 `gunicorn -w N`
或 `uvicorn --workers N`，就会出现这种情况：

    用户在 A 进程发出敏感操作 → A 挂着等确认 → 用户点"确认"
    → 这个 POST /approve 被负载均衡打到 B 进程 → B 没有这条记录 → 404
    → A 那边永远等不到结果（600 秒后超时）

表现为"点了确认没反应、最后任务超时失败"，而且**不报错**。

## 用法

设置 `REDIS_URL`（如 `redis://127.0.0.1:6379/0`）即启用 Redis；
不设、或 redis 包没装、或连不上，自动退回进程内实现并打一次告警。

**退化是刻意的**：缓存层不可用时宁可退回"单 worker 行为"，
也不能因为 Redis 挂了把对话/审批整个打死。

## 接口

故意只做四个方法（set_json / get_json / delete / keys），够用且好替换。
写操作只保证"单键最终一致"，不做事务 —— 审批场景里每个 item 只会被点一次，
极端并发下最坏是重复决策，不是数据损坏。
"""
import json
import os
import threading
import time

from agent.logger import get_logger

logger = get_logger(__name__)

_backend = None
_backend_lock = threading.Lock()


class MemoryBackend:
    """进程内实现。多 worker 时各进程独立（等于退化），单 worker 下语义等价。"""

    name = "memory"

    def __init__(self):
        self._data = {}
        self._lock = threading.Lock()

    def set_json(self, key: str, obj, ttl: int = None):
        with self._lock:
            expire_at = (time.time() + ttl) if ttl else None
            self._data[key] = (expire_at, json.dumps(obj, ensure_ascii=False))

    def get_json(self, key: str):
        with self._lock:
            item = self._data.get(key)
            if not item:
                return None
            expire_at, raw = item
            if expire_at and expire_at <= time.time():
                self._data.pop(key, None)
                return None
        try:
            return json.loads(raw)
        except Exception:
            return None

    def delete(self, key: str):
        with self._lock:
            self._data.pop(key, None)

    def keys(self, prefix: str = ""):
        now = time.time()
        with self._lock:
            for k in [k for k, (exp, _) in self._data.items() if exp and exp <= now]:
                self._data.pop(k, None)
            return [k for k in self._data if k.startswith(prefix or "")]


class RedisBackend:
    """Redis 实现。redis 包缺失或连接失败时由 get_backend() 退回内存。"""

    name = "redis"

    def __init__(self, url: str):
        import redis  # 延迟导入：没装也能跑（退化成内存）
        self._client = redis.Redis.from_url(url, decode_responses=True,
                                            socket_timeout=3, socket_connect_timeout=3)
        self._client.ping()   # 连不上就抛，交给上层退化

    def set_json(self, key: str, obj, ttl: int = None):
        payload = json.dumps(obj, ensure_ascii=False)
        if ttl:
            self._client.set(key, payload, ex=int(ttl))
        else:
            self._client.set(key, payload)

    def get_json(self, key: str):
        raw = self._client.get(key)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return None

    def delete(self, key: str):
        self._client.delete(key)

    def keys(self, prefix: str = ""):
        # 备注：生产规模下 SCAN 更稳；这里用 keys 是因为前缀很窄、键量极小
        # （每个待确认分组 / 每个活跃会话一条），且都在 TTL 内自然过期。
        return list(self._client.scan_iter(match=f"{prefix}*", count=200))


def get_backend():
    """取全局后端（单例）。首次调用时决定用 Redis 还是内存。"""
    global _backend
    if _backend is not None:
        return _backend
    with _backend_lock:
        if _backend is not None:
            return _backend
        url = (os.environ.get("REDIS_URL") or "").strip()
        if not url:
            _backend = MemoryBackend()
            logger.info("审批/信任状态使用进程内存储（未配置 REDIS_URL；多 worker 需配 Redis）")
            return _backend
        try:
            _backend = RedisBackend(url)
            logger.info(f"审批/信任状态使用 Redis: {url.split('@')[-1]}")
        except Exception as e:
            _backend = MemoryBackend()
            logger.warning(
                f"Redis 不可用，已退回进程内存储（多 worker 下审批会跨进程丢失）: {e}"
            )
        return _backend


def reset_backend(backend=None):
    """给测试用：替换后端实例。"""
    global _backend
    with _backend_lock:
        _backend = backend
