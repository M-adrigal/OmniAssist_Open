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

# 安装 Node.js（可选，用于前端构建）
curl -fsSL https://deb.nodesource.com/setup_18.x | sudo -E bash -
sudo apt install -y nodejs
```

## 3. 部署代码

### 方式一：从本地 Git 推送
```bash
# 在本地机器上添加远程仓库
cd /Users/yuanye/Documents/Lightweight_agent_service
git remote add server ssh://agent@your-server-ip:/home/agent/agent-framework.git
git push server main
```

### 方式二：在服务器上克隆
```bash
# 在服务器上
cd /home/agent
git clone https://github.com/your-repo/agent-framework.git
# 或从本地 scp
scp -r /Users/yuanye/Documents/Lightweight_agent_service/* agent@your-server-ip:/home/agent/agent-framework/
```

## 4. 安装 Python 依赖

```bash
cd /home/agent/agent-framework
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## 5. 配置环境

### 创建配置文件
```bash
# 创建配置目录
mkdir -p ~/.agent

# 创建 .env 文件（可选）
cat > .env << EOF
API_KEY=your-api-key
BASE_URL=https://ark.cn-beijing.volces.com/api/v3
MODEL_NAME=deepseek-v3-2-251201
EOF
```

### 初始化配置
```bash
# 首次运行会创建加密盐文件
python3 -c "from agent.config import AgentConfig; config = AgentConfig(); print('Config initialized')"
```

## 6. 使用 systemd 运行服务

### 创建服务文件
```bash
sudo nano /etc/systemd/system/agent.service
```

```ini
[Unit]
Description=Agent Framework Web Service
After=network.target

[Service]
Type=simple
User=agent
Group=agent
WorkingDirectory=/home/agent/agent-framework
Environment="PATH=/home/agent/agent-framework/venv/bin"
ExecStart=/home/agent/agent-framework/venv/bin/python server/main.py
Restart=always
RestartSec=10
StandardOutput=syslog
StandardError=syslog
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

## 7. 配置 Nginx 反向代理（推荐）

### 创建 Nginx 配置
```bash
sudo nano /etc/nginx/sites-available/agent
```

```nginx
server {
    listen 80;
    server_name your-domain.com;  # 或服务器 IP

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
    }
}
```

### 启用站点
```bash
sudo ln -s /etc/nginx/sites-available/agent /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

## 8. 配置防火墙

```bash
# 允许 SSH、HTTP、HTTPS
sudo ufw allow 22/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable

# 如果直接访问 17520 端口
sudo ufw allow 17520/tcp
```

## 9. 安全加固

### 修改默认端口（可选）
编辑 `server/main.py`：
```python
uvicorn.run(app, host="0.0.0.0", port=17520)  # 改为其他端口
```

### 使用 HTTPS（推荐）
```bash
# 安装 Certbot
sudo apt install -y certbot python3-certbot-nginx

# 获取证书
sudo certbot --nginx -d your-domain.com

# 自动续期
sudo certbot renew --dry-run
```

## 10. 日常维护

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
cd /home/agent/agent-framework
git pull
source venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart agent
```

### 备份配置
```bash
# 备份加密配置和工具
tar -czf agent-backup-$(date +%Y%m%d).tar.gz \
    agent/.agent_config \
    agent/.agent_salt \
    agent/agent_tools/ \
    Document_output/
```

## 11. 故障排除

### 服务无法启动
```bash
# 检查端口占用
sudo netstat -tlnp | grep :17520

# 检查 Python 依赖
python3 -c "import fastapi; print('FastAPI OK')"

# 检查配置文件
ls -la agent/.agent_config agent/.agent_salt
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

### 内存不足
```bash
# 查看内存使用
free -h
top -o %MEM

# 限制 Python 内存使用（可选）
# 在 systemd 服务文件中添加：
# Environment="PYTHONUNBUFFERED=1"
# Environment="PYTHONMALLOC=debug"
```

## 12. 性能优化

### 使用 Gunicorn（生产环境）
```bash
pip install gunicorn
gunicorn server.main:app -w 4 -k uvicorn.workers.UvicornWorker -b 0.0.0.0:17520
```

### 调整 systemd 服务
```ini
[Service]
...
LimitNOFILE=65535
LimitNPROC=65535
```

## 13. 监控

### 安装监控工具
```bash
# 安装 htop
sudo apt install -y htop

# 安装 netdata（可选）
bash <(curl -Ss https://my-netdata.io/kickstart.sh)
```

### 健康检查
访问 `http://your-server:17520/api/health` 应返回 `{"status":"ok"}`