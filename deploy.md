# 部署指南

面向：在一台 Linux 服务器上首次部署，或从旧版本升级。本文所有路径以 `/home/agent/Lightweight_agent_service` 为例，用户名以 `agent` 为例——换成你自己的即可，全文一致即可。

| 项 | 默认值 |
|---|---|
| 主服务端口 | `17520` |
| 数据库管理界面 | `17521`（仅监听 `127.0.0.1`） |
| 数据目录 | `data/`（SQLite + 密钥，受 `.gitignore` 保护） |
| 用户产出目录 | `document_output/{public_id}/` |
| 沙箱目录 | `tool_sandbox/` |
| 日志目录 | `logs/` |

## 1. 准备服务器

- Ubuntu 20.04+ / Debian 11+ / CentOS 8+
- Python 3.10+（3.10 ~ 3.14 均可）
- 至少 2GB 内存、10GB 磁盘

```bash
ssh root@your-server-ip
adduser agent
usermod -aG sudo agent
su - agent
```

## 2. 安装系统依赖

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3 python3-pip python3-venv git nginx sqlite3 curl
```

中文 PDF 输出需要系统字体（文档技能用到）：

```bash
sudo apt install -y fonts-noto-cjk
# 字体通常落在 /usr/share/fonts/opentype/noto/ 或 /usr/share/fonts/truetype/noto/
```

## 3. 获取代码

```bash
cd /home/agent
git clone https://github.com/M-adrigal/OmniAssist_Open.git Lightweight_agent_service
cd Lightweight_agent_service
```

无法直连 GitHub 时，也可以在本地打包上传：

```bash
scp -r /path/to/Lightweight_agent_service agent@your-server-ip:/home/agent/
```

## 4. 安装 Python 依赖

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## 5. 构建沙箱依赖

**必做，不能跳过。** 技能脚本运行在每用户独立 venv 中，这些 venv 通过 `PYTHONPATH` 继承一个共享基础 venv（`tool_sandbox/shared/pyX.Y/venv`，按 Python 小版本分桶）里的通用库（python-docx / openpyxl / reportlab / python-pptx / lxml / Pillow 等），避免每个用户重复安装重型库。

```bash
python3 scripts/build_sandbox_venv.py

# 默认用清华源；需要换源：
SANDBOX_PIP_INDEX=https://pypi.org/simple python3 scripts/build_sandbox_venv.py
```

漏掉这一步不会让服务起不来——首次用到时会懒构建，但那一刻用户要等几分钟，且如果网络不通就直接失败。

## 6. 首次启动

```bash
python server/main.py
```

首次启动自动完成：

- 在 `data/` 下建库 `users.db`（用户 / 会话 / 模型配置 / 搜索配置 / 权限 / 用量等表）
- 建默认管理员 `admin`，**初始密码随机生成，只打印在启动控制台**
- 初始化默认权限（`admin` 全量，`user` 基础）
- 生成 `data/.db_web_password`（权限 `0600`），供数据库管理界面认证

> ⚠️ 初始密码**只进 stdout，不写 `logs/`**（日志文件管理员可下载）。记下控制台里 `默认管理员初始密码` 那一行。首次登录后必须改密，否则平台功能不可用；改密后 `.db_web_password` 自动同步。

浏览器访问 `http://your-server-ip:17520`。确认能登录后按 `Ctrl+C` 停掉，交给 systemd 托管。

### 数据库管理界面

随服务自动启动（基于 sqlite-web），地址 `http://127.0.0.1:17521`，Basic Auth 用管理员账号密码。只绑回环，从本地访问需 SSH 隧道：

```bash
ssh -L 17521:127.0.0.1:17521 agent@your-server-ip
# 然后在本机浏览器打开 http://127.0.0.1:17521
```

> **不要在防火墙上开放 17521**。普通用户无法通过认证，仅管理员可进。

## 7. 交给 systemd 托管

> **生产铁律：只允许 systemd 托管，禁止 `nohup` / 手动前台启动。** 两种方式并存会争抢 17520：systemd 绑定失败（`exit-code=3`），配合 `Restart=always` 陷入反复重启，服务长时间不可用。

