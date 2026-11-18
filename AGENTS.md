# AGENTS.md — 改代码的规范

## 1. 项目概述

轻量级 AI Agent 框架：技能系统、多 Agent 池、自然语言创建技能、多用户管理、流式对话，提供 Web 与终端两种交互方式。

管理员账号 `admin`，初始密码随机生成，只打印在首次启动的控制台，首次登录强制改密。

**双 ID 体系**：对外使用不透明 `public_id`（UUID 前缀 `u_`），用于 JWT、前端、文件目录 `document_output/{public_id}/`；内部使用整数自增 `db_id`，用于数据库关联、权限网关 `get_user_role`、沙箱 venv 隔离。

`get_current_user()` 返回的字典里键名是 `public_id` 和 `db_id`，**没有 `"id"` 键**。
写任何 DB 整数外键列、整数技能目录、trust owner 时必须用 `db_id`；`public_id` 只用于对外标识与文件路径。归属比对传错**不报错，只会静默返回空**（SQLite 弱类型，`'u_abc' = 1` 只是不相等）。详见 `docs/ARCHITECTURE.md` 5.34。

## 2. 环境与依赖

- **Python** 3.10+；**pip** + `requirements.txt`
- **核心依赖** `openai>=1.0.0`、`fastapi>=0.100.0`、`uvicorn[standard]>=0.23.0`、`pydantic>=2.0.0`、`python-multipart`、`tavily-python`、`sqlite-web`
- **数据库** SQLite（WAL），`data/users.db`；沙箱 venv 按 Python 小版本分桶
- **前端** 原生 JS/HTML/CSS，无框架、无构建工具
- **时间口径** 全库统一本地时间：代码写 `datetime.now()`，建表默认 `datetime('now','localtime')`。禁用 `CURRENT_TIMESTAMP`（SQLite 中是 UTC），时间字段一律显式传值

**环境变量**（均有默认值）：

| 变量 | 作用 |
|---|---|
| `AGENT_PORT` | 仅 `scripts/deploy.sh` 的健康探测使用，默认 17520；服务本身端口硬编码在 `server/main.py` |
| `SANDBOX_NETWORK_ALLOWLIST` | 沙箱网络出口白名单（逗号分隔主机后缀）；留空即完全禁止外联 |
| `SANDBOX_WRITE_DIRS` | 追加沙箱写白名单目录 |
| `SANDBOX_PIP_INDEX` | 沙箱依赖安装的 pip 镜像源 |
| `REDIS_URL` | 审批与信任模式共享存储；多 worker 部署必须设，留空用进程内存 |
| `AGENT_STREAM_STOP_GRACE` | 客户端断开后后台任务自动取消的宽限秒数，默认 60，0 表示关闭 |

## 3. 常用命令

```bash
pip install -r requirements.txt
python server/main.py          # Web 服务，默认 http://localhost:17520
cd agent && python main.py     # 终端模式
python scripts/run_tests.py    # 全量回归测试
python scripts/clean_orphans.py           # 查孤儿数据（默认只报告，--apply 才删）
```

## 4. 代码规范

**Python**：函数/变量 `snake_case`，类 `PascalCase`，常量 `UPPER_SNAKE`；中文 Google 风格 docstring；导入顺序为标准库 → 第三方 → 项目内部；优先双引号。

**JavaScript（`static/`）**：`camelCase`；请求统一走 `API` 对象（`API.get/post/put/del`）；状态集中在全局 `state`。

**技能（`agent/skills/`）**：每个技能是文件夹，含 `SKILL.md`（YAML frontmatter + Markdown 指令）与 `scripts/`。脚本通过 AST 从 `execute()` 签名与 docstring 生成工具定义，无需手写 schema。系统技能在 `agent/skills/{name}/`，用户技能在 `agent/skills/user/{db_id}/{name}/`。

### 4.1 前端脚本

