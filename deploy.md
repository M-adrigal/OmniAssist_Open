# 服务器部署指南

## 1. 准备服务器

### 系统要求
- Ubuntu 20.04+ / Debian 11+ / CentOS 8+
- Python 3.10+（兼容 3.10~3.14，本机开发使用 3.14）
- 至少 2GB RAM，10GB 磁盘空间

### 创建部署用户
```bash
# 登录服务器
ssh root@your-server-ip

# 创建部署用户
adduser agent
usermod -aG sudo agent
su - agent
```

## 2. 安装依赖

```bash
# 更新系统
sudo apt update && sudo apt upgrade -y

# 安装 Python 和 Git
sudo apt install -y python3 python3-pip python3-venv git nginx
```

## 3. 部署代码

### 方式一：使用 Git 部署
```bash
# 在服务器上
cd /home/agent
git clone https://github.com/your-repo/Lightweight_agent_service.git
cd Lightweight_agent_service
```

### 方式二：通过 SCP 上传
```bash
# 在本地机器上
scp -r /path/to/Lightweight_agent_service/* agent@your-server-ip:/home/agent/Lightweight_agent_service/
```

## 4. 安装 Python 依赖

```bash
cd /home/agent/Lightweight_agent_service
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 如需在 Linux 服务器上生成中文 PDF（文档技能），需安装系统中文字体：
# sudo apt install -y fonts-noto-cjk
# 安装后字体通常位于 /usr/share/fonts/opentype/noto/ 或 /usr/share/fonts/truetype/noto/
```

## 5. 首次启动与初始化

```bash
# 启动服务
python server/main.py
```

首次启动时会自动完成以下初始化：
- 在 `data/` 目录下创建 SQLite 数据库（`users.db`），包含用户表、会话表、模型配置表、搜索配置表、权限表
- 创建默认管理员账户：用户名 `admin`，密码 `admin123`
- 初始化默认权限（admin 拥有全部权限，user 拥有基础权限）
- 创建密码文件 `data/.db_web_password`（权限 0o600），用于数据库管理界面认证

> ⚠️ **重要**：首次登录后必须修改管理员密码，否则无法使用平台功能。修改密码后，`.db_web_password` 文件会自动同步更新。

启动成功后，通过浏览器访问 `http://your-server-ip:17520` 登录。

### 数据库管理界面

服务启动后会自动启动数据库管理界面（基于 sqlite-web）：

- **地址**：`http://127.0.0.1:17521`（仅监听本地回环，不对外网开放）
- **访问方式**：在服务器本机浏览器直接访问，或通过 SSH 隧道从本地访问：
  `ssh -L 17521:127.0.0.1:17521 agent@your-server-ip`，随后在本机浏览器打开 `http://127.0.0.1:17521`
- **认证**：使用管理员账号 `admin` 和当前密码登录（Basic Auth）
- **依赖**：需安装 `sqlite-web`（已在 `requirements.txt` 中）

> 注意：数据库管理界面仅管理员可访问，普通用户无法通过认证。出于安全考虑已绑定 `127.0.0.1`，**请勿在防火墙上开放 17521 端口**。

## 6. 配置模型与搜索

登录 Web 界面后，在左侧设置面板中配置：

### 模型配置
- **API Key**：你的 LLM API 密钥（加密存储）
- **Base URL**：API 端点地址，如 `https://api.openai.com/v1`
- **Model Name**：模型名称，如 `gpt-4`、`deepseek-v3-2-251201`
- **Context Limit**：上下文 token 上限，如 `32k`、`64k`、`128k`（留空则不限制）
- **最大迭代次数**：单轮对话允许的工具调用/思考循环上限，默认 10（留空用默认）。任务复杂、工具链较长时可适当调大
- **温度策略**：`auto`（默认）或 `static`。`auto` 由 `agent/temperature.py` 按任务类型动态计算每次调用的温度（脑暴/写作较发散，代码/分析较确定），并随迭代轮次向 0.2 收敛；`static` 则全程使用固定温度值，结果可复现
- **温度值**：`temperature_mode=auto` 时作为分析/未知类任务的基准温度，`static` 时作为唯一温度。范围 0~2，默认 0.7。修改后保存即热更新生效，无需重启

