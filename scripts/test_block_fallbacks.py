#!/usr/bin/env python3
"""阻断/中断时的「兜底话术」回归测试（零依赖）。

背景（2026-10-26 用户反馈）：
  1. 任务阻断时直接甩一句"请检查账号权限 / 请调整请求"，用户不知道下一步该干什么；
  2. 阻断时前端用错误文本**覆盖**已输出的正文，用户看到的内容直接消失。

本测试只锁第 1 点的文案规则（第 2 点在 static/js/04-stream.js，靠 test_frontend_flows 兜）。
判据：
  - 必须点出"问题出在哪"（如「权限不足」）；
  - 必须告诉用户"已经拿到的内容还在"（否则用户会以为白跑了）；
  - 必须给出下一步选项（换个做法 / 找管理员 / 拆小一点）；
  - 不许出现"请检查相关功能是否正常""请调整请求"这类把责任推回用户的话。

用法：python3 scripts/test_block_fallbacks.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.loop_guard import evaluate, new_loop_state, _call_signature  # noqa: E402

BLAMING = ("请检查相关功能", "请调整请求", "请检查账号权限", "请检查相关")
KEEP_WORDS = ("上面", "保留", "都在")
NEXT_STEP = ("可以", "或者", "告诉", "换")


def collect_stops():
    """把五条停止路径都跑一遍，拿到 (名称, msg, answer)。"""
    out = []

    def _no_files():
        return set()

    # 权限不足
    msg, ans = evaluate(new_loop_state(), _no_files, False, True, ["diag_x"], ["s1"], ["s1"])
    out.append(("权限不足", msg, ans))

    # 同一调用连续失败 3 次
    st = new_loop_state()
    sig = _call_signature("t", {"a": 1})
    for _ in range(3):
        msg, ans = evaluate(st, _no_files, True, False, ["t"], [sig], [sig])
    out.append(("同一操作反复失败", msg, ans))

    # 累计失败达 8 次（每轮签名都变）
    st2 = new_loop_state()
    for i in range(8):
        s = _call_signature("t", {"a": i})
        msg, ans = evaluate(st2, _no_files, True, False, ["t"], [s], [s])
    out.append(("累计失败过多", msg, ans))

    # 空转：同一组签名连续 3 轮
    st3 = new_loop_state()
    s = _call_signature("t", {"a": 1})
    for _ in range(3):
        msg, ans = evaluate(st3, _no_files, False, False, ["t"], [s], [])
    out.append(("原地重复", msg, ans))

    return [x for x in out if x[1]]


def main():
    ok = True
    n = 0

    def check(name, cond, extra=""):
        nonlocal ok, n
        n += 1
        print(f"  {'✓' if cond else '✗'} {name}{(' — ' + str(extra)) if extra and not cond else ''}")
        if not cond:
            ok = False

    stops = collect_stops()
    print(f"\n捕获到 {len(stops)} 条停止路径：{[s[0] for s in stops]}")

    for label, msg, ans in stops:
        print(f"\n[{label}]")
        check("给出的是人话（不含甩锅措辞）", not any(b in msg + ans for b in BLAMING), msg + ans)
        check("说明了原因或状态", len(msg) >= 8, msg)
        check("告诉用户已产出内容还在", any(w in ans for w in KEEP_WORDS), ans)
        check("给出下一步选项", any(w in ans for w in NEXT_STEP), ans)

    print("\n[权限不足] 保留可检索关键词（前端/日志按此判定）")
    denied = [s for s in stops if s[0] == "权限不足"]
    check("文案里仍有「权限不足」", bool(denied) and "权限不足" in denied[0][1], denied)

    print("\n" + "=" * 68)
    print(f"{'✓ 全部通过' if ok else '✗ 存在失败项'}（{n} 项检查）")
    print("=" * 68)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
