#!/usr/bin/env python3
"""loop_guard 判据单元测试。

覆盖 2026-10-23 与 2026-10-27 两轮误杀判据的回归（A~G 旧判据 + H 新调优）：
  A. 查日志式探索（参数每次都变）→ **不得**被判为空转
  B. 真重复（工具名+参数完全相同）→ **必须**被判为空转

用法：python3 scripts/test_loop_guard.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.loop_guard import evaluate, new_loop_state, _call_signature  # noqa: E402


def _no_files():
    return set()


def _files(*names):
    return set(names)


def run_case(name, rounds, expect_stop_at=None):
    """rounds: [(iter_failed, iter_perm_denied, tool_names, signatures, fail_sigs)] 逐轮"""
    state = new_loop_state()
    stopped_at = None
    for i, rnd in enumerate(rounds, 1):
        failed, denied, names, sigs = rnd[0], rnd[1], rnd[2], rnd[3]
        fail_sigs = rnd[4] if len(rnd) > 4 else None
        msg, answer = evaluate(state, _no_files, failed, denied, names, sigs, fail_sigs)
        if msg is not None:
            stopped_at = (i, msg)
            break
    ok = True
    if expect_stop_at is None:
        if stopped_at is not None:
            print(f"  ✗ {name}: 期望继续，但第 {stopped_at[0]} 轮被停：{stopped_at[1][:60]}")
            ok = False
        else:
            print(f"  ✓ {name}: 全程未误停")
    else:
        if stopped_at is None:
            print(f"  ✗ {name}: 期望第 {expect_stop_at} 轮停止，但全程未停")
            ok = False
        elif stopped_at[0] != expect_stop_at:
            print(f"  ✗ {name}: 期望第 {expect_stop_at} 轮停止，实际第 {stopped_at[0]} 轮：{stopped_at[1][:60]}")
            ok = False
        else:
            print(f"  ✓ {name}: 第 {stopped_at[0]} 轮正确停止")
    return ok


def main():
    print("=" * 62)
    print("loop_guard 判据测试")
    print("=" * 62)

    all_ok = True

    print("\n【A】查日志式探索 —— 参数每次都变，必须放行（线上误杀场景）")
    rounds = []
    for lines, logname in ((200, "app"), (500, "app"), (2000, "app"),
                           (200, "error"), (500, "app.log.2026-08-06")):
        args = {"log_name": logname, "lines": lines}
        sig = _call_signature("diag_read_logs", args)
        rounds.append((False, False, ["diag_read_logs"], [sig]))
    all_ok &= run_case("参数递变的日志探查", rounds, expect_stop_at=None)

    print("\n【B】真重复 —— 工具名+参数完全相同，必须停（连续 3 轮）")
    sig = _call_signature("diag_read_logs", {"log_name": "app", "lines": 200})
    rounds = [(False, False, ["diag_read_logs"], [sig]) for _ in range(5)]
    all_ok &= run_case("完全相同的重复调用", rounds, expect_stop_at=3)

    print("\n【C】同工具但参数不同 —— 必须放行")
    rounds = []
    for lines in (200, 201, 202, 203, 204):
        sig = _call_signature("diag_read_logs", {"log_name": "app", "lines": lines})
        rounds.append((False, False, ["diag_read_logs"], [sig]))
    all_ok &= run_case("仅 lines 变化", rounds, expect_stop_at=None)

    print("\n【D】诊断与其它工具交替 —— 必须放行")
    rounds = []
    for i in range(6):
        if i % 2 == 0:
            sig = _call_signature("diag_read_logs", {"log_name": "app", "lines": 200})
            rounds.append((False, False, ["diag_read_logs"], [sig]))
        else:
            sig = _call_signature("diag_service_status", {})
            rounds.append((False, False, ["diag_service_status"], [sig]))
    all_ok &= run_case("诊断工具交替调用", rounds, expect_stop_at=None)

    print("\n【E】权限不足 —— 立即硬停（不依赖轮数）")
    state = new_loop_state()
    msg, _ = evaluate(state, _no_files, False, True, ["diag_read_logs"], ["x"])
    ok = msg is not None and "权限不足" in msg
    print(f"  {'✓' if ok else '✗'} 权限不足硬停：{msg[:50] if msg else '未触发'}")
    all_ok &= ok

    print("\n【F】失败判定（2026-10-24 改判据：同一调用反复失败才算死循环）")
    # F1 不同签名失败 5 次 —— 合法场景（批量容错/参数修正），不得停
    rounds = [(True, False, ["some_tool"], [f"sig{i}"], [f"sig{i}"]) for i in range(5)]
    all_ok &= run_case("不同操作各失败一次（批量容错）", rounds, expect_stop_at=None)
    # F2 同一签名反复失败 3 次 —— 真死循环，第 3 轮停
    rounds = [(True, False, ["some_tool"], ["sig_same"], ["sig_same"]) for _ in range(5)]
    all_ok &= run_case("同一操作反复失败", rounds, expect_stop_at=3)
    # F3 失败总次数宽限：8 次不同失败后停（此前 3 次就误停）
    rounds = [(True, False, ["some_tool"], [f"sig{i}"], [f"sig{i}"]) for i in range(10)]
    all_ok &= run_case("轮着换操作失败达宽限才停", rounds, expect_stop_at=8)
    # F4 失败中夹杂成功轮 —— 计数清零，不得停
    rounds = []
    for i in range(10):
        if i % 3 == 2:
            rounds.append((False, False, ["other_tool"], ["ok"], None))
        else:
            rounds.append((True, False, ["some_tool"], [f"sig{i}"], [f"sig{i}"]))
    all_ok &= run_case("失败与成功交替（计数清零）", rounds, expect_stop_at=None)
    # F5 不传失败签名（兼容旧调用方）—— 连续失败 3 次仍停
    rounds = [(True, False, ["some_tool"], [f"sig{i}"], None) for i in range(5)]
    all_ok &= run_case("无失败签名走兼容判据", rounds, expect_stop_at=3)

    print("\n【G】不传签名时走旧判据（兼容兜底）")
    state = new_loop_state()
    stopped = None
    for i in range(5):
        msg, _ = evaluate(state, _no_files, False, False, ["diag_read_logs"], None)
        if msg:
            stopped = i + 1
            break
    ok = stopped == 3
    print(f"  {'✓' if ok else '✗'} 旧判据仍在第 {stopped} 轮停止（期望 3）")
    all_ok &= ok

    # ============================================================
    print("\n【H】2026-10-27 调优：权限豁免 / 进展清零 / 无害工具")
    # ============================================================
    from agent.loop_guard import HARMLESS_TOOLS

    # H1 同一权限错误：首次豁免、第 2 次硬停
    state = new_loop_state()
    sig_a = _call_signature("save_word", {"filename": "x"})
    m1, _ = evaluate(state, _no_files, False, True, ["save_word"], [sig_a], None, [sig_a])
    e1 = state["guard_events"][-1]["rule"] if state["guard_events"] else None
    ok1 = m1 is None and e1 == "perm_absorbed"
    m2, _ = evaluate(state, _no_files, False, True, ["save_word"], [sig_a], None, [sig_a])
    ok1 = ok1 and m2 is not None and "两次" in m2
    print(f"  {'✓' if ok1 else '✗'} 同一权限错误首次豁免、第 2 次硬停")
    all_ok &= ok1

    # H2 换路子：A 被拒 → 改调 B 又被拒 → 都只豁免一次；回头再试 A（第 2 次）才停
    state = new_loop_state()
    sig_b = _call_signature("run_command", {"command": "cat /etc/passwd"})
    m1, _ = evaluate(state, _no_files, False, True, ["save_word"], [sig_a], None, [sig_a])
    m2, _ = evaluate(state, _no_files, False, True, ["run_command"], [sig_b], None, [sig_b])
    m3, _ = evaluate(state, _no_files, False, True, ["save_word"], [sig_a], None, [sig_a])
    ok2 = m1 is None and m2 is None and m3 is not None
    print(f"  {'✓' if ok2 else '✗'} 不同权限错误各豁免一次，回头再试同一种才停")
    all_ok &= ok2

    # H3 无签名兼容：拿不到签名时保持旧的立即硬停
    state = new_loop_state()
    m1, _ = evaluate(state, _no_files, False, True, ["save_word"], [sig_a], None)
    ok3 = m1 is not None
    print(f"  {'✓' if ok3 else '✗'} 无权限签名时退回立即硬停（兼容）")
    all_ok &= ok3

    # H4 有产出进展 → 失败计数清零（批量坏文件的误杀场景）
    files = set()
    state = new_loop_state(seen_files=files)
    stopped = None
    for i in range(12):   # 12 轮里每轮失败 1 次但都有新文件 —— 远超 8 次上限也不许停
        files.add(f"out_{i}.txt")
        m, _ = evaluate(state, lambda f=files: set(f), True, False,
                        ["convert"], [f"cv{i}"], [f"cv{i}"])
        if m:
            stopped = i + 1
            break
    ok4 = stopped is None
    print(f"  {'✓' if ok4 else '✗'} 每轮有新产出则失败计数清零（12 轮不停）")
    all_ok &= ok4

    # H5 无进展时 8 次上限仍然生效
    state = new_loop_state()
    stopped = None
    for i in range(10):
        m, _ = evaluate(state, _no_files, True, False,
                        ["convert"], [f"cv{i}"], [f"cv{i}"])
        if m:
            stopped = i + 1
            break
    ok5 = stopped == 8
    print(f"  {'✓' if ok5 else '✗'} 无进展时累计失败第 {stopped} 轮停止（期望 8）")
    all_ok &= ok5

    # H6 无害工具不进空转签名：每轮都查时间也不算原地打转
    state = new_loop_state()
    time_sig = _call_signature("get_datetime", {"format": "datetime"})
    stopped = None
    for i in range(5):
        m, _ = evaluate(state, _no_files, False, False, ["get_datetime"], [time_sig])
        if m:
            stopped = i + 1
            break
    ok6 = stopped is None and all(
        "get_datetime" not in s.split(":")[0] for r in state["recent_signatures"] for s in r)
    print(f"  {'✓' if ok6 else '✗'} 纯无害工具轮次不触发空转（5 轮不停且不进签名）")
    all_ok &= ok6

    # H7 真重复照旧要停：无害工具 + 同一真实调用连发 3 轮
    state = new_loop_state()
    real_sig = _call_signature("web_search", {"query": "同一句话"})
    stopped = None
    for i in range(4):
        m, _ = evaluate(state, _no_files, False, False,
                        ["get_datetime", "web_search"], [time_sig, real_sig])
        if m:
            stopped = i + 1
            break
    ok7 = stopped == 3
    print(f"  {'✓' if ok7 else '✗'} 真实调用连发第 {stopped} 轮仍判空转（期望 3）")
    all_ok &= ok7

    # H8 全部触发/豁免事件都进了 guard_events
    state2 = new_loop_state()
    evaluate(state2, _no_files, False, True, ["save_word"], [sig_a], None, [sig_a])
    events = [e["rule"] for e in state2["guard_events"]]
    ok8 = events == ["perm_absorbed"]
    print(f"  {'✓' if ok8 else '✗'} 防护事件写入 guard_events 供审计（{events}）")
    all_ok &= ok8

    print("\n" + "=" * 62)
    print("✓ 全部通过" if all_ok else "✗ 存在失败用例")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
