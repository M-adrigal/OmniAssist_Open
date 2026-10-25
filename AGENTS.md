# AGENTS.md — AI 编程助手规范

## 0. 这份文档是什么 / 不是什么

本文件是**改代码时的规矩**：架构约束、踩过的坑、验证手段。**不写**「怎么部署」（去 `deploy.md`）、**不写**「这版本改了什么」（去 `CHANGELOG.md`）、**不写**「项目有什么功能」（去 `README.md`）。

同一事实只允许存在于一份文档里。发现两处重复，删掉一处改成引用 —— 重复就是下一次矛盾的起点。

## 1. 项目概述

轻量级 AI Agent 框架，支持技能系统（Skill）、多 Agent 池、自然语言动态创建技能、多用户管理、流式对话。提供 Web 界面和终端两种交互方式。

**默认管理员账号**: `admin` / **初始密码随机生成**，只在首次启动的控制台打印（`print` 到 stdout，**不进 `logs/`** —— 日志文件管理员可下载）。首次登录强制改密。任何文档里写死固定口令都是过期信息，别抄。

**当前版本**: v2.1.0（历史见 `CHANGELOG.md`）。身份体系为**双 ID**：对外不透明 `public_id`（UUID 前缀 `u_`）用于 JWT / 前端 / 文件目录 `document_output/{public_id}/`；内部整数自增 `db_id` 仅用于数据库关联与权限网关（`get_user_role`）、沙箱 venv 隔离。**写任何 DB 整数外键列、整数技能目录、trust owner 的地方必须用 `db_id`；`public_id` 仅用于对外标识与文件路径**。升级部署前必须先跑 `scripts/migrate_public_id.py` 回填并改名（详见 `deploy.md`），否则存量用户全员 401 锁死。

## 2. 环境与依赖

- **Python**: 3.10+（本机开发与测试用 3.14；共享 venv 按 Python 小版本分桶，跨版本不会互相污染）
- **包管理器**: pip（`requirements.txt`）
- **核心依赖**: `openai>=1.0.0`, `fastapi>=0.100.0`, `uvicorn[standard]>=0.23.0`, `pydantic>=2.0.0`, `python-multipart`, `tavily-python`, `sqlite-web`
- **数据库**: SQLite（WAL 模式），文件位于 `data/users.db`
- **前端**: 原生 JS/HTML/CSS，无框架，无构建工具
- **模型配置**: 走 Web 界面 / 终端命令，API Key 加密存储在 `data/.agent_config`
- **运维环境变量**（都有默认值，正常不需要设）:

| 变量 | 作用 |
|---|---|
| `AGENT_PORT` | 服务端口，默认 17520（`scripts/deploy.sh` 也读它） |
| `SANDBOX_NETWORK_ALLOWLIST` | 沙箱网络出口白名单（逗号分隔主机后缀）；**留空 = 完全禁止外联**，最安全 |
| `SANDBOX_DISABLE_NETWORK` | 设为 1 时熔断沙箱网络（即使白名单非空） |
| `SANDBOX_WRITE_DIRS` | 额外追加的沙箱写白名单目录 |
| `SANDBOX_PIP_INDEX` | 沙箱依赖安装的 pip 镜像源 |

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

### 4.1 前端脚本规范（`static/js/`）

**架构：多文件平铺 + 全局作用域，无模块系统、无构建工具。**

主界面脚本按职责拆成 **13 个文件**（`static/js/01-core.js` ~ `13-skill-panel.js`），
在 `index.html` 末尾以**多个 `<script>` 按序号顺序**加载。拆分方式为
**连续切分**（每个文件对应原 `app.js` 的一段连续行区间），因此加载顺序即原代码顺序，
行为与拆分前完全等价。

**硬性规则**：

1. **新增函数/变量必须放进职责对应的文件**，不要往别的文件里塞；确实不好归类时优先放 `08-picker-ui.js`（通用 UI）或 `01-core.js`（通用工具）
2. **顶层符号名必须全项目唯一**。多个 `<script>` 共享同一个全局作用域，跨文件重名会**静默覆盖**（后加载的赢），不报错——这类 bug 极难查。`scripts/check_static.py` 的「JS 顶层重名」检查会扫 `static/` 与 `static/js/` 两个目录，改完必须跑一次
3. **`12-theme-init.js` 必须最后一个加载**——它包含 `DOMContentLoaded` 初始化入口，会调用前面所有文件的函数
4. **不要引入 ES Module / npm 依赖 / 构建工具**（见 NEVER 规则第 7 条）。若确需模块化，先讨论
5. **独立的页面**（如 `usage.html`）用单独脚本文件 + **IIFE 包裹 + `'use strict'`**（参考 `static/usage.js`），避免污染主界面全局

