# OmniAssist

自托管的多用户 AI Agent。能力以「技能」为单位扩展：每个技能是一个文件夹（`SKILL.md` + `scripts/*.py`），
模型按需调用其中的脚本；脚本在每用户独立的 venv 与子进程里执行，敏感操作先经审批。

当前版本见 [`CHANGELOG.md`](CHANGELOG.md)。

## 文档索引

| 文件 | 内容 |
|---|---|
| `README.md` | 项目定位、能力清单、启动方式 |
| `AGENTS.md` | 改代码的规范：目录结构、改动路径、架构约束、测试 |
| `deploy.md` | 生产部署与运维 |
| `CHANGELOG.md` | 版本历史 |

## 能力

| 模块 | 能力 |
|---|---|
| 对话 | 流式输出、中途停止、重新生成、多会话并行、上下文超限自动压缩、`/help` 与 `/reset` |
| 思考过程 | 三档 `off / low / high`，解析 `<thinking>` 标签后单独展示 |
| 图片 | 上传或粘贴，交给模型的视觉能力；只发送本轮图片 |
| 文档 | 按需注入：本轮上传或引用、消息中提及、最近一轮，三类信号触发；其余只在清单里列出 |
| 联网搜索 | 模型自主调用 `web_search`；供应商可选 DeepSeek 官方 Search 或 Tavily；结果与抓取内容分别缓存；失败时明确标记，不当作资料使用 |
| 长期记忆 | 分段结构，注册表可扩展；手写或从会话提炼（提炼出的条目需勾选才落库）；每次改动留档可恢复；关闭开关只停注入，不删内容 |
| 文件 | 上传、预览、下载、重命名；产出按 `document_output/{public_id}/` 隔离，回答内可点击 |
| 技能 | 系统技能 + 用自然语言创建 + 上传 zip / SKILL.md（安装前扫描风险）；卸载为软删除可恢复；用户技能的脚本按用户注册为工具 |
| 模型 | 多供应商（OpenAI 兼容）；平台 Key 与用户自配 Key 隔离；温度策略 `auto` / `static`；最大迭代次数可配 |
| 安全 | 对外只暴露不透明 `public_id`；沙箱隔离；敏感操作走审批门与信任模式；执行审计 |
| 观测 | Token 用量区分服务商回传的精确值与本地估算；任务中断与异常对账 |

## 快速开始

要求 Python 3.10+ 与一个 OpenAI 兼容的 API 端点。

```bash
pip install -r requirements.txt
python server/main.py
```

访问 `http://localhost:17520`。首次启动自动建库并生成管理员账号 `admin`，
初始密码随机生成并只打印在启动控制台，首次登录强制改密。
登录后在「设置 → 模型配置」填写 API Key、Base URL 与模型名。

数据库管理界面随服务启动，地址 `http://localhost:17521`，仅管理员可进。

终端模式与 Web 共用同一个 Agent 核心：

```bash
cd agent && python main.py
```

## 目录结构

```
agent/            核心（不依赖 server/）
  agent.py        ReAct 主循环
  tools.py        工具注册与执行分发
  llm.py          LLM 客户端（流式 / 用量上报 / 思考解析）
  sandbox.py      沙箱：独立 venv + 子进程 + 读写边界 + 命令白名单
  skill_*.py      技能加载、编辑、对账、上传风险扫描
  skills/         系统技能 + user/{db_id}/ 用户技能
server/            Web 层（FastAPI）
  main.py         应用入口
  database.py     SQLite 访问层（所有表操作的唯一入口）
  *_store.py      用量、模型、记忆、审批、信任状态存储
  routes/         API 路由模块
static/            前端（原生 JS，无构建工具）
scripts/           运维脚本与回归测试
```

## 执行路径

| 路径 | 执行位置 | 适用范围 |
|---|---|---|
| `local_execution` | 用户沙箱（独立 venv + 子进程） | 绝大多数技能 |
| `http_request` | 主进程 | 调用外部 API；用户上传的技能禁止此路径 |

技能的脚本用 `execute(...)` 声明参数，注册时由 AST 解析成工具定义，工具名即脚本名。
