# OmniAssist

> 自托管的多用户 AI Agent：技能（Skill）驱动工具调用，沙箱隔离执行，审批门控敏感操作。
> **当前版本 v2.3.0** · 变更历史见 [`CHANGELOG.md`](CHANGELOG.md)

一个轻量级 AI Agent 框架。能力以「技能」为单位扩展 —— 每个技能是一个文件夹（`SKILL.md` + `scripts/*.py`），
模型按需调用其中的脚本；脚本在**每用户独立 venv + 子进程**里执行，涉及敏感操作时先经审批。

## 文档地图

四份文档各有明确边界，**同一个事实只写在一处**，避免多处矛盾：

| 文件 | 回答什么问题 | 谁看 | 体量 |
|---|---|---|---|
| `README.md`（本文） | 这是什么、有什么、怎么跑起来 | 第一次接触项目的人 | 短，通读 |
| `AGENTS.md` | 改代码要守什么规矩：目录树、常用改动路径、索引、最容易踩的 8 条、禁令、测试清单 | 改代码的人 / AI 助手（**每次会话都加载**） | 约 400 行，常驻 |
| `docs/ARCHITECTURE.md` | 33 个架构小节的完整说明、事故复盘、踩坑记录 | 改到某模块时按索引查 | 约 1500 行，按需 |
| `deploy.md` | 怎么部署、怎么运维、出故障怎么查 | 运维 | 按需 |
| `CHANGELOG.md` | 每个版本改了什么 | 升级前查影响 | 按需 |

> **为什么分常驻 / 按需**：`AGENTS.md` 是每次 AI 会话都要整份读的，写太长会挤占上下文、稀释重点。
> 所以只留「规矩」；「这条规矩是怎么来的」属于查阅资料，放 `docs/ARCHITECTURE.md`。

---

## 1. 能力全景

### 对话

- **流式输出**，可中途停止；答案重生成（丢弃最后一条用户消息之后的全部内容重跑）
- **思考过程可视化**：三档 `off / low / high`，`<thinking>` 标签解析后分开展示
- **多会话**：切换会话保留完整 DOM；任务在后台继续跑，切走再回来可重连
- **上下文压缩**：超过 token 上限自动压缩历史消息
- **状态提示**：处理中 / 调用工具 / 重试 / 任务被掐断，都会在回答上方显示。异常类提示常驻，不会一闪而过
- **斜杠命令**：`/help`、`/reset`

### 多模态与上下文注入

- **图片**：上传或粘贴，走模型视觉能力；只发**本轮**图片，历史图不重发
- **文档按需注入**：文档不无脑全塞。只放行「本轮上传/引用」+「消息里点了名的」+「最近一轮」，其余列成清单让模型知道"有但没读"，引导用 @ 引用
- **联网搜索**：模型自主调用 `web_search` 工具，回答末尾列出来源

### 长期记忆

- **结构可扩展**：记忆分成若干「段」，段的类型写在注册表里（文本段按字数封顶 / 列表段按条数封顶）。
  加一个新记忆段 = 注册表加一行，界面、导入导出、对话注入、模型提示四处自动跟上
- **两条写入通道**：自己写；或让模型从某个会话里提炼提议 —— **提议只展示不落库**，勾选后才逐条写入
- **每次改动留档**（保留最近 30 版），改坏了能在历史里一键恢复；**清空也变成可撤销的操作**
- **关 ≠ 删**：关掉开关只是不再注入对话，内容原样保留

### 文件

- 上传 / 预览 / 下载 / 重命名；管理员可跨用户监管
- 产出文件（Word / Excel / PPT / PDF / 文本）按 `document_output/{public_id}/` 隔离，文件名在回答里可点击下载

### 技能（能力扩展的单位）

- **系统技能 7 个**：`calculator`、`chinese-counter`、`datetime`、`document`、`lunar-converter`、`run-command`、`web-fetch`
- **用户自建技能**：用自然语言让 Agent 生成，或**直接上传 zip / SKILL.md** 安装
- **上传安装有风险扫描**：命中主进程执行、凭证访问、外传组合等直接拦截；命中 `subprocess`、网络、文件写等需确认后安装
- **卸载是软删除**：进回收站保留 30 天，可恢复
- **对话内选用**：左下角「+」→ 技能面板勾选，本轮把技能全文注入 system prompt