**新增一个功能的推荐顺序**：
```
① 在对应职责的 js/ 文件里加函数（确认符号名不与其它文件重名）
② 样式加到 static/css/ 下对应职责的文件（注意 .dark 覆盖与相对顺序）
③ node --check static/js/xx.js  验证语法
④ python3 scripts/check_static.py  验证无重名/无缺失引用
```

> 2026-10-23 首次拆分：`app.js` 4,980 行 → 12 个文件（最大 727 行，其中 664 行是天然的
> `DOMContentLoaded` 初始化入口，无法再分）；`style.css` 同步拆成 `css/` 下 16 个文件。
> 2026-10-24 新增技能面板 → js 变 **13 个**、css 变 **17 个**（`13-skill-panel.js` / `17-skill-panel.css`）。
> 验证结果：274 个符号集合一致、23 项 DOM 行为一致、运行时报错 0 条、截图 1,728 万像素逐字节一致。
>
> 拆分用的四个一次性工具（`analyze_js.py` / `split_js.py` / `split_css.py` / `css_cascade.py`）
> 与当时的验证脚本已在 2026-10-24 随冗余清理一并移出仓库 —— 拆分是一次性工程，
> 原文件在 git 历史里；**现在的等价性保障是 `check_static.py` + `test_frontend_flows.py`**。
> 真要再拆一次，从 git 历史取回脚本即可：`git log --all --diff-filter=D -- scripts/split_js.py`。

### 4.2 常用改动路径

改哪块代码，先看这里的顺序与坑位；具体约束在后面第 5 章对应小节。

| 要改的东西 | 动哪里 | 必须注意 |
|---|---|---|
| 加一个工具能力 | `agent/skills/<name>/SKILL.md` + `scripts/<tool>.py`（含 `execute()`） | 脚本在沙箱跑；需要额外依赖写 `# DEPENDENCIES:` 注释 |
| 加一个 API 端点 | `server/routes/` 新模块 → `routes/__init__.py` 的 `routers` 注册 | **注册顺序**：字面路径要排在参数化路径前，否则 `GET /trash` 会被 `GET /{id}` 吃掉 |
| 加一张表 / 一列 | `server/database.py::init_db()` | `CREATE TABLE IF NOT EXISTS` + 幂等 `ALTER`，老库要能平滑升 |
| 改 SSE 事件 | 后端 `chat.py` 的 `yield` + 前端 `04-stream.js::handleStreamEvent` | 两处必须同时改，事件名不一致不会报错、只会静默失效 → 跑 `check_contract.py` |
| 改前端脚本 | `static/js/` 新文件 → `index.html` 按顺序加 `<script>` | 顶层符号全项目唯一；`12-theme-init.js` 永远最后 → 跑 `check_static.py` |
| 改前端请求 | `05-chat-actions.js` / `06-attachments-files.js` 等 | 请求体字段要与 `server/models.py` 对齐 → 跑 `check_contract.py` + `test_send_body.py` |
| 改样式 | `static/css/` 对应职责文件 | 文件顺序即层叠顺序；新规则靠后加载更稳 → 跑 `check_static.py` + `test_frontend_flows.py` |
| 改沙箱边界 | `agent/sandbox.py` | 删 `os.*` 前先想标准库有没有依赖它（`os.open` → `tempfile`）→ 跑 `test_sandbox_compat.py` |
| 改技能存储 | `skill_editor.py` / `skill_sync.py` / `skill_upload.py` | 上传技能是 DB-only，对账必须跳过它 → 跑 `test_skill_sync.py` + `test_skill_upload.py` |

## 5. 架构约束（索引 + 目录树）

> 本章只放三样东西：**目录树**（文件在哪）、**索引**（哪个话题在第几节）、**最容易踩的 8 条**。
> 33 个小节的完整说明、事故复盘与踩坑记录已外置到 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) —— 那是查阅型资料，不占常驻上下文。

