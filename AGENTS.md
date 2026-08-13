# AGENTS.md — AI 编程助手规范

## 1. 项目概述

轻量级 AI Agent 框架，支持技能系统（Skill）、多 Agent 池、自然语言动态创建技能、多用户管理、流式对话。提供 Web 界面和终端两种交互方式。

**默认管理员账号**: `admin` / `admin123`（首次登录强制修改密码）

## 2. 环境与依赖

- **Python**: 3.14.0
- **包管理器**: pip（`requirements.txt`）
- **核心依赖**: `openai>=1.0.0`, `fastapi>=0.100.0`, `uvicorn[standard]>=0.23.0`, `pydantic>=2.0.0`, `python-multipart`, `tavily-python`, `sqlite-web`
- **数据库**: SQLite（WAL 模式），文件位于 `data/users.db`
- **前端**: 原生 JS/HTML/CSS，无框架，无构建工具
- **环境变量**: 无。所有配置通过 Web 界面或终端命令设置，API Key 加密存储在 `data/.agent_config`

## 3. 常用命令

```bash
# 安装依赖
pip install -r requirements.txt

# 启动 Web 服务（默认 http://localhost:17520）
python server/main.py

# 终端模式
cd agent && python main.py

# 查看已安装技能
ls agent/skills/
```

## 4. 代码风格与规范

### Python
- **命名**: 函数/变量 `snake_case`，类 `PascalCase`，常量 `UPPER_SNAKE`
- **文档字符串**: 使用中文，Google 风格（Args/Returns）
- **导入顺序**: 标准库 → 第三方库 → 项目内部模块
- **类型注解**: 推荐但不强制，关键函数接口应标注
- **字符串**: 优先使用双引号，f-string 用于格式化

### JavaScript（`static/`）
- **命名**: 函数/变量 `camelCase`
- **API 封装**: 统一通过 `API` 对象（`API.get/post/put/del`）发起请求
- **状态管理**: 全局 `state` 对象，无框架

### 技能定义（`agent/skills/`）
- 每个技能是一个文件夹，包含 `SKILL.md`（YAML frontmatter + Markdown 指令）和 `scripts/` 目录
- 脚本通过 AST 自动解析，从 `execute()` 函数签名和 docstring 生成工具定义，无需手写 JSON Schema
- 技能分为系统技能（`agent/skills/{name}/`）和用户技能（`agent/skills/user/{user_id}/{name}/`）

## 5. 架构约束

```
agent/                  # Agent 核心（不依赖 server/）
  agent.py              # SimpleAgent 主循环
  agent_pool.py         # 子 Agent 池管理（多 Agent 委派与调度）
  llm.py                # LLMClient（OpenAI 兼容）
  tools.py              # ToolRegistry 工具注册
  sandbox.py            # 沙箱隔离执行（含用户目录路径替换、SandboxPool）
  config.py             # 加密配置管理
  model_gateway.py      # 多模型参数适配（思考模式/温度等）
  logger.py             # 日志系统（格式化、轮转、上下文注入）
  skill_registry.py     # 技能注册中心（系统/用户双仓库、脚本发现、上下文注入）
  skill_editor.py       # 用户技能编辑器（CRUD）
  intent_keywords.py    # 用户级意图关键词管理
  task_reviewer.py      # 任务复盘系统（执行日志、失败分析、优化建议）
  file_parser.py        # 文件解析器（支持多种格式）
  tool_secrets.py       # 工具密钥管理
  profiles/             # Agent 配置文件（YAML）
    analyst.yaml        # 数据分析 Agent
    document.yaml       # 文档生成 Agent
    researcher.yaml     # 研究搜索 Agent
  skills/               # 技能脚本
    document/           # 文档生成技能（含格式化引擎）
    calculator/         # 计算器
    datetime/           # 日期时间
    weather/            # 天气查询
    web-fetch/          # 网页抓取
    gold-price/         # 金价查询
    lunar-converter/    # 农历转换
    chinese-counter/    # 中文字数统计
    user/               # 用户自定义技能
server/                 # Web 服务层（依赖 agent/）
  main.py               # FastAPI 应用入口，全局服务初始化
  database.py           # SQLite 操作（线程本地连接，含用户目录管理）
  models.py             # Pydantic 请求/响应模型
  routes/               # API 路由模块
    auth.py             # 登录认证
    chat.py             # 对话（传递 user_id 给沙箱，含工具管理预拦截）
    config.py           # 系统配置
    files.py            # 文件列表/下载/预览（用户隔离 + 权限控制）
    sessions.py         # 会话管理
    skills.py           # 技能管理 API
    upload.py           # 文件上传
    users.py            # 用户管理（创建/删除含文件保留选项）
static/                 # 前端静态文件（独立，不依赖 server/ 内部）
  index.html            # 主页
  login.html            # 登录页
  app.js                # 前端逻辑
  style.css             # 样式
  favicon.svg           # 网站图标
```