管理员可配置"全局模型配置"，供所有用户共享使用；普通用户可配置"个人模型配置"，优先级高于全局配置。

### 搜索配置（可选）
- **Tavily API Key**：用于联网搜索功能，可在 [https://tavily.com](https://tavily.com) 免费注册获取

## 7. 使用 systemd 运行服务

### 创建服务文件
```bash
sudo nano /etc/systemd/system/agent.service
```

```ini
[Unit]
Description=Lightweight Agent Service
After=network.target

[Service]
Type=simple
User=agent
Group=agent
WorkingDirectory=/home/agent/Lightweight_agent_service
Environment="PATH=/home/agent/Lightweight_agent_service/venv/bin"
# 沙箱网络出口白名单（可选，强烈建议保持默认即全阻断）。
# 逗号分隔的主机后缀列表；匹配「主机名等于该项」或「主机名以 .该项 结尾」才放行，
# 其余一律拒绝并记录到 sandbox_audit.log。
# 留空（即不设置此行）= 沙箱子进程完全禁止访问外部网络，最安全。
# 若确有需要让沙箱内技能脚本访问特定外网（如某些需访问外部 API 的技能若改为沙箱运行），
# 再按需放开，例如：
# Environment="SANDBOX_NETWORK_ALLOWLIST=api.example.com,query1.finance.yahoo.com"
ExecStart=/home/agent/Lightweight_agent_service/venv/bin/python server/main.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=agent

[Install]
WantedBy=multi-user.target
```

### 启动服务
```bash
sudo systemctl daemon-reload
sudo systemctl enable agent
sudo systemctl start agent
sudo systemctl status agent
```

## 8. 配置 Nginx 反向代理（推荐）

### 创建 Nginx 配置
```bash
sudo nano /etc/nginx/sites-available/agent
```

```nginx
server {
    listen 80;
    server_name your-domain.com;

    client_max_body_size 50m;

    location / {
        proxy_pass http://127.0.0.1:17520;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_cache_bypass $http_upgrade;
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
    }
}
```

> 注意：由于 AI 对话可能耗时较长，`proxy_read_timeout` 和 `proxy_send_timeout` 需设置足够大。

### 启用站点
```bash
sudo ln -s /etc/nginx/sites-available/agent /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

## 9. 配置防火墙

```bash
sudo ufw allow 22/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
```

## 10. 使用 HTTPS（推荐）

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d your-domain.com
sudo certbot renew --dry-run
```

## 11. 日常维护

### 查看日志

服务内置了日志系统，日志文件位于 `logs/` 目录：

```bash
# 应用日志（全量，INFO 及以上）
tail -f /home/agent/Lightweight_agent_service/logs/app.log

# 错误日志（仅 ERROR 级别）
tail -f /home/agent/Lightweight_agent_service/logs/error.log
```

日志格式：`[时间戳] [级别] [模块名] [user:N] [sess:xxx] 消息内容`

日志配置：
- **轮转策略**：按天轮转 + 单文件 10MB 上限
- **保留时间**：30 天自动清理
- **错误分离**：ERROR 级别日志同时写入独立的 `error.log`
- **审计日志**：审批与权限模式切换事件写入独立的 `audit.log`
- **历史归档**：轮转/清理前的旧日志归入 `logs/archive/`
- **uvicorn 日志已统一接入**：服务启动、访问等 uvicorn 内部日志现统一写入 `logs/app.log`，**不会在根目录产生散落的 `serverN.log`**（若仍出现散落日志，说明日志接管未生效，需检查服务是否正常启动）

```bash
# 服务日志（systemd 方式）
sudo journalctl -u agent -f
sudo tail -f /var/log/nginx/error.log
```

### 更新代码

> **v2.0.0 升级必读**：本版本引入双 ID 体系，升级前必须先跑迁移脚本回填 `public_id` 并重命名目录，否则存量用户会被全部判为未登录（401 锁死）。迁移脚本幂等，可重复执行。

```bash
cd /home/agent/Lightweight_agent_service
git pull
source venv/bin/activate
pip install -r requirements.txt

# 升级到 v2.0.0 及以上：先停服 → 迁移 → 启动（顺序不可颠倒）
sudo systemctl stop agent
python3 scripts/migrate_public_id.py   # 回填 public_id + 重命名 document_output/{整数id}→{public_id} + 补齐产出子目录
sudo systemctl start agent

# 旧版本平滑更新（无双 ID 变更时）可直接 restart：
# sudo systemctl restart agent
```

> 迁移脚本仅重命名目录、不删除文件；`data/` 与 `document_output/` 受 `.gitignore` 保护，`git pull` 不会触碰用户数据。若脚本结尾提示「仍有 N 个用户缺少 public_id」，**禁止启动服务**，需先排查再继续。
>
> （可选）回归校验：在临时副本上验证整数外键修复，不污染真实库 —— `python3 scripts/verify_fk_fix.py`

### 备份数据
```bash
# 备份数据库、配置、文档和日志
tar -czf agent-backup-$(date +%Y%m%d).tar.gz \
    data/ \
    document_output/ \
    logs/
```

### 用户管理
登录 Web 界面后，管理员可在"用户管理"页面：
- 创建新用户（设置用户名、密码、角色）
- 编辑用户信息（修改密码、角色、描述）
- 删除用户

## 12. 故障排除

### 服务无法启动
```bash
# 检查端口占用
sudo ss -tlnp | grep 17520

# 检查 Python 依赖
python3 -c "import fastapi; print('FastAPI OK')"
python3 -c "import openai; print('OpenAI OK')"

# 检查数据库文件权限
ls -la data/users.db

# 手动启动查看错误
cd /home/agent/Lightweight_agent_service
source venv/bin/activate
python server/main.py
```

### 无法访问
```bash
# 检查防火墙
sudo ufw status

# 检查 Nginx
sudo nginx -t
sudo systemctl status nginx

# 检查服务
sudo systemctl status agent
```

### 数据库问题
```bash
# 检查数据库完整性
sqlite3 data/users.db "PRAGMA integrity_check;"

# 查看用户列表
sqlite3 data/users.db "SELECT id, username, user_type FROM users;"
```

### 内存不足
```bash
# 查看内存使用
free -h
top -o %MEM

# 在 systemd 服务文件中限制内存（可选）：
# MemoryHigh=1G
# MemoryMax=2G
```

### 沙箱依赖安装问题

工具的技能脚本（如 `python-docx`、`openpyxl`、`reportlab` 等）运行在独立的沙箱虚拟环境中，与主服务隔离。

**工作原理**：
- 每位用户拥有独立的沙箱 venv，位于 `tool_sandbox/{user_id}/venv/`
- 用户沙箱通过 `PYTHONPATH` 继承**共享基础 venv** `tool_sandbox/shared/pyX.Y/venv`（按 Python 小版本分桶，例如 `py3.14`）中的通用依赖（python-docx / openpyxl / reportlab / python-pptx / lxml / Pillow 等），避免每个用户重复安装重型库
- 共享 venv 的依赖清单见仓库根 `requirements.sandbox.txt`，由 `scripts/build_sandbox_venv.py` 按当前 Python 版本分桶构建
- 技能脚本所需的、共享 venv 中没有的依赖，会在首次执行时通过 AST 解析 `import` 自动安装进该用户自己的 venv
- 沙箱池（SandboxPool）负责管理用户沙箱的懒加载和线程安全调度

**部署后必做**：构建共享基础 venv（否则首跑会按需懒构建，或由每个用户各自安装）：

```bash
cd /home/agent/Lightweight_agent_service
python3 scripts/build_sandbox_venv.py
# 可选：自定义镜像源（默认已用清华源）
SANDBOX_PIP_INDEX=https://pypi.org/simple python3 scripts/build_sandbox_venv.py
```

**常见问题**：

```bash
# 1. 确保 python3-venv 已安装（沙箱需要创建虚拟环境）
sudo apt install -y python3-venv

# 2. 查看共享 venv 与用户沙箱列表
ls -la /home/agent/Lightweight_agent_service/tool_sandbox/shared/
ls -la /home/agent/Lightweight_agent_service/tool_sandbox/

# 3. 手动检查某用户沙箱是否正常（以用户 ID=1 为例）
ls -la /home/agent/Lightweight_agent_service/tool_sandbox/1/venv/bin/python

# 4. 跨 Python 版本注意：共享 venv 与用户 venv 的 Python 小版本必须一致。
#    若用户 venv 是用不同 Python 创建的，沙箱会跳过共享继承、改为在该用户 venv 内单独安装，
#    不会出现「跨版本注入 C 扩展导致 ImportError」的问题。排查版本：
cat /home/agent/Lightweight_agent_service/tool_sandbox/shared/py3.14/venv/pyvenv.cfg
cat /home/agent/Lightweight_agent_service/tool_sandbox/1/venv/pyvenv.cfg

# 5. pip 安装超时（网络慢）：共享 venv 用 SANDBOX_PIP_INDEX 控制镜像源；
#    也可在用户 venv 中设置：
/home/agent/Lightweight_agent_service/tool_sandbox/1/venv/bin/pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple

# 6. 清理并重建某用户沙箱（以用户 ID=1 为例）
rm -rf /home/agent/Lightweight_agent_service/tool_sandbox/1
sudo systemctl restart agent
```

## 13. 性能优化

### 使用 Gunicorn（生产环境推荐）
```bash
pip install gunicorn

# 修改 systemd 服务文件的 ExecStart：
# ExecStart=/home/agent/Lightweight_agent_service/venv/bin/gunicorn server.main:app \
#     -w 4 -k uvicorn.workers.UvicornWorker -b 0.0.0.0:17520 \
#     --timeout 300 --graceful-timeout 30
```

### 调整系统限制
在 systemd 服务文件中添加：
```ini
[Service]
...
LimitNOFILE=65535
LimitNPROC=65535
```

## 14. 健康检查

访问 `http://your-server:17520/api/health` 应返回 `{"status":"ok"}`。

## 15. 审批与权限模式（部署注意）

平台提供「请求批准 / 完全访问」双权限模式与聊天框审批门（详见 README「审批与权限模式」章节）。部署时请注意：

- **单进程内存态**：审批状态机（`approval_store`）与会话权限状态机（`trust_store`）均为**单进程内存态**，仅存在于当前运行进程。
- **多 worker 需共享存储**：若在 systemd 中使用 `gunicorn -w N`（N>1）启用多 worker，跨进程不共享审批/信任状态，会导致审批卡片无法被同一会话的其它 worker 唤醒。生产环境如确需多 worker，请将这两个存储替换为 Redis 等跨进程共享方案（接口保持一致即可），否则建议**保持单进程**（`python server/main.py` 或 `gunicorn -w 1`）。
- **operator 在场要求**：`request`（请求批准）模式下，敏感操作会暂停等待人工在聊天框点击「允许 / 拒绝 / 跳过」。若无人值守（如定时任务、无人监控的后台调用），操作将一直挂起——此类场景应切换为 `full` 模式或确保有操作员实时在线。

## 16. 安全加固说明（部署必读）

本版本对多处历史安全风险做了**彻底修复**（非间接缓解），部署上线前请确认以下加固已生效。

### 16.1 凭据与敏感数据加密（强加密替代弱 XOR）

- 模型 API Key、数据库内加密字段现已使用**标准库实现的 AEAD 语义强加密**（`agent/crypto_utils.py`，HMAC-SHA256 密钥流 + HMAC 认证标签），密钥来源：
  - 配置文件：`config_dir` 下的 salt 派生 32 字节密钥；
  - 数据库：`.db_secret` 文件（自动生成、权限 0o600）。
- 旧版 XOR 弱加密格式（`v1:` 前缀）仍可读但不再写入，新数据全部为 `v2:` 前缀的强加密。
- **无需额外第三方依赖**：加密完全基于 Python 标准库，服务器无需 `pip install cryptography`。

### 16.2 沙箱网络出口默认全阻断

- 沙箱子进程（`tool_sandbox/{user_id}/venv/`）的网络解析（`socket.getaddrinfo`）默认**全部拒绝**，任何外部主机访问都会抛 `PermissionError` 并记入 `logs/sandbox_audit.log`。
- 仅当显式设置 `SANDBOX_NETWORK_ALLOWLIST`（见第 7 节 service 文件注释）时才放行匹配的主机后缀。
- 主进程技能（web-fetch 等通过 `ToolRegistry` 直调，不经沙箱）**不受此限制**，仍按部署环境正常联网。

### 16.3 沙箱文件读取边界

沙箱脚本读取文件时受以下边界约束（越界写入 `sandbox_audit.log` 并拒绝）：

- **系统/敏感目录拒绝**：`/etc`、`/home`、`/Users`、`/root`、`/var`、`/proc`、`/sys`、`/boot`、`/usr`、`/bin`、`/sbin`、`/lib`、`/opt` 及服务自身 `data/`/`workbuddy/`/用户 `workbuddy/` 目录。
- **密钥文件拒绝**：文件名命中密钥清单（如 `id_rsa`、`*.pem`、`_db_secret` 等）一律拒绝。
- **跨用户文档隔离**：仅允许访问「当前用户」的 `document_output/{uid}/` 目录，禁止读取其他用户的产出。
- **写入白名单**：文件创建/写入仅限白名单目录（如 `document_output/{uid}/`、`/tmp`、`user_skill` 等），其余目录不可写。

### 16.4 run_command 参数路径限制

受控命令执行（`agent/skills/run-command`）新增参数约束：

- 禁止绝对路径（`/` 开头）与 `~` 家目录展开；
- 禁止父目录遍历（`..` / `../`）；
- 禁止访问密钥文件名；
- 命令本身仍受 `_ALLOWED_COMMANDS` 白名单约束（**已移除 `python3`/`python`/`node` 等解释器，杜绝以 `python -c` 绕过沙箱钩子**）。
- 任何拒绝/放行均写入 `sandbox_audit.log`，便于审计。

### 16.5 沙箱审计日志

- 路径：`logs/sandbox_audit.log`。
- 记录事件：`net_denied`（网络被拒）、`file_read_denied`（读敏感/越界文件）、`cmd_denied`/`cmd_path_denied`/`cmd_secret_denied`（命令执行被拒）、`cmd_exec`（放行执行）、`file_delete_denied`/`rename_denied`/`symlink_denied`/`link_denied`（危险文件操作被拒）等。
- 与第 11 节的 `audit.log`（审批/权限事件）相互独立，排查安全事件时可结合查看。

### 16.6 会话内容脱敏与截断

- 持久化会话消息（`server/routes/chat.py`）在落库前经过 `_sanitize_messages` 脱敏与截断：
  - 自动识别并遮盖密钥样例（`sk-`/`ghp_`/`glpat_`/`AKIA` 等）、JWT、以及 `key:`/`password:` 等赋值式中的敏感值；
  - 对 thought / 工具入参 / 工具结果 / 答案 / 搜索词按上限截断（thought 2000、工具结果 3000、答案 20000、搜索词 1500 字符），防止数据库膨胀与敏感内容长期留存。
- 返回给前端的消息为原始完整内容，脱敏**仅作用于持久化存储**。

> 上述加固均为代码层默认行为，无需额外配置即可生效。若需主动放开沙箱网络，请仅按 16.2 的最小必要原则设置 `SANDBOX_NETWORK_ALLOWLIST`。