```
agent/                  # Agent 核心（不依赖 server/）
  agent.py              # SimpleAgent 主循环
  agent_pool.py         # 子 Agent 池管理（多 Agent 委派与调度）
  llm.py                # LLMClient（OpenAI 兼容）
  tools.py              # ToolRegistry 工具注册
  sandbox.py            # 沙箱隔离执行（含用户目录路径替换、SandboxPool）
  config.py             # 加密配置管理
  model_gateway.py      # 多模型参数适配（思考模式/温度等）
  temperature.py        # 温度策略（Auto 按任务类型发散 / Static 固定）
  logger.py             # 日志系统（格式化、轮转、上下文注入）
  skill_registry.py     # 技能注册中心（系统/用户双仓库、脚本发现、上下文注入）
  skill_editor.py       # 用户技能编辑器（CRUD + 软删除 + 备份恢复）
  skill_sync.py         # 用户技能「磁盘 ↔ user_skills 表」同步（磁盘为真相源）
  loop_guard.py         # 迭代级循环防护（连续失败 / 权限不足 / 调用签名重复）
  diagnostics.py        # 管理员诊断工具（读日志/查库/读文件/重启服务，全部 admin 限定）
  intent_keywords.py    # 用户级意图关键词管理
  parallel_executor.py  # 多工具并行执行
  task_reviewer.py      # 任务复盘系统（执行日志、失败分析、优化建议）
  file_parser.py        # 文件解析器（支持多种格式）
  tool_secrets.py       # 工具密钥管理
  user_secrets.py       # 用户级密钥管理
  crypto_utils.py       # 加解密工具
  profiles/             # Agent 配置文件（YAML）
    analyst.yaml        # 数据分析 Agent
    document.yaml       # 文档生成 Agent
    researcher.yaml     # 研究搜索 Agent
  skills/               # 技能脚本
    document/           # 文档生成技能（含格式化引擎）
    calculator/         # 计算器
    datetime/           # 日期时间
    web-fetch/          # 网页抓取
    lunar-converter/    # 农历转换
    chinese-counter/    # 中文字数统计
    user/               # 用户自定义技能（.trash/ 为软删除备份区）
server/                 # Web 服务层（依赖 agent/）
  main.py               # FastAPI 应用入口，全局服务初始化（含 lifespan：启动对账/清理）
  database.py           # SQLite 操作（线程本地连接，含用户目录管理）
  agent_audit.py        # 任务执行审计（工具调用/审批决策，落 logs/agent_audit.log）
  cleanup_scheduler.py  # 后台清理调度（技能备份 30 天过期自动删）
  approval_store.py     # 审批状态机（进程内，按 session 隔离）
  trust_store.py        # 会话信任模式（request / full）
  usage_store.py        # Token 用量统计
  model_store.py        # 模型配置存储
  models.py             # Pydantic 请求/响应模型
  routes/               # API 路由模块
    auth.py             # 登录认证
    chat.py             # 对话（传递 user_id 给沙箱，含工具管理预拦截）
    config.py           # 系统配置
    models.py           # 模型与供应商配置
    files.py            # 文件列表/下载/预览（用户隔离 + 权限控制）
    sessions.py         # 会话管理
    skills.py           # 技能管理 API
    upload.py           # 文件上传
    users.py            # 用户管理（创建/删除含文件保留选项）
    approval.py         # 审批提交与信任模式切换
    usage.py            # Token 用量统计查询
    audit.py            # 执行审计查询（admin 限定）
static/                 # 前端静态文件（独立，不依赖 server/ 内部）
  index.html            # 主页
  login.html            # 登录页（照片背景，独立样式不共用 style.css）
  usage.html            # Token 用量看板页
  js/                   # 脚本（按职责拆分，13 个文件，加载顺序见下方注）
    01-core.js          #   API/state/通用工具（$/$$/toast/confirm/format）/会话容器
    02-sessions.js      #   会话管理（列表/搜索/增删/切换/任务轮询）
    03-messages.js      #   消息渲染（气泡/历史/压缩历史/内容渲染/操作行）
    04-stream.js        #   流式渲染（流容器/工具摘要/文件预览/审批卡片/搜索思考区/SSE）
    05-chat-actions.js  #   对话动作（任务订阅/发送/重新生成/停止）
    06-attachments-files.js # 附件上传 chips + 云文件列表
    07-skills.js        #   技能管理
    08-picker-ui.js     #   通用 UI（文件选择器/命令建议/抽屉/模态/信任 UI）
    09-config.js        #   配置中心（模型列表/供应商/模型池/搜索配置/温度）
    10-model-switch.js  #   模型切换菜单 + 思考模式开关
    11-users-files.js   #   用户管理 + 文件库
    13-skill-panel.js   #   「+」二级技能面板（搜索/勾选/上传/管理入口）
    12-theme-init.js    #   主题切换 + DOMContentLoaded 入口（**必须最后加载**）
    ── 注意：实际加载顺序是 …11 → 13 → 12。12 是全局入口（DOMContentLoaded 里
       绑定所有模块的事件），必须最后；13 是后加的技能面板，编号顺手取了 13。
       **编号 ≠ 加载顺序**，以此处列出的顺序为准（= index.html 里 <script> 的先后）。
  usage.js              # 用量看板逻辑（IIFE 隔离，独立页面）
  css/                  # 样式（按职责拆分，17 个文件，顺序即层叠顺序）
    01-variables.css    #   主题变量（:root / .dark）——必须最先加载
    02-base.css         #   基础层：重置、body、#app 骨架
    03-sidebar.css      #   侧边栏 + 设置抽屉
    04-chat-area.css    #   主聊天区域
    05-buttons.css      #   按钮
    06-modals.css       #   模态框
    07-dialog-controls.css
    08-tables.css
    09-form-controls.css
    10-message.css      #   消息体三段式、搜索区、思考区
    11-attachments.css
    12-file-picker.css
    13-skills-approval.css
    14-overlays.css
    15-model-switcher.css
    16-model-config.css
    17-skill-panel.css  #   技能二级面板 + 上传弹窗 + 技能管理弹窗视觉重构（后加载，覆盖 13 的同名规则）
  favicon.svg           # 网站图标
scripts/                # 运维脚本 + 回归测试（2026-10-24 清理后只留「现在还在用」的）
  deploy.sh             # 一键部署
  migrate_public_id.py  # public_id 迁移（升级必跑）
  build_sandbox_venv.py # 沙箱 venv 构建
  check_static.py       # 静态冒烟（CSS 变量 / JS 重名 / HTML 引用）
  check_contract.py     # 前后端契约对账（端点 / SSE 事件 / 请求体字段）
  smoke_js_load.py      # 无头浏览器加载冒烟（js/ 全加载 + 零错误 + 表单在位）
  sync_skills_db.py     # 用户技能磁盘↔表对账 CLI
  audit_query.py        # 任务执行审计查询 CLI
  calibrate_tokens.py   # 用量自校准系数
  security_audit_admin.py # 权限溢出安全审计
  eval/                 # 质量评估集（cases.json + eval_runner.py）
  test_*.py             # 17 套回归测试（清单与依赖要求见第 7 节）
```

