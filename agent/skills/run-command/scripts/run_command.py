"""在隔离沙箱中执行一条 shell 命令并返回输出。

注意：本脚本在沙箱子进程内执行，沙箱已向全局命名空间注入受控函数
`run_command(cmd, cwd=None, timeout=30)`（命令白名单 + 工作目录白名单 + 超时
+ 输出截断）。直接调用即可，无需也不应使用 subprocess 自行启动进程。

安全边界（沙箱强制）：
- 仅允许白名单命令：cat/date/echo/grep/ls/pwd/sed/sort/wc/head/tail/jq/cut/
  tr/uniq/nl/od/base64/xxd/file/stat/printf/test/expr/tee/diff/comm/cal/uname/
  whoami。完整清单以报错信息里列出的为准。
- 解释器（python3/python/node）与 awk 被**刻意禁止** —— 它们是沙箱逃逸通道
  （可起不受 import/文件钩子约束的进程；awk 内置 system() 可执行任意命令）。
  Python 逻辑请直接写在 execute() 函数体内，那里同样在沙箱保护下运行。
- **本工具不是 shell**：不支持管道 `|`、重定向 `>`、`&&` 串联，也不支持 `cd`
  （要换工作目录用 cwd 参数）。多步处理请写在 execute() 里用 Python 完成。
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
        command: 要执行的单条命令字符串，例如 "ls -la" 或 "grep -n 关键词 data.txt"。
                 仅允许白名单内的命令（cat/echo/date/ls/grep/sed/sort/wc/head/tail
                 等，完整清单见报错信息）。解释器（python3/python/node）与 awk
                 会被沙箱拒绝 —— Python 逻辑请直接写在 execute() 函数体内。
                 本工具不是 shell：不支持管道 |、重定向 >、&& 串联；cd 请用 cwd 参数。
                 路径参数只能用相对路径（相对 cwd），禁止 /tmp 这类绝对路径。
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