```bash
sudo nano /etc/systemd/system/agent.service
```

```ini
[Unit]
Description=OmniAssist
After=network.target

[Service]
Type=simple
User=agent
Group=agent
WorkingDirectory=/home/agent/Lightweight_agent_service

# 必须显式列出系统目录：systemd 一旦用 Environment= 设了 PATH，就不再追加默认路径。
# 只写 venv/bin 会让服务进程里没有 /bin、/usr/bin，沙箱内 run_command 直接 FileNotFoundError。
Environment="PATH=/home/agent/Lightweight_agent_service/venv/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# 环境变量（可选，按需开）。各自含义见第 16 节。
# Environment="AGENT_STREAM_STOP_GRACE=60"
# Environment="SANDBOX_NETWORK_ALLOWLIST=api.example.com"
# Environment="REDIS_URL=redis://127.0.0.1:6379/0"

ExecStart=/home/agent/Lightweight_agent_service/venv/bin/python server/main.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=agent

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now agent
sudo systemctl status agent
```

服务自带端口守卫：手动启动时若检测到 systemd 托管会拒绝启动；systemd 重启时会清理手动残留实例。

## 8. 反向代理与 HTTPS

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

        # 对话是流式长连接，超时必须放大，否则长任务会在中途被切断
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/agent /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

HTTPS：

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d your-domain.com
sudo certbot renew --dry-run
```

> 走反向代理时，若要让登录失败锁定按真实 IP 计数，需在 systemd 单元里加 `Environment="TRUSTED_PROXY_IPS=127.0.0.1"`。不在白名单内的来源，`X-Forwarded-For` **不被采信**（防伪造）。

## 9. 防火墙

```bash
sudo ufw allow 22/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
```

只开放这三个。`17520` 由 Nginx 反代访问，`17521` 只绑回环，都不需要对外。

## 10. 配置模型与搜索

在「设置」面板中配置。

### 模型

| 项 | 说明 |
|---|---|
| API Key | LLM 密钥，加密存储 |
| Base URL | OpenAI 兼容端点，如 `https://api.openai.com/v1` |
| Model Name | 模型名，如 `gpt-4`、`deepseek-v3-2-251201` |
| Context Limit | 上下文上限，如 `32k` / `64k` / `128k`；留空不限 |
| 最大迭代次数 | 单轮允许的工具调用 / 思考循环上限，默认 10 |
| 温度策略 | `auto` 按任务类型动态计算并随轮次收敛到 0.2；`static` 全程固定值，可复现 |
| 温度值 | `auto` 时作分析/未知类任务的基准；`static` 时作唯一值。范围 0~2，默认 0.7 |

管理员可配「全局模型配置」供所有用户共享；普通用户的「个人模型配置」优先级更高。改完保存即热更新，无需重启。

### 联网搜索（可选）

支持两家，**两把 Key 相互独立**，在「设置 → 联网搜索」里选一家：

