"""在隔离沙箱中执行一条 shell 命令并返回输出。

注意：本脚本在沙箱子进程内执行，沙箱已向全局命名空间注入受控函数
`run_command(cmd, cwd=None, timeout=30)`（命令白名单 + 工作目录白名单 + 超时
+ 输出截断）。直接调用即可，无需也不应使用 subprocess 自行启动进程。

安全边界（沙箱强制）：
- 仅允许白名单命令（ls/cat/echo/grep/awk/sed/jq 等）；解释器（python3/node）与
  危险命令（rm/sudo/dd 等）一律拒绝。
- 命令参数中的路径禁止绝对路径、家目录展开（~）、父目录遍历（..）、密钥文件名，
  且不得指向 /etc、/home、/Users、/var、数据库目录等受限前缀，杜绝读取 SSH 私钥、
  数据库、密钥文件或系统文件。
- 工作目录（cwd）被限制为可写白名单目录（用户产出目录 / 临时目录 / 用户技能目录）。
- 沙箱子进程默认禁止所有外网出口，仅放行 SANDBOX_NETWORK_ALLOWLIST 配置的主机。
"""
import json


def execute(command: str, timeout: int = 30) -> str:
    """在隔离沙箱中执行一条 shell 命令并返回其输出。

    Args:
        command: 要执行的 shell 命令字符串，例如 "ls -la /tmp"。
                 仅允许白名单内的命令（ls/cat/echo/date/grep/awk/sed/jq 等），
                 危险命令（rm/sudo/dd 等）以及解释器（python3/python/node）
                 会被沙箱拒绝。执行 Python 代码请使用代码执行工具而非本工具。
        timeout: 命令超时时间（秒），默认 30，最大 120。

    Returns:
        JSON 字符串，包含 command、output（stdout+stderr 合并，截断至 8000 字符）、
        以及出错时的 error 字段。
    """
    try:
        _timeout = max(1, min(int(timeout), 120))
        out = run_command(command, timeout=_timeout)
        return json.dumps(
            {"command": command, "output": out},
            ensure_ascii=False,
        )
    except Exception as e:
        return json.dumps(
            {"command": command, "error": f"{type(e).__name__}: {e}"},
            ensure_ascii=False,
        )
