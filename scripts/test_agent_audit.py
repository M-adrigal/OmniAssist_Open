#!/usr/bin/env python3
"""agent_audit 模块测试：脱敏、截断、查询过滤、落盘格式。

用法：python3 scripts/test_agent_audit.py
"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# server/__init__.py 会 import fastapi（本机没装），直接按文件加载 agent_audit 模块
import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "agent_audit_standalone", ROOT / "server" / "agent_audit.py"
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
AgentAudit = _mod.AgentAudit
_redact = _mod._redact
_snapshot = _mod._snapshot


def main():
    all_ok = True

    def check(name, cond, extra=""):
        nonlocal all_ok
        print(f"  {'✓' if cond else '✗'} {name}{(' — ' + str(extra)) if extra and not cond else ''}")
        if not cond:
            all_ok = False

    print("=" * 62)
    print("agent_audit 测试")
    print("=" * 62)

    print("\n【1】密钥脱敏")
    r = _redact({"api_key": "sk-abc123", "password": "p@ss", "name": "x",
                 "nested": {"token": "t", "ok": 1}})
    check("api_key 被脱敏", r["api_key"] == "***", r)
    check("password 被脱敏", r["password"] == "***", r)
    check("嵌套 token 被脱敏", r["nested"]["token"] == "***", r)
    check("普通字段保留", r["name"] == "x" and r["nested"]["ok"] == 1, r)

    print("\n【2】长文本摘要保留头尾")
    long = "A" * 400 + "M" * 400 + "Z" * 400
    s = _snapshot(long, 200)
    check("长度受控", len(s) < 400, len(s))
    check("包含省略标记", "省略" in s)
    check("保留开头", s.startswith("A"))
    check("保留结尾", s.rstrip().endswith("Z"))

    print("\n【3】记录与查询")
    tmp = Path(tempfile.mkdtemp()) / "audit.log"
    a = AgentAudit(path=tmp)

    a.record_turn("s1", 1, "start", message_len=10)
    a.record_tool("s1", 1, "diag_read_logs", arguments={"log_name": "app", "lines": 200},
                  result="ok", error=False, duration_ms=12, iteration=1)
    a.record_approval_requested("s1", 1, "g_abc",
                                [{"name": "delete_user_skill", "args": {"name": "grill_me"}}])
    a.record_approval_decision("s1", 1, "g_abc", {"i1": "approve"}, wait_ms=1500)
    a.record_turn("s1", 1, "finish", iterations=3)

    items = a.session_timeline("s1")
    check("共 5 条记录", len(items) == 5, len(items))
    types = [i["type"] for i in items]
    check("类型齐全", types.count("turn") == 2 and types.count("tool") == 1
          and types.count("approval") == 2, types)
    check("按时间排序", all(items[i]["ts"] <= items[i + 1]["ts"] for i in range(len(items) - 1)))

    print("\n【4】按类型/用户过滤")
    check("按类型过滤", len(a.query(session_id="s1", types=["tool"])) == 1)
    check("按用户过滤", len(a.query(user_id=999)) == 0)
    check("查别的会话为空", len(a.session_timeline("s2")) == 0)

    print("\n【5】审批记录含工具名与参数（排查关键）")
    ap = [i for i in items if i["type"] == "approval" and i.get("event") == "requested"][0]
    check("记录了工具名", ap["items"][0]["name"] == "delete_user_skill", ap)
    check("记录了参数", ap["items"][0]["arguments"]["name"] == "grill_me", ap)

    dec = [i for i in items if i["type"] == "approval" and i.get("event") == "decided"][0]
    check("记录了决策", dec["decisions"] == {"i1": "approve"}, dec)
    check("记录了等待时长", dec["wait_ms"] == 1500, dec)

    print("\n【6】落盘为 JSONL，可被 grep/jq 处理")
    lines = tmp.read_text(encoding="utf-8").strip().split("\n")
    check("行数一致", len(lines) == 5, len(lines))
    parsed = [json.loads(l) for l in lines]
    check("每行都是合法 JSON", len(parsed) == 5)
    check("含时间戳字符串", all(p.get("ts_str") for p in parsed))

    print("\n【7】工具结果里的密钥也被脱敏")
    a2 = AgentAudit(path=Path(tempfile.mkdtemp()) / "a2.log")
    a2.record_tool("s3", 1, "set_user_secret",
                   arguments={"name": "x", "api_key": "sk-secret"},
                   result="已保存 api_key=sk-secret2")
    rec = a2.query(session_id="s3")[0]
    check("参数中的密钥被脱敏", rec["arguments"]["api_key"] == "***", rec)
    check("结果里的密钥未被脱敏（结果原样留证）", "sk-secret2" in rec["result"], rec)

    print()
    print("=" * 62)
    print("✓ 全部通过" if all_ok else "✗ 存在失败用例")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
