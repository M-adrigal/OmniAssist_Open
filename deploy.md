# 服务器部署指南

## 1. 准备服务器

### 系统要求
- Ubuntu 20.04+ / Debian 11+ / CentOS 8+
- Python 3.8+
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
scp -r /Users/yuanye/Documents/Lightweight_agent_service/* agent@your-server-ip:/home/agent/Lightweight_agent_service/
```

## 4. 安装 Python 依赖

```bash
cd /home/agent/Lightweight_agent_service
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## 5. 首次启动与初始化

```bash
# 启动服务
python server/main.py
```

首次启动时会自动完成以下初始化：
- 在 `agent/` 目录下创建 SQLite 数据库（`users.db`），包含用户表、会话表、模型配置表、搜索配置表、权限表
- 创建默认管理员账户（用户名 `admin`），**随机密码会输出在终端，请务必记录**
- 初始化默认权限（admin 拥有全部权限，user 拥有基础权限）

看到终端输出的管理员密码后，即可通过浏览器访问 `http://your-server-ip:17520` 登录。

## 6. 配置模型与搜索

登录 Web 界面后，在左侧设置面板中配置：

### 模型配置
- **API Key**：你的 LLM API 密钥（加密存储）
- **Base URL**：API 端点地址，如 `https://api.openai.com/v1`
- **Model Name**：模型名称，如 `gpt-4`、`deepseek-v3-2-251201`
- **Context Limit**：上下文 token 上限，如 `32k`、`64k`、`128k`（留空则不限制）

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
```bash
# 服务日志
sudo journalctl -u agent -f

# Nginx 访问日志
sudo tail -f /var/log/nginx/access.log
sudo tail -f /var/log/nginx/error.log
```

### 更新代码
```bash
cd /home/agent/Lightweight_agent_service
git pull
source venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart agent
```

### 备份数据
```bash
# 备份数据库、配置和工具
tar -czf agent-backup-$(date +%Y%m%d).tar.gz \
    agent/users.db \
    agent/.agent_config \
    agent/.agent_salt \
    agent/agent_tools/ \
    document_output/
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
ls -la agent/users.db

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
sqlite3 agent/users.db "PRAGMA integrity_check;"

# 查看用户列表
sqlite3 agent/users.db "SELECT id, username, user_type FROM users;"
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