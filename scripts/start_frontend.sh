#!/usr/bin/env bash
# 前端启动脚本：开发环境启动本地静态服务器，或加载 Nginx 配置
set -e

cd "$(dirname "$0")/../frontend"

if command -v nginx >/dev/null 2>&1 && [ "$1" = "nginx" ]; then
  echo ">>> 启动 Nginx"
  nginx -c "$(pwd)/nginx.conf"
else
  echo ">>> 启动本地静态服务器"
  python3 -m http.server 8080
fi