#!/usr/bin/env python3
"""回归测试总入口：一条命令跑完全部，不用记解释器差异。

为什么不是「把 18 个文件合并成 1 个」：
  · 每个用例都要建**自己的临时库 / 临时技能目录**（避免污染 data/users.db）。
    挤进一个进程里，前一个用例改过的全局状态（monkeypatch、模块级缓存）会漏给下一个。
  · 依赖不同：5 个要 fastapi、1 个要 openai、2 个要 Chrome、1 个要真实沙箱子进程。
    合成一个文件等于要求「所有依赖都装同一个解释器里」，本机做不到。
  · 出错定位：分开跑能有独立的失败输出；合在一起只会得到「第 347 个断言挂了」。

所以这里做的是「一个入口，各自隔离」：本脚本负责挑解释器、判依赖、并行不了就串行、
汇总结果。加新测试只要放 scripts/test_*.py，自动被发现。

用法：
  python3 scripts/run_tests.py                 # 全部
  python3 scripts/run_tests.py --list          # 只列出会跑哪些、各自要什么
  python3 scripts/run_tests.py --only 技能,循环  # 按名字关键词筛（子串匹配）
  python3 scripts/run_tests.py --skip-chrome   # 跳过要浏览器的（CI/无 GUI 环境）
  python3 scripts/run_tests.py --quiet          # 只打汇总表
  python3 scripts/run_tests.py --fail-fast      # 第一个失败就停

退出码：全过 0；有失败 1；前置条件不满足（如缺 fastapi）2。
"""
import argparse
import os
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

# 需要 fastapi 的脚本（这些用「能 import fastapi 的解释器」跑）
NEEDS_FASTAPI = {
    "test_doc_on_demand.py", "test_image_round_isolation.py", "test_model_config.py",
    "test_skill_upload.py", "test_usage_pending.py", "test_security_fixes.py",
    "test_memory_routes.py",
}
NEEDS_OPENAI = {"test_usage_calibration.py"}
NEEDS_CHROME = {"test_frontend_flows.py", "test_send_body.py"}
# 每个用例一句话说明（新增测试时在这里补一行，否则显示「(未登记)」）
DESC = {
    "test_loop_guard.py": "循环防护判据（含误杀场景回归）",
    "test_skill_delete.py": "技能软删除 / 恢复",
    "test_trash_cleanup.py": "备份 30 天保留期清理",
    "test_skill_sync.py": "技能磁盘↔表对账（含上传技能不被当幽灵清）",
    "test_skill_upload.py": "上传技能：结构校验 / 风险分级 / 软删",
    "test_agent_audit.py": "执行审计：脱敏 / 落盘 / 检索",
    "test_fixes_integration.py": "跨模块集成：自伤工具隔离 + 审计轨迹",
    "test_security_fixes.py": "安全基线（XFO / MIME / 归属校验）",
    "test_model_config.py": "模型配置：全局隔离 / 编辑 / 自定义供应商",
    "test_usage_calibration.py": "用量双轨 / 图片计数 / 单价语义",
    "test_usage_pending.py": "任务中断与异常对账",
    "test_image_support.py": "图片多模态链路",
    "test_image_round_isolation.py": "图片只发本轮",
    "test_doc_on_demand.py": "文档按需注入",
    "test_sandbox_compat.py": "沙箱：依赖可用 + 越权仍被拒",
    "test_send_body.py": "前端发送请求体（抓作用域错）",
    "test_frontend_flows.py": "前端主流程（真实页面 + 无头 Chrome）",
    "test_memory.py": "记忆：四段读写 / 上限 / 导入导出 / 注入",
    "test_memory_routes.py": "记忆接口：归属 / 上限 / 关≠删 / 整理三结局",
    "security_audit_admin.py": "权限溢出审计",
}


def discover() -> list:
    files = sorted(SCRIPTS.glob("test_*.py"))
    audit = SCRIPTS / "security_audit_admin.py"
    if audit.exists():
        files.append(audit)
    return files


def needs(path: pathlib.Path) -> str:
    n = path.name
    if n in NEEDS_FASTAPI:
        return "fastapi"
    if n in NEEDS_OPENAI:
        return "openai"
    if n in NEEDS_CHROME:
        return "chrome"
    return "none"


