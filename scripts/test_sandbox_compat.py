#!/usr/bin/env python3
"""沙箱「兼容性 vs 安全性」回归测试（真实子进程，不是打桩）。

背景（2026-10-24）：`save_excel` 一直生成不出 xlsx。排查发现是**两个叠在一起的沙箱缺陷**：
  ① `mimetypes.init()` 要读 /etc/.../mime.types，被读禁区拦下 → openpyxl ImportError；
  ② `os.open` 被整个删掉 → tempfile 无法建临时文件 → 一切用临时文件的库（pandas/PIL/
     openpyxl）全废。
只补 ① 会立刻撞上 ②，所以两个要一起修，且必须验证「修完没有放宽边界」。

本测试做三件事：
  A 依赖可用：openpyxl 能 import、save_excel 能真的产出 xlsx、tempfile 能用
  B 边界未松：os.open 读 /etc、读密钥、越界写、删配置 一律被拒
  C 受控替代确实装回去了：每次 `import os` 之后 os.open/os.unlink 都还在（不是被钩子删没）

用法：python3 scripts/test_sandbox_compat.py
"""
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PROBE_ID = "u_sbx_probe"     # 专用探针目录，跑完即删，不碰任何真实用户目录


def main():
    from agent.sandbox import SandboxPool

    ok = True

    def check(name, cond, extra=""):
        nonlocal ok
        print(f"  {'✓' if cond else '✗'} {name}{(' — ' + str(extra)) if extra and not cond else ''}")
        if not cond:
            ok = False

    sb = SandboxPool().get(1)

    def run(code):
        return sb.execute(code, {}, user_id=1, public_id=PROBE_ID, tool_name="compat_probe")

    print("=" * 68)
    print("沙箱兼容性 × 安全性回归")
    print("=" * 68)

    # ---------- A 依赖可用 ----------
    print("\n【A】依赖可用性")
    out = run(
        "import os, tempfile\n"
        "def execute():\n"
        "    import openpyxl\n"
        "    fd = os.open(os.path.join(tempfile.gettempdir(), 'sbx_ok.txt'),\n"
        "                 os.O_CREAT | os.O_WRONLY, 0o600)\n"
        "    os.write(fd, b'ok'); os.close(fd)\n"
        "    return 'openpyxl=%s tmpdir=%s' % (openpyxl.__version__, tempfile.gettempdir())\n"
    )
    check("openpyxl 可 import 且 tempfile 可用", out.startswith("openpyxl="), out[:200])

    # save_excel 端到端：真的写盘
    excel_src = (ROOT / "agent/skills/document/scripts/save_excel.py").read_text(encoding="utf-8")
    out = run_src(sb, excel_src, {"filename": "sbx_probe", "headers": "项目", "rows": "买牛奶"})
    produced = list((ROOT / "document_output" / PROBE_ID).rglob("sbx_probe.xlsx"))
    check("save_excel 正常返回成功", "已成功保存" in out, out[:200])
    check("xlsx 真的落盘且非空", bool(produced) and produced[0].stat().st_size > 1000,
          produced[0].stat().st_size if produced else "无文件")

    # ---------- B 边界未松 ----------
    print("\n【B】边界未松（os.open 不能成为绕过口）")
    out = run(
        "import os\n"
        "def execute():\n"
        "    r = []\n"
        "    def t(label, fn):\n"
        "        try:\n"
        "            fn(); r.append(label + '=LEAK')\n"
        "        except PermissionError:\n"
        "            r.append(label + '=denied')\n"
        "        except Exception as e:\n"
        "            r.append(label + '=' + type(e).__name__)\n"
        "    t('read_etc', lambda: os.open('/etc/passwd', os.O_RDONLY))\n"
        "    t('read_secret', lambda: os.open(%r, os.O_RDONLY))\n"
        "    t('write_out', lambda: os.open(%r, os.O_CREAT | os.O_WRONLY, 0o600))\n"
        "    t('unlink_cfg', lambda: os.unlink(%r))\n"
        "    return '|'.join(r)\n" % (
            str(ROOT / "data" / ".db_secret"),
            str(Path.home() / "sbx_evil.txt"),
            str(ROOT / "config.yaml"),
        )
    )
    check("读 /etc 被拒", "read_etc=denied" in out, out)
    check("读密钥文件被拒", "read_secret=denied" in out, out)
    check("越界写被拒", "write_out=denied" in out, out)
    check("删除配置被拒", "unlink_cfg=denied" in out, out)

    # ---------- C 受控替代持续生效 ----------
    print("\n【C】每次 import os 后受控函数仍在")
    out = run(
        "import os\n"
        "def execute():\n"
        "    return 'open=%s unlink=%s makedirs=%s' % (\n"
        "        hasattr(os, 'open'), hasattr(os, 'unlink'), hasattr(os, 'makedirs'))\n"
    )
    check("import os 后 os.open / os.unlink / os.makedirs 都在", "open=True unlink=True makedirs=True" in out, out)
    out = run("import os\ndef execute():\n    return 'kill=%s system=%s' % (hasattr(os,'kill'), hasattr(os,'system'))\n")
    check("危险函数仍然删除（kill / system 不可用）", "kill=False system=False" in out, out)

    # ---- D. PATH 兜底（2026-10-26 线上踩到）----
    # systemd 单元里 Environment="PATH=.../venv/bin" 一旦显式赋值，systemd 就不再
    # 补系统默认路径。结果服务进程 PATH 里没有 /bin、/usr/bin，沙箱内
    # run_command("echo") 抛 FileNotFoundError —— 报错像"命令不存在"，其实是 PATH 缺目录。
    # 本地终端 PATH 正常，所以这条只能在"父进程 PATH 被精简"下才测得出来。
    print("\n[D] PATH 兜底：父进程 PATH 被精简时，沙箱仍找得到二进制")
    from agent.sandbox import ToolSandbox
    _saved_path = os.environ.get("PATH")
    os.environ["PATH"] = "/nonexistent/venv/bin"
    try:
        _p = ToolSandbox._system_path("/sandbox/venv/bin").split(":")
        check("_system_path 补进系统目录", "/usr/bin" in _p and "/bin" in _p, _p)
        check("沙箱 venv/bin 仍排第一", _p[0] == "/sandbox/venv/bin", _p)
        out = run("def execute():\n    return run_command('echo sandbox-path-ok')\n")
        check("父进程 PATH 精简时 run_command echo 仍可执行",
              "sandbox-path-ok" in out, out)
    finally:
        if _saved_path is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = _saved_path

    # ============================================================
    print("\n[E] 带参数命令不被路径校验误伤（macOS 的 TMPDIR 在 /var 下）")
    # ============================================================
    # 曾经的 bug：把每个参数当相对路径拼到 cwd，再比对「禁读前缀」。
    # macOS 的工作目录是 /private/var/folders/...，而 /var 在禁读前缀里 →
    # echo hi 的 hi 被拼成 cwd/hi 命中 /var，报「禁止访问受限路径: hi」。
    # 结果：**任何带参数的命令都跑不了**（ls -la 的 -la 同样被拒）。
    # Linux 的 TMPDIR 是 /tmp 不在 /var 下，所以这个 bug 只在 macOS 露头 ——
    # 服务器上一切正常，本地全挂，最容易误判成"本地环境有问题"。
    out = run("def execute():\n    return run_command('echo hi')\n")
    check("echo hi 能执行（参数不被当成受限路径）",
          "hi" in out and "禁止访问受限路径" not in out, out)

    out = run("def execute():\n    return run_command('ls -la')\n")
    check("ls -la 能执行（-la 不被当成受限路径）",
          "禁止访问受限路径" not in out, out)

    # 安全边界不能被这次修改带松：绝对路径和父目录遍历仍必须拒
    out = run("def execute():\n    return run_command('cat /etc/passwd')\n")
    check("cat 绝对路径仍被拒", "禁止访问绝对路径" in out, out)

    out = run("def execute():\n    return run_command('cat ../../../etc/passwd')\n")
    check("cat 父目录遍历仍被拒", "父目录遍历" in out, out)

    out = run("def execute():\n    return run_command('cat .tool_secrets')\n")
    check("cat 密钥文件仍被拒", "禁止访问密钥文件" in out, out)

    # ============================================================
    print("\n[F] 白名单报错必须自带出路（否则模型只能瞎猜连环试错）")
    # ============================================================
    # 2026-10-26 评估跑批发现：SKILL.md 教模型 execute("python3 -c 'print(1+1)'")，
    # 但沙箱刻意禁解释器；报错又只有一句「命令不在白名单」→ 模型只能换着猜
    # （cd / nslookup / cal / awk…），每猜一次浪费一轮。
    # 修复后：报错要列出可用命令、点明解释器被禁的原因、给出替代方案。
    out = run("def execute():\n    return run_command('python3 --version')\n")
    check("python3 报错列出可用命令", "可用命令" in out and "grep" in out, out)
    check("python3 报错指明替代写法", "execute() 函数体内" in out, out)

    out = run("def execute():\n    return run_command(\"awk 'BEGIN{print 1}'\")\n")
    check("awk 报错给出被禁原因与替代", "system()" in out and "grep/sed/cut" in out, out)

    out = run("def execute():\n    return run_command('cd ..')\n")
    check("cd 报错指向 cwd 参数", "cwd 参数" in out, out)

    # 新增白名单命令（模型真实用到且零风险）
    out = run("def execute():\n    return run_command('cal 10 2026')\n")
    check("cal 已进白名单（不报白名单错误）",
          "不在白名单" not in out, out)
    out = run("def execute():\n    return run_command('uname -s')\n")
    check("uname 已进白名单", "不在白名单" not in out, out)

    # 文档与沙箱必须一致：不得再教模型用被禁命令（文档撒谎 = 教模型试错）
    skill_md = (ROOT / "agent/skills/run-command/SKILL.md").read_text(encoding="utf-8")
    script_src = (ROOT / "agent/skills/run-command/scripts/run_command.py").read_text(encoding="utf-8")
    check("SKILL.md 不再给 python3 示例", "python3 -c" not in skill_md, skill_md[:60])
    check("SKILL.md 白名单不含被禁解释器",
          "python3/python/node/" not in skill_md.replace(" ", ""), skill_md[:60])
    check("docstring 不再把 awk 列进允许清单", "grep/awk" not in script_src, "")

    # ============================================================
    print("\n[G] shutil 受控放行（xlsxwriter / pptx 图表链，2026-10-26 跑批抓到）")
    # ============================================================
    # xlsxwriter 在模块导入时就要 from shutil import copy；python-pptx 的图表
    # 模块 import 即拉起 xlsxwriter。shutil 整包被禁导致「带图表排版的 PPT
    # 一直生成不出来」（模型第一次调 save_ppt 失败、去掉排版重试才成功）。
    # 放行方式：shutil 允许 import；rmtree 换成自实现的纯路径版（每步走受控
    # unlink/rmdir）；os.chmod / os.rmdir / unlink 补受控版本。
    out = run("import xlsxwriter, pptx.chart.xlsx\ndef execute():\n    return 'xlsx-ok'")
    check("xlsxwriter 与 pptx.chart 可导入", "xlsx-ok" in out, out)

    out = run(
        "import os, shutil, tempfile\n"
        "def execute():\n"
        "    d = os.path.join(tempfile.gettempdir(), 'sbx_g1')\n"
        "    sub = os.path.join(d, 'sub')\n"
        "    os.makedirs(sub, exist_ok=True)\n"
        "    open(os.path.join(d, 'a'), 'w').write('x')\n"
        "    open(os.path.join(sub, 'b'), 'w').write('x')\n"
        "    shutil.rmtree(d)\n"
        "    return 'rmtree-ok' if not os.path.exists(d) else 'STILL-EXISTS'\n")
    check("shutil.rmtree 白名单内可用（含嵌套）", "rmtree-ok" in out, out)

    out = run(
        "import shutil\n"
        "def execute():\n"
        "    try:\n"
        "        shutil.rmtree('/etc')\n"
        "        return 'LEAK'\n"
        "    except Exception:\n"
        "        return 'denied'\n")
    check("rmtree 白名单外仍被拒", "denied" in out, out)

    out = run(
        "import os, shutil, tempfile\n"
        "def execute():\n"
        "    d = os.path.join(tempfile.gettempdir(), 'sbx_g2')\n"
        "    os.makedirs(d, exist_ok=True)\n"
        "    os.symlink('/etc', os.path.join(d, 'evil'))\n"
        "    shutil.rmtree(d)\n"
        "    return 'link-only, /etc 完好=' + str(os.path.isdir('/etc'))\n")
    check("rmtree 遇符号链接只删链接、不碰目标", "/etc 完好=True" in out, out)

    out = run(
        "import os\n"
        "def execute():\n"
        "    try:\n"
        "        os.chmod('/etc/hosts', 0o644)\n"
        "        return 'LEAK'\n"
        "    except Exception:\n"
        "        return 'denied'\n")
    check("os.chmod 白名单外仍被拒", "denied" in out, out)

    out = run(
        "import shutil\n"
        "def execute():\n"
        "    try:\n"
        "        shutil.copy('/etc/passwd', 'leak.txt')\n"
        "        return 'LEAK'\n"
        "    except Exception:\n"
        "        return 'denied'\n")
    check("shutil.copy 读禁区仍被拒（走 open 钩子）", "denied" in out, out)

    # 清理探针目录
    probe_dir = ROOT / "document_output" / PROBE_ID
    if probe_dir.is_dir():
        shutil.rmtree(probe_dir, ignore_errors=True)

    print("\n" + "=" * 68)
    print("✓ 全部通过" if ok else "✗ 存在失败项")
    print("=" * 68)
    return 0 if ok else 1


def run_src(sb, code, params):
    """把一段真实技能源码当用户代码跑（用于端到端验 save_excel）。"""
    return sb.execute(code, params, user_id=1, public_id=PROBE_ID, tool_name="save_excel")


if __name__ == "__main__":
    sys.exit(main())
