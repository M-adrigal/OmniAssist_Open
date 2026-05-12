# Agent Framework

一个轻量级的 AI Agent 框架，支持工具调用（Function Calling），可通过自然语言动态创建和管理工具，无需编写代码即可扩展 Agent 的能力。

## 特性

- **双界面支持**：Web 前端界面（推荐）和终端命令行，共享同一套 Agent 核心
- **自然语言创建工具**：用自然语言描述需求，Agent 自动生成完整的工具定义（包括 API 端点、参数、执行代码等）
- **智能工具生成**：利用 LLM 知识自主确定 API 细节，优先使用免费公开 API，尽量减少用户输入
- **工具自测**：工具创建或修改后自动用样本参数执行测试，验证工具是否能正常工作
- **重复工具检测**：创建新工具时自动检测是否与已有工具功能重复，提示用户选择继续创建或优化现有工具
- **自动输出目录管理**：文件输出类工具（Word、Excel、PDF 等）自动创建对应的输出目录，并带有路径安全检查
- **三种执行模式**：
  - `local_execution`：本地执行 Python 代码，适用于精确计算、数据处理、编码转换、文件生成等
  - `http_request`：发起真实 HTTP 请求调用外部 API，适用于天气查询、汇率查询等
  - `llm_simulated`：由 LLM 返回结果，适用于信息查询、内容生成等
- **沙箱隔离执行**：工具代码在独立虚拟环境和子进程中执行，崩溃不影响主服务，带超时保护
- **安全模块管控**：自动拦截危险模块（subprocess、socket、shutil 等），安全化 os 模块（移除 system、popen 等危险函数）
- **对话管理**：支持多轮对话、多会话切换、上下文重置、历史轮数限制
- **思考过程显示**：可开启/关闭 Agent 的思考过程，便于调试和理解 Agent 决策
- **安全配置存储**：API Key 使用文件盐加密存储，重启或切换网络后无需重新配置
- **流式响应**：AI 回答实时流式输出，支持中途停止生成
- **文件管理**：前端可直接查看和下载工具生成的文件

## 快速开始

### 环境要求

- Python 3.8+
- OpenAI 兼容的 API 端点（可使用任何兼容 OpenAI API 格式的服务）

### 安装

```bash
pip install -r requirements.txt
```

### 启动 Web 服务（推荐）

```bash
python server/main.py
```

首次使用需在左下角设置中配置模型参数（API Key、Base URL、Model Name），配置一次后重启无需重新输入。

### 终端模式

如果偏好命令行交互：

```bash
cd agent
python main.py
```

进入交互界面后，使用 `/model set` 命令配置模型参数。

## Web 前端功能

- **多会话管理**：左侧边栏支持创建、切换、删除对话会话，首次对话自动创建会话
- **流式对话**：AI 回答实时流式输出，右下角蓝色按钮在生成中变为停止按钮
- **命令面板**：输入 `/` 查看可用命令，支持键盘上下选择
- **设置抽屉**：左下角设置按钮打开设置面板，可配置模型、管理工具、查看文件
- **文件列表**：查看和下载工具生成的所有输出文件
- **工具管理**：查看已安装工具列表，支持新增和删除工具

## 使用示例

```
用户：今天是几号？
Agent：[调用 get_current_datetime 工具] 今天是 2026 年 5 月 7 日。

用户：帮我算一下 123 * 456
Agent：[调用 simple_calculator 工具] 123 × 456 = 56088

用户：2026年10月1日是农历几号？
Agent：[调用 convert_gregorian_to_lunar 工具] 2026年10月1日是农历八月廿一...

用户：帮我写一篇关于RAG的文章，保存为Word文档
Agent：[调用 save_to_word 工具] Word文档已成功保存至: Document_output/RAG是什么.docx
```

## 终端命令参考

| 命令                        | 说明                             |
| --------------------------- | -------------------------------- |
| `/help`                     | 显示帮助信息                     |
| `exit`                      | 退出程序                         |
| `reset`                     | 重置对话上下文                   |
| `/tool list`                | 查看所有已安装的工具             |
| `/tool add`                 | 通过自然语言新增工具             |
| `/tool update <工具名>`     | 通过自然语言修改已有工具         |
| `/tool delete <工具名>`     | 删除指定工具                     |
| `/model set`                | 配置模型参数（API Key 加密存储） |
| `/model show`               | 查看当前模型配置                 |
| `/model update`             | 修改单个配置项                   |
| `/agent thought on\|off`    | 开启/关闭思考过程显示            |

## 创建自定义工具

用自然语言描述你需要的工具，Agent 会自动生成完整的工具定义。例如：

```
/tool add
请输入工具描述：帮我创建一个可以查询任意城市实时天气的工具
```

Agent 会：

