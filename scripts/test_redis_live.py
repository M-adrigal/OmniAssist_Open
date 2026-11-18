#!/usr/bin/env python3
"""审批 / 信任模式在**真实 Redis** 上的跨进程验证。

和 test_shared_state.py 的区别：
- 那个用「同进程两个 Store 实例共享后端」模拟两个 worker，只能证明"对象没绑死内存"
- 这个起**独立子进程**（真实的两个 PID），证明"一个进程写的状态，另一个进程看得见"

这层区别很关键：多 worker 出问题时恰恰是跨 PID 的，同进程模拟永远测不出来。

用法：
    ~/.local/redis-local/redis-local.sh start
    python3 scripts/test_redis_live.py

没起 Redis 时打印 [跳过] 并以 0 退出 —— 让 run_tests 在没装 Redis 的机器上不误报。
用 db 15（独立库），跑完清空，不碰默认库。
"""
import asyncio
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 用独立库，避免污染 db 0 里的真实数据
REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://127.0.0.1:6379/15")
os.environ["REDIS_URL"] = REDIS_URL

CHILD = str(ROOT / "scripts" / "_redis_child.py")


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


kv = _load("kv", "server/kv.py")

# server/__init__.py 会拉 fastapi，注入假包（同 test_shared_state.py）
_fake_server = type(sys)("server")
_fake_server.kv = kv
sys.modules["server"] = _fake_server
sys.modules["server.kv"] = kv

approval_mod = _load("approval_store", "server/approval_store.py")
trust_mod = _load("trust_store", "server/trust_store.py")

store = approval_mod.approval_store
trust = trust_mod.trust_store

PY = sys.executable


def child(*args, timeout=20):
    """跑一次子进程（= 另一个 worker），返回解析后的 JSON。"""
    p = subprocess.run([PY, CHILD, *args], cwd=str(ROOT), capture_output=True,
                       text=True, timeout=timeout,
                       env={**os.environ, "REDIS_URL": REDIS_URL})
    out = (p.stdout or "").strip().splitlines()
    if not out:
        raise RuntimeError(f"子进程无输出 rc={p.returncode} err={p.stderr[-400:]}")
    return json.loads(out[-1])


