# Agent Framework

一个轻量级 AI Agent 框架，支持工具调用（Function Calling），可通过自然语言动态创建和管理工具，无需编写代码即可扩展 Agent 的能力。

## 特性

- **双界面支持**：提供 Web 前端界面和终端命令行两种交互方式，共享同一套 Agent 核心
- **多用户系统**：支持用户注册、登录、权限管理（RBAC），每位用户可独立配置模型参数
- **自然语言创建工具**：用自然语言描述需求，Agent 自动生成完整的工具定义，包括参数、执行逻辑等
- **智能工具生成**：利用 LLM 知识自主确定 API 细节，优先使用免费公开 API，减少手动配置
- **自动验证**：工具创建或修改后自动执行自测，确保工具可用
- **三种执行模式**：
  - `local_execution`：本地执行代码，适用于计算、数据处理、文件生成等
  - `http_request`：调用外部 API，适用于天气查询、汇率查询等
  - `llm_simulated`：由 LLM 直接返回结果，适用于信息查询、内容生成等
- **联网搜索**：支持 Tavily 搜索引擎，可智能识别用户意图（实时信息、事实知识、教程指南、对比分析等场景）并自动优化搜索策略
- **沙箱隔离**：工具代码在独立 venv 虚拟环境和子进程中执行，带超时保护，确保安全与稳定
- **多会话管理**：支持多轮对话、多会话切换、上下文管理，会话持久化存储
- **流式响应**：AI 回答实时流式输出，支持中途停止
- **文件管理**：前端可浏览、预览和下载工具生成的文件，点击文件名即可在新标签页中预览（支持 PDF、图片、文本等格式）
- **上下文压缩**：支持设置上下文 token 上限（如 32k、64k、128k），自动压缩历史消息避免超限

## 快速开始

### 环境要求

- Python 3.8+
- OpenAI 兼容的 API 端点（可使用任何兼容 OpenAI API 格式的服务）
- （可选）Tavily API Key，用于联网搜索功能

### 安装

```bash
pip install -r requirements.txt
```

### 启动 Web 服务

```bash
python server/main.py
```

服务默认运行在 `http://localhost:17520`。

首次使用会自动创建 SQLite 数据库并生成默认管理员账户（用户名 `admin`，随机密码会输出在终端）。登录后可在设置中配置模型参数（API Key、Base URL、Model Name）和 Tavily 搜索 API Key，配置后重启无需重新输入。

管理员可在用户管理页面创建其他用户，并分配不同角色权限。

### 终端模式

```bash
cd agent
python main.py
```

进入交互界面后，使用 `/model set` 命令配置模型参数。

## 使用示例

```
用户：今天是几号？
Agent：今天是 2026 年 5 月 7 日。

用户：帮我算一下 123 * 456
Agent：123 × 456 = 56088

用户：帮我写一篇文章，保存为 Word 文档
Agent：Word 文档已保存至 document_output/word_output/

用户：最近有什么科技新闻？（需配置 Tavily API Key）
Agent：[自动联网搜索并整理最新科技新闻]
```

## 终端命令

| 命令 | 说明 |
| --- | --- |
| `/help` | 显示帮助信息 |
| `exit` | 退出程序 |
| `reset` | 重置对话上下文 |
| `/tool list` | 查看所有已安装的工具 |
| `/tool add` | 通过自然语言新增工具 |
| `/tool update <工具名>` | 修改已有工具 |
| `/tool delete <工具名>` | 删除指定工具 |
| `/model set` | 配置模型参数（API Key 加密存储） |
| `/model show` | 查看当前模型配置 |
| `/model update` | 修改单个配置项 |
| `/agent thought on` | 开启 Agent 思考过程显示 |
| `/agent thought off` | 关闭 Agent 思考过程显示 |

## 创建自定义工具

用自然语言描述你需要的工具，Agent 会自动完成分析、生成、验证全流程：

```
/tool add
请输入工具描述：帮我创建一个可以查询任意城市实时天气的工具
```

Agent 会自动完成以下步骤：

