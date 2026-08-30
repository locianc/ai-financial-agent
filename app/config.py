"""统一配置加载（Phase 19A）。

服务入口（app.api.routes）导入本模块时强制通过 python-dotenv 加载
项目根目录 .env，保证 uvicorn 直接启动时环境变量必定生效：

- override=False：已存在的真实环境变量（容器 / docker-compose 注入、
  shell export）优先级最高，.env 仅作本地开发兜底来源；
- 容器镜像不复制 .env，密钥一律经 docker-compose environment / env_file 注入；
- 本模块零副作用：仅当被显式导入时才执行加载。
"""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv

# 项目根目录：app/ 的上一级（本地 E:/github/ai-financial-agent，容器内 /app）
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_env(override: bool = False) -> bool:
    """加载项目根目录 .env；返回是否成功找到并解析 .env 文件。"""
    return load_dotenv(PROJECT_ROOT / ".env", override=override)


load_env()
