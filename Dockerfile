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
# SQLite 单 worker：避免多进程锁竞争（多 worker 由 compose 显式覆盖才启用）
ENV LOG_DIR=/app/logs \
    AGENT_DB_PATH=/data/agent_runs.db \
    UVICORN_WORKERS=1

# 非 root 运行（公网 Demo 最小权限）：固定 uid 1000，便于宿主机授权数据目录。
# 宿主机 bind mount 的 logs/ data/ 需可被 uid 1000 写入（见 docker-compose 注释）。
RUN groupadd -g 1000 appuser && useradd -u 1000 -g 1000 -d /app -s /sbin/nologin appuser \
    && mkdir -p /app/logs /data \
    && chown -R appuser:appuser /app/logs /data
USER appuser

EXPOSE 8000

# uvicorn 作为 ASGI 服务器；workers 数可用 UVICORN_WORKERS 覆盖
CMD ["sh", "-c", "uvicorn app.api.routes:app --host 0.0.0.0 --port 8000 --workers \"${UVICORN_WORKERS:-1}\""]
