#!/usr/bin/env python3
"""前端类型检查（tsc --noEmit）。没装 node 依赖时跳过，不算失败。

为什么要有这一步：
  static/js 是原生 JS 平铺加载，210 个顶层符号全在同一个全局作用域里。这类
  结构最常见的两类事故——顶层重名静默覆盖、跨文件调用拼错名字——`check_static.py`
  只能查前者（靠正则比对声明），后者要到运行时才炸。

  tsc 在 checkJs 模式下能同时抓住两者，还能发现属性名笔误、参数个数不对、
  可选性不兼容。这些正是「AI 生成代码里 94% 的编译错误都是类型不匹配」所指的那类。

  **它不产出任何文件**（noEmit），浏览器直接加载原来的 .js，没有构建步骤。

依赖：node + `npm i`（或 `npm i -g typescript`）。缺任一者本脚本跳过并说明原因，
不阻塞其它测试——CI 里没装 node 是常态，不该因此判定整个套件失败。
"""
import os
import pathlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
TSCONFIG = ROOT / "tsconfig.json"


def _find_tsc():
    """优先用项目本地 tsc，其次全局，最后 npx。

    返回 str 或 None。注意不用 `str | None` 注解：3.9 不支持该写法，
    本项目要兼容 3.10~3.14 且部分机器上跑的是 3.9。
    """
    local = ROOT / "node_modules" / ".bin" / "tsc"
    if local.exists():
        return str(local)
    g = shutil.which("tsc")
    if g:
        return g
    if shutil.which("npx"):
        return "npx"   # 交给 npx 现场找（可能触发下载，仅在全局没有时才走）
    return None


def main() -> int:
    if not TSCONFIG.exists():
        print("缺少 tsconfig.json，跳过")
        return 0
    if not shutil.which("node"):
        print("未安装 node，跳过（前端类型检查非必需）")
        return 0

    tsc = _find_tsc()
    if not tsc:
        print("找不到 tsc。装一下：npm i   或   npm i -g typescript")
        return 0

    env = dict(os.environ)
    env.setdefault("NODE_NO_WARNINGS", "1")

    cmd = ([tsc, "--noEmit", "-p", str(TSCONFIG)] if tsc != "npx"
           else ["npx", "--no-install", "tsc", "--noEmit", "-p", str(TSCONFIG)])

    try:
        proc = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True,
                              text=True, timeout=180)
    except subprocess.TimeoutExpired:
        print("tsc 超时（180s）")
        return 1
    except FileNotFoundError:
        print("tsc 无法执行，跳过")
        return 0

    out = (proc.stdout or "") + (proc.stderr or "")
    # npx --no-install 找不到包时会非零退出，这种情况按"没装"处理
    if proc.returncode != 0 and ("could not determine executable" in out.lower()
                                 or "not found" in out.lower() and "error TS" not in out):
        print("tsc 不可用（未安装），跳过")
        return 0

    errs = [ln for ln in out.splitlines() if "error TS" in ln]
    if not errs:
        print(f"✓ 前端类型检查通过（{_count_files()} 个文件，0 错误）")
        return 0

    print(f"✗ 前端类型检查发现 {len(errs)} 个错误：")
    for ln in errs[:40]:
        print("   ", ln.replace(str(ROOT) + os.sep, ""))
    if len(errs) > 40:
        print(f"    …… 另有 {len(errs) - 40} 条")
    return 1


def _count_files() -> int:
    return len(list((ROOT / "static" / "js").glob("*.js")))


if __name__ == "__main__":
    sys.exit(main())
