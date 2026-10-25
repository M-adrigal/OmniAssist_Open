#!/usr/bin/env python3
"""静态冒烟检查：一次跑完三类问题，供拆分/重构前后对比使用。

用法：
    python3 scripts/check_static.py            # 检查并打印报告
    python3 scripts/check_static.py --json     # 输出 JSON（供程序消费）

检查项：
    1. CSS 变量：被 var(--x) 引用但在任何样式文件里都没定义 --x
    2. JS 顶层重名：同一文件或跨文件出现同名顶层定义（后者会静默覆盖）
    3. HTML 引用缺失：link/script 引用的本地文件不存在
    4. 选择器笔误：$('xxx') 少了 # 或 . —— $ 是 querySelector 语义，
       少了前缀永远查不到元素，函数里 `if (!el) return` 会静默变成空操作
       （2026-10-24 回收站「永远加载中」就是这个原因，页面不报错、没人发现）
退出码：0 = 全部通过，1 = 有 ERROR 级问题
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
CSS_DIRS = [STATIC, STATIC / "css"]
JS_DIRS = [STATIC, STATIC / "js"]


def rel(p: Path) -> str:
    try:
        return str(p.relative_to(ROOT))
    except ValueError:
        return str(p)


def collect_css_files():
    out = []
    for d in CSS_DIRS:
        if d.exists():
            out.extend(sorted(d.glob("*.css")))
    return out


def collect_js_files():
    out = []
    for d in JS_DIRS:
        if d.exists():
            out.extend(sorted(d.glob("*.js")))
    return out


# ---------- 1. CSS 变量 ----------
def check_css_vars(css_files):
    defined = set()
    used = []  # (file, var_name, line)
    for f in css_files:
        text = f.read_text(encoding="utf-8", errors="replace")
        for i, line in enumerate(text.split("\n"), 1):
            for m in re.finditer(r"(--[A-Za-z0-9_-]+)\s*:", line):
                defined.add(m.group(1))
            for m in re.finditer(r"var\(\s*(--[A-Za-z0-9_-]+)", line):
                used.append((f, m.group(1), i))

    missing = []
    seen = set()
    for f, name, ln in used:
        if name not in defined:
            key = (rel(f), name)
            if key in seen:
                continue
            seen.add(key)
            missing.append({"file": rel(f), "var": name, "line": ln})
    return {"defined": len(defined), "used": len(used), "missing": missing}


# ---------- 2. JS 顶层重名 ----------
TOP_PATTERNS = [
    (re.compile(r"^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)"), "function"),
    (re.compile(r"^(?:const|let|var)\s+([A-Za-z_$][\w$]*)"), "var"),
    (re.compile(r"^class\s+([A-Za-z_$][\w$]*)"), "class"),
]


def top_level_names(text):
    """粗略提取顶层定义名：只在 depth==0 的行上匹配（跳过函数体内部）。"""
    names = []
    depth = 0
    for i, line in enumerate(text.split("\n"), 1):
        if depth == 0:
            s = line.rstrip()
            if s and not s.startswith((" ", "\t", "//", "*", "/*")):
                for pat, kind in TOP_PATTERNS:
                    m = pat.match(s)
                    if m:
                        names.append((m.group(1), kind, i))
                        break
        depth += line.count("{") - line.count("}")
        if depth < 0:
            depth = 0
    return names


def check_js_dupes(js_files):
    per_file = {}
    for f in js_files:
        text = f.read_text(encoding="utf-8", errors="replace")
        per_file[rel(f)] = top_level_names(text)

    issues = []
    for fname, names in per_file.items():
        seen = {}
        for name, kind, ln in names:
            if name in seen:
                issues.append({
                    "scope": "同文件",
                    "file": fname,
                    "name": name,
                    "lines": [seen[name], ln],
                })
            else:
                seen[name] = ln

    # 跨文件重名（只对多个 js 文件的情况）
    if len(js_files) > 1:
        owner = {}
        for fname, names in per_file.items():
            for name, kind, ln in names:
                owner.setdefault(name, []).append((fname, ln))
        for name, places in owner.items():
            if len(places) > 1:
                issues.append({
                    "scope": "跨文件",
                    "file": ", ".join(p[0] for p in places),
                    "name": name,
                    "lines": [p[1] for p in places],
                })
    return issues


# ---------- 3. HTML 引用完整性 ----------
def check_html_refs():
    issues = []
    for f in sorted(STATIC.glob("*.html")):
        text = f.read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(r'<link[^>]*href="([^"]+)"', text):
            href = m.group(1)
            if href.startswith(("http://", "https://", "data:", "#")):
                continue
            # /favicon.ico 没有对应磁盘文件，由 server/main.py 的路由动态返回 static/favicon.svg
            if href == "/favicon.ico":
                continue
            target = href.lstrip("/")
            p = ROOT / target if href.startswith("/") else (f.parent / href)
            if not p.exists():
                issues.append({"file": rel(f), "kind": "link", "ref": href})
        for m in re.finditer(r'<script[^>]*src="([^"]+)"', text):
            src = m.group(1)
            if src.startswith(("http://", "https://", "data:", "#")):
                continue
            target = src.lstrip("/")
            p = ROOT / target if src.startswith("/") else (f.parent / src)
            if not p.exists():
                issues.append({"file": rel(f), "kind": "script", "ref": src})
    return issues


# ---------- 4. 选择器笔误 ----------
def check_selectors(js_files):
    """$('xxx') / $$('xxx') 里没有 # 或 . 的，基本是笔误。

    只报「纯标识符」形式（如 'skill-trash-list'）；带空格/逗号的复杂选择器
    （如 '#a .b'）与标签选择器（'div'）不在此列 —— 后者合法但项目里不用，也不误报。
    """
    issues = []
    pat = re.compile(r"\$\$?\('([A-Za-z][\w-]*)'\)")
    for f in js_files:
        # 只看 static/js/ 下共享 01-core.js 那套 $（querySelector 语义）。
        # static/usage.js 自带 function $(id){getElementById}，裸 id 在那里是**正确**写法，
        # 混在一起会报一堆假阳性。
        if f.parent.name != "js":
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        if re.search(r"function\s+\$\s*\([^)]*\)\s*\{\s*return\s+document\.getElementById", text):
            continue              # 该文件自定义了 id 语义的 $
        for i, line in enumerate(text.split("\n"), 1):
            if line.lstrip().startswith(("//", "*")):
                continue          # 注释里提到的不算
            for m in pat.finditer(line):
                issues.append({"file": rel(f), "line": i, "selector": m.group(1)})
    return issues


def main():
    css_files = collect_css_files()
    js_files = collect_js_files()

    var_res = check_css_vars(css_files)
    dupes = check_js_dupes(js_files)
    refs = check_html_refs()
    selectors = check_selectors(js_files)

    report = {
        "css_files": [rel(f) for f in css_files],
        "js_files": [rel(f) for f in js_files],
        "css_vars": var_res,
        "js_dupes": dupes,
        "html_missing_refs": refs,
        "selector_typos": selectors,
    }

    if "--json" in sys.argv:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    print("=" * 60)
    print("静态冒烟检查")
    print("=" * 60)

    print(f"\n[1] CSS 变量  (样式文件 {len(css_files)} 个)")
    print(f"    已定义 {var_res['defined']} 个，被引用 {var_res['used']} 次")
    if var_res["missing"]:
        print(f"    ✗ {len(var_res['missing'])} 个引用了但未定义：")
        for m in var_res["missing"]:
            print(f"      {m['file']}:{m['line']}  var({m['var']})")
    else:
        print("    ✓ 全部引用都有定义")

    print(f"\n[2] JS 顶层重名  (脚本 {len(js_files)} 个)")
    if dupes:
        for d in dupes:
            lines = ", ".join(f"L{x}" for x in d["lines"])
            print(f"    ✗ [{d['scope']}] {d['name']}  ({d['file']} @ {lines})")
    else:
        print("    ✓ 无重名")

    print(f"\n[3] HTML 本地引用")
    if refs:
        for r in refs:
            print(f"    ✗ {r['file']} 引用了不存在的 {r['kind']}: {r['ref']}")
    else:
        print("    ✓ 全部存在")

    print(f"\n[4] 选择器写法  ($ 需带 # 或 .)")
    if selectors:
        for it in selectors:
            print(f"    ✗ {it['file']}:{it['line']}  $('{it['selector']}')  ← 少了 # 或 .")
    else:
        print("    ✓ 无笔误")

    has_err = bool(var_res["missing"] or dupes or refs or selectors)
    print("\n" + ("✗ 存在问题" if has_err else "✓ 全部通过"))
    return 1 if has_err else 0


if __name__ == "__main__":
    sys.exit(main())