多文件平铺 + 全局作用域，无模块系统。`static/js/` 下文件按序号顺序在 `index.html` 加载，序号即加载顺序。

1. 新增函数/变量放进职责对应的文件；不好归类时放 `08-picker-ui.js`（通用 UI）或 `01-core.js`（通用工具）
2. **顶层符号名全项目唯一**——多个 `<script>` 共享作用域，重名会静默覆盖。改完跑 `scripts/check_static.py`
3. `12-theme-init.js` 必须最后加载（含 `DOMContentLoaded` 入口）
4. 不引入 ES Module、npm 依赖或构建工具
5. 独立页面（如 `usage.html`）用单独脚本 + IIFE 包裹 + `'use strict'`

新增功能的顺序：在对应 js 文件加函数 → 样式加到 `static/css/` 对应职责文件 → `node --check` → `python3 scripts/check_static.py`。

### 4.2 改动路径

| 要改的东西 | 动哪里 | 注意 |
|---|---|---|
| 加工具能力 | `agent/skills/<name>/SKILL.md` + `scripts/<tool>.py`（含 `execute()`） | 脚本在沙箱跑；额外依赖写 `# DEPENDENCIES:` 注释 |
| 加 API 端点 | `server/routes/` 新模块 → `routes/__init__.py` 的 `routers` 注册 | 字面路径必须排在参数化路径前，否则 `GET /trash` 会被 `GET /{id}` 吃掉 |
| 加表/列 | 核心表在 `server/database.py::init_db()`；`model_providers`/`model_entries` 在 `model_store.py`，`token_usage`/`usage_pending` 在 `usage_store.py`，记忆三表在 `memory_store.py`，`search_usage` 在 `search_runtime.py` | `CREATE TABLE IF NOT EXISTS` + 幂等 `ALTER`；先 `grep` 确认表建在哪个模块，往 `init_db()` 硬塞会与模块自初始化打架 |
| 改 SSE 事件 | 后端 `chat.py` 的 `yield` + 前端 `04-stream.js::handleStreamEvent` | 两处必须同时改；事件名不一致不报错、只静默失效 → 跑 `check_contract.py` |
| 改前端脚本 | `static/js/` 新文件 → `index.html` 按顺序加 `<script>` | 顶层符号唯一；`12-theme-init.js` 永远最后 → 跑 `check_static.py` |
| 改前端请求 | `05-chat-actions.js` / `06-attachments-files.js` 等 | 请求体字段与 `server/models.py` 对齐 → 跑 `check_contract.py` + `test_send_body.py` |
| 改样式 | `static/css/` 对应职责文件 | 文件顺序即层叠顺序 → 跑 `check_static.py` + `test_frontend_flows.py` |
| 改沙箱边界 | `agent/sandbox.py` | 删 `os.*` 前先查标准库是否依赖它（`os.open` → `tempfile`）→ 跑 `test_sandbox_compat.py` |
| 改技能存储 | `skill_editor.py` / `skill_sync.py` / `skill_upload.py` | 上传技能是 DB-only，对账必须跳过它 → 跑 `test_skill_sync.py` + `test_skill_upload.py` |

## 5. 架构约束

### 目录结构