> **样式文件的层叠依赖**：`static/css/` 下的文件靠 `<link>` 的先后顺序决定层叠，
> 新增样式时请插入到对应职责文件，并确认插入位置不破坏层叠顺序。
> 改完样式跑 `python3 scripts/check_static.py`（变量/引用）+ `python3 scripts/test_frontend_flows.py`（真实页面渲染）。

- **依赖方向**: `server/` → `agent/`，不可反向
- **`agent/` 可独立运行**（终端模式），不依赖 FastAPI
- **数据库**: 仅通过 `server/database.py` 访问，使用 `_get_connection()` 获取线程本地连接
- **工具注册**: 通过 `skill_registry.py` 从 `agent/skills/` 加载脚本，AST 自动解析生成工具定义，`ToolRegistry.register_tool()` 注册执行器
- **前端路由**: 登录页 `/static/login.html`，主页 `/static/index.html`，API 前缀 `/api/`

**33 个架构小节的完整说明在 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)** —— 改到哪块，先按下面索引定位小节号，再去读那一节。

| 主题 | 小节 |
|---|---|
| 文件与消息格式 | 5.1 文档输出隔离 · 5.2 文件预览 · 5.3 消息存储 · 5.4 前端四段式布局 |
| 账号与鉴权 | 5.5 密码与 DB 管理 · 5.18 安全基线 |
| 沙箱与审批 | 5.7 用户沙箱隔离 · 5.11 审批与权限模式 · 5.14 自伤型工具隔离 · 5.26 沙箱兼容性踩坑 |
| 技能系统 | 5.8 双仓库 · 5.13 磁盘为真相源 · 5.15 软删除与保留期 · 5.21 用户自助上传 |
| 模型与用量 | 5.16 全局/个人配置隔离 · 5.19 用量双轨 · 5.20 记账时机 · 5.24 中断对账 |
| 上下文与多模态 | 5.17 图片支持 · 5.22 图片只发本轮 + 文档按需注入 |
| 循环与编排 | 5.23 循环防护判据 |
| 可观测与质量 | 5.9 意图关键词 · 5.10 任务复盘 · 5.12 执行审计 · 5.25 质量评估集 · 5.27 流程级缺陷盘点 |
| 会话与权限 | 5.28 `/reset` 是上下文边界（不删历史） · 5.29 模型可见性在解析层生效 |
| 记忆 | 5.30 记忆是第三份用户数据（两条写入通道 / 上限 / 关≠删 / 注入位置） |
| 存储 | 5.31 数据库连接线程共享，用完不许关 |
| 记忆结构 | 5.32 段注册表 + 条目存储（加段只改一处）+ 每次改动留档可恢复 |
| 记忆结构 | 5.33 进化通道：模型从对话提炼，只提议不落库 + 归一化去重 |

