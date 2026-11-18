#!/usr/bin/env python3
"""系统提示词里点名的工具，必须真实存在。

背景（2026-10-26）：system prompt 里有 4 个工具名写错了 —— `simple_calculator`、
`get_current_datetime`、`save_to_word`、`convert_gregorian_to_lunar`，真实名字
分别是 `calculate` / `get_datetime` / `save_word` / `convert_lunar`。
模型按提示词去调，调的是不存在的工具；而连续失败 ≥3 次会触发循环防护提前收尾，
表现为"任务莫名其妙中断"。这类错误不报错、不影响启动，只能靠扫描兜住。

判据（从真实代码里反推，不维护第二份名单）：
  工具名 = agent/skills/*/scripts/*.py 的文件名（去 _ 开头的）
         ∪ server/main.py 里 register_tool 的内置工具
         ∪ agent/diagnostics.py 里的 def diag_*

用法：python3 scripts/test_prompt_tools.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROMPT_FILES = [ROOT / "server" / "routes" / "chat.py"]


def real_tools() -> set:
    names = set()
    skills_dir = ROOT / "agent" / "skills"
    for scripts_dir in skills_dir.glob("*/scripts"):
        for py in scripts_dir.glob("*.py"):
            if not py.name.startswith("_"):
                names.add(py.stem)
    # 诊断工具：def diag_xxx(...)
    diag = ROOT / "agent" / "diagnostics.py"
    if diag.exists():
        names.update(re.findall(r"^def (diag_\w+)\(", diag.read_text(encoding="utf-8"), re.M))
    # 内置工具：register_tool( ... name="xxx"
    main_py = ROOT / "server" / "main.py"
    if main_py.exists():
        names.update(re.findall(r'name="([a-z][a-z0-9_]{2,})"', main_py.read_text(encoding="utf-8")))
    return names


def mentioned_tools() -> dict:
    """从提示词里抽取"被点名当工具用"的标识符 → 出现位置。"""
    hits = {}
    for f in PROMPT_FILES:
        text = f.read_text(encoding="utf-8")
        for m in re.finditer(r"([a-z][a-z0-9_]{2,})\s*工具", text):
            line = text[: m.start()].count("\n") + 1
            hits.setdefault(m.group(1), []).append(f"{f.name}:{line}")
    return hits


def main():
    real = real_tools()
    used = mentioned_tools()
    missing = {k: v for k, v in used.items() if k not in real}

    print(f"\n真实工具 {len(real)} 个：{', '.join(sorted(real))}")
    print(f"提示词点名 {len(used)} 个：{', '.join(sorted(used))}")

    ok = True
    print("\n[1] 提示词点名的工具都存在")
    if missing:
        ok = False
        for name, where in sorted(missing.items()):
            print(f"  ✗ {name} 不存在（出现在 {', '.join(where)}）")
            # 给个最近的候选，便于直接改
            near = [r for r in real if name.split("_")[0] in r or r.split("_")[0] in name]
            if near:
                print(f"    可能是：{', '.join(sorted(near)[:4])}")
    else:
        print(f"  ✓ {len(used)} 个全部存在")

    print("\n[2] 已知易错名不再出现")
    legacy = {
        "simple_calculator": "calculate",
        "get_current_datetime": "get_datetime",
        "save_to_word": "save_word",
        "convert_gregorian_to_lunar": "convert_lunar",
        "web_fetch": "fetch_url",
    }
    for bad, good in legacy.items():
        found = [f for f in PROMPT_FILES if bad in f.read_text(encoding="utf-8")]
        if found:
            ok = False
            print(f"  ✗ 仍出现 {bad}（应为 {good}）：{[f.name for f in found]}")
        else:
            print(f"  ✓ 未出现 {bad}")

    print("\n" + "=" * 68)
    print("✓ 全部通过" if ok else "✗ 存在失败项")
    print("=" * 68)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
