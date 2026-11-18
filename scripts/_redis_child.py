#!/usr/bin/env python3
"""test_redis_live.py 的「另一个 worker」进程。

为什么要单独一个进程：同进程里两个 Store 实例共享后端只能证明"对象没绑死内存"，
证明不了"两个 PID 之间能看见对方"。多 worker 出问题时恰恰是跨 PID 的。

`server/__init__.py` 里有 `from .main import app`（会拉 fastapi），所以这里
和 test_shared_state.py 一样注入一个假的 server 包，只放 kv 进去。

用法（由测试脚本调用，不手工执行）：
    python3 scripts/_redis_child.py ping
    python3 scripts/_redis_child.py kv-write <key> <json>
    python3 scripts/_redis_child.py kv-read <key>
    python3 scripts/_redis_child.py approve-wait <gid>
    python3 scripts/_redis_child.py trust-set <sid> <owner_id> <mode>
    python3 scripts/_redis_child.py trust-get <sid> <owner_id>
"""
import asyncio
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


kv = _load("kv", "server/kv.py")

_fake_server = type(sys)("server")
_fake_server.kv = kv
sys.modules["server"] = _fake_server
sys.modules["server.kv"] = kv

approval_mod = _load("approval_store", "server/approval_store.py")
trust_mod = _load("trust_store", "server/trust_store.py")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "ping"
    backend = kv.get_backend()

    if cmd == "ping":
        print(json.dumps({"backend": backend.name}))

    elif cmd == "kv-write":
        key, raw = sys.argv[2], sys.argv[3]
        backend.set_json(key, json.loads(raw))
        print(json.dumps({"ok": True, "backend": backend.name}))

    elif cmd == "kv-read":
        print(json.dumps({"value": backend.get_json(sys.argv[2]),
                          "backend": backend.name}, ensure_ascii=False))

    elif cmd == "approve-wait":
        gid = sys.argv[2]
        store = approval_mod.approval_store

        async def _run():
            # 超时保护：拿不到就明文报出来，别让主进程等到天荒地老
            try:
                decisions = await asyncio.wait_for(store.wait(gid), timeout=8)
                return {"decisions": decisions}
            except asyncio.TimeoutError:
                return {"decisions": None, "error": "timeout"}
            except KeyError:
                return {"decisions": None, "error": "not_found"}

        print(json.dumps(asyncio.run(_run()), ensure_ascii=False))

    elif cmd == "trust-set":
        sid, owner, mode = sys.argv[2], int(sys.argv[3]), sys.argv[4]
        # 显式传 enabled/mode：只传 mode 会落到 enabled 位置上（truthy 巧合为真）
        ok = trust_mod.trust_store.set(sid, owner, mode == "full", mode)
        print(json.dumps({"ok": ok, "backend": backend.name}))

    elif cmd == "trust-get":
        sid, owner = sys.argv[2], int(sys.argv[3])
        print(json.dumps({
            "trusted": trust_mod.trust_store.is_trusted(sid, owner),
            "mode": trust_mod.trust_store.get(sid, owner),
        }, ensure_ascii=False))

    else:
        print(json.dumps({"error": f"unknown cmd: {cmd}"}))
        sys.exit(2)


if __name__ == "__main__":
    main()
