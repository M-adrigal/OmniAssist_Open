# Agent Framework

一个轻量级 AI Agent 框架，支持工具调用（Function Calling），可通过自然语言动态创建和管理工具，无需编写代码即可扩展 Agent 的能力。

## 特性

- **双界面支持**：提供 Web 前端界面和终端命令行两种交互方式，共享同一套 Agent 核心
- **自然语言创建工具**：用自然语言描述需求，Agent 自动生成完整的工具定义，包括参数、执行逻辑等
- **智能工具生成**：利用 LLM 知识自主确定 API 细节，优先使用免费公开 API，减少手动配置
- **自动验证**：工具创建或修改后自动执行自测，确保工具可用
- **三种执行模式**：
  - `local_execution`：本地执行代码，适用于计算、数据处理、文件生成等
  - `http_request`：调用外部 API，适用于天气查询、汇率查询等
  - `llm_simulated`：由 LLM 直接返回结果，适用于信息查询、内容生成等
- **沙箱隔离**：工具代码在独立环境中执行，带超时保护，确保安全与稳定
- **多会话管理**：支持多轮对话、多会话切换、上下文管理
- **流式响应**：AI 回答实时流式输出，支持中途停止
- **文件管理**：前端可直接查看和下载工具生成的文件

## 快速开始

### 环境要求

- Python 3.8+
- OpenAI 兼容的 API 端点（可使用任何兼容 OpenAI API 格式的服务）

### 安装

```bash
pip install -r requirements.txt
```

### 启动 Web 服务

```bash
python server/main.py
```

服务默认运行在 `http://localhost:17520`。首次使用需在设置中配置模型参数（API Key、Base URL、Model Name），配置一次后重启无需重新输入。

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

## 安全机制

- **执行隔离**：工具代码在独立虚拟环境和子进程中运行，崩溃不影响主服务
- **超时保护**：每次执行设有超时限制，防止死循环
- **模块管控**：自动拦截危险系统模块，仅允许安全的 Python 标准库
- **路径安全**：文件操作限定在指定目录内，防止路径穿越攻击

## 项目结构

```
├── server/              # Web 服务
│   ├── main.py          # 应用入口
│   ├── models.py        # 数据模型
│   └── routes/          # API 路由
├── static/              # 前端静态资源
├── agent/               # Agent 核心
│   ├── main.py          # 终端入口
│   ├── agent.py         # Agent 核心逻辑
│   ├── llm.py           # LLM 客户端
│   ├── config.py        # 配置管理
│   ├── tools.py         # 工具注册与执行
│   ├── tool_builder.py  # 自然语言工具生成
│   ├── sandbox.py       # 安全执行沙箱
│   └── agent_tools/     # 工具定义文件
├── requirements.txt     # Python 依赖
└── README.md
```

## License

MIT