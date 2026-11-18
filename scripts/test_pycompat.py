#!/usr/bin/env python3
"""Python 语法兼容性检查：拦住「只有 3.12+ 才支持」的写法。

## 为什么需要

`server/routes/chat.py` 里曾有一行：

    yield f"data: {json.dumps({'type': 'status', 'content': f'正在调用工具: {_to_exec[0]["name"]}...'})}\\n\\n"

在 f-string 的替换字段里再嵌一个用**同种引号**的 f-string，只有 Python 3.12+
支持（PEP 701）。项目声明 3.10+，所以在 3.10 / 3.11 上导入 chat.py 就是
`SyntaxError` —— **服务根本起不来**。

它为什么能潜伏两个多月：单测不 import `server/routes/chat.py`（要 fastapi），
所以 30 项测试全绿也发现不了；而开发机是 3.13，永远复现不了。

`ast.parse(src, feature_version=(3, 10))` **抓不到这个**（3.13 的解析器在
feature_version 下仍允许嵌套同引号），所以只能自己做静态扫描。

## 规则边界（实测 3.9 / 3.13 确认过）

| 写法 | 3.9 | 判定 |
|---|---|---|
| `f"{d["k"]}"` | 失败 | **报错** |
| `f'{d['k']}'` | 失败 | **报错** |
| `f"{d['k']}"` | 通过 | 不报 |
| 三引号 f-string，内嵌双引号 | 通过 | 不报（三引号内的单个引号不终止字符串） |
| `f"{{literal}}"` | 通过 | 不报 |
| `'{"a": 1}'`（非 f） | 通过 | 不报 |

用法：python3 scripts/test_pycompat.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 字符串前缀（可空）+ 引号；前置断言防止把标识符里的 f 当前缀
_PREFIX = re.compile(r'(?<![A-Za-z0-9_])(?P<p>[rRbBuUfF]{0,2})(?P<q>"""|\'\'\'|"|\')')

SKIP_DIRS = {".git", "node_modules", "tool_sandbox", "__pycache__", ".venv",
             "venv", "data", "logs", "document_output"}


def scan(src: str):
    """返回 [(行号, 该行内容)] —— f-string 替换字段里出现外层同种引号的位置。"""
    hits = []
    i, n = 0, len(src)

    while i < n:
        ch = src[i]

        if ch == "\n":
            i += 1
            continue

        # 注释：跳到行尾（否则注释里的示例代码会被误报）
        if ch == "#":
            j = src.find("\n", i)
            i = n if j < 0 else j
            continue

        m = _PREFIX.match(src, i)
        if not m:
            i += 1
            continue

        prefix, quote = m.group("p"), m.group("q")
        is_f = "f" in prefix.lower()
        triple = len(quote) == 3
        q = quote[0]
        j = m.end()

        while j < n:
            c = src[j]

            if c == "\\":                 # 转义：跳过被转义的字符
                j += 2
                continue

            if c == "\n":
                if not triple:            # 单行字符串未闭合 → 不是字符串，退出
                    j = n
                    break
                j += 1
                continue

            if triple:
                if src.startswith(quote, j):
                    j += 3
                    break
                j += 1
                continue

            if c == q:                    # 结束引号
                j += 1
                break

            if is_f and c == "{":
                if j + 1 < n and src[j + 1] == "{":   # {{ 转义，不是替换字段
                    j += 2
                    continue
                depth, k = 1, j + 1
                while k < n and depth:
                    if src[k] == "{":
                        depth += 1
                    elif src[k] == "}":
                        depth -= 1
                    k += 1
                segment = src[j + 1:k - 1]
                if q in segment:
                    lineno = src.count("\n", 0, j) + 1
                    hits.append((lineno, src.splitlines()[lineno - 1].strip()[:100]))
                j = k
                continue

            j += 1

        i = j

    return hits


def self_check():
    """先验：扫描器本身对不对。错的检查比没有检查更糟（给出假绿）。"""
    should_hit = [
        ('x = f"{d["k"]}"', "双引号内嵌双引号"),
        ("x = f'{d['k']}'", "单引号内嵌单引号"),
        ('x = f"结果 {f"{y}"}"', "f-string 嵌套 f-string 同引号"),
    ]
    should_pass = [
        ("x = f\"{d['k']}\"", "内层用不同引号"),
        ('x = f"""{d["k"]}"""', "三引号内嵌双引号"),
        ('x = f"{{literal}}"', "双大括号转义"),
        ('x = "{\\"a\\": 1}"', "非 f-string"),
        ("x = '{\"a\": 1}'", "非 f-string（单引号）"),
        ('# x = f"{d["k"]}"', "注释里的示例"),
        ("x = f\"{ {'a': 1}['a'] }\"", "替换字段里是 dict 字面量"),
    ]

    ok = True
    for code, why in should_hit:
        hit = bool(scan(code))
        print(f"  {'✓' if hit else '✗'} 应报错 — {why}")
        ok &= hit
    for code, why in should_pass:
        hit = bool(scan(code))
        print(f"  {'✓' if not hit else '✗'} 不应报错 — {why}"
              f"{'' if not hit else '（误报：' + str(scan(code)) + '）'}")
        ok &= not hit
    return ok


def main():
    print("[1] 检查器自检（防假绿）")
    if not self_check():
        print("\n检查器本身有问题，先修它 —— 否则后面的「通过」没有意义")
        return 1

    print("\n[2] 全仓扫描（按 3.10 语法）")
    files, bad = 0, []
    for root, dirs, names in __import__("os").walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in names:
            if not name.endswith(".py"):
                continue
            p = Path(root) / name
            files += 1
            try:
                src = p.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                bad.append((str(p.relative_to(ROOT)), 0, "不是 UTF-8 编码"))
                continue
            for lineno, text in scan(src):
                bad.append((str(p.relative_to(ROOT)), lineno, text))

    for path, lineno, text in bad:
        print(f"  ✗ {path}:{lineno}  {text}")
    print(f"  扫描 {files} 个文件 → {'全部兼容 3.10+' if not bad else f'{len(bad)} 处不兼容'}")

    print("\n" + "=" * 68)
    if bad:
        print("失败：上面的写法只有 Python 3.12+ 支持，3.10/3.11 会导入即 SyntaxError")
        print("改法：把内层 f-string 先算成变量，或内层改用另一种引号")
        print("=" * 68)
        return 1
    print("✓ 通过")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    sys.exit(main())