1. **分析需求**：利用知识储备自主确定 API 细节（如使用 wttr.in 免费天气 API）
2. **重复检测**：自动检测是否与已有工具功能重复，如有重复则提示用户选择继续创建或优化现有工具
3. **生成工具**：生成包含 API 端点、请求方式、参数定义、响应解析的完整工具 JSON
4. **自动创建输出目录**：如果是文件输出类工具，自动在项目根目录创建对应的输出文件夹
5. **安装依赖**：如果工具需要第三方 pip 包，交互式引导用户安装
6. **自测验证**：自动用样本参数执行自测，验证工具能否正常工作
7. **保存生效**：将工具保存到 `agent_tools/` 目录，立即可用

### 工具定义格式

工具以 JSON 文件形式存储在 `agent_tools/` 目录中：

```json
{
  "name": "get_weather",
  "description": "查询指定城市的实时天气情况",
  "parameters": {
    "type": "object",
    "properties": {
      "city": {
        "type": "string",
        "description": "城市名称"
      }
    },
    "required": ["city"]
  },
  "execution_mode": "http_request",
  "execution_prompt": "请根据API返回数据提取天气信息...",
  "http_config": {
    "url": "https://wttr.in/{city}?format=j1",
    "method": "GET",
    "headers": {}
  }
}
```

文件输出类工具示例（Word 文档生成）：

```json
{
  "name": "save_to_word",
  "description": "将模型生成的内容保存到Word文档中",
  "parameters": {
    "type": "object",
    "properties": {
      "content": { "type": "string", "description": "要保存的文本内容" },
      "filename": { "type": "string", "description": "文件名（无需扩展名）" }
    },
    "required": ["content", "filename"]
  },
  "execution_mode": "local_execution",
  "execution_code": "from docx import Document\n...",
  "dependencies": ["python-docx"],
  "output_dir": "Document_output"
}
```

## 沙箱安全机制

工具代码在隔离的沙箱环境中执行，确保安全：

### 执行隔离

- **独立虚拟环境**：所有第三方依赖安装在 `tool_sandbox/venv/` 中，与宿主 Python 环境完全隔离
- **子进程执行**：工具代码在独立子进程中运行，崩溃不影响主服务
- **超时保护**：每次执行有 30 秒超时限制，防止死循环

### 模块管控

**允许的安全模块**：json、math、datetime、re、hashlib、csv、io、zipfile、random、string、itertools、functools、collections、typing、copy、textwrap、uuid、html、xml、struct、binascii、decimal、fractions、statistics

**拦截的危险模块**：subprocess、socket、shutil、ctypes、http、urllib、requests、multiprocessing、threading、asyncio、ssl、signal、pty、fcntl、posix、pickle、shelve、marshal 等

**安全化的 os 模块**：os 模块可正常使用（路径操作、makedirs 等），但以下危险函数被移除：
`system`、`popen`、`execv`、`execve`、`spawn*`、`remove`、`unlink`、`rmdir`、`removedirs`、`renames`、`chmod`、`chown`、`link`、`symlink`、`kill`、`killpg`、`setuid`、`setgid`、`fork`、`forkpty`

### 路径安全

- 工具的输出目录不能以 `..` 或 `/` 开头，防止路径穿越攻击
- 所有文件输出限定在项目根目录下的指定子目录中

## 项目结构

```
├── server/                  # Web 服务
│   ├── main.py              # FastAPI 应用入口，启动 Web 服务
│   ├── models.py            # 数据模型定义
│   └── routes/              # API 路由
│       ├── chat.py          # 对话接口（流式响应）
│       ├── config.py        # 配置管理接口
│       ├── sessions.py      # 会话管理接口
│       ├── tools.py         # 工具管理接口
│       └── files.py         # 文件列表接口
├── static/                  # Web 前端静态资源
│   ├── index.html           # 主页面
│   ├── style.css            # 样式表
│   └── app.js               # 前端逻辑
├── agent/                   # Agent 核心
│   ├── main.py              # 终端交互入口
│   ├── agent.py             # Agent 核心循环，处理多轮对话和工具调用
│   ├── llm.py               # LLM 客户端，封装 OpenAI API 调用
│   ├── config.py            # 配置管理，支持文件盐加密存储 API Key
│   ├── tools.py             # 工具注册表，管理和执行工具
│   ├── tool_builder.py      # 工具构建器，自然语言生成工具定义
│   ├── sandbox.py           # 工具执行沙箱，隔离执行环境与安全管控
│   ├── agent_tools/         # 工具定义文件目录
│   │   ├── get_current_datetime.json
│   │   ├── simple_calculator.json
│   │   ├── convert_gregorian_to_lunar.json
│   │   ├── web_fetch.json
│   │   └── save_to_word.json
│   ├── .agent_config        # 加密的配置文件（已在 .gitignore 中）
│   └── .agent_salt          # 加密盐文件（已在 .gitignore 中）
├── Document_output/         # 文件输出类工具的默认输出目录
├── requirements.txt         # Python 依赖
└── README.md
```