#!/usr/bin/env python3
"""导出活体工具注册表：name / risk_level / require_approval / block_during_stream。

临时诊断脚本：跑 init_services() 后遍历 _tool_registry，给出与线上一致的
真实审批口径（静态 grep 看不到技能脚本这类动态注册）。
"""
import sys

sys.path.insert(0, ".")
import server.main as m  # noqa: E402

m.init_services()
reg = m._tool_registry
rows = []
for name, info in reg.tools.items():
    rows.append((name, info.get("risk_level", "safe"),
                 bool(info.get("require_approval")),
                 bool(info.get("block_during_stream"))))
rows.sort()
print(f"{'工具':36}{'risk':8}{'审批':6}{'禁流中':6}")
for n, r, a, b in rows:
    print(f"{n:36}{r:8}{'Y' if a else '-':6}{'Y' if b else '-':6}")
print(f"\n共 {len(rows)} 个工具")
from collections import Counter
print("risk 分布:", dict(Counter(r for _, r, _, _ in rows)))
