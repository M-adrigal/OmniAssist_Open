#!/usr/bin/env python3
"""查询 Agent 任务执行审计轨迹。

用法：
    python3 scripts/audit_query.py <session_id>          # 某会话的完整轨迹
    python3 scripts/audit_query.py --recent 20            # 最近 20 条
    python3 scripts/audit_query.py --tools                # 只看工具调用
    python3 scripts/audit_query.py --approvals            # 只看审批
    python3 scripts/audit_query.py --all                  # 读落盘文件（含历史）
    python3 scripts/audit_query.py <session_id> --json    # JSON 输出

数据来源：
  默认读内存索引（仅当前进程，实时）；
  加 --all 则读落盘文件 logs/agent_audit.log（含全部历史，JSONL）。
"""
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

spec = importlib.util.spec_from_file_location("aa", ROOT / "server" / "agent_audit.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

AUDIT_FILE = ROOT / "logs" / "agent_audit.log"


def load_from_file(session_id=None, types=None):
    """从落盘文件读（JSONL，一行一条）。文件不存在返回空。"""
    if not AUDIT_FILE.is_file():
        return []
    out = []
    try:
        with open(AUDIT_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if session_id and r.get("session_id") != session_id:
                    continue
                if types and r.get("type") not in types:
                    continue
                out.append(r)
    except Exception as e:
        print(f"读取审计文件失败: {e}", file=sys.stderr)
    out.sort(key=lambda r: r.get("ts", 0))
    return out


def fmt_item(r):
    t = r.get("type")
    ts = r.get("ts_str", "")
    if t == "turn":
        extra = ""
        if r.get("tools_used"):
            extra = f" tools={r['tools_used']}"
        if r.get("iterations"):
            extra += f" iters={r['iterations']}"
        return f"[{ts}] TURN  {r.get('event')}{extra}"
    if t == "tool":
        flag = "✗" if r.get("error") else "✓"
        args = json.dumps(r.get("arguments") or {}, ensure_ascii=False)
        if len(args) > 90:
            args = args[:90] + "…"
        dur = r.get("duration_ms")
        dur_s = f" {dur}ms" if dur is not None else ""
        dfr = " [延迟执行]" if r.get("deferred") else ""
        return (f"[{ts}] TOOL  {flag} {r.get('tool')}{dfr}{dur_s}\n"
                f"           args={args}")
    if t == "approval":
        if r.get("event") == "requested":
            items = r.get("items") or []
            names = ", ".join(
                f"{i.get('name')}({json.dumps(i.get('arguments') or {}, ensure_ascii=False)[:60]})"
                for i in items)
            return f"[{ts}] APPR  请求确认 → {names}"
        if r.get("skipped_reason"):
            return f"[{ts}] APPR  免确认执行（{r.get('skipped_reason')}）"
        dec = r.get("decisions") or {}
        wait = r.get("wait_ms")
        w = f" 等待 {wait}ms" if wait is not None else ""
        return f"[{ts}] APPR  用户决策 → {json.dumps(dec, ensure_ascii=False)}{w}"
    if t == "tool_deferred":
        tools = ", ".join(x.get("name", "?") for x in (r.get("tools") or []))
        return f"[{ts}] DEFER 延迟执行 → {tools}（{r.get('reason', '')}）"
    return f"[{ts}] {str(t).upper():6s} {json.dumps(r, ensure_ascii=False)[:120]}"


def main():
    argv = sys.argv[1:]
    args = [a for a in argv if not a.startswith("--")]
    as_json = "--json" in argv
    only_tools = "--tools" in argv
    only_approvals = "--approvals" in argv
    read_all = "--all" in argv
    recent = 20 if "--recent" in argv else None

    types = None
    if only_tools:
        types = ["tool", "tool_deferred"]
    elif only_approvals:
        types = ["approval"]

    if read_all:
        items = load_from_file(session_id=args[0] if args else None, types=types)
        src = str(AUDIT_FILE)
    else:
        audit = mod.AgentAudit()
        items = audit.query(
            session_id=args[0] if args else None,
            types=types,
            limit=5000, tail=True,
        )
        src = "内存索引（当前进程）"

    if recent:
        items = items[-recent:]

    if as_json:
        print(json.dumps(items, ensure_ascii=False, indent=2))
        return 0

    if not items:
        print("（无记录）")
        if not read_all:
            print("提示：内存索引仅含当前进程的记录。查历史请加 --all 读落盘文件。")
        else:
            print(f"      落盘文件不存在或为空: {AUDIT_FILE}")
        return 0

    print(f"来源：{src}\n")
    for r in items:
        print(fmt_item(r))
    print(f"\n共 {len(items)} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