def pick_python(need: str) -> str:
    """按依赖挑解释器：优先系统 python3（本机只有它装了 fastapi/openai）。"""
    candidates = ["/usr/local/bin/python3", sys.executable]
    for exe in candidates:
        if not exe or not os.path.exists(exe):
            continue
        if need == "none":
            return exe
        mod = "openai" if need == "openai" else "fastapi"
        try:
            r = subprocess.run([exe, "-c", f"import {mod}"],
                               capture_output=True, timeout=20)
            if r.returncode == 0:
                return exe
        except Exception:
            pass
    return ""


def main():
    ap = argparse.ArgumentParser(description="回归测试总入口")
    ap.add_argument("--list", action="store_true", help="只列出会跑什么")
    ap.add_argument("--only", default="", help="按名字关键词筛（逗号分隔，子串匹配）")
    ap.add_argument("--skip-chrome", action="store_true", help="跳过需要浏览器的用例")
    ap.add_argument("--quiet", action="store_true", help="只打汇总表")
    ap.add_argument("--fail-fast", action="store_true")
    ap.add_argument("--timeout", type=int, default=300, help="单个用例超时（秒）")
    args = ap.parse_args()

    files = discover()
    if args.only:
        keys = [k.strip() for k in args.only.split(",") if k.strip()]
        files = [f for f in files if any(k in f.name for k in keys)]
    if args.skip_chrome:
        files = [f for f in files if f.name not in NEEDS_CHROME]
    if not files:
        print("没有匹配的测试")
        return 2

    has_chrome = os.path.exists(CHROME)
    rows, skipped, missing_dep = [], [], []

    if args.list:
        print(f"{'脚本':32}{'依赖':10}说明")
        for f in files:
            nd = needs(f)
            note = DESC.get(f.name, "(未登记)")
            if nd == "chrome" and not has_chrome:
                note += "  ← 本机无 Chrome，会跳过"
            print(f"{f.name:32}{nd:10}{note}")
        print(f"\n共 {len(files)} 个")
        return 0

    t_all = time.time()
    for i, f in enumerate(files, 1):
        nd = needs(f)
        if nd == "chrome" and not has_chrome:
            skipped.append((f.name, "本机无 Chrome"))
            print(f"[{i}/{len(files)}] {f.name:32} 跳过（无 Chrome）")
            continue
        exe = pick_python(nd)
        if not exe:
            missing_dep.append((f.name, nd))
            print(f"[{i}/{len(files)}] {f.name:32} 跳过（找不到带 {nd} 的解释器）")
            continue
        if not args.quiet:
            print(f"[{i}/{len(files)}] {f.name:32} …", end="", flush=True)
        t0 = time.time()
        try:
            r = subprocess.run([exe, str(f)], cwd=str(ROOT), capture_output=True,
                               text=True, timeout=args.timeout)
            ok = r.returncode == 0
            out = (r.stdout or "") + (r.stderr or "")
        except subprocess.TimeoutExpired:
            ok, out = False, f"[超时 > {args.timeout}s]"
        dt = time.time() - t0
        rows.append((f.name, ok, dt, out))
        if not args.quiet:
            print(f"\r[{i}/{len(files)}] {f.name:32} {'✓' if ok else '✗'}  {dt:5.1f}s")
        if args.quiet and not ok:
            print(f"  ✗ {f.name}")
        if not ok:
            tail = "\n".join([l for l in out.strip().split("\n") if l.strip()][-6:])
            print(f"    └─ 失败输出（末尾 6 行）:\n" + "\n".join("       " + l for l in tail.split("\n")))
            if args.fail_fast:
                break

    passed = sum(1 for _, ok, _, _ in rows if ok)
    failed = [r for r in rows if not r[1]]
    print("\n" + "=" * 70)
    print(f"结果：{passed} 通过 / {len(failed)} 失败 / {len(skipped)} 跳过"
          f"　用时 {time.time() - t_all:.1f}s")
    if failed:
        print("失败：" + "、".join(n for n, _, _, _ in failed))
    if skipped:
        print("跳过：" + "、".join(f"{n}（{why}）" for n, why in skipped))
    if missing_dep:
        print("缺依赖：" + "、".join(f"{n} 需 {d}" for n, d in missing_dep))
    print("=" * 70)
    if failed:
        return 1
    return 2 if missing_dep else 0


if __name__ == "__main__":
    sys.exit(main())