1. **分析需求**：理解工具用途，自动确定所需 API 和执行方式
2. **生成定义**：生成完整的工具配置，包括参数、执行逻辑等
3. **安装依赖**：如需第三方库，引导安装
4. **自测验证**：自动执行测试，确保工具可用
5. **立即可用**：保存后即可在对话中调用

## 联网搜索

项目集成了 Tavily 搜索引擎，支持智能场景识别：

| 场景 | 说明 |
| --- | --- |
| 实时信息 | 天气、股价、新闻等时效性查询，自动附加当前日期 |
| 事实知识 | 百科类知识查询，搜索结果作为补充参考 |
| 最新动态 | 版本更新、趋势动态，按时间倒序整理 |
| 教程指南 | 操作步骤类查询，整理为清晰的分步指南 |
| 对比分析 | 多事物对比，从多维度系统分析 |
| 本地化信息 | 地点相关查询，确认地点一致性 |
| 通用搜索 | 默认搜索模式 |

在 Web 界面设置中配置 Tavily API Key（可在 [https://tavily.com](https://tavily.com) 免费注册获取）即可启用。

## 用户与权限

系统内置基于角色的访问控制（RBAC）：

| 角色 | 权限 |
| --- | --- |
| `admin` | 全部权限：用户管理、全局模型配置、工具管理、搜索配置等 |
| `user` | 基础权限：对话、会话管理、个人模型配置 |

管理员可在 Web 界面创建、编辑、删除用户，分配角色。

## 安全机制

- **用户认证**：基于 HMAC-SHA256 签名的 Token 认证，24 小时过期
- **密码加密**：SHA256 哈希存储，不保存明文
- **API Key 加密**：敏感信息使用 XOR + SHA256 派生密钥加密存储
- **执行隔离**：工具代码在独立 venv 虚拟环境和子进程中运行，崩溃不影响主服务
- **超时保护**：每次执行设有超时限制，防止死循环
- **模块管控**：自动拦截危险系统模块，仅允许安全的 Python 标准库
- **路径安全**：文件操作限定在指定目录内，防止路径穿越攻击
- **权限校验**：API 路由层进行角色权限校验，防止越权操作

## 项目结构

```
├── server/               # Web 服务
│   ├── main.py           # 应用入口，全局服务初始化
│   ├── database.py       # SQLite 数据库（用户、会话、配置、权限）
│   ├── models.py         # Pydantic 数据模型
│   └── routes/           # API 路由
│       ├── auth.py       # 认证（登录/登出/Token）
│       ├── chat.py       # 聊天（流式对话/联网搜索）
│       ├── config.py     # 模型与搜索配置
│       ├── files.py      # 文件浏览、预览与下载
│       ├── sessions.py   # 会话管理
│       ├── tools.py      # 工具管理 API
│       └── users.py      # 用户管理
├── static/               # 前端静态资源
│   ├── index.html        # 主页面
│   ├── login.html        # 登录页面
│   ├── app.js            # 前端逻辑
│   └── style.css         # 样式
├── agent/                # Agent 核心
│   ├── main.py           # 终端入口
│   ├── agent.py          # Agent 核心逻辑（多轮对话/上下文压缩）
│   ├── llm.py            # LLM 客户端（OpenAI 兼容）
│   ├── config.py         # 配置管理（加密存储）
│   ├── tools.py          # 工具注册与执行
│   ├── tool_builder.py   # 自然语言工具生成（分析/生成/修复）
│   ├── sandbox.py        # 安全执行沙箱（venv + 子进程）
│   ├── pdf_formatter.py  # PDF 文档格式化引擎（ReportLab）
│   ├── document_formatter.py  # Word 文档格式化引擎（python-docx）
│   ├── excel_formatter.py     # Excel 文档格式化引擎（openpyxl）
│   ├── ppt_formatter.py       # PPT 文档格式化引擎（python-pptx）
│   └── agent_tools/      # 工具定义 JSON 文件
├── document_output/      # 用户文件输出目录（按用户 ID 隔离）
├── requirements.txt      # Python 依赖
├── README.md             # 项目说明
└── deploy.md             # 部署指南
```

## License

MIT