```
agent/                  核心（不依赖 server/）
  agent.py              ReAct 主循环
  agent_pool.py         子 Agent 池（profiles/*.yaml）
  llm.py                LLM 客户端
  tools.py              工具注册与执行分发
  sandbox.py            沙箱：独立 venv + 子进程 + 读写边界 + 命令白名单
  config.py             加密配置
  model_gateway.py      模型能力位（温度 / 视觉）
  temperature.py        温度策略
  logger.py             日志
  skill_registry.py     技能注册（系统 / 用户双仓库、脚本发现、上下文注入）
  skill_editor.py       用户技能 CRUD + 软删除 + 备份恢复
  skill_sync.py         磁盘 ↔ user_skills 表对账（磁盘为真相源）
  skill_risk.py         上传技能风险扫描（P0 拒绝 / P1 需确认 / P2 放行）
  loop_guard.py         迭代级循环防护
  parallel_executor.py  多工具并行
  diagnostics.py        diag_* 管理员工具
  intent_keywords.py    用户级意图关键词
  task_reviewer.py      任务复盘
  file_parser.py        文件解析（含图片多模态）
  tool_secrets.py       工具级密钥
  user_secrets.py       用户级密钥
  crypto_utils.py       加解密
  profiles/             子 Agent 配置
  skills/               系统技能 + user/{db_id}/ + .trash/

server/                 Web 层
  main.py               应用入口、安全响应头、端口检查、启动对账
  database.py           SQLite 访问层（表操作唯一入口）
  models.py             请求/响应模型
  agent_audit.py        执行审计
  approval_store.py     审批状态机（按 session 隔离）
  trust_store.py        信任模式（request / full）
  kv.py                 上述两者的共享后端（内存 / Redis）
  usage_store.py        Token 用量
  memory_store.py       长期记忆
  model_store.py        供应商与模型池
  search_client.py      搜索与抓取客户端
  search_runtime.py     搜索缓存与计数
  user_tools.py         用户技能脚本注册为工具
  cleanup_scheduler.py  后台清理
  _restart_helper.py    自重启辅助进程
  routes/               auth / chat / config / models / files / sessions / skills /
                        skill_upload / upload / users / memory / approval / usage / audit

static/
  index.html            主界面
  login.html            登录页（单文件，不共用 css）
  usage.html + usage.js 用量看板
  js/                   15 个，顺序加载、共享全局作用域
  css/                  19 个，顺序即层叠顺序
  img/                  登录页背景

scripts/                运维脚本 + 回归测试
data/                   运行时数据（不提交）
document_output/        用户产出（按 public_id 隔离，不提交）
tool_sandbox/           沙箱 venv（不提交）
logs/                   日志（不提交）
```

### 约束

- 依赖方向 `server/` → `agent/`，不可反向；`agent/` 可独立运行
- 数据库只通过 `server/database.py` 访问，用 `_get_connection()` 取线程本地连接
- 工具由 `skill_registry.py` 从 `agent/skills/` 加载脚本，AST 解析生成定义，`ToolRegistry.register_tool()` 注册执行器
- 路由：登录页 `/static/login.html`，主页 `/static/index.html`，API 前缀 `/api/`
- **状态持久化**：思考模式（`off`/`low`/`high`）存 `localStorage` 且同步到服务端 `model_configs.thinking_mode`，两端须一致；温度策略仅存服务端，前端不缓存
- **并发切换**：`switchSession` 用 `_switchLock` + `_switchSeqId` 防并发混乱，`deleteSession` 用 `_deleteLock`
- **消息字段兼容**：`search`/`thought`/`tools` 均为可选字段，新增时保持向后兼容；`renderHistoryMessage()` 通过 `hasMeta` 判断是否用三段式布局

### 最容易踩的 9 条

1. **双 ID**：DB 整数外键、沙箱 venv 目录、trust owner 用 `db_id`；JWT、前端、文件路径用 `public_id`。传错静默返回空
2. **沙箱里删危险函数 ≠ 安全**：`os.open` 删了 `tempfile` 就瘫，pandas/PIL/openpyxl 全废。要换成带同样校验的包装；`_safe_import` 钩子每次 `import os` 都会重删一遍，必须把受控版本装回去
3. **上传技能是 DB-only**（`source='uploaded'`）：磁盘为真相源的对账必须跳过它，否则启动对账会删光用户上传的技能
4. **SSE 事件名与请求体字段前后端必须同时改**：不一致不报错、只静默失效
5. **字面路径路由排在参数化路径之前**
6. **前端无模块系统**：顶层符号唯一，`12-theme-init.js` 最后加载。`$` 是 querySelector 语义，必须带 `#`/`.`
7. **重新生成不带历史图片**：历史不记录原轮上传清单，全发会回到图片叠加问题
8. **迁移脚本要在改目录/改令牌载荷之前跑完**：`migrate_public_id.py` 未跑完就重启，存量用户全部 401
9. **f-string 替换字段不能嵌同种引号**：`f"{d["k"]}"` 仅 3.12+ 支持，3.10/3.11 导入即 SyntaxError

