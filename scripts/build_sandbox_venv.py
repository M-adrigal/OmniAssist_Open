#!/usr/bin/env python3
"""构建沙箱共享基础 venv（按当前 Python 小版本分桶）。

沙箱依赖注入机制：每个用户沙箱（tool_sandbox/user_N/venv）通过 PYTHONPATH 继承
tool_sandbox/shared/pyX.Y/venv 的依赖，避免每个用户重复 pip 安装重型库。

本脚本在「部署后」或「新增 Python 版本」时运行一次，把 requirements.sandbox.txt
里的通用依赖装进对应版本的共享 venv。ToolSandbox 在首次使用、且共享 venv 缺失时
也会懒构建（每个 Python 版本仅尝试一次），因此即使忘记手动运行，也能自愈。

用法：
    python3 scripts/build_sandbox_venv.py
可覆盖镜像源：
    SANDBOX_PIP_INDEX=https://pypi.org/simple python3 scripts/build_sandbox_venv.py
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REQ = os.path.join(ROOT, "requirements.sandbox.txt")
PY_TAG = f"py{sys.version_info.major}.{sys.version_info.minor}"
SHARED_VENV = os.path.join(ROOT, "tool_sandbox", "shared", PY_TAG, "venv")


def main() -> int:
    if not os.path.isfile(REQ):
        print(f"[build_sandbox_venv] 缺少依赖清单: {REQ}", file=sys.stderr)
        return 1

    os.makedirs(os.path.dirname(SHARED_VENV), exist_ok=True)

    if not os.path.isdir(os.path.join(SHARED_VENV, "bin")):
        print(f"[build_sandbox_venv] 创建共享 venv: {SHARED_VENV}")
        subprocess.check_call([sys.executable, "-m", "venv", SHARED_VENV])

    index = os.environ.get("SANDBOX_PIP_INDEX", "https://pypi.tuna.tsinghua.edu.cn/simple")
    shared_py = os.path.join(SHARED_VENV, "bin", "python3")
    cmd = [shared_py, "-m", "pip", "install",
           "--no-cache-dir", "-q", "--disable-pip-version-check",
           "--timeout", "30", "--retries", "2"]
    if index:
        cmd += ["-i", index]
        host = index.split("//", 1)[-1].split("/")[0] if "//" in index else ""
        if host:
            cmd += ["--trusted-host", host]
    cmd += ["-r", REQ]

    # 清空代理环境变量，避免主进程 HTTP_PROXY 导致 pip 安装失败
    env = dict(os.environ)
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
              "ALL_PROXY", "all_proxy"):
        env.pop(k, None)

    print(f"[build_sandbox_venv] 安装共享依赖到 {SHARED_VENV}")
    subprocess.check_call(cmd, env=env)
    print("[build_sandbox_venv] 完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
