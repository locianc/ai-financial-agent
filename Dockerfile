# =============================================================
# AI Financial Agent - Backend
# Python 3.12 轻量镜像 + uvicorn ASGI 服务
# 构建：docker build -f Dockerfile -t ai-financial-agent-backend .
# =============================================================

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 先安装依赖（层缓存：requirements.txt 不变时复用镜像层）。
# gcc/g++/libffi-dev 用于个别包缺少 manylinux wheel 时的源码编译兜底，
# 装完即清理，避免残留编译工具放大镜像体积。
COPY requirements.txt .
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc g++ make libffi-dev \
    && pip install --no-cache-dir -r requirements.txt \
    && apt-get purge -y --auto-remove gcc g++ make libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# 应用代码（不复制 .env：密钥经 docker-compose env_file 注入，
# 镜像内无密钥残留，符合最小权限原则）
COPY app ./app
COPY tools ./tools
COPY main.py .

# 运行期环境变量（容器内路径；可被 docker-compose environment 覆盖）
ENV LOG_DIR=/app/logs \
    AGENT_DB_PATH=/data/agent_runs.db \
    UVICORN_WORKERS=2

EXPOSE 8000

# uvicorn 作为 ASGI 服务器；workers 数可用 UVICORN_WORKERS 覆盖
CMD ["sh", "-c", "uvicorn app.api.routes:app --host 0.0.0.0 --port 8000 --workers \"${UVICORN_WORKERS:-2}\""]