34 个架构小节的完整说明在 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)。

| 主题 | 小节 |
|---|---|
| 文件与消息格式 | 5.1 文档输出隔离 · 5.2 文件预览 · 5.3 消息存储 · 5.4 前端四段式布局 |
| 账号与鉴权 | 5.5 密码与 DB 管理 · 5.18 安全基线 |
| 日志 | 5.6 日志系统 |
| 沙箱与审批 | 5.7 用户沙箱隔离 · 5.11 审批与权限模式 · 5.14 自伤型工具隔离 · 5.26 沙箱兼容性 |
| 技能系统 | 5.8 双仓库 · 5.13 磁盘为真相源 · 5.15 软删除与保留期 · 5.21 用户自助上传 |
| 模型与用量 | 5.16 全局/个人配置隔离 · 5.19 用量双轨 · 5.20 记账时机 · 5.24 中断对账 |
| 上下文与多模态 | 5.17 图片支持 · 5.22 图片只发本轮 + 文档按需注入 |
| 循环与编排 | 5.23 循环防护判据 |
| 可观测与质量 | 5.9 意图关键词 · 5.10 任务复盘 · 5.12 执行审计 · 5.25 质量评估集 · 5.27 流程级缺陷 |
| 会话与权限 | 5.28 `/reset` 是上下文边界 · 5.29 模型可见性在解析层生效 |
| 记忆 | 5.30 记忆是第三份用户数据 · 5.32 段注册表 + 条目存储 · 5.33 进化通道 |
| 存储 | 5.31 数据库连接线程共享，用完不许关 |

## 6. NEVER 规则

1. 硬编码 API Key、密码或密钥 —— 用 `AgentConfig` 加密存储或数据库
2. 在 `agent/` 中导入 `server/` 的任何内容
3. 直接操作 `data/users.db` —— 必须通过 `server/database.py`
4. 用 SQL 修改用户密码 —— 密码加盐哈希，覆盖后原密码不可恢复
5. 在工具 `execution_code` 中执行危险操作 —— 沙箱会拦截，但不应依赖沙箱
6. 修改 `agent/skills/` 中已有技能的 `name` 字段 —— 会破坏已有会话的工具调用记录
7. 在前端引入 npm 依赖或构建工具
8. 在生产代码中保留 `print()` —— 用 `agent.logger` 的 `get_logger()`
9. 修改 `document_output/` 的目录结构或命名规则 —— 沙箱路径替换、文件 API、前端渲染均依赖 `{public_id}/{type_output}/`
10. 修改 `agent/skills/` 中系统技能的脚本 —— 用户技能必须放 `agent/skills/user/{db_id}/`
11. 移除 `chat.py` 的 `MAX_MESSAGE_LENGTH`（10000）或 `sessions.py` 的 `MAX_TITLE_LENGTH`（200）
12. 移除 `chat_stream` 的 `get_session()` 存在性检查
13. 移除 `.session-messages` 的 `display: flex; flex-direction: column` —— 用户消息右对齐依赖它
14. 让 `_classify_skill_risk()` 对用户自建技能返回 `require_approval=False`
15. 移除 `approval_store.py` / `trust_store.py` 中的 `session_id` 归属校验

## 7. 测试

测试脚本在 `scripts/` 下。

```bash
python3 scripts/run_tests.py                  # 全量，自动挑解释器、判依赖
python3 scripts/run_tests.py --list           # 列出用例与依赖
python3 scripts/run_tests.py --only 技能,循环    # 按名字子串筛
python3 scripts/run_tests.py --skip-chrome    # 无 GUI 环境
```

