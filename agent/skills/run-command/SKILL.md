---
name: run-command
description: 在隔离沙箱中执行一条 shell 命令并返回输出（受命令白名单、目录白名单、超时与输出截断限制）。
---

# run-command（沙箱命令工具）

在**隔离沙箱**中执行一条 shell 命令并返回其标准输出/错误，供 Agent 在需要时查看
系统状态、处理文件、做轻量文本处理。

## 安全边界（由沙箱强制，不可绕过）

- **命令白名单**：仅允许以下只读/轻量命令 ——
  `cat, cal, comm, cut, date, diff, echo, expr, false, file, grep, head, jq, ls,
  nl, od, base64, printf, pwd, sed, sort, stat, tail, tee, test, tr, true, uniq,
  uname, whoami, wc, xxd`
  完整清单以报错信息里列出的为准。
- **解释器被刻意禁止**：`python3` / `python` / `node` / `awk` 一律拒绝 —— 它们
  是沙箱逃逸通道（可起不受约束的进程 / 内置 system() 可执行任意命令）。
  **要跑 Python 逻辑，直接写在 execute() 函数体内**，那里同样受沙箱保护，
  且能用标准库（json/re/datetime/itertools 等）。
- **本工具不是 shell**：不支持管道 `|`、重定向 `>`、`&&` 串联；不支持 `cd`
  （要换工作目录用 `cwd` 参数）。多步处理写在 execute() 里用 Python 完成。
- **路径只能用相对路径**（相对 cwd）：绝对路径、`~`、`..`、密钥文件名、
  `/etc` `/Users` `/var` 等受限前缀一律被拒。
- **目录白名单**：命令工作目录限定在沙箱允许目录（临时目录与用户文档输出目录）。
- **超时**：单条命令默认 30s，最大 120s。
- **输出截断**：合并 stdout+stderr，截断至 8000 字符。

## 使用方式

调用 `execute(command, timeout=30)`。例如：

- `execute("ls -la")` 列出当前工作目录
- `execute("date")` 查看当前日期时间
- `execute("cal 10 2026")` 查看某月日历
- `execute("grep -n 关键词 data.txt")` 在文件里查找（data.txt 须在 cwd 下）

## 说明

该技能为**系统内置技能**（`is_system=True`），默认**免审批**直接执行，但其所有行为
仍受上述沙箱边界约束；因此即使直接执行，也无法越权操作软件本体或系统配置。
需要 Python 级别的处理（算数、日期推算、字符串加工）时，**优先用对应专用工具
（calculator / get_datetime）或写在 execute() 函数体内**，不要试图经本工具调解释器。
