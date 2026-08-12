#!/usr/bin/env bash
# push-all.sh — 通过本地代理推送所有 git 远端
# 解决：中文网络下本机 git 直连 GitHub 报 SSL connection timeout 的问题。
#
# 背景：
#   1) git 默认不读取 HTTP_PROXY / HTTPS_PROXY 环境变量，本机终端直接
#      `git push` 会直连 github.com 而超时。
#   2) 本机若无长期代理（如 Clash 7890），通常只有 WorkBuddy 的本地代理
#      (127.0.0.1:49561，会话级端口，每次会话可能变化) 能访问 GitHub。
#   本脚本自动定位可用代理，并以 `git -c http.proxy=...` 显式传给 git，
#   避免硬编码端口、随 WorkBuddy 会话自适应。
#
# 用法：
#   ./scripts/push-all.sh            # 推送当前分支到所有远端
#   ./scripts/push-all.sh main       # 推送指定分支到所有远端
#   BRANCH=dev ./scripts/push-all.sh # 也可用环境变量指定
set -uo pipefail

BRANCH="${1:-${BRANCH:-$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo main)}}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT" || exit 1

# 1) 优先取环境变量代理（WorkBuddy 会话会注入 HTTP_PROXY / HTTPS_PROXY）
PROXY="${HTTP_PROXY:-${HTTPS_PROXY:-}}"

# 2) 回退探测本地代理端口（WorkBuddy 代理 + 常见本地代理）
if [ -z "$PROXY" ]; then
  for p in 49561 7890 7891 7892 1087 1080 8080 33210; do
    if curl -s --connect-timeout 2 -x "http://127.0.0.1:$p" https://github.com -o /dev/null 2>/dev/null; then
      PROXY="http://127.0.0.1:$p"
      break
    fi
  done
fi

if [ -z "$PROXY" ]; then
  echo "⚠️  未检测到可用代理，将尝试直连（可能 SSL timeout）。" >&2
  GIT_OPTS=()
else
  echo "🔧 使用代理: $PROXY"
  # http.timeout/lowSpeed* 防止代理抖动时无限卡死（快速失败以便重试）
  GIT_OPTS=(-c "http.proxy=$PROXY" -c "https.proxy=$PROXY" -c "http.sslVerify=false" \
            -c "http.timeout=60" -c "http.lowSpeedLimit=1" -c "http.lowSpeedTime=30")
fi

# 3) 推送到所有远端
REMOTES=($(git remote))
if [ "${#REMOTES[@]}" -eq 0 ]; then
  echo "❌ 没有任何 git remote，退出。" >&2
  exit 1
fi

STATUS=0
for remote in "${REMOTES[@]}"; do
  echo ""
  echo ">>> 推送 $remote/$BRANCH"
  if git "${GIT_OPTS[@]}" push "$remote" "$BRANCH" 2>&1; then
    echo "✅ $remote/$BRANCH 完成"
  else
    echo "❌ $remote/$BRANCH 推送失败（见上方输出）" >&2
    STATUS=1
  fi
done

exit $STATUS
