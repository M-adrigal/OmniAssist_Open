---
name: run-command
description: 在隔离沙箱中执行一条 shell 命令并返回输出（受命令白名单、目录白名单、超时与输出截断限制）。
---

# run-command（沙箱命令工具）

在**隔离沙箱**中执行一条 shell 命令并返回其标准输出/错误，供 Agent 在需要时查看
系统状态、运行轻量脚本、处理文件等。

## 安全边界（由沙箱强制，不可绕过）

- **命令白名单**：仅允许 `python3/python/node/ls/cat/echo/date/pwd/wc/sort/head/
  tail/grep/awk/sed/jq/cut/tr/uniq/...` 等只读/轻量命令；`rm/sudo/dd` 等危险命令被拒绝。
- **目录白名单**：命令工作目录限定在沙箱允许目录（如 `/tmp` 与用户文档输出目录）。
- **超时**：单条命令默认 30s，最大 120s。
- **输出截断**：合并 stdout+stderr，截断至 8000 字符。
- **软件本体不可控**：沙箱阻断对 `agent/`、`server/` 源码、密钥文件与数据库的读写。

## 使用方式

调用 `execute(command, timeout=30)`。例如：

- `execute("ls -la /tmp")` 列出临时目录
- `execute("date && uname -a")` 查看系统信息
- `execute("python3 -c 'print(1+1)'")` 运行一段 Python

## 说明

该技能为**系统内置技能**（`is_system=True`），默认**免审批**直接执行，但其所有行为
仍受上述沙箱边界约束；因此即使直接执行，也无法越权操作软件本体或系统配置。
