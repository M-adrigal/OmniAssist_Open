# AGENTS.md — AI 编程助手规范

## 1. 项目概述

轻量级 AI Agent 框架，支持自然语言动态创建工具、多用户管理、流式对话。提供 Web 界面和终端两种交互方式。

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

# 查看已安装工具
ls agent/agent_tools/
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

### 工具定义（`agent/agent_tools/*.json`）
- 工具名 `snake_case`，描述用中文
- 参数定义遵循 JSON Schema 规范
- `execution_mode` 三选一：`local_execution` / `http_request` / `llm_simulated`
- `response_formatter`（可选）：Python 代码字符串，直接格式化 API 响应绕过 LLM

## 5. 架构约束

```
agent/                  # Agent 核心（不依赖 server/）
  agent.py              # SimpleAgent 主循环
  llm.py                # LLMClient（OpenAI 兼容）
  tools.py              # ToolRegistry 工具注册
  tool_builder.py       # ToolBuilder 自然语言创建工具
  sandbox.py            # 沙箱隔离执行（含用户目录路径替换）
  config.py             # 加密配置管理
  model_gateway.py      # 多模型参数适配（思考模式/温度等）
  pdf_formatter.py      # PDF 文档格式化引擎（ReportLab）
  document_formatter.py # Word 文档格式化引擎（python-docx）
  excel_formatter.py    # Excel 文档格式化引擎（openpyxl）
  ppt_formatter.py      # PPT 文档格式化引擎（python-pptx）
  agent_tools/          # 工具 JSON 定义文件
server/                 # Web 服务层（依赖 agent/）
  main.py               # FastAPI 应用入口，全局服务初始化
  database.py           # SQLite 操作（线程本地连接，含用户目录管理）
  models.py             # Pydantic 请求/响应模型
  routes/               # API 路由模块
    auth.py             # 登录认证
    chat.py             # 对话（传递 user_id 给沙箱）
    config.py           # 系统配置
    files.py            # 文件列表/下载/预览（用户隔离 + 权限控制）
    sessions.py         # 会话管理
    tools.py            # 工具管理
    users.py            # 用户管理（创建/删除含文件保留选项）
static/                 # 前端静态文件（独立，不依赖 server/ 内部）
  index.html            # 主页
  login.html            # 登录页
  app.js                # 前端逻辑
  style.css             # 样式
```

- **依赖方向**: `server/` → `agent/`，不可反向
- **`agent/` 可独立运行**（终端模式），不依赖 FastAPI
- **数据库**: 仅通过 `server/database.py` 访问，使用 `_get_connection()` 获取线程本地连接
- **工具注册**: 通过 `ToolRegistry.load_tools_from_dir()` 从 JSON 文件加载，`func_factory` 回调创建执行器
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

Office 文档（`.docx .xlsx .pptx`）不支持浏览器端预览，仅提供下载。

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

**思考模式开关**：用户可通过输入框下方按钮切换，状态持久化到 `localStorage`，同时同步到服务端 `model_configs.show_thought`。

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

## 6. NEVER 规则

1. **NEVER** 在代码中硬编码 API Key、密码或密钥 — 使用 `AgentConfig` 加密存储或数据库
2. **NEVER** 在 `agent/` 模块中导入 `server/` 的任何内容 — 保持核心独立
3. **NEVER** 直接操作 `data/users.db` 数据库文件 — 必须通过 `server/database.py` 的函数
4. **NEVER** 直接用 SQL 修改用户密码 — 密码经过加盐哈希，直接覆盖会导致原密码不可恢复；应通过 `database.py` 的 `update_user_password()` 或 Web 界面修改
5. **NEVER** 在工具 `execution_code` 中执行危险操作（文件删除、系统命令、网络外连）— 沙箱会拦截，但不应依赖沙箱
6. **NEVER** 修改 `agent/agent_tools/` 中已有工具的名称（`name` 字段）— 可能破坏已有会话的工具调用记录
7. **NEVER** 在前端引入 npm 依赖或构建工具 — 保持原生 JS 零依赖
8. **NEVER** 在生产代码中保留 `print()` 调试输出 — 使用 `logging` 模块或移除
9. **NEVER** 修改 `document_output/` 的目录结构或命名规则 — 沙箱路径替换、文件列表 API、前端渲染均依赖 `{user_id}/{type_output}/` 结构

## 7. 测试要求

**[待补充]** 项目当前无测试框架。

建议：
- 后端 API 测试：`pytest` + `httpx`（FastAPI 官方推荐）
- Agent 核心测试：`pytest`，mock LLM 响应
- 工具执行测试：针对每个 `agent_tools/*.json` 编写参数化测试

```bash
# 建议添加的开发依赖
pip install pytest httpx
```

## 8. AI 行为指引

- **修改前先阅读**: 理解相关模块的完整上下文后再动手，特别是 `tool_builder.py` 和 `tools.py` 的工具加载链路
- **新增工具**: 在 `agent/agent_tools/` 下创建 JSON 文件即可，服务重启后自动加载，无需改代码
- **新增 API 路由**: 在 `server/routes/` 下创建模块，并在 `__init__.py` 的 `routers` 列表中注册
- **不确定时**: 先询问用户，不要猜测 API 端点、参数格式或业务逻辑
- **修改后验证**: 确保 `python server/main.py` 能正常启动，检查终端无 import 错误
- **数据库变更**: 如需新增表或字段，在 `database.py` 的 `init_db()` 中添加 `CREATE TABLE IF NOT EXISTS`
- **提交规范**: commit message 使用中文，格式 `type: 简短描述`（如 `feat:`, `fix:`, `refactor:`）
- **文件操作**: 所有文档输出路径遵循 `document_output/{user_id}/{type}/` 结构，沙箱会自动替换路径
- **前端文件列表**: 三层嵌套结构（用户 → 类型 → 文件），`renderFileFolder()` 自动检测子节点类型适配
- **测试用账户**: 需要测试 API 时创建临时测试用户，不要修改已有用户的密码或数据
- **密码相关**: 如需重置密码，通过 Web 界面或调用 `database.py` 的 `update_user_password()` 函数
- **消息格式**: 扩展消息字段（`search`/`thought`/`tools`）均为可选，新增时需保持向后兼容，`renderHistoryMessage()` 通过 `hasMeta` 判断是否使用三段式布局
- **SSE 事件**: 新增 SSE 事件类型时需同步更新前端 `app.js` 的事件处理 switch-case 和 `style.css` 样式
- **前端状态**: 思考模式开关状态通过 `localStorage` 持久化，同时同步到服务端 `model_configs.show_thought`，两端保持一致
- **思考过程**: 使用 `<thinking>...</thinking>` 标签包裹，自然独白风格。后端 `_split_thinking()` 解析后分别存储 `thought` 和 `content`，前端实时流式展示并过滤标签
- **登录页**: 支持毛玻璃效果（`backdrop-filter`）、浮动动画光斑（`.bg-orb`）、时段渐变背景（4 个时段 × 亮/暗双模式）。主题切换为三态循环（自动 → 暗色 → 亮色），自动模式跟随系统偏好
- **消息布局顺序**: 思考过程 → 联网搜索 → 工具调用 → 回答。联网搜索结果延迟到思考结束后显示