### 5.0 最容易踩的 8 条（结论常驻；每条在 ARCHITECTURE 里都有完整来龙去脉）

1. **双 ID 铁律**：写 DB 整数外键列 / 沙箱 venv 目录 / trust owner 用 `db_id`；JWT、前端、文件路径用 `public_id`。归属比对传错一个，查询会**静默**返回空而不是报错。
2. **沙箱里「删掉危险函数」≠ 安全**：`os.open` 删了 `tempfile` 就瘫（pandas/PIL/openpyxl 全废）。要换成带同样校验的包装，并且 `_safe_import` 钩子**每次 `import os` 都会重删一遍**，必须把受控版本装回去。
3. **上传技能是 DB-only**（`source='uploaded'`）：任何「磁盘为真相源」的对账都必须跳过它，否则服务启动对账会把用户上传的技能删光。
4. **SSE 事件名 / 请求体字段前后端必须同时改**：名字对不上不报错，只会静默失效（用户看到的是「没反应」）。改完跑 `check_contract.py`。
5. **字面路径路由要排在参数化路径之前**：否则 `GET /trash` 会被 `GET /{id}` 吃掉并报 422。
6. **前端无模块系统**：新增顶层符号必须全项目唯一；`12-theme-init.js` 永远最后加载（它是全局入口）。
   另外 `$` 是 **querySelector 语义，必须带 `#`/`.`** —— 写成 `$('some-id')` 查不到元素，函数里 `if (!el) return` 会静默变成空操作（回收站「永远加载中」就是这么来的）。`check_static.py` 第 4 项会拦。
7. **重新生成不带历史图片**：历史不记录原轮上传清单，全发就会回到「图片叠加」；原消息带图时 UI 必须提示，不许静默降级。
8. **迁移脚本要在「改目录 / 改令牌载荷」之前跑完**：`migrate_public_id.py` 没跑完就重启，存量用户全部 401 锁死。

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

测试脚本在 `scripts/` 下，21 个（20 个 `test_*.py` + `security_audit_admin.py`）。
**解释器选错会误报"依赖缺失"**，按下表用对：

| 依赖 | 脚本 |
|---|---|
| 需 `fastapi`（用系统 Python `/usr/local/bin/python3`） | `test_doc_on_demand`、`test_image_round_isolation`、`test_model_config`、`test_skill_upload`、`test_usage_pending`、`test_memory_routes` |
| 需 `openai` | `test_usage_calibration` |
| 需本机 Chrome（无头） | `test_frontend_flows`、`test_send_body` |
| 需真实沙箱子进程 | `test_sandbox_compat` |
| 零依赖，任何解释器 | `test_loop_guard`、`test_skill_delete`、`test_trash_cleanup`、`test_skill_sync`、`test_agent_audit`、`test_fixes_integration`、`test_security_fixes`、`test_image_support`、`test_ctx_reset_and_model_gate`、`test_memory`、`security_audit_admin` |