- **依赖方向**: `server/` → `agent/`，不可反向
- **`agent/` 可独立运行**（终端模式），不依赖 FastAPI
- **数据库**: 仅通过 `server/database.py` 访问，使用 `_get_connection()` 获取线程本地连接
- **工具注册**: 通过 `skill_registry.py` 从 `agent/skills/` 加载脚本，AST 自动解析生成工具定义，`ToolRegistry.register_tool()` 注册执行器
- **前端路由**: 登录页 `/static/login.html`，主页 `/static/index.html`，API 前缀 `/api/`

### 5.1 文档输出与用户文件隔离

**目录结构**:
```
document_output/
  1/                    # 用户 ID 为目录名
    word_output/        # Word 文档
    excel_output/       # Excel 表格
    pdf_output/         # PDF 文档
    ppt_output/         # PPT 演示
    csv_output/         # CSV 文件
    image_output/       # 图片文件
  3/                    # 另一个用户
    ...
```

**工作原理**:
1. **创建用户** → `database.py` 的 `_create_user_directories()` 自动在 `document_output/` 下创建 `{user_id}/` 及 6 个子目录
2. **生成文件** → `sandbox.py` 的 `execute()` 方法接收 `user_id` 参数，自动将代码中的 `document_output/` 路径替换为 `document_output/{user_id}/`，文件存入用户专属目录
3. **查看文件** → `files.py` 的 `list_files()` 根据用户角色返回不同范围：
   - 管理员：看到所有用户的文件，按用户 → 类型 → 文件三层嵌套
   - 普通用户：只看到自己目录下的文件
4. **删除用户** → `users.py` 的 `delete_user_api` 支持 `keep_files` 参数：
   - `keep_files=false`（默认）：连文件一起删除
   - `keep_files=true`：保留文件，仅删除用户记录

**关键函数**:
- `database.py`: `_create_user_directories(user_id)`, `_delete_user_files(user_id)`, `delete_user(user_id, keep_files)`
- `sandbox.py`: `execute(code, params, timeout, user_id)` — 路径替换
- `files.py`: `list_files()` — 三层嵌套结构，`_check_file_access()` — 权限校验
- `files.py`: `preview_file()` — 支持 text/image/pdf 三种预览类型

### 5.2 文件预览

预览 API（`/api/files/preview`）支持三种文件类型：

| 类型 | 扩展名 | 前端渲染方式 |
|------|--------|-------------|
| text | `.txt .md .csv .json .xml .html .css .js .py .log .yaml .yml` | `<pre><code>` 语法高亮 |
| image | `.png .jpg .jpeg .gif .bmp .svg .webp .ico` | `<img>` 标签 |
| pdf | `.pdf` | `<iframe>` 浏览器内置 PDF 阅读器 |
| docx | `.docx` | 提取正文文本（`word/document.xml`）后 `<pre>` 展示 |
| xlsx | `.xlsx` | 解析共享字符串后渲染为 HTML 表格 |
| csv | `.csv` | 直接渲染为 HTML 表格 |

> 注：pptx 等少数格式仍仅提供下载；文本类预览支持的扩展名同 text 行。预览逻辑位于 `server/routes/files.py` 的 `/api/files/preview`。

