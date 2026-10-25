#!/usr/bin/env python3
"""三项修复的集成验证：自伤工具延迟、loop_guard 签名判据、审计落盘。

不依赖 fastapi（按文件加载模块），用真实的源码逻辑跑。

用法：python3 scripts/test_fixes_integration.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main():
    all_ok = True

    def check(name, cond, extra=""):
        nonlocal all_ok
        print(f"  {'✓' if cond else '✗'} {name}{(' — ' + str(extra)) if extra and not cond else ''}")
        if not cond:
            all_ok = False

    print("=" * 62)
    print("修复项集成验证")
    print("=" * 62)

    # ---------- 1. 自伤工具标记与延迟机制 ----------
    print("\n【1】自伤型工具隔离")
    from agent.tools import ToolRegistry
    reg = ToolRegistry()
    reg.register_tool("diag_restart_service", "重启", {"type": "object", "properties": {}},
                      lambda: "ok", risk_level="exec", block_during_stream=True)
    reg.register_tool("diag_read_logs", "读日志", {"type": "object", "properties": {}},
                      lambda: "ok", risk_level="read")
    reg.register_tool("save_word", "存文档", {"type": "object", "properties": {}},
                      lambda: "ok", risk_level="write")

    check("restart 被标记为流式中禁止", reg.blocks_during_stream("diag_restart_service") is True)
    check("读日志不受影响", reg.blocks_during_stream("diag_read_logs") is False)
    check("写操作不受影响", reg.blocks_during_stream("save_word") is False)
    check("未注册的工具返回 False", reg.blocks_during_stream("nonexistent") is False)

    # 模拟 chat.py 的划分逻辑
    def partition(tool_calls):
        safe, pending, selfharm = [], [], []
        for tc in tool_calls:
            name = tc["name"]
            args = tc.get("args", {})
            if reg.blocks_during_stream(name):
                selfharm.append({"name": name, "args": args})
                continue
            if reg.needs_approval(name, args):
                pending.append({"name": name, "args": args})
            else:
                safe.append({"name": name, "args": args})
        return safe, pending, selfharm

    s, p, sh = partition([
        {"name": "diag_restart_service", "args": {}},
        {"name": "diag_read_logs", "args": {"log_name": "app"}},
        {"name": "save_word", "args": {"filename": "x"}},
    ])
    check("restart 被摘出到自伤队列", [x["name"] for x in sh] == ["diag_restart_service"], sh)
    check("只读工具进 safe", [x["name"] for x in s] == ["diag_read_logs"], s)
    check("写工具进待审批", [x["name"] for x in p] == ["save_word"], p)

    # ---------- 2. loop_guard 签名判据 ----------
    print("\n【2】loop_guard 签名判据（真实代码）")
    from agent.loop_guard import evaluate, new_loop_state, _call_signature

    # 复现线上事故：连续查日志但参数每次都变
    state = new_loop_state()
    stopped = None
    for i, (ln, lg) in enumerate([(200, "app"), (500, "app"), (2000, "app"),
                                  (200, "error"), (500, "app.log.2026-08-06"),
                                  (2000, "app")], 1):
        sig = _call_signature("diag_read_logs", {"log_name": lg, "lines": ln})
        msg, _ = evaluate(state, lambda: set(), False, False, ["diag_read_logs"], [sig])
        if msg:
            stopped = i
            break
    check("参数递变的日志探查不被误杀", stopped is None, f"第 {stopped} 轮被停")

    # 真重复必须被停
    state = new_loop_state()
    stopped = None
    sig = _call_signature("diag_read_logs", {"log_name": "app", "lines": 200})
    for i in range(1, 6):
        msg, _ = evaluate(state, lambda: set(), False, False, ["diag_read_logs"], [sig])
        if msg:
            stopped = i
            break
    check("完全相同的重复调用被停", stopped == 3, f"第 {stopped} 轮")

    # ---------- 3. 审计落盘 ----------
    print("\n【3】审计落盘与可检索性")
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "aa", ROOT / "server" / "agent_audit.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    tmp = Path(tempfile.mkdtemp()) / "audit.log"
    audit = mod.AgentAudit(path=tmp)

    # 模拟一次真实场景：审批删技能 → 执行 → 再查库
    audit.record_turn("sess_x", 1, "start", message_len=20)
    audit.record_approval_requested("sess_x", 1, "g_1", [
        {"name": "delete_user_skill", "args": {"name": "grill_me"}}])
    audit.record_approval_decision("sess_x", 1, "g_1", {"call_1": "approve"}, wait_ms=2300)
    audit.record_tool("sess_x", 1, "delete_user_skill", arguments={"name": "grill_me"},
                      result='{"success": true, "message": "Skill \'grill_me\' 已删除"}',
                      error=False, duration_ms=8, iteration=2, risk_level="write")
    audit.record_tool("sess_x", 1, "diag_db_query",
                      arguments={"sql": "SELECT * FROM user_skills WHERE user_id = 1"},
                      result='{"success": true, "row_count": 0}', error=False,
                      duration_ms=5, iteration=3, risk_level="read")
    audit.record_turn("sess_x", 1, "finish", iterations=4)

    # 验证：能否从审计里还原"谁删了哪个技能"
    lines = tmp.read_text(encoding="utf-8").strip().split("\n")
    recs = [json.loads(l) for l in lines]
    check("6 条记录落盘", len(recs) == 6, len(recs))

    dels = [r for r in recs if r.get("tool") == "delete_user_skill"]
    check("能查到删除操作", len(dels) == 1)
    if dels:
        check("记录了被删的技能名", dels[0]["arguments"]["name"] == "grill_me", dels[0])
    aps = [r for r in recs if r.get("type") == "approval"]
    check("记录了 2 条审批事件", len(aps) == 2, len(aps))
    req = [r for r in aps if r.get("event") == "requested"][0]
    check("审批请求含工具名与参数",
          req["items"][0]["name"] == "delete_user_skill"
          and req["items"][0]["arguments"]["name"] == "grill_me", req)
    dec = [r for r in aps if r.get("event") == "decided"][0]
    check("审批决策含用户选择", dec["decisions"] == {"call_1": "approve"}, dec)
    check("审批决策含等待时长", dec["wait_ms"] == 2300, dec)

    # 按 session 检索
    tl = audit.session_timeline("sess_x")
    check("按 session 检索到完整轨迹", len(tl) == 6, len(tl))
    check("时间有序", all(tl[i]["ts"] <= tl[i + 1]["ts"] for i in range(len(tl) - 1)))

    # 4. 模拟原事故：没有审计时能查到什么 vs 有审计时
    print("\n【4】对比：这次事故现在能否查到")
    qs = audit.query(session_id="sess_x", types=["tool"])
    check("可按类型筛出工具调用", len(qs) == 2, len(qs))
    check("一次会话的完整工具链可还原",
          [r["tool"] for r in qs] == ["delete_user_skill", "diag_db_query"],
          [r["tool"] for r in qs])

    print()
    print("=" * 62)
    print("✓ 全部通过" if all_ok else "✗ 存在失败用例")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
