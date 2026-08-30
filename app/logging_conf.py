"""统一结构化日志配置（Phase 19A）。

- 控制台输出（StreamHandler -> stderr，便于容器/systemd 采集）；
- 文件输出（RotatingFileHandler，默认 logs/agent.log，5MB 轮转保留 3 份）；
- 级别 / 目录通过 LOG_LEVEL / LOG_DIR 环境变量覆盖（默认 INFO / logs）；
- setup_logging 幂等：重复调用先移除本模块先前添加的 handler 再重建，
  可安全地在模块导入期与服务 lifespan 中重复触发；
- uvicorn 默认 LOGGING_CONFIG 不配置 root logger，因此这里设置的根 handler
  在 uvicorn 启动后仍然保留（已验证 0.52.4）。

核心事件埋点位置（调用方）：
- 服务启动/停止：app.api.routes lifespan；
- Tool 调用与耗时：app.agent.orchestrator._execute_tool；
- API 请求与错误：app.api.routes / app.api.stream。
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

_LOG_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_FILE_MAX_BYTES = 5 * 1024 * 1024
_FILE_BACKUP_COUNT = 3

# 标记本模块添加的 handler，供幂等重建时识别
_HANDLER_ATTR = "_ai_agent_handler"


def setup_logging(level: str | None = None, log_dir: str | None = None) -> None:
    """配置根日志器：控制台 + 文件（可重复调用，自动去重重建）。

    level / log_dir 缺省时读取 LOG_LEVEL / LOG_DIR 环境变量；
    日志目录不可写时降级为仅控制台输出，不抛出异常。
    """
    log_level = (level or os.getenv("LOG_LEVEL", "INFO")).upper()
    root = logging.getLogger()
    root.setLevel(log_level)

    # 移除本模块先前添加的 handler，避免重复
    for handler in list(root.handlers):
        if getattr(handler, _HANDLER_ATTR, False):
            root.removeHandler(handler)

    formatter = logging.Formatter(_LOG_FORMAT, _DATE_FORMAT)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    setattr(console, _HANDLER_ATTR, True)
    root.addHandler(console)

    try:
        log_dir_path = Path(log_dir or os.getenv("LOG_DIR", "logs"))
        log_dir_path.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_dir_path / "agent.log",
            maxBytes=_FILE_MAX_BYTES,
            backupCount=_FILE_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        setattr(file_handler, _HANDLER_ATTR, True)
        root.addHandler(file_handler)
    except OSError:
        # 日志目录不可写（如只读容器文件系统）：仅控制台输出，不影响服务
        pass
