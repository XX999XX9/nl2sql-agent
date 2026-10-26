# ============================================================
# 以 Spider 测试环境启动后端服务（生产 backend/.env 完全不动）
# 原理：config_handler 加载 backend/.env 时不覆盖已有环境变量，
#       本脚本先 export 测试 DB_* 变量，优先级即高于生产 .env
# 用法：
#   bash test_env/run_test_server.sh                 # 默认 department_store 库
#   bash test_env/run_test_server.sh baseball_1      # 指定其他 Spider 库
#   bash test_env/run_test_server.sh 库名 8003       # 指定端口（默认 8003，避免与生产 8002 冲突）
# 注意：测试会话与检查点写入 Redis（默认用户 default），不再落本地 JSON 文件
# ============================================================

set -e
cd "$(dirname "$0")"
DB_NAME_ARG="${1:-department_store}"
PORT_ARG="${2:-8003}"

# 动态解析 WSL 网关（Windows 宿主）IP，覆盖 .env.test 静态值，防 WSL 重启漂移
DB_HOST_NOW=$(ip route show default | awk '{print $3}')

# 加载测试凭据并导出（空值跳过，如未覆盖的 LLM 三项沿用生产配置）
set -a
source ./.env.test
set +a
# 清掉空值变量，避免空串覆盖生产 .env 中的同名配置（如 LLM key）
for var in $(grep -E '^(\w+)=$' .env.test | cut -d= -f1); do
    [ -z "${!var:-}" ] && unset "$var"
done
export DB_HOST="${DB_HOST_NOW:-$DB_HOST}"
export DB_NAME="$DB_NAME_ARG"
export NL2SQL_PORT="$PORT_ARG"
export NL2SQL_SERVE_FRONTEND=true
# 测试环境关闭结果截断（Guard 不追加强制 LIMIT、执行器不裁行），与评测口径一致；
# 需要压测截断保护时可显式覆盖：NL2SQL_FORCED_LIMIT=1000 NL2SQL_RESULT_ROW_LIMIT=1000
export NL2SQL_FORCED_LIMIT="${NL2SQL_FORCED_LIMIT:-0}"
export NL2SQL_RESULT_ROW_LIMIT="${NL2SQL_RESULT_ROW_LIMIT:-0}"

echo "=============================================="
echo " Spider 测试环境后端启动"
echo " 目标库 : $DB_NAME (Spider 测试集)"
echo " 数据库 : $DB_HOST:$DB_PORT (user=$DB_USER_NAME)"
echo " 服务   : http://localhost:$PORT_ARG"
echo " LLM    : 沿用生产百炼配置"
echo "=============================================="

# 解析 Python 解释器：优先 $NL2SQL_PYTHON，其次常见 conda 环境，最后 python3
if [ -n "${NL2SQL_PYTHON:-}" ] && command -v "$NL2SQL_PYTHON" >/dev/null 2>&1; then
    PY="$NL2SQL_PYTHON"
elif [ -x "$HOME/miniconda3/envs/rag/bin/python" ]; then
    PY="$HOME/miniconda3/envs/rag/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PY=python3
else
    echo "未找到可用 Python，请设置: export NL2SQL_PYTHON=/path/to/python"; exit 1
fi
echo "解释器: $PY"

cd ../backend
exec "$PY" run.py