def main():
    ok = True
    n = 0

    def check(name, cond, extra=""):
        nonlocal ok, n
        n += 1
        print(f"  {'✓' if cond else '✗'} {name}{(' — ' + str(extra)) if extra and not cond else ''}")
        if not cond:
            ok = False

    # ---------- 0. 前置：能连上真实 Redis 吗 ----------
    try:
        backend = kv.get_backend()
        alive = backend.name == "redis"
    except Exception as e:
        print(f"[跳过] 连不上 Redis（{e}）")
        print("       先启动：~/.local/redis-local/redis-local.sh start")
        return 0

    if not alive:
        print("[跳过] REDIS_URL 已设置但后端退化成了内存 —— 检查 Redis 是否真的在跑")
        print(f"       URL: {REDIS_URL}")
        return 0

    backend._client.flushdb()   # 干净起点（独立库，不会影响真实数据）
    print(f"Redis 实测  后端={backend.name}  库={REDIS_URL.rsplit('/', 1)[-1]}\n")

    # ---------- 1. 后端确实是 Redis ----------
    print("[1] 后端选择")
    check("用的是 Redis 后端（没退化成内存）", backend.name == "redis", backend.name)
    check("子进程也是 Redis 后端", child("ping").get("backend") == "redis")

    # ---------- 2. 跨进程 KV：真两个 PID ----------
    print("\n[2] 跨进程读写（子进程写 → 主进程读）")
    child("kv-write", "probe:xproc", json.dumps({"from": "child", "v": 42}))
    got = backend.get_json("probe:xproc")
    check("主进程读到了子进程写的值", isinstance(got, dict) and got.get("v") == 42, got)
    backend.set_json("probe:xproc2", {"from": "parent"})
    check("反向：子进程读到主进程写的值",
          (child("kv-read", "probe:xproc2").get("value") or {}).get("from") == "parent")

    # ---------- 3. 审批：一个进程等，另一个进程决议 ----------
    print("\n[3] 审批等待跨进程（A 等待 / 决议被另一个进程写入）")

    async def case_approve():
        gid, fut = store.create("sess-xproc", 7, [
            {"item_id": "i1", "tool": "run_command", "risk": "exec"},
        ])
        # 子进程扮演另一个 worker：它没有本地 Future，只能靠后端看见这条记录
        proc = subprocess.Popen([PY, CHILD, "approve-wait", gid], cwd=str(ROOT),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, env={**os.environ, "REDIS_URL": REDIS_URL})
        await asyncio.sleep(0.8)      # 让子进程挂上轮询
        store.resolve_item(gid, "i1", "approve", "sess-xproc", 7)
        out, err = await asyncio.to_thread(proc.communicate, 15)
        child_res = json.loads((out or "{}").strip().splitlines()[-1]) if out.strip() else {"error": err[-200:]}
        main_res = await asyncio.wait_for(fut, 3)
        store.cleanup(gid)
        return child_res, main_res

    child_res, main_res = asyncio.run(case_approve())
    check("等待中的子进程收到了决议",
          (child_res.get("decisions") or {}).get("i1") == "approve", child_res)
    check("主进程本地 future 也拿到了", (main_res or {}).get("i1") == "approve", main_res)

    # ---------- 4. 取消会话跨进程 ----------
    print("\n[4] 取消会话跨进程（A 等待 / 另一个进程取消）")

    async def case_cancel():
        gid, fut = store.create("sess-cancel", 8, [
            {"item_id": "i1", "tool": "run_command"},
        ])
        proc = subprocess.Popen([PY, CHILD, "approve-wait", gid], cwd=str(ROOT),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, env={**os.environ, "REDIS_URL": REDIS_URL})
        await asyncio.sleep(0.8)
        store.cancel_session("sess-cancel")
        out, err = await asyncio.to_thread(proc.communicate, 15)
        child_res = json.loads((out or "{}").strip().splitlines()[-1]) if out.strip() else {"error": err[-200:]}
        main_res = await asyncio.wait_for(fut, 3)
        store.cleanup(gid)
        return child_res, main_res

    child_res, main_res = asyncio.run(case_cancel())
    check("子进程侧被按取消处理（reject）",
          (child_res.get("decisions") or {}).get("i1") == "reject", child_res)
    check("主进程侧同样收到 reject", (main_res or {}).get("i1") == "reject", main_res)

    # ---------- 5. 归属校验：知道 group_id 也不能替别人决议 ----------
    print("\n[5] 归属校验（跨进程同样生效）")

    async def case_forbid():
        gid, fut = store.create("sess-own", 9, [{"item_id": "i1"}])
        errs = {}
        for label, sess, owner in (("错的会话", "sess-other", 9),
                                   ("错的用户", "sess-own", 10)):
            try:
                store.resolve_item(gid, "i1", "approve", sess, owner)
                errs[label] = None
            except PermissionError:
                errs[label] = "forbidden"
            except Exception as e:
                errs[label] = type(e).__name__
        # 正主能决议，否则说明校验写死了
        okown = store.resolve_item(gid, "i1", "approve", "sess-own", 9)
        await asyncio.wait_for(fut, 3)
        store.cleanup(gid)
        return errs, okown

    errs, okown = asyncio.run(case_forbid())
    check("别人的会话决议被拒", errs["错的会话"] == "forbidden", errs)
    check("别的用户决议被拒", errs["错的用户"] == "forbidden", errs)
    check("正主可以决议", okown is True, okown)

    # ---------- 6. 信任模式跨进程 ----------
    print("\n[6] 信任模式跨进程（A 开启 / B 也认）")
    child("trust-set", "sess-trust", "11", "full")
    check("子进程开的完全访问，主进程看得见", trust.is_trusted("sess-trust", 11) is True)
    check("模式也是 full", trust.get("sess-trust", 11).get("mode") == "full",
          trust.get("sess-trust", 11))
    trust.set("sess-trust2", 12, True, trust.MODE_FULL)
    check("反向：子进程看到主进程开的权限",
          child("trust-get", "sess-trust2", "12").get("trusted") is True)
    check("别人的会话看不到这份权限", trust.is_trusted("sess-trust", 13) is False)

    # ---------- 7. TTL 由 Redis 原生过期（不是本地判断） ----------
    print("\n[7] TTL 到期由 Redis 自己处理")
    backend.set_json("probe:ttl", {"v": 1}, ttl=1)
    check("刚写入时读得到", backend.get_json("probe:ttl") is not None)
    time.sleep(1.4)
    check("1.4 秒后过期读不到", backend.get_json("probe:ttl") is None)
    raw_ttl = backend._client.ttl(kv_key := "trust:session:sess-trust")
    check("存活键确实带 TTL（不是永不过期）", raw_ttl > 0, f"ttl={raw_ttl}")

    # ---------- 8. 连不上时退回内存，不把服务打死 ----------
    print("\n[8] Redis 挂掉时的退化")
    kv.reset_backend(None)
    os.environ["REDIS_URL"] = "redis://127.0.0.1:6390/0"   # 故意指一个没人听的端口
    try:
        deg = kv.get_backend()
        check("退化成内存后端", deg.name == "memory", deg.name)
        deg.set_json("probe:degraded", {"v": 1})
        check("退化后仍可读写（服务不中断）",
              (deg.get_json("probe:degraded") or {}).get("v") == 1)
    except Exception as e:
        check("退化时不该抛异常", False, e)
    finally:
        os.environ["REDIS_URL"] = REDIS_URL
        kv.reset_backend(None)
    check("恢复后重新用回 Redis", kv.get_backend().name == "redis")

    # ---------- 收尾 ----------
    kv.get_backend()._client.flushdb()

    print(f"\n{'全部通过' if ok else '有失败项'}：{n} 项")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
