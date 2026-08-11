# 操作审批（用户确认）验证流程

本文档说明如何验证「敏感操作需用户确认」机制是否生效，覆盖触发点、验证步骤、
预期行为与通过标准。配合系统内置的 **run-command（沙箱命令工具）** 与诊断/技能
管理工具使用。

---

## 1. 机制回顾

审批门由 `agent/tools.py: needs_approval()` 决定：

```python
def needs_approval(self, name, args=None):
    ra = t.get("require_approval")
    if ra is not None:
        return bool(ra)
    return t.get("risk_level", "safe") in ("write", "exec", "admin")
```

- 显式 `require_approval=True` → 必确认。
- 未显式设置时，`risk_level ∈ {write, exec, admin}` → 确认；`safe`/`read` → 免确认。

权限模式（工具栏按钮）由 `trust_store.mode` 驱动：
- **请求批准（默认）**：审批门生效，exec/write 级工具弹确认框，用户逐项否决/确认。
- **完全访问**：审批门跳过，所有工具直接执行（仍受沙箱边界约束）。

> 系统技能（`is_system=True`，如 run-command、calculator）一律 `read` 级、免审批；
> 用户自建技能（`is_system=False`）一律需审批。

---

## 2. 触发审批的操作清单

### 2.1 系统内置敏感工具（无需预建技能，100% 触发）

| 工具 | risk_level | 触发动作 | 验证 prompt 示例 |
|---|---|---|---|
| `diag_restart_service` | exec | 重启主服务 | 「重启一下主服务让新配置生效」 |
| `diag_delete_file` | write | 删除生成文件 | 「删除 document_output 下 old_report.txt」 |
| `diag_rename_file` | write | 重命名文件 | 「把 document_output/a.txt 改名为 b.txt」 |
| `create_user_skill` | write | 新建用户技能 | 「新建一个名为 demo 的用户技能，功能是 X」 |
| `update_user_skill` | write | 更新用户技能 | 「更新 demo 技能的描述」 |
| `delete_user_skill` | write | 删除用户技能 | 「删除 demo 技能」 |

> 注意：带 `【管理员】` 前缀的工具需 `admin` 角色才出现在工具候选列表。

### 2.2 用户自建技能（需先在「技能」页或经 API 创建）

| execution_mode | risk_level | 触发动作 |
|---|---|---|
| `local_execution` | exec | 在沙箱执行自定义 Python 代码 |
| `http_request` | write | 向外部地址发起自定义请求 |

---

## 3. 验证步骤

### 用例 A：请求批准模式 — exec 级确认（重启服务）

1. 进入会话，确认工具栏权限按钮显示 **「请求批准」**。
2. 发送：`重启一下主服务让新配置生效`
3. **预期**：
   - 弹出确认框，文案含「重启主服务（当前连接将短暂中断）」。
   - 点击 **否决/取消** → 不重启，回复说明操作被取消。
   - 点击 **确认** → 服务重启（连接短暂中断后恢复）。

### 用例 B：请求批准模式 — write 级确认（删除文件）

1. 发送：`删除 document_output 下 old_report.txt`
2. **预期**：弹出「删除文件：...」确认框；否决则不删，确认才删。

### 用例 C：用户自建技能 — exec 级确认（run-command 之外的执行类）

1. 先经 `create_user_skill` 建一个 `local_execution` 用户技能（如 demo_ping）。
2. 发送：`执行 demo_ping 并告诉我输出`
3. **预期**：弹出 exec 级确认框；确认后沙箱执行、返回输出。

### 用例 D：完全访问模式 — 跳过确认

1. 点击权限按钮切到 **「完全访问」**（会弹范围确认窗，确认后变红）。
2. 重复用例 A/B/C 的同一条指令。
3. **预期**：**不再弹确认框**，操作直接执行。

### 用例 E：沙箱命令工具（免审批但受沙箱约束）

1. 发送：`用 run-command 执行 ls -la /tmp`
2. **预期**：
   - 不弹确认框（系统技能免审批），直接返回 `/tmp` 列表。
   - 若命令不在白名单（如 `rm -rf /tmp/x`）：返回 `[沙箱] 命令不在白名单` 错误，
     不会真正执行。
   - 软件本体（`agent/`、`server/`）与密钥文件不可读写。

---

## 4. 通过标准（验收清单）

- [ ] 请求批准模式下，2.1/2.2 中任一操作**均弹出确认框**，文案正确。
- [ ] 否决后操作**确实未执行**（查日志/文件状态确认）。
- [ ] 确认后操作**确实执行**且结果正确。
- [ ] 完全访问模式下，同样指令**不再弹框**、直接执行。
- [ ] run-command 等系统技能**免审批**，但危险命令被沙箱拒绝、本体不可控。
- [ ] 否决/确认状态与 `ApprovalStore` 一致，多用户并发互不干扰。

---

## 5. 排查要点

- 若某操作未弹框：检查该工具的 `risk_level` 是否为 `write/exec/admin`，或
  `require_approval` 是否被显式设为 `False`（系统技能）。
- 若切换模式无效：确认 `trust_store.mode` 已更新（GET `/api/trust` 返回 `mode`）。
- 若 LLM 未调用工具：看 `logs/app.log` 中意图选择/工具调用日志；必要时用
  `update_intent_keywords` 补充关键词。
