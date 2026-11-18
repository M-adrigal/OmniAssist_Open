#!/usr/bin/env python3
"""审批/信任状态的跨进程共享回归测试（零依赖，用内存后端模拟两个 worker）。

背景：审批状态与信任模式原来只存进程内存。多 worker 部署时会出现
"用户在 A 进程等待确认 → 确认请求被负载均衡打到 B 进程 → B 没有记录 → 404
→ A 永远等到 600 秒超时"，全程不报错。

本测试用**两个 Store 实例共享同一个后端**来模拟两个 worker：
能互相看见 → 跨进程方案成立；看不见 → 说明还绑在进程内存里。

用法：python3 scripts/test_shared_state.py
"""
import asyncio
import importlib.util
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


kv = _load("kv", "server/kv.py")

# approval_store / trust_store 写的是 `from server import kv`。测试环境里不导入
# 真正的 server 包（会拉进 fastapi），用一个假包把它指向上面这个 kv 模块。
_fake_server = types.ModuleType("server")
_fake_server.kv = kv
sys.modules["server"] = _fake_server
sys.modules["server.kv"] = kv

approval_mod = _load("approval_store", "server/approval_store.py")
trust_mod = _load("trust_store", "server/trust_store.py")


def main():
    ok = True
    n = 0

    def check(name, cond, extra=""):
        nonlocal ok, n
        n += 1
        print(f"  {'✓' if cond else '✗'} {name}{(' — ' + str(extra)) if extra and not cond else ''}")
        if not cond:
            ok = False

    print("\n[1] 后端基础语义（内存）")
    backend = kv.MemoryBackend()
    kv.reset_backend(backend)
    check("未配 REDIS_URL 时默认内存后端", kv.get_backend().name == "memory", kv.get_backend().name)
    backend.set_json("k1", {"a": 1}, ttl=60)
    check("写入后可读回", backend.get_json("k1") == {"a": 1})
    backend.set_json("k2", {"b": 2}, ttl=0.01)
    import time as _t
    _t.sleep(0.02)
    check("TTL 到期后读不到", backend.get_json("k2") is None)
    check("keys 前缀过滤", set(backend.keys("k")) == {"k1"}, backend.keys("k"))
    backend.delete("k1")
    check("删除生效", backend.get_json("k1") is None)

    print("\n[2] 两个 worker 共享审批状态")
    items = [{"item_id": "i1", "name": "tool_a"}, {"item_id": "i2", "name": "tool_b"}]

    async def scenario():
        worker_a = approval_mod.ApprovalStore()   # 负责等待的进程
        worker_b = approval_mod.ApprovalStore()   # 收到 /approve 的进程
        gid, _fut = worker_a.create("sess-1", 7, items)
        wait_task = asyncio.ensure_future(worker_a.wait(gid))
        await asyncio.sleep(0.3)
        check("等待中还没结果", not wait_task.done())
        # B 进程写入决议（跨进程的关键：A 要能看见）
        worker_b.resolve_item(gid, "i1", "approve", "sess-1", 7)
        await asyncio.sleep(0.3)
        check("只决定一项时不提前放行", not wait_task.done())
        worker_b.resolve_item(gid, "i2", "reject", "sess-1", 7)
        verdict = await asyncio.wait_for(wait_task, timeout=3)
        check("两项都决定后等待方拿到结果",
              verdict == {"i1": "approve", "i2": "reject"}, verdict)

        # 归属校验
        gid2, _ = worker_a.create("sess-2", 7, items)
        try:
            worker_b.resolve_item(gid2, "i1", "approve", "sess-2", 999)
            check("非所有者写决议应被拒", False)
        except PermissionError:
            check("非所有者写决议被拒", True)
        try:
            worker_b.resolve_item(gid2, "i1", "approve", "other-session", 7)
            check("跨会话写决议应被拒", False)
        except PermissionError:
            check("跨会话写决议被拒", True)

        # 取消会话
        gid3, _ = worker_a.create("sess-3", 7, items)
        t3 = asyncio.ensure_future(worker_a.wait(gid3))
        await asyncio.sleep(0.2)
        worker_b.cancel_session("sess-3")
        v3 = await asyncio.wait_for(t3, timeout=3)
        check("取消会话 → 全部按 reject 释放",
              v3 == {"i1": "reject", "i2": "reject"}, v3)
        worker_a.cleanup(gid3)
        check("cleanup 后记录消失", backend.get_json(f"approval:group:{gid3}") is None)

        # wait 一个不存在的分组
        try:
            await worker_a.wait("g_not_exist")
            check("不存在的分组应抛 KeyError", False)
        except KeyError:
            check("不存在的分组抛 KeyError", True)

    asyncio.run(scenario())

    print("\n[3] 两个 worker 共享信任模式")
    ta = trust_mod.TrustStore()
    tb = trust_mod.TrustStore()
    check("默认不是完全访问", ta.get("s1", 5)["mode"] == "request")
    check("A 进程开启完全访问", ta.set("s1", 5, mode="full") is True)
    check("B 进程能读到（跨进程关键）", tb.is_trusted("s1", 5) is True)
    check("非所有者读不到", tb.is_trusted("s1", 6) is False)
    check("非所有者改不动", tb.set("s1", 6, mode="request") is False)
    check("所有者改回请求批准", ta.set("s1", 5, mode="request") is True)
    check("B 进程看到已回退", tb.is_trusted("s1", 5) is False)
    ta.clear("s1")
    check("清理后回到默认", tb.get("s1", 5)["enabled"] is False)

    # 位置参数把模式串塞进 enabled 的老坑：非空字符串是 truthy，会被当成"已开启"
    check("set(sid, owner, \"full\") 位置传错被拒",
          ta.set("s-bad", 5, "full") is False)
    check("被拒后不留下任何状态（没被 truthy 蒙过去）",
          tb.get("s-bad", 5)["enabled"] is False and tb.get("s-bad", 5)["mode"] == "request")
    check("显式 enabled=True 仍然可用（兼容旧调用）", ta.set("s-ok", 5, True) is True)
    ta.clear("s-ok")

    print("\n[4] Redis 缺失时优雅退化")
    kv.reset_backend(None)
    import os
    _old = os.environ.pop("REDIS_URL", None)
    try:
        b = kv.get_backend()
        check("无 REDIS_URL → 内存后端（不抛异常）", b.name == "memory", b.name)
    finally:
        if _old is not None:
            os.environ["REDIS_URL"] = _old
        kv.reset_backend(None)

    print("\n" + "=" * 68)
    print(f"{'✓ 全部通过' if ok else '✗ 存在失败项'}（{n} 项检查）")
    print("=" * 68)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
