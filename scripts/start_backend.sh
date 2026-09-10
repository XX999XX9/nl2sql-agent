#!/usr/bin/env bash
# 后端启动脚本：安装依赖并以指定端口启动 uvicorn
set -e

cd "$(dirname "$0")/../backend"

echo ">>> 安装依赖"
pip install -r requirements.txt

echo ">>> 启动后端服务"
python run.py