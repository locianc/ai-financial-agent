"""FastAPI Service Layer 包：把 Agent 能力通过 HTTP API 暴露。

最小 MVP：GET /health + POST /chat。app 实例定义在 routes.py，本文件仅导出。
启动方式（可选）：uvicorn app.api.routes:app
"""

from app.api.routes import app

__all__ = ["app"]