### 模型

- 多供应商（OpenAI 兼容端点），平台 Key 与用户自配 Key 隔离 —— 管理员看不到用户的 Key
- 温度策略：`auto`（按任务类型定首轮温度并向 0.2 收敛）/ `static`（固定）
- 最大迭代次数可配（默认 10）

### 安全

- **双 ID**：对外只用不透明 `public_id`，内部整数 `db_id` 不外露，杜绝主键枚举
- **沙箱**：每用户独立 venv + 子进程；模块黑名单、危险 OS 函数删除、读禁区 / 写白名单、内存 512MB、超时、命令 allowlist、网络出口默认全阻断
- **审批门**：敏感工具逐项确认，支持信任模式（角色级免确认）
- **审计**：每次工具调用与审批决策落 `logs/agent_audit.log`（JSONL，密钥参数脱敏）

### 观测

- **Token 用量双轨**：服务商回传的精确值 vs 本地字符估算，分开列不混加
- **中断 / 异常对账**：任务被重启打断、或跑完却没记账，看板会显示「近 7 天 N 次」，不制造静默丢失

---

## 2. 快速开始

### 环境要求

- Python 3.10+（本机开发与测试用 3.14）
- 一个 OpenAI 兼容的 API 端点
- （可选）Tavily API Key，用于联网搜索

### 安装与启动

```bash
pip install -r requirements.txt
python server/main.py
```

打开 `http://localhost:17520`。

**首次启动**会自动建库并生成管理员：

- 用户名 `admin`
- **初始密码是随机生成的，只打印在启动控制台**（不写进 `logs/`，因为日志文件管理员可下载）
- 首次登录强制改密后才能使用

登录后到「设置 → 模型配置」填 API Key / Base URL / 模型名。

### 数据库管理界面

随服务启动（sqlite-web），`http://localhost:17521`，用管理员账号密码登录，仅管理员可进。

> 生产环境它默认只监听本机，远程访问请走 SSH 隧道：
> `ssh -L 17521:127.0.0.1:17521 user@your-server-ip`

### 终端模式（调试用）

```bash
cd agent
python main.py
```

与 Web 共用同一个 Agent 核心；模型配置用 `/model set`，或直接在对话里描述。

---

## 3. 三种「执行」到底怎么走

| 路径 | 在哪执行 | 用在哪 |
|---|---|---|
| `local_execution` | 用户沙箱（独立 venv + 子进程） | 计算、数据处理、文件生成 —— **绝大多数技能** |
| `http_request` | 主进程（等价于信任该脚本） | 调用外部 API。**用户上传的技能一律禁止走这条**，扫描阶段直接拦截 |

技能的脚本用 `execute(...)` 函数声明参数，注册时被 AST 解析成工具定义（工具名 = 脚本名），模型看到的就是这份 schema。

---

## 4. 项目结构