### 5.3 消息存储格式

会话消息存储在 `sessions.messages` JSON 字段中，格式如下：

```json
[
  {"role": "user", "content": "用户消息"},
  {"role": "assistant", "content": "回答内容（已剥离 <thinking> 标签）", "search": {...}, "thought": "...", "tools": [...]}
]
```

**字段说明**：
- `role`, `content`：必选，所有消息都有
- `content`：存储解析后的纯净回答，不含 `<thinking>...</thinking>` 标签。原始 LLM 输出中的思考内容被提取到 `thought` 字段
- `search`：可选，联网搜索信息 `{query, scenario, results}`，仅当该轮对话使用了联网搜索时存在
- `thought`：可选，模型的思考过程文本（从 `<thinking>` 标签中提取），仅当该轮对话开启了思考模式时存在（多轮思考用 `\n\n` 拼接）
- `tools`：可选，工具调用记录数组 `[{name, arguments, result, error}]`，仅当该轮对话使用了工具时存在

**兼容性**：旧消息只有 `{role, content}`，前端 `renderHistoryMessage()` 自动降级为简单布局。

### 5.4 前端消息布局（四段式）

每条 AI 回复采用四段式可折叠布局，按流程顺序排列：

```
┌─ think-area（折叠）    — 思考过程：模型的推理内容（含工具调用和联网搜索的思考记录）
├─ search-area（折叠）   — 联网搜索：场景、关键词、搜索结果（思考结束后才显示）
├─ tool-summary（折叠）  — 工具调用：工具名、参数、结果、错误标记
└─ answer-area（始终可见）— 最终回答
```

**联网搜索延迟显示**：搜索触发时，搜索信息先融入思考内容（显示"联网搜索: xxx"），不弹出独立区块。思考结束后，搜索折叠区才出现在思考下方。无思考模式时在流结束（`done`）时显示。

**SSE 事件类型**（`chat.py` → 前端 `app.js`）：

| 事件类型 | 说明 | 触发时机 |
|---------|------|---------|
| `web_search` | 联网搜索结果 | 搜索完成后，融入思考内容，不立即显示独立区块 |
| `thought` | 思考内容 | 模型输出工具调用前的推理（实时流式） |
| `tool_call` | 工具调用通知 | 每个工具调用时，融入思考内容 |
| `tool_result` | 工具执行结果 | 每个工具执行完成后，融入思考内容 |
| `tool_summary` | 工具调用汇总 | 所有工具执行完毕，最终回答前 |
| `token` | 回答内容 | 最终回答的流式输出 |
| `status` | 状态提示 | 搜索进度等 |
| `error` | 错误信息 | 发生错误时 |
| `done` | 流结束标记 | 回答完成 |

**思考模式开关**：用户可通过输入框下方按钮在 `关 / 低 / 高` 三态间循环切换，状态持久化到 `localStorage`，同时同步到服务端 `model_configs.thinking_mode`（off/low/high）。

**思考过程格式**：使用自然流畅的独白形式，通过 `<thinking>...</thinking>` 标签包裹。后端 `_split_thinking()` 解析标签，提取思考内容存入 `thought` 字段，纯净回答存入 `content` 字段。前端实时流式展示思考内容（过滤标签），思考结束后自动折叠。

### 5.5 密码与数据库管理

**管理员密码机制**:
- 首次启动：自动创建管理员账户 `admin`，初始密码固定为 `admin123`
- 首次登录：强制修改密码（`must_change_password=1`），修改后才能使用平台功能
- 密码文件：管理员密码以 SHA-256 哈希（`salt:hash` 格式）存储在 `data/.db_web_password`（权限 0o600），用于数据库管理界面认证
- 密码同步：管理员通过 Web 界面修改密码时，自动同步更新 `.db_web_password` 文件（哈希格式）
- 密码文件丢失恢复：启动时检测 `.db_web_password` 是否存在，若丢失则自动重置管理员密码并输出到终端
- 向后兼容：代理同时支持旧版明文格式和新版哈希格式，管理员下次修改密码时自动迁移为哈希

