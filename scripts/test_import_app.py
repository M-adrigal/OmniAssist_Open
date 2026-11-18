#!/usr/bin/env python3
"""主应用加载冒烟：真正 import server.main，抓「测试全绿但服务起不来」类问题。

背景（结构性盲区，2026-10-26 两天里连环验证过两次）：
- 31 套件全绿的状态下，先后漏掉了 f-string 嵌套同引号（3.10/3.11 上**导入即
  SyntaxError**）和 shutil 禁令打死 xlsxwriter —— 因为**没有任何测试真正加载
  主程序**。语法/依赖缺失/循环导入这类问题，不 import 就永远发现不了。
- 为什么不能在测试进程里直接 import：跑全量的解释器可能是 3.9（没装 fastapi
  或不支持 3.10+ 语法），而服务实际跑在 3.14 上。所以本测试**找一台合格的
  解释器，用子进程 import**，父解释器是什么无所谓。

找一个「Python >= 3.10 且装了 fastapi」的解释器：
  依次尝试 sys.executable、常见安装路径、PATH 上的 python3.x；
  找到 → 子进程 import server.main（冒烟）+ SessionTask 断连宽限期行为回归；
  找不到 → 打印 [跳过]（这台机器本来也起不了服务），exit 0。

环境变量 AGENT_STREAM_STOP_GRACE 在行为回归里被设成 1 秒以加速验证。
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_SMOKE_SRC = "import sys; sys.path.insert(0, '.'); import server.main; print('IMPORT-OK')"

# 断连宽限期行为回归：断开→宽限到点→取消；宽限内重连→保命；完成后断开→不倒计时
_GRACE_SRC = r"""
import asyncio, os, sys
os.environ["AGENT_STREAM_STOP_GRACE"] = "1"
sys.path.insert(0, ".")
from server.routes.chat import SessionTask

async def main():
    results = []
    t = SessionTask("s1", "hi", 1)
    async def bg1():
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            t.mark_failed("cancelled")
            raise
    t._bg_task = asyncio.create_task(bg1())
    q = await t.subscribe()
    t.unsubscribe(q)
    await asyncio.sleep(1.8)
    results.append(("断开超宽限期后自动取消", t.status == "failed"))

    t2 = SessionTask("s2", "hi", 1)
    async def bg2():
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            t2.mark_failed("cancelled")
            raise
    t2._bg_task = asyncio.create_task(bg2())
    q2 = await t2.subscribe()
    t2.unsubscribe(q2)
    q3 = await t2.subscribe()      # 宽限期内重连
    await asyncio.sleep(1.4)
    results.append(("宽限期内重连则保命", t2.status == "running"))
    t2._bg_task.cancel()

    t3 = SessionTask("s3", "hi", 1)
    t3.mark_completed()
    q4 = await t3.subscribe()
    t3.unsubscribe(q4)
    results.append(("任务完成后断开不启动倒计时", t3._grace_task is None))
    print("GRACE:" + repr(results))

asyncio.run(main())
"""


def _candidate_interpreters():
    """按优先级列出候选解释器：先当前解释器，再常见路径与 PATH 上的 python3.x。"""
    seen, cands = set(), []

    def add(p):
        if p and p not in seen:
            seen.add(p)
            cands.append(p)

    add(sys.executable)
    for p in ("/usr/local/bin/python3", "/opt/homebrew/bin/python3"):
        add(p)
    for v in ("3.14", "3.13", "3.12", "3.11", "3.10"):
        add(f"/usr/local/bin/python{v}")
        add(f"/opt/homebrew/bin/python{v}")
    for v in ("3", "3.14", "3.13", "3.12", "3.11", "3.10"):
        from shutil import which
        add(which(f"python{v}"))
    return [c for c in cands if c]


def _find_capable_interpreter():
    """返回 (解释器路径, None)；全不合格时返回 (None, 原因)。"""
    for py in _candidate_interpreters():
        try:
            r = subprocess.run(
                [py, "-c", "import sys, fastapi; assert sys.version_info >= (3, 10); print('OK')"],
                capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if r.returncode == 0 and "OK" in r.stdout:
            return py, None
    return None, "没有找到「Python>=3.10 且装了 fastapi」的解释器"


def main():
    ok = True
    print("=" * 68)
    print("主应用加载冒烟（import server.main）")
    print("=" * 68)

    py, reason = _find_capable_interpreter()
    if py is None:
        print(f"[跳过] {reason} —— 本机无法起服务，冒烟无从谈起")
        return 0

    # ---- 1) import 冒烟：抓语法错误 / 依赖缺失 / 循环导入 ----
    r = subprocess.run([py, "-c", _SMOKE_SRC], cwd=ROOT,
                       capture_output=True, text=True, timeout=180)
    if "IMPORT-OK" in r.stdout:
        print(f"  ✓ import server.main 成功（{py}）")
    else:
        ok = False
        tail = "\n".join((r.stderr or r.stdout).splitlines()[-8:])
        print(f"  ✗ import 失败（{py}）—— 这类问题不 import 就发现不了：")
        print("    " + tail.replace("\n", "\n    "))
        return 1

    # ---- 2) 断连宽限期行为回归（真 asyncio，不是文本断言） ----
    r = subprocess.run([py, "-c", _GRACE_SRC], cwd=ROOT,
                       capture_output=True, text=True, timeout=120)
    if "GRACE:" in r.stdout:
        results = eval(r.stdout.split("GRACE:", 1)[1].strip(), {}, {})
        for name, good in results:
            print(f"  {'✓' if good else '✗'} 断连宽限期：{name}")
            ok = ok and good
    else:
        ok = False
        tail = "\n".join((r.stderr or "").splitlines()[-6:])
        print(f"  ✗ 断连宽限期行为回归没跑起来：{tail}")

    print("\n" + "=" * 68)
    print("✓ 全部通过" if ok else "✗ 存在失败项")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