新增测试放 `scripts/test_*.py` 即被自动发现；在 `run_tests.py` 的 `DESC` 补一行说明，并按需登记到 `NEEDS_FASTAPI` / `NEEDS_OPENAI` / `NEEDS_CHROME`。

部分用例依赖特定环境，缺失时总入口会跳过而非失败：

| 依赖 | 脚本 |
|---|---|
| `fastapi` | `test_doc_on_demand`、`test_image_round_isolation`、`test_model_config`、`test_skill_upload`、`test_usage_pending`、`test_security_fixes`、`test_memory_routes`、`test_user_skill_scripts` |
| `openai` | `test_usage_calibration` |
| 无头 Chrome | `test_frontend_flows`、`test_send_body` |
| 真实 Redis | `test_redis_live`（先 `bash scripts/dev_redis.sh start`） |
| 真实沙箱子进程 | `test_sandbox_compat` |

测试脚本统一用 `importlib.util.spec_from_file_location` 按路径加载模块，绕开 `server/__init__.py` 的 fastapi 导入。

### 检查脚本

```bash
python3 scripts/check_static.py       # CSS 变量 / JS 顶层重名 / HTML 引用 / 选择器笔误
python3 scripts/check_contract.py     # 端点 / SSE 事件 / 请求体字段
python3 scripts/test_frontend_flows.py  # 真实页面主流程（无头 Chrome）
python3 scripts/test_sandbox_compat.py  # 沙箱兼容性与安全性
```

质量评估：`scripts/eval_samples.py` 是 75 条样本的唯一真相源，`eval_agent.py` 跑批采集，`build_eval_sheet.py` 汇总成表格。过程类指标自动判；任务达成与幻觉需人工判。

### 前端类型检查

前端是原生 JS 平铺加载，没有构建步骤；`tsconfig.json` 只用于**类型检查**（`noEmit`），不产出任何文件。浏览器直接加载 `static/js/*.js`，行为与之前完全一致。

```bash
npm i                    # 只装 typescript（devDependency），每台机器一次
npm run typecheck        # = tsc --noEmit -p tsconfig.json
```

已接入 `run_tests.py`（`test_frontend_types.py`）：没装 node 或没装依赖时**跳过而非失败**。

它挡的是「数百个顶层符号共享一个全局作用域」带来的两类事故（当前 `check_static.py` 口径下 319 个）：

| 场景 | 能否拦住 |
|---|---|
| 全局函数改名但漏改调用方（如 `showToast`） | 能。实测改一个名字抓出 139 处 |
| 跨文件用了未定义的变量 / 函数 | 能（TS2304） |
| 顶层重名（与 `check_static.py` 互补） | 能（重复声明） |
| 对具体元素调用不存在的方法 | 能（元素类型已知时） |
| 对 `any` 参数瞎调方法 | **不能**——参数是 `any` 时编译器无能为力 |

`static/js/global.d.ts` 是刻意放宽的兜底声明：`$`/`querySelector` 的返回值在标准库里是 `Element`，但代码大量读 `value`/`dataset`/`checked`。把这些成员补声明到 `Element` 上，避免 200+ 条噪音；**新增代码仍应用具体元素类型**（如 `HTMLInputElement`）以获得严格检查。

### 提交前门禁

```bash
python3 scripts/run_tests.py
python3 scripts/check_contract.py --strict
python3 scripts/check_static.py
npm run typecheck
```

## 8. 提交约定

- commit message 用中文，格式 `type: 简短描述`（`feat:` / `fix:` / `refactor:` / `docs:` / `chore:`）
- 只提交、不推送；推送由维护者执行
- 需要测 API 时创建临时测试用户，不改动已有用户的密码或数据
- 需求或接口格式不确定时先询问，不猜端点、参数或业务逻辑