**数据库管理界面**:
- 地址：`http://localhost:17521`（仅管理员可访问）
- 认证：使用 `admin` 用户名 + `.db_web_password` 中的密码（Basic Auth）
- 后端：`sqlite-web` 绑定 `127.0.0.1:17523`，通过认证代理转发
- 依赖：需安装 `sqlite-web`（`pip install sqlite-web`）

**关键文件**:
- `data/users.db` — SQLite 数据库（WAL 模式）
- `data/.db_web_password` — 管理员密码哈希（0o600 权限）
- `data/.agent_config` — 加密的模型配置（0o600 权限）
- `data/.agent_salt` — 加密盐值（0o600 权限）

### 5.6 日志系统

日志系统（`agent/logger.py`）提供统一的结构化日志输出，支持轮转和上下文注入。

**日志格式**: `[时间戳] [级别] [模块名] [user:N] [sess:xxx] 消息内容`

**日志级别**: DEBUG / INFO / WARNING / ERROR / CRITICAL

**日志文件**:
- `logs/app.log` — 全量日志（INFO 及以上），按天轮转 + 10MB 大小限制，保留 30 天
- `logs/error.log` — 错误日志（仅 ERROR 及以上），独立存储

**上下文注入**: 通过 `set_context(user_id, session_id)` 和 `clear_context()` 实现线程安全的用户/会话上下文注入。

**模块名约定**:
| 模块名 | 文件 |
|--------|------|
| `agent.se` | `server/main.py` |
| `agent.au` | `server/routes/auth.py` |
| `agent.ch` | `server/routes/chat.py` |
| `agent.db` | `server/database.py` |
| `agent.ll` | `agent/llm.py` |
| `agent.sa` | `agent/sandbox.py` |
| `agent.po` | `agent/agent_pool.py` |
| `agent.sk` | `agent/skill_registry.py` |

**NEVER 规则**:
- **NEVER** 使用 `print()` 输出调试信息 — 使用 `logger.debug/info/warning/error`

### 5.7 用户沙箱隔离

每位用户拥有独立的沙箱虚拟环境，避免多用户共享依赖池导致的版本冲突。

**目录结构**:
```
tool_sandbox/
  1/                    # 用户 ID
    venv/               # 独立的 Python 虚拟环境
    deps.json           # 已安装依赖列表
  2/                    # 另一个用户
    venv/
    deps.json
```

**工作原理**:
1. **沙箱池** → `sandbox.py` 的 `SandboxPool` 管理所有用户沙箱，懒加载创建
2. **依赖安装** → 通过 AST 解析脚本中的 `import` 语句，自动提取并安装依赖
3. **线程安全** → `SandboxPool.get_sandbox(user_id)` 确保线程安全的沙箱获取与释放

**关键函数**:
- `sandbox.py`: `SandboxPool.get_sandbox(user_id)` / `release_sandbox(user_id)` / `destroy_sandbox(user_id)`
- `sandbox.py`: `ToolSandbox._parse_imports(code)` — AST 依赖提取
- `sandbox.py`: `ToolSandbox.install(packages)` — 静默安装
- `sandbox.py`: `ToolSandbox.install_verbose(packages)` — 带输出安装

### 5.8 技能系统（双仓库）

技能系统支持系统技能和用户技能两套仓库，物理隔离。

**目录结构**:
```
agent/skills/
  calculator/           # 系统技能（只读，仅管理员可维护）
  document/
  ...
  user/                 # 用户技能
    1/                  # 用户 ID
      my_skill/         # 用户自定义技能
    2/
      my_skill/
```

**关键规则**:
- 系统技能由 `skill_registry.py` 从 `agent/skills/{name}/` 加载，仅管理员可创建/修改/删除
- 用户技能存储在 `agent/skills/user/{user_id}/{name}/`，用户可自由管理
- 同名用户技能覆盖系统技能（用户优先级更高）
- 技能开关（enabled/disabled）通过 `skill_registry.py` 的 `toggle_skill()` 控制

