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