```
agent/                    # 核心（不依赖 server/）
  main.py                 # 终端入口
  agent.py                # ReAct 主循环
  tools.py                # 工具注册与执行分发
  llm.py                  # LLM 客户端（流式 / 用量上报 / 思考解析）
  sandbox.py              # 沙箱：独立 venv + 子进程 + 读写边界 + 命令白名单
  skill_registry.py       # 技能加载（磁盘 / 数据库两种来源）
  skill_editor.py         # 用户技能增删改（磁盘为真相源）
  skill_sync.py           # 磁盘 ↔ user_skills 表对账
  skill_risk.py           # 上传技能的风险扫描（P0 拦截 / P1 确认 / P2 放行）
  loop_guard.py           # 迭代级循环防护
  parallel_executor.py    # 多工具并行
  agent_pool.py           # 子 Agent（profiles/*.yaml）
  intent_keywords.py      # 意图关键词（按需裁剪工具）
  task_reviewer.py        # 任务复盘
  file_parser.py          # 上传文件解析（含图片多模态）
  model_gateway.py        # 模型能力位（温度 / 视觉）
  diagnostics.py          # 诊断工具 diag_*（仅管理员）
  skills/                 # 系统技能 + user/{db_id}/ 用户技能 + .trash/ 软删除备份

server/                   # Web 层（FastAPI）
  main.py                 # 应用入口、安全响应头、端口检查、启动对账
  database.py             # SQLite 访问层（所有表操作的唯一入口）
  models.py               # 请求/响应模型
  usage_store.py          # Token 用量（双轨 + pending 对账）
  model_store.py          # 供应商 / 模型池
  memory_store.py         # 长期记忆（段注册表 + 条目存储 + 版本留档）
  agent_audit.py          # 执行审计
  approval_store.py       # 审批（逐项确认、会话隔离）
  trust_store.py          # 信任模式
  cleanup_scheduler.py    # 后台清理（备份保留期等）
  routes/                 # 14 个路由模块，97 个端点（另有 main.py 直挂 5 个；注册顺序见 routes/__init__.py 的说明）

static/                   # 前端（原生 JS，无构建工具）
  index.html              # 主界面
  login.html              # 登录（单文件，不共用 css）
  usage.html + usage.js   # 用量看板（独立页）
  js/ 14 个               # 顺序加载、共享全局作用域
  css/ 18 个              # 顺序即层叠顺序
  img/                    # 登录页背景照片

scripts/                  # 运维工具 + 回归测试（清单见 AGENTS.md 第 7 节）
data/                     # 运行时数据（SQLite，不提交）
document_output/          # 用户产出文件（按 public_id 隔离，不提交）
tool_sandbox/             # 沙箱 venv（不提交；可用 scripts/build_sandbox_venv.py 重建）
logs/                     # 日志（不提交）
```

---

## 5. 数据模型

SQLite 单库 `data/users.db`，15 张业务表（另有 1 张 SQLite 内部表 `sqlite_sequence`）：

| 表 | 作用 | 关键点 |
|---|---|---|
| `users` | 账号 | `public_id` 对外，`id` 内部；密码加盐哈希 |
| `sessions` | 会话与消息 | 消息以 JSON 存 `messages`，含 `tools` / `thought` / `files` 元数据 |
| `model_configs` | 模型配置 | 平台级与个人级靠 `user_id` 是否为空区分 |
| `model_providers` / `model_entries` | 供应商与模型池 | 个人行只有本人可见 |
| `user_skills` | 用户技能索引 | `source`(editor/uploaded) / `risk_level` / `confirmed_at` |
| `token_usage` | 用量账本 | 每次模型调用一行；`is_estimated` 区分精确/估算轨 |
| `usage_pending` | 任务生命周期 | `0 running / 1 finished / 2 interrupted / 3 orphan` |
| `tool_operation_logs` | 工具执行日志 | 供任务复盘 |
| `permissions` | 角色权限 | `get_user_role` 是唯一鉴权事实来源 |
| `user_secrets` / `search_config` | 用户密钥 / 搜索配置 | 加密存储 |
| `user_memory` | 记忆总开关 | 关掉只停注入，内容保留 |
| `user_memory_entries` | 记忆条目 | 按段存，一条一行；段类型见 `memory_store.SECTION_REGISTRY` |
| `user_memory_versions` | 记忆留档 | 每次改动前存快照，保留最近 30 版，可恢复 |

> 所有表操作都经 `server/database.py`。**不要直接改库**（密码是加盐哈希，直改不可恢复）。

---

## 6. 平台能做什么（按使用场景）

这一节讲**用起来有什么**，不讲怎么改代码。想改代码看 `AGENTS.md` 的「常用改动路径」。