**关键文件**:
- `agent/skill_registry.py` — 技能注册中心（系统/用户双仓库）
- `agent/skill_editor.py` — 用户技能 CRUD 操作
- `server/routes/skills.py` — 技能管理 API

### 5.9 意图关键词系统

意图关键词系统（`agent/intent_keywords.py`）用于基于用户输入动态匹配工具，减少 LLM 请求的 token 开销。

**工作原理**:
1. 每个用户维护独立的意图关键词配置（JSON 文件）
2. 用户输入时，先匹配关键词，再注入相关工具到 LLM 上下文
3. 支持动态优化：任务复盘后可自动更新关键词

**关键函数**:
- `intent_keywords.py`: `match_intent(user_id, message)` — 意图匹配
- `intent_keywords.py`: `update_keywords(user_id, intent, keywords)` — 更新关键词

### 5.10 任务复盘系统

任务复盘系统（`agent/task_reviewer.py`）记录任务执行日志，分析失败模式，生成优化建议。

**工作原理**:
1. 每次工具调用完成后，记录执行日志（JSONL 格式）
2. 分析失败模式：超时、依赖缺失、API 错误等
3. 生成 Skill 优化建议，自动更新意图关键词

**关键函数**:
- `task_reviewer.py`: `log_task_execution(user_id, session_id, tools)` — 记录任务日志
- `task_reviewer.py`: `analyze_failures(user_id)` — 分析失败模式
- `task_reviewer.py`: `generate_suggestions(user_id)` — 生成优化建议

### 5.11 审批与权限模式系统

平台对敏感操作提供**双权限模式**与**聊天框审批门**，防止 LLM 自动执行不可信（用户自建）代码。

**两种权限模式（会话级，由 `server/trust_store.py` 管理）**：
- `request`（请求批准，**默认**）：敏感操作执行前需在聊天框逐项确认
- `full`（完全访问权限）：敏感操作直接执行，不再弹确认卡片（提权模式，前端持续提示）

**风险分级（登录时由 `server/main.py` 的 `_classify_skill_risk()` 判定，写入工具 `require_approval`）**：
- 系统内置技能（`is_system=True`）：视为可信，在已加固沙箱中执行，**免审批**
- 用户自建技能 local_execution：标为 `exec`，`require_approval=True` → 必经审批门
- 用户自建技能 http_request：标为 `write`，`require_approval=True` → 必经审批门

**审批状态机（由 `server/approval_store.py` 管理，进程内 asyncio.Future）**：
- `ApprovalStore.create()` 创建一组待确认请求（含若干 item），持有 `asyncio.Future`
- Agent 循环在工具执行前 `await future`；`POST /api/chat/{session_id}/approve` 在同一事件循环内 `set_result` 唤醒，**不阻塞事件循环**
- 三选项 **allow / reject / skip**，前端点击即执行；`resolve_item()` 增量累积决议，全部项齐备后释放 future
- `cancel_session()`：任务被停止/取消时整组记为 reject 并清理
- 以 `session_id + group_id` 隔离，多用户并发互不影响

**关键端点**（`server/routes/approval.py`）：
- `POST /api/chat/{session_id}/approve` — 提交决议（校验会话归属，防越权确认他人会话），写入 `logs/audit.log`
- `GET/POST /api/chat/{session_id}/trust` — 查询/切换权限模式（同样校验归属）

**信任模式（角色级免确认）**：持有 `tools:execute_sensitive` 权限的角色可在信任模式下免除逐项确认。

> ⚠️ 部署注意：审批状态机与信任状态机均为**单进程内存态**；若启用多 worker（如 `gunicorn -w N`），需替换为 Redis 等跨进程共享存储（接口保持一致）。

**关键文件**：
- `server/approval_store.py` — 审批状态机（Future + 增量决议）
- `server/trust_store.py` — 会话级权限模式状态机 + 统一 `audit_log()`
- `server/routes/approval.py` — 审批/信任 API 端点
- `server/main.py` — `_classify_skill_risk()` 风险分级

