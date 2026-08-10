"""
受控重启助手（由 agent/diagnostics.py 的 diag_restart_service 以 detached 方式启动）。

职责：
  1. 找到监听主端口（默认 17520）的旧进程 PID
  2. 优雅终止（SIGTERM，超时后 SIGKILL）
  3. 等待端口释放后，在项目根目录重新拉起 server/main.py（detached）

本脚本以 start_new_session=True 脱离父会话运行，因此即使发起重启的旧服务进程退出，
本助手仍能存活并完成重生，避免端口残留孤儿进程。
"""

import os
import sys
import time
import signal
import subprocess

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN_PORT = 17520
ENTRY = os.path.join(PROJECT_ROOT, "server", "main.py")
PYTHON = sys.executable


def _pids_on_port(port):
    try:
        out = subprocess.run(
            ["lsof", "-ti", f":{port}"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0 and out.stdout.strip():
            return [int(x) for x in out.stdout.strip().split("\n") if x.strip()]
    except Exception:
        pass
    return []


def _kill_old():
    pids = _pids_on_port(MAIN_PORT)
    if not pids:
        return
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    # 等待退出
    for _ in range(20):
        time.sleep(0.5)
        if not _pids_on_port(MAIN_PORT):
            return
    # 超时强杀
    for pid in _pids_on_port(MAIN_PORT):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    time.sleep(1)


def _start_new():
    subprocess.Popen(
        [PYTHON, ENTRY],
        cwd=PROJECT_ROOT,
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def main():
    _kill_old()
    # 确保端口确实释放
    for _ in range(10):
        if not _pids_on_port(MAIN_PORT):
            break
        time.sleep(0.5)
    _start_new()


if __name__ == "__main__":
    main()
