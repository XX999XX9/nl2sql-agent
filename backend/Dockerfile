# NL2SQL 后端镜像（FastAPI + uvicorn）
#
# 说明：
#   - 只打包代码与依赖；config / prompts / data / logs 由 docker-compose 以卷挂载，
#     便于在宿主机直接改配置与提示词、并保留运行时数据；
#   - .env（密钥）不打进镜像，由 docker-compose 的 env_file 注入进程环境变量。
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# chromadb 依赖的 hnswlib 等组件在部分架构上没有预编译轮子，保留最小编译链
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc g++ \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8002

# run.py 读取 NL2SQL_HOST / NL2SQL_PORT（默认 0.0.0.0:8002）
CMD ["python", "run.py"]