## 6. NEVER 规则

1. **NEVER** 在代码中硬编码 API Key、密码或密钥 — 使用 `AgentConfig` 加密存储或数据库
2. **NEVER** 在 `agent/` 模块中导入 `server/` 的任何内容 — 保持核心独立
3. **NEVER** 直接操作 `data/users.db` 数据库文件 — 必须通过 `server/database.py` 的函数
4. **NEVER** 直接用 SQL 修改用户密码 — 密码经过加盐哈希，直接覆盖会导致原密码不可恢复；应通过 `database.py` 的 `update_user_password()` 或 Web 界面修改
5. **NEVER** 在工具 `execution_code` 中执行危险操作（文件删除、系统命令、网络外连）— 沙箱会拦截，但不应依赖沙箱
6. **NEVER** 修改 `agent/skills/` 中已有技能的名称（`name` 字段）— 可能破坏已有会话的工具调用记录
7. **NEVER** 在前端引入 npm 依赖或构建工具 — 保持原生 JS 零依赖
8. **NEVER** 在生产代码中保留 `print()` 调试输出 — 使用 `agent.logger` 模块的 `get_logger()` 获取日志器，按级别输出
9. **NEVER** 修改 `document_output/` 的目录结构或命名规则 — 沙箱路径替换、文件列表 API、前端渲染均依赖 `{user_id}/{type_output}/` 结构
10. **NEVER** 修改 `agent/skills/` 中系统技能的脚本文件 — 系统技能仅管理员可维护，用户自定义技能应存储在 `agent/skills/user/{user_id}/` 下
11. **NEVER** 移除 `chat.py` 中的 `MAX_MESSAGE_LENGTH`（10000）和 `sessions.py` 中的 `MAX_TITLE_LENGTH`（200）输入校验 — 防止资源耗尽攻击
12. **NEVER** 移除 `chat_stream` 中的 `get_session()` 会话存在性检查 — 不存在会话必须返回 404
13. **NEVER** 移除 `.session-messages` 的 `display: flex; flex-direction: column` CSS — 用户消息右对齐（`align-self: flex-end`）依赖父容器为 flex 容器
14. **NEVER** 修改 `server/main.py` 的 `_classify_skill_risk()` 使不可信（用户自建）技能返回 `require_approval=False` — 否则 LLM 会自动执行用户自定义的危险代码，破坏审批门纵深防御
15. **NEVER** 在 `server/approval_store.py` / `server/trust_store.py` 中移除 `session_id` 归属校验 — 审批与权限模式以会话隔离，防越权确认/切换他人会话

## 7. 测试

测试脚本位于 `tests/scripts/`，覆盖 API 冒烟、并发压力、安全边界和暴力测试：

```bash
# 一键全量测试（~4万 token）
bash tests/scripts/run_all.sh

# 快速模式（跳过 LLM 调用，0 token）
bash tests/scripts/run_all.sh --fast

# 按需单独运行
bash tests/scripts/run_all.sh --smoke       # 冒烟测试（API 全链路）
bash tests/scripts/run_all.sh --concurrent  # 并发压力测试
bash tests/scripts/run_all.sh --security    # 安全边界测试
bash tests/scripts/run_all.sh --stress      # 暴力测试（专找 Bug）
```

测试方案文档：`tests/test_plan.md`（120+ 用例，覆盖 API/前端/安全/集成/压力 5 个维度）

```bash
# 建议添加的开发依赖
pip install pytest httpx
```

## 8. AI 行为指引