```bash
# 全量：一条命令。总入口会自己挑解释器（fastapi 们用系统 python3）、判 Chrome、汇总
python3 scripts/run_tests.py
python3 scripts/run_tests.py --list           # 有哪些用例、各自依赖
python3 scripts/run_tests.py --only 技能,循环   # 名字子串筛
```

> 加新测试：放 `scripts/test_*.py` 即被自动发现；**在 `run_tests.py` 的 `DESC` 里补一行说明**，否则清单里显示「(未登记)」。
> 需要特殊依赖的，同步登记到 `NEEDS_FASTAPI` / `NEEDS_OPENAI` / `NEEDS_CHROME`。

| 脚本 | 守什么 |
|---|---|
| `test_loop_guard` | 循环防护判据（含线上误杀场景回归） |
| `test_skill_delete` / `test_trash_cleanup` / `test_skill_sync` | 技能软删除 / 备份保留期 / 磁盘↔表对账 |
| `test_skill_upload` | 上传技能 45 项（结构校验 / 风险分级 / 软删恢复 / 回收站永久删除 / 导出 zip / 元数据） |
| `test_image_round_isolation` | 图片只发本轮 16 项 |
| `test_doc_on_demand` | 文档按需注入 23 项 |
| `test_image_support` | 图片多模态链路 45 项 |
| `test_usage_calibration` | 用量双轨 / 图片计数 / 单价语义 43 项 |
| `test_usage_pending` | 任务中断与异常对账 24 项 |
| `test_model_config` | 模型配置路由级 47 项（全局隔离 / 编辑 / 自定义供应商 / 用量拆分） |
| `test_security_fixes` | 安全基线 63 项（含 X-Frame-Options / MIME 白名单 / 归属校验） |
| `test_agent_audit` | 执行审计（脱敏 / 落盘 / 查询） |
| `test_fixes_integration` | 历史修复项集成 |
| `test_sandbox_compat` | 沙箱兼容性 × 安全性（openpyxl/tempfile 可用 + 越权仍被拒） |
| `test_send_body` | 前端发送请求体（真实 JS + stub fetch，抓作用域错） |
| `test_frontend_flows` | 前端主流程运行时探针（会话 / 技能面板 / 流事件 / 审批 / 记忆面板 28 项） |
| `security_audit_admin` | 权限溢出审计（篡改 token / 伪造签名 / 过期 token） |
| `test_ctx_reset_and_model_gate` | `/reset` 上下文边界裁剪 + 模型可见性门禁 22 项 |
| `test_memory` | 记忆 102 项（四段读写 / 两层上限 / 导入导出 / 注入 / 条目 / 留档恢复 / 迁移 / 进化通道） |
| `test_memory_routes` | 记忆接口 41 项（归属隔离 / 两层超限 400 / 关≠删 / 整理与提炼的结局） |

> 路由级测试（`test_model_config`）用 `importlib.util.spec_from_file_location` 按文件加载真实模块 +
> 临时库 + 可切换身份的 auth stub，绕开 `server/__init__.py` 的 fastapi 导入。

非测试类验证脚本：

```bash
python3 scripts/check_static.py       # 静态冒烟（CSS 变量 / JS 重名 / HTML 引用 / 选择器笔误）
python3 scripts/check_contract.py     # 前后端契约对账（端点 / SSE 事件 / 请求体字段）
python3 scripts/smoke_js_load.py      # 无头浏览器加载冒烟（js/ 全加载 + 零错误 + 表单在位）
python3 scripts/test_send_body.py     # 前端发送请求体（真实 JS + stub fetch，抓作用域/参数错）
python3 scripts/test_frontend_flows.py  # 前端主流程运行时探针（会话/技能/流事件/审批/文件）
python3 scripts/test_sandbox_compat.py  # 沙箱兼容性×安全性（openpyxl/tempfile 可用 + 越权仍被拒）
python3 scripts/sync_skills_db.py     # 技能磁盘↔表对账
python3 scripts/audit_query.py --all  # 查任务执行审计轨迹
python3 scripts/calibrate_tokens.py --samples --fit  # 用量自校准系数
```