- **DeepSeek**：与模型同一把 Key，[platform.deepseek.com](https://platform.deepseek.com/api_keys)。按成功请求计费 ¥5 / 1000 次（搜不到、超时、报错均不计费）
- **Tavily**：[tavily.com](https://tavily.com) 免费注册获取

选了哪家就只用哪家。选 DeepSeek 但没填 Key 会直接报「未配置」，**不会回落到 Tavily**。

## 11. Redis（可选，当前版本不需要）

**按默认方式部署不需要装 Redis。** 保留这一节是因为网上和旧文档容易让人以为装了就"更生产"，当前版本装了没有收益。

### 它管什么

只有两样东西会用到它：审批状态（`approval_store`）和会话权限模式（`trust_store`）。代码在 `server/kv.py`：

- 不设 `REDIS_URL` → 用进程内存
- 设了 `REDIS_URL` → 用 Redis
- 设了但 `redis` 包缺失或连不上 → **自动退回进程内存**，并打一条告警

连不上就退回进程内存，这是刻意的：缓存不可用时退回单进程行为，比因为 Redis 挂了把整个对话打死要好。

### 为什么现在不需要

单进程（默认）下，内存实现与 Redis 语义等价——审批状态的生命周期本来就短（600 秒超时），且必须由一个活着的进程持有等待方。把同样的数据再往 Redis 写一份，不改变任何行为，只多了一个可能连不上的依赖。

那为什么代码里还支持？为了将来支持多 worker 预留。但**只配 Redis 并不能开启多 worker**，见下节。

### 如果确实要装

```bash
# 方式一：系统包
sudo apt install -y redis-server
sudo sed -i 's/^# *bind 127.0.0.1 ::1/bind 127.0.0.1/' /etc/redis/redis.conf
sudo systemctl enable --now redis-server

# 方式二：Docker（不落盘，纯缓存）
docker run -d --name agent-redis -p 127.0.0.1:6379:6379 redis:7-alpine redis-server --save ""
```

两种方式都只绑回环，**不要对公网开放 6379**。然后在 systemd 单元里加一行并重启：

```ini
Environment="REDIS_URL=redis://127.0.0.1:6379/0"
```

```bash
sudo systemctl daemon-reload && sudo systemctl restart agent
```

### 验证是否真的用上了

```bash
sudo journalctl -u agent -n 100 | grep -E "审批/信任状态使用"
```

- 生效：`审批/信任状态使用 Redis: 127.0.0.1:6379/0`
- 退回：`Redis 不可用，已退回进程内存储（多 worker 下审批会跨进程丢失）`

连通性单独测：

```bash
redis-cli -u redis://127.0.0.1:6379/0 ping    # 期望 PONG
```

## 12. 不要用多 worker

不要用 `gunicorn -w N`（N>1）或 `uvicorn --workers N`。**配 Redis 也不行。**

流式对话的任务句柄 `_running_tasks` 是**进程内字典**（`server/routes/chat.py`）。除了审批，下面这些接口都要先在它里面找到任务，找不到直接 404：

| 接口 | 作用 | 多 worker 下的表现 |
|---|---|---|
| `GET /api/chat/subscribe/{session_id}` | 刷新页面后重连正在跑的流 | 打到别的进程 → 404，页面一片空白，但任务还在后台烧 token |
| `POST /api/chat/stop/{session_id}` | 停止按钮 | 404，停不下来 |
| `POST /api/chat/regenerate/{session_id}` | 重新生成 | 并发保护失效，可能同一会话跑两轮 |
| `POST /api/approval/{session_id}/approve` | 审批决议 | 404，审批卡片点了没反应，600 秒后超时 |

所以 Redis 只解决了「审批**数据**能跨进程读到」，解决不了「审批**请求**打不到持有等待方的那个进程」。这两件事是分开的，前者已修，后者没有。

另外：数据库管理界面（`sqlite_web` 子进程）与沙箱池（`SandboxPool`，进程内缓存 + 线程锁）也都是单进程假设，多 worker 会重复启动、缓存不共享。

**单进程 + 异步 I/O 已经能撑住内部自用规模。** 需要更高并发时，优先调大 `PARALLEL_TOOL_MAX_WORKERS`（工具并发，默认 4），而不是加 worker 进程。

## 13. 日常运维

### 13.1 更新代码

一键脚本（推荐）：

```bash
bash scripts/deploy.sh
```

它依次做：端口占用预检并清理手动残留 → `git pull --ff-only` → 装依赖 → `sudo systemctl restart agent` → 15 次健康探测。

> ⚠️ **`deploy.sh` 不跑数据库迁移**，它只管代码与进程。凡本文写明「升级必跑」的迁移，都必须自己执行一次。推荐顺序：`git pull` → **跑迁移** → `bash scripts/deploy.sh`（迁移会先备份，此时服务还没重启，出问题好回退）。

**从旧版本升级**：迁移要求随版本变化，逐步的变更明细见 [`CHANGELOG.md`](CHANGELOG.md)。两条必须知道的：

- **v2.0.0 引入双 ID 体系**：升级前必须跑 `python3 scripts/migrate_public_id.py` 回填 `public_id` 并重命名 `document_output/{整数id}` → `{public_id}`，否则存量用户全部被判未登录（401 锁死）。脚本幂等。结尾若提示「仍有 N 个用户缺少 public_id」，**禁止启动服务**，先排查。
- **时间基准统一迁移**：全库时间统一为本地时间（此前个别写入路径混入 UTC）。含此改动的版本升级后、重启前执行一次（幂等，自动备份到 `data/users.db.bak-tz`）：

```bash
python3 scripts/migrate_timezone.py --dry-run   # 先看要改什么
python3 scripts/migrate_timezone.py
```

手动分步（升级场景，顺序不可颠倒）：

```bash
git pull
source venv/bin/activate
pip install -r requirements.txt
sudo systemctl stop agent
python3 scripts/migrate_public_id.py
sudo systemctl start agent
```

迁移脚本只重命名目录、不删文件；`data/` 与 `document_output/` 在 `.gitignore` 内，`git pull` 不碰用户数据。

### 13.2 日志

| 文件 | 内容 |
|---|---|
| `logs/app.log` | 全量 INFO 及以上 |
| `logs/error.log` | 仅 ERROR |
| `logs/audit.log` | 审批决议、权限模式切换 |
| `logs/sandbox_audit.log` | 沙箱越权尝试与放行（网络、文件、命令） |
| `logs/archive/` | 轮转与清理前的归档 |

按天轮转，单文件上限 10MB，保留 30 天。格式 `[时间戳] [级别] [模块] [user:N] [sess:xxx] 消息`。uvicorn 内部日志已统一接入 `app.log`，不会在根目录散落 `serverN.log`。

```bash
tail -f logs/app.log
sudo journalctl -u agent -f
```

### 13.3 备份

```bash
tar -czf agent-backup-$(date +%Y%m%d).tar.gz data/ document_output/ logs/
```

`data/` 里有 `.db_secret` 与 `.db_web_password`，备份文件按密钥对待。

### 13.4 用户管理

管理员在「用户管理」页可创建用户（用户名 / 密码 / 角色 / 描述）、改密改角色、删除用户。

## 14. 故障排除

### 服务起不来

```bash
sudo ss -tlnp | grep 17520
python3 -c "import fastapi; print('FastAPI OK')"
python3 -c "import openai; print('OpenAI OK')"
ls -la data/users.db
```

看真实报错（**先停 systemd，避免端口争抢**）：

```bash
sudo systemctl stop agent
cd /home/agent/Lightweight_agent_service && source venv/bin/activate
python server/main.py
# 排障完必须 sudo systemctl start agent 恢复托管
```

### 端口冲突（`exit-code=3` / 反复重启）

```bash
# 1. 找占用者
sudo ss -tlnp | grep 17520
#    输出如 users:(("python",pid=94909,fd=17))

# 2. 确认是手动残留再杀。若该进程 cgroup 含 agent.service，别手动杀，直接 restart。
sudo kill 94909

# 3. 恢复
sudo systemctl restart agent && curl -s http://127.0.0.1:17520/api/health
```

### 页面能开但功能异常

```bash
sudo ufw status
sudo nginx -t && sudo systemctl status nginx
sudo systemctl status agent
tail -50 logs/error.log
```

### 数据库

```bash
sqlite3 data/users.db "PRAGMA integrity_check;"
sqlite3 data/users.db "SELECT id, username, user_type FROM users;"
```

### 内存不足

```bash
free -h && top -o %MEM
```

可在单元文件加 `MemoryHigh=1G` / `MemoryMax=2G`。

### 技能跑不起来 / 依赖缺失

先确认共享 venv 存在：

```bash
ls tool_sandbox/shared/
```

列表为空说明第 5 步漏了，补跑 `python3 scripts/build_sandbox_venv.py`。其余排查：

```bash
# 某用户沙箱是否正常
ls tool_sandbox/user_1/venv/bin/python

# 跨 Python 版本：共享 venv 与用户 venv 的小版本必须一致，否则会跳过继承、改为各自安装
cat tool_sandbox/shared/py3.14/venv/pyvenv.cfg
cat tool_sandbox/user_1/venv/pyvenv.cfg

# pip 超时
tool_sandbox/user_1/venv/bin/pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple

# 重建某用户沙箱
rm -rf tool_sandbox/1 && sudo systemctl restart agent
```

技能脚本所需的、共享 venv 里没有的依赖，会在首次执行时通过 AST 解析 `import` 自动装进该用户自己的 venv。

## 15. 安全边界（默认已生效，无需配置）

| 边界 | 默认行为 |
|---|---|
| 凭据加密 | API Key 与加密字段用 HMAC-SHA256 密钥流 + 认证标签（`v2:` 前缀）。纯标准库实现，无需装 `cryptography`。密钥来自 `data/.db_secret`（`0600`） |
| 沙箱网络 | 子进程 `getaddrinfo` **全部拒绝**，越权记 `sandbox_audit.log`。要放行需显式设 `SANDBOX_NETWORK_ALLOWLIST` |
| 主进程技能联网 | 不受限。`web-fetch` 等经 `ToolRegistry` 直调，不走沙箱 |
| 沙箱读取 | 拒绝系统目录、他人 home、密钥文件；只允许读本人的 `document_output/{public_id}/` |
| 沙箱写入 | 仅 `document_output/{public_id}/`、`TMPDIR`、`/tmp/user_skill`（可用 `SANDBOX_WRITE_DIRS` 追加） |
| 命令执行 | 白名单制，禁绝对路径、`~`、`..`、密钥文件名；解释器（`python`/`node`）已从白名单移除 |
| 会话落库 | 自动遮盖 `sk-`/`ghp_`/`glpat_`/`AKIA` 等密钥样例与 JWT，并按上限截断（thought 2000、工具结果 3000、答案 20000、搜索词 1500 字符）。**脱敏只作用于存储**，前端收到的仍是原文 |
| 框架头 | `X-Frame-Options: SAMEORIGIN` + `frame-ancestors 'self'`（本站自己有同源 iframe：Token 看板、PDF 预览，设成 DENY 会让它们白屏） |

沙箱内的 `os.open` **不是被删掉，而是换成了带同样校验的包装**（写判定按 `O_ACCMODE`/`O_CREAT` 位，`dir_fd` 一律拒绝）。直接删会打死 `tempfile`，进而让一切用临时文件的库失灵。

## 16. 环境变量速查

全部可选，不设即用默认值。

| 变量 | 默认 | 作用 |
|---|---|---|
| `REDIS_URL` | 空 | 审批/信任状态改用 Redis。空 = 进程内存。见第 11 节 |
| `AGENT_STREAM_STOP_GRACE` | `60` | 客户端断连后后台任务再跑多少秒自动取消。宽限期内重连会取消倒计时，正常刷新不受影响。`0` = 关闭自动停止 |
| `SANDBOX_NETWORK_ALLOWLIST` | 空 | 沙箱网络出口白名单，逗号分隔主机后缀。匹配「等于」或「以 `.该项` 结尾」才放行。空 = 全阻断 |
| `SANDBOX_WRITE_DIRS` | 空 | 追加沙箱可写目录（逗号分隔） |
| `SANDBOX_PIP_INDEX` | 清华源 | 沙箱依赖安装的 pip 镜像源，见第 5 节 |
| `SANDBOX_PIP_TIMEOUT` | `300` | 沙箱内 pip 安装超时（秒） |
| `PARALLEL_TOOL_MAX_WORKERS` | `4` | 工具并行执行的最大线程数。并发不够时先调这个，别加 worker |
| `TRUSTED_PROXY_IPS` | 空 | 可信反向代理 IP 白名单。只有来自这些 IP 的 `X-Forwarded-For` 才被采信，用于登录失败按真实 IP 计数 |

## 17. 健康检查

```bash
curl -s http://127.0.0.1:17520/api/health     # 期望 {"status":"ok"}
```