- **修改前先阅读**: 理解相关模块的完整上下文后再动手，特别是 `tools.py` 的工具注册链路、`skill_registry.py` 的技能加载机制和 `sandbox.py` 的安全机制
- **技能系统**: 技能脚本位于 `agent/skills/`，通过 `skill_registry.py` 注册、加载和上下文注入。用户技能通过 `skill_editor.py` 管理（CRUD），存储在 `agent/skills/user/{user_id}/` 下
- **Agent 池**: 子 Agent 定义在 `agent/profiles/*.yaml`，通过 `agent_pool.py` 管理和调度，以 Agent-as-Tool 模式注册到主 Agent 工具列表
- **意图关键词**: 用户级关键词配置由 `intent_keywords.py` 管理，用于按需选择工具减少 token 开销
- **任务复盘**: `task_reviewer.py` 记录每次工具调用日志，支持失败分析和自动生成优化建议
- **工具密钥**: `tool_secrets.py` 管理工具级 API Key，支持加密存储和会话隔离
- **新增 API 路由**: 在 `server/routes/` 下创建模块，并在 `__init__.py` 的 `routers` 列表中注册
- **不确定时**: 先询问用户，不要猜测 API 端点、参数格式或业务逻辑
- **修改后验证**: 确保 `python server/main.py` 能正常启动，检查终端无 import 错误
- **调试输出**: 使用 `from agent.logger import get_logger` + `logger = get_logger(__name__)` 替代 `print()`，日志自动写入 `logs/app.log`
- **日志级别**: DEBUG 用于详细调试信息，INFO 用于关键流程，WARNING 用于异常但可恢复，ERROR 用于错误
- **数据库变更**: 如需新增表或字段，在 `database.py` 的 `init_db()` 中添加 `CREATE TABLE IF NOT EXISTS`
- **提交规范**: commit message 使用中文，格式 `type: 简短描述`（如 `feat:`, `fix:`, `refactor:`）
- **文件操作**: 所有文档输出路径遵循 `document_output/{user_id}/{type}/` 结构，沙箱会自动替换路径
- **前端文件列表**: 三层嵌套结构（用户 → 类型 → 文件），`renderFileFolder()` 自动检测子节点类型适配
- **测试用账户**: 需要测试 API 时创建临时测试用户，不要修改已有用户的密码或数据
- **密码相关**: 如需重置密码，通过 Web 界面或调用 `database.py` 的 `update_user_password()` 函数
- **消息格式**: 扩展消息字段（`search`/`thought`/`tools`）均为可选，新增时需保持向后兼容，`renderHistoryMessage()` 通过 `hasMeta` 判断是否使用三段式布局
- **SSE 事件**: 新增 SSE 事件类型时需同步更新前端 `app.js` 的事件处理 switch-case 和 `style.css` 样式
- **前端状态**: 思考模式开关状态（off/low/high）通过 `localStorage` 持久化，同时同步到服务端 `model_configs.thinking_mode`，两端保持一致
- **思考过程**: 使用 `<thinking>...</thinking>` 标签包裹，自然独白风格。后端 `_split_thinking()` 解析后分别存储 `thought` 和 `content`，前端实时流式展示并过滤标签
- **登录页**: 支持毛玻璃效果（`backdrop-filter`）、浮动动画光斑（`.bg-orb`）、时段渐变背景（4 个时段 × 亮/暗双模式）。主题切换为三态循环（自动 → 暗色 → 亮色），自动模式跟随系统偏好
- **消息布局顺序**: 思考过程 → 联网搜索 → 工具调用 → 回答。联网搜索结果延迟到思考结束后显示
- **输入校验**: `chat.py` 的 `chat_stream` 入口校验消息长度（`MAX_MESSAGE_LENGTH=10000`，超长返回 413）和会话存在性（`get_session()` 不存在返回 404），`sessions.py` 的创建/改名接口校验标题长度（`MAX_TITLE_LENGTH=200`）
- **前端 DOM 隔离**: 每个会话使用独立的 `.session-messages` 容器（`showSessionContainer`/`removeSessionContainer`），切换会话时 show/hide 而非销毁 DOM。`.session-messages` 必须为 flex 容器（`display: flex; flex-direction: column`），否则用户消息右对齐失效
- **并发操作锁**: `switchSession` 使用 `_switchLock` + `_switchSeqId` 防止并发切换混乱，`deleteSession` 使用 `_deleteLock` 防止并发删除
- **测试脚本**: 测试位于 `tests/scripts/`，修改前端逻辑后应运行 `bash tests/scripts/run_all.sh --fast` 验证 API 层无回归