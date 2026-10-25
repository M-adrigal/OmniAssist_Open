#!/usr/bin/env bash
# =============================================================================
# Lightweight Agent Service — 一键部署脚本
#
# 统一部署入口：端口冲突预检 + git pull + 依赖安装 + systemctl restart + 健康检查。
#
# 为什么需要它？
#   「手动 nohup 启动」与「systemd 托管」并存会导致端口争抢：
#   systemd 因端口被占绑定失败（exit-code=3），Restart=always 又触发反复重启，
#   服务长时间不可用。本脚本在重启前主动清理残留进程，杜绝此类故障。
#
# 生产环境铁律：只允许 systemd 托管，禁止 nohup / 手动前台启动。
#
# 用法（在服务器上，以服务运行用户如 agent 执行）：
#   bash scripts/deploy.sh
#   或一键组合（远程 SSH 时）：
#   ssh admin@server 'cd /home/admin/Lightweight_agent_service && bash scripts/deploy.sh'
#
# 权限说明：git pull / pip install 用当前用户执行（需对项目目录有写权限）；
#           systemctl restart 需要 sudo（脚本会提示输入密码）。
# =============================================================================
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_NAME="agent"
PORT="${AGENT_PORT:-17520}"
LOG="[deploy]"

cd "$APP_DIR"

echo "$LOG 项目目录: $APP_DIR"
echo "$LOG 当前版本: $(git rev-parse --short HEAD 2>/dev/null || echo 未知)"

# ---------- 0) 拒绝 root 直接部署（避免文件属主混乱） ----------
if [ "$(id -u)" = "0" ]; then
  echo "$LOG ⚠️  检测到 root 身份。请以服务运行用户执行："
  echo "        sudo -u agent bash $0"
  echo "    （若服务 User= 不是 agent，替换为对应用户名）"
  exit 1
fi

# ---------- 1) 端口占用预检：清理手动残留，避免与 systemd 争抢 ----------
echo "$LOG 预检端口 $PORT 占用..."
# 优先用 sudo 读 pid（非 root 下 ss -tlnp 不显示 pid 列）；无 sudo 权限则尽力而为
if sudo -n true 2>/dev/null; then
  SS_CMD="sudo ss -tlnp"
else
  SS_CMD="ss -tlnp"
fi
OCCUPY_PIDS="$($SS_CMD 2>/dev/null | grep ":$PORT " | grep -oP 'pid=\K[0-9]+' | sort -u || true)"
if [ -n "$OCCUPY_PIDS" ]; then
  for p in $OCCUPY_PIDS; do
    cmd="$(ps -p "$p" -o command= 2>/dev/null || echo 未知)"
    if echo "$cmd" | grep -q "server/main.py\|uvicorn"; then
      echo "$LOG ⚠️  发现手动 nohup 残留进程: PID=$p cmd=[$cmd]"
      echo "$LOG    终止该进程以释放端口 $PORT ..."
      kill "$p" 2>/dev/null && echo "$LOG ✅ 已终止 PID $p" \
        || { echo "$LOG    无权限终止，请手动执行: sudo kill $p"; exit 1; }
      sleep 1
    else
      echo "$LOG ⚠️  端口 $PORT 被其他程序占用: PID=$p cmd=[$cmd]"
      echo "$LOG    请确认是否可终止：sudo kill $p（不确定时先排查：ss -tlnp | grep $PORT）"
      exit 1
    fi
  done
else
  echo "$LOG ✅ 端口 $PORT 空闲"
fi

# ---------- 2) 拉取代码（仅快进合并，避免本地提交被覆盖） ----------
echo "$LOG 拉取最新代码..."
git pull --ff-only || { echo "$LOG ❌ git pull 失败（可能是本地有未推送提交）。请先处理后再部署"; exit 1; }

# ---------- 3) 安装依赖 ----------
if [ -d venv ]; then
  echo "$LOG 安装/更新 Python 依赖..."
  venv/bin/pip install -r requirements.txt -q || { echo "$LOG ❌ 依赖安装失败"; exit 1; }
fi

# ---------- 4) 重启 systemd 服务 ----------
echo "$LOG 重启 systemd 服务 $SERVICE_NAME ..."
if sudo systemctl restart "$SERVICE_NAME"; then
  echo "$LOG ✅ systemctl restart 成功"
else
  echo "$LOG ❌ 重启失败，请检查: sudo systemctl status $SERVICE_NAME"
  exit 1
fi

# ---------- 5) 健康检查 ----------
echo "$LOG 等待服务启动（最多 30 秒）..."
for i in $(seq 1 15); do
  sleep 2
  code="$(curl -s -o /dev/null -w '%{http_code}' --noproxy '*' "http://127.0.0.1:$PORT/api/health" 2>/dev/null || true)"
  if [ "$code" = "200" ]; then
    echo "$LOG ✅ 健康检查通过 (HTTP 200)"
    echo "$LOG 🎉 部署完成。当前版本: $(git rev-parse --short HEAD)"
    exit 0
  fi
done

echo "$LOG ❌ 健康检查失败（15 次探测未获 200）"
echo "$LOG    请查看服务日志: sudo journalctl -u $SERVICE_NAME -n 50 --no-pager"
exit 1
