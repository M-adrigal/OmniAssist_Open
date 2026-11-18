#!/bin/bash
# 本地测试用 Redis 的启停脚本（只监听 127.0.0.1，不落盘）
#
#   bash scripts/dev_redis.sh start    启动
#   bash scripts/dev_redis.sh stop     停止
#   bash scripts/dev_redis.sh status   状态 + 键统计
#   bash scripts/dev_redis.sh cli      进交互命令行
#   bash scripts/dev_redis.sh reset    清空所有键
#   bash scripts/dev_redis.sh test     起服务并跑 scripts/test_redis_live.py
#
# 用途：验证 OmniAssist 多 worker 下审批状态能否跨进程共享。
# **不配 REDIS_URL 时服务仍用进程内存储**，行为和以前完全一致；这个脚本只是
# 给你一个能真跑跨进程验证的环境，不影响线上。
#
# 只监听回环、关闭持久化、限制 128MB —— 它是测试实例，不是生产库。
set -u

PORT="${REDIS_LOCAL_PORT:-6379}"
BASE="${REDIS_LOCAL_HOME:-$HOME/.local/redis-local}"
DATA="$BASE/data"
LOG="$BASE/redis.log"
PIDFILE="$BASE/redis.pid"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

mkdir -p "$DATA"

find_server() {
  # 优先用本脚本装好的那一份，其次用系统装的（服务器上 apt install 就是这种）
  for c in "$BASE/bin/redis-server" "$(command -v redis-server 2>/dev/null)"; do
    [ -n "${c:-}" ] && [ -x "$c" ] && { echo "$c"; return 0; }
  done
  return 1
}

cli() {
  for c in "$BASE/bin/redis-cli" "$(command -v redis-cli 2>/dev/null)"; do
    [ -n "${c:-}" ] && [ -x "$c" ] && { "$c" -p "$PORT" "$@"; return $?; }
  done
  return 127
}

is_up() { cli ping >/dev/null 2>&1; }

cmd_start() {
  if is_up; then
    echo "已在运行：127.0.0.1:$PORT"
    return 0
  fi
  local srv
  if ! srv="$(find_server)"; then
    cat <<'TIP'
找不到 redis-server。任选一种装上：
  macOS（无 brew 时）：下载源码编译
      curl -sSL https://download.redis.io/redis-stable.tar.gz | tar xz
      cd redis-stable && make -j"$(sysctl -n hw.ncpu)" MALLOC=libc
      mkdir -p ~/.local/redis-local/bin && cp src/redis-server src/redis-cli ~/.local/redis-local/bin/
  Linux：  sudo apt install redis-server
  容器：   docker run -d --name agent-redis -p 127.0.0.1:6379:6379 redis:7-alpine redis-server --save ""
TIP
    return 1
  fi

  # 参数即配置：不另放 redis.conf，免得"脚本说一套、配置文件说另一套"
  "$srv" --bind 127.0.0.1 --port "$PORT" --daemonize yes \
         --pidfile "$PIDFILE" --logfile "$LOG" --dir "$DATA" \
         --save "" --appendonly no \
         --maxmemory 128mb --maxmemory-policy noeviction || {
    echo "启动失败，看日志：$LOG"; tail -5 "$LOG" 2>/dev/null; return 1; }

  for _ in $(seq 1 20); do is_up && break; sleep 0.3; done
  if is_up; then
    echo "已启动：127.0.0.1:${PORT}（$("$srv" --version 2>/dev/null | cut -d' ' -f1-3)）"
    echo "连接串：redis://127.0.0.1:$PORT/0"
  else
    echo "起不来，看日志：$LOG"; tail -5 "$LOG" 2>/dev/null; return 1
  fi
}

cmd_stop() {
  if is_up; then
    cli shutdown nosave 2>/dev/null
    echo "已停止"
  else
    echo "没在运行"
  fi
}

cmd_status() {
  if ! is_up; then
    echo "未运行（bash scripts/dev_redis.sh start）"
    return 1
  fi
  echo "运行中  127.0.0.1:$PORT  pid=$(cat "$PIDFILE" 2>/dev/null || echo '?')"
  echo "版本    $(cli info server 2>/dev/null | grep -m1 redis_version | tr -d '\r')"
  echo "键数量  $(cli dbsize 2>/dev/null)"
  echo "审批键  $(cli --scan --pattern 'approval:*' 2>/dev/null | wc -l | tr -d ' ')"
  echo "信任键  $(cli --scan --pattern 'trust:*' 2>/dev/null | wc -l | tr -d ' ')"
}

case "${1:-}" in
  start)  cmd_start ;;
  stop)   cmd_stop ;;
  status) cmd_status ;;
  cli)
    for x in "$BASE/bin/redis-cli" "$(command -v redis-cli 2>/dev/null)"; do
      [ -n "${x:-}" ] && [ -x "$x" ] && exec "$x" -p "$PORT"
    done
    echo "找不到 redis-cli"; exit 1
    ;;
  reset)
    is_up || { echo "未运行"; exit 1; }
    cli flushdb >/dev/null && echo "已清空当前库"
    ;;
  test)
    cmd_start || exit 1
    echo
    cd "$ROOT" && exec /usr/bin/python3 scripts/test_redis_live.py
    ;;
  *)
    sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
    ;;
esac