| 场景 | 你能做的事 | 入口 |
|---|---|---|
| 对话 | 提问、流式回答、中途停止、重新生成；多会话并存，切走任务继续跑；思考过程三档（关 / 简 / 详）；消息里 `@` 引用会话中的文件 | 主界面输入框 |
| 让 Agent 干具体的活 | 调用技能：算数、日期、文档生成（Word/Excel/PPT/PDF）、网页抓取、执行命令、汉字统计、农历换算 | 直接说需求，模型自己选技能 |
| 扩展能力 | ① 用自然语言让 Agent 造一个技能 ② 上传现成技能（zip / SKILL.md，安装前有风险扫描）③ 本轮临时选用某个技能 ④ **导出技能为 zip**（备份或分享）⑤ 卸载进回收站，可恢复或**彻底删除** | 左下角「+」→ 技能；或「设置 → 技能」 |
| 喂资料 | 上传文档（pdf/docx/xlsx/pptx/csv/txt）和图片；**图片在对话里直接显示缩略图，点击看原图**；图片只送本轮给模型，文档按需注入；也可从文件库「引用」历史文件 | 左下角「+」→ 文件；或直接粘贴图片 |
| 拿产出 | Agent 生成的文件在回答里可点击预览 / 下载；文件库按类型和来源筛，支持多关键词搜索 | 回答内文件卡；「+」→ 文件库 |
| 选模型 | 用平台配好的模型，或自己填供应商 + Key（自己的 Key 只有自己看得见）；调温度策略与最大迭代次数 | 设置 → 模型配置 |
| 养记忆 | 自己写背景与关注点；或让模型从某个会话提炼新条目（勾选后才写入）；改动前自动留档，可在历史里恢复；导入导出为纯文本 | 设置 → 记忆 |
| 看花了多少 | 按天 / 按模型看 token；精确值与估算值分开列；「近 7 天 N 次任务中断」提示未入账的消耗 | 左下角 → Token |
| 管人 | 建用户、改角色、重置密码；首次登录强制改密 | 设置 → 用户管理 |
| 管安全 | 敏感操作弹审批卡片（允许 / 跳过）；信任模式下免逐项确认；所有工具调用与审批决策留审计日志 | 会话内卡片；`logs/agent_audit.log` |
| 查问题 | 管理员可直接在对话里让 Agent 查日志、查库、看端口、看文件（`diag_*` 系列） | 对话 |

---

## 7. 测试与检查

```bash
# 回归测试：一条命令跑完全部（自动挑解释器、判依赖、汇总结果）
python3 scripts/run_tests.py

# 常用参数
python3 scripts/run_tests.py --list          # 看有哪些用例、各自要什么依赖
python3 scripts/run_tests.py --only 技能,循环  # 按名字筛
python3 scripts/run_tests.py --skip-chrome    # 无 GUI 环境
```

# 改动前后来一遍的四项检查
python3 scripts/check_static.py        # CSS 变量 / JS 重名 / HTML 引用 / 选择器笔误
python3 scripts/check_contract.py      # 前端调用 ↔ 后端端点 / SSE 事件 / 请求体字段
python3 scripts/test_frontend_flows.py # 真实页面主流程（无头 Chrome）
python3 scripts/test_sandbox_compat.py # 沙箱：依赖可用 + 越权仍被拒
```

质量评估（需服务在跑 + 登录 cookie）：

```bash
python3 scripts/eval/eval_runner.py --cookie <auth_token> --judge
```

> 部分脚本需要 fastapi，用系统 Python（`/usr/local/bin/python3`）跑；零依赖脚本两种解释器都行。

---

## 8. 已知边界

- 沙箱内网络默认**完全阻断**，需要外联的技能要向 `SANDBOX_NETWORK_ALLOWLIST` 登记主机
- 沙箱内还留有少量已知缺口（`socket.connect` 走 C 层可绕过 Python 层拦截；无 `RLIMIT_CPU/FSIZE`），**真正的边界是独立子进程 + 低权限用户**
- 重新生成**不携带原消息里的图片**（历史不记录原轮上传清单；全发就会回到「图片叠加」的老问题），原消息带图时界面会提示
- 单个会话的上传目录不自动清空，删会话时才连带删除
- 多 worker 部署时，审批与信任模式的内存存储需换成 Redis（当前单 worker 设计）

---

## 9. 文档维护约定

- **版本历史只写 `CHANGELOG.md`**，README 不写"本次更新"
- **新增功能**：改 README 的能力清单；涉及架构约束的，写进 `AGENTS.md`
- **新增环境变量 / 部署步骤**：只写 `deploy.md`
- 同一事实出现两处，就是下一次矛盾的起点 —— 宁可直接引用另一份文档
- **写之前先核对代码**，别信上一版文档。这份 README 重写时发现过期的有：默认管理员密码（旧版文档写死成固定口令，实际是随机生成、只打控制台）、第三种「LLM 模拟」执行模式（已不存在，只剩沙箱执行与 HTTP 调用两种）、登录页「4 个时段渐变背景」（已改为照片）、「暴力测试套件」（目录已不存在）