> 前端验证的三层分工：`check_static` 查静态结构（变量/重名/引用）→ `smoke_js_load` 查加载期错误
> → `test_frontend_flows` / `test_send_body` 查真实运行行为（作用域、事件处理、渲染结果）。
> 2026-10-24 清理掉了 13 个一次性脚本（拆分器、`shot_*` 截图探针、`verify_*` 老验证），
> 需要时从 git 历史取回。

**质量评估集**（需服务在跑 + 登录 cookie；用例设计与判定口径见 `docs/ARCHITECTURE.md` 5.25）：

```bash
python3 scripts/eval/eval_runner.py --cookie <auth_token>            # 全量 20 条（只跑硬断言）
python3 scripts/eval/eval_runner.py --cookie <auth_token> --judge    # 加 LLM 评委打分
```

> ⚠️ 本机常未安装 fastapi/uvicorn，**测试脚本统一用 `importlib.util.spec_from_file_location`
> 按文件路径加载模块**，绕开 `server/__init__.py` 的 fastapi 导入。
> 浏览器类脚本（smoke/shot/verify）走无头 Chrome（`--no-sandbox`），并把 `/static/...`
> 重写为 `file://` 绝对路径——`file://` 下绝对路径会指到文件系统根。

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
- **SSE 事件**: 新增 SSE 事件类型时需同步更新前端 `static/js/04-stream.js` 的事件处理 switch-case 与 `static/css/` 下对应样式文件
- **模型配置归属**: 全局配置（`/api/config/providers`、`/api/config/pool`）只管理平台级行；用户自配供应商与模型是个人私有数据，只能通过 `/my-models`、`/my-providers` 端点由本人访问。改这两组接口时**绝不能放宽归属过滤**（详见 `docs/ARCHITECTURE.md` 5.16）
- **前端状态**: 思考模式开关状态（off/low/high）通过 `localStorage` 持久化，同时同步到服务端 `model_configs.thinking_mode`，两端保持一致；温度策略（`temperature_mode`/`temperature`）仅存于服务端 `model_configs`，通过 `/api/config` 与 `/api/config/global` 读写并热更新，前端设置面板新增「温度策略」下拉与温度值输入
- **思考过程**: 使用 `<thinking>...</thinking>` 标签包裹，自然独白风格。后端 `_split_thinking()` 解析后分别存储 `thought` 和 `content`，前端实时流式展示并过滤标签
- **登录页**: 使用真实照片背景（`static/img/bg-{light,dark}.jpg`）+ 三层场景叠加（照片/色调/暗角，浅色多一层薄纱）；左右为同一种玻璃的两种厚度（左侧展示区更薄、右侧表单区更厚），接缝用两道内阴影做出折光；主题切换为三态循环（自动 → 暗色 → 亮色），自动模式跟随系统偏好。登录页为单文件（不共用 `static/css/`）
- **消息布局顺序**: 思考过程 → 联网搜索 → 工具调用 → 回答。联网搜索结果延迟到思考结束后显示
- **输入校验**: `chat.py` 的 `chat_stream` 入口校验消息长度（`MAX_MESSAGE_LENGTH=10000`，超长返回 413）和会话存在性（`get_session()` 不存在返回 404），`sessions.py` 的创建/改名接口校验标题长度（`MAX_TITLE_LENGTH=200`）
- **前端 DOM 隔离**: 每个会话使用独立的 `.session-messages` 容器（`showSessionContainer`/`removeSessionContainer`），切换会话时 show/hide 而非销毁 DOM。`.session-messages` 必须为 flex 容器（`display: flex; flex-direction: column`），否则用户消息右对齐失效
- **并发操作锁**: `switchSession` 使用 `_switchLock` + `_switchSeqId` 防止并发切换混乱，`deleteSession` 使用 `_deleteLock` 防止并发删除
- **修改后验证**: 改动 `agent/`|`server/` 跑对应的 `test_*.py`；改动 `static/` 跑 `check_static.py` + `test_frontend_flows.py`；动了 SSE 事件名/请求体字段再跑 `check_contract.py`
- **删除脚本前**: 先 `grep` 确认没有代码引用（只有文档提到不算依赖），再同步改 AGENTS/README 里的清单 —— 否则留下的是一份指向空气的文档
