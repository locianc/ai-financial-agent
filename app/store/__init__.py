"""Phase 17/18：Agent 运行记录持久化层（最小 SQLite 存储，标准库 sqlite3）。

- sessions 表：会话（id / title / created_at / updated_at），save_run 落库时刷新
  updated_at；title 为会话标题（Phase 18，/chat/stream 新建会话时用消息截断填充）；
- agent_runs 表：一次 Agent Run 完整快照
  id / question / answer / tool_calls(JSON TEXT) / tool_rounds /
  max_rounds_reached(SQLite 无 bool，存 0/1) / error(NULL 表示无错误) /
  created_at(UTC ISO8601，含 Z 时区标记) / session_id(可空外键，指向 sessions.id)。
- 每次操作独立连接（FastAPI 并发安全），WAL 模式，PRAGMA foreign_keys=ON 启用外键
  （SQLite 默认关闭外键，须每个连接显式开启）。
- _ensure_schema 幂等建表 + 迁移：旧库（Phase 16）agent_runs 缺 session_id 时
  ALTER TABLE ADD COLUMN 补齐，旧记录保留且 session_id 为 NULL；旧库（Phase 17）
  sessions 缺 title 时同样 ALTER TABLE 补齐，旧会话 title 为 NULL（向后兼容）。
- init_db(path) 显式建表（幂等）；save_run / get_run / create_session / get_session /
  list_sessions / list_runs 内部懒建表，任何调用顺序都不依赖先执行 init_db。
- 数据库路径为模块级 _DB_PATH（默认本包目录 agent_runs.db），
  init_db(path) 可切换；测试通过 init_db(临时目录) 隔离。
- 本模块零 import 副作用：不连库、不建文件，导入安全。
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

_SESSIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question TEXT NOT NULL,
    answer TEXT NOT NULL DEFAULT '',
    tool_calls TEXT NOT NULL DEFAULT '[]',
    tool_rounds INTEGER NOT NULL DEFAULT 0,
    max_rounds_reached INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at TEXT NOT NULL,
    session_id INTEGER REFERENCES sessions(id)
)
"""

# Phase 19：数据库路径支持 AGENT_DB_PATH 环境变量覆盖（Docker 挂载持久化），
# 默认仍为本包目录 agent_runs.db（本地开发行为不变）。
_DB_PATH: Path = Path(os.getenv("AGENT_DB_PATH", str(Path(__file__).resolve().parent / "agent_runs.db")))


def init_db(path: Optional[Union[str, Path]] = None) -> None:
    """建表（幂等）；传入 path 时切换模块级数据库路径。

    save_run / get_run / create_session / list_runs 内部也会懒建表，
    因此 init_db 非必须；显式调用主要用于部署时提前初始化或测试隔离。
    """
    global _DB_PATH
    if path is not None:
        _DB_PATH = Path(path)
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = _connect()
    try:
        _ensure_schema(conn)
    finally:
        conn.close()


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """幂等建表 + 迁移旧库。

    - Phase 16 旧库：agent_runs 无 session_id 列，ALTER TABLE 补齐，
      旧记录保留且 session_id 为 NULL；
    - Phase 17 旧库：sessions 无 title 列，ALTER TABLE 补齐，
      旧会话 title 为 NULL（向后兼容）。
    """
    conn.execute(_SESSIONS_SCHEMA)
    conn.execute(_SCHEMA)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(agent_runs)")}
    if "session_id" not in columns:
        conn.execute(
            "ALTER TABLE agent_runs ADD COLUMN session_id INTEGER REFERENCES sessions(id)"
        )
    session_columns = {row["name"] for row in conn.execute("PRAGMA table_info(sessions)")}
    if "title" not in session_columns:
        conn.execute("ALTER TABLE sessions ADD COLUMN title TEXT")
    conn.commit()


def _connect() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL").fetchone()
    conn.execute("PRAGMA foreign_keys=ON").fetchone()
    conn.row_factory = sqlite3.Row
    return conn


def _utc_now_iso() -> str:
    """当前 UTC 时间，ISO8601 含 Z 标记，如 2026-08-23T12:34:56Z。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def create_session(title: Optional[str] = None) -> int:
    """新建会话，返回自增 id（session_id）。

    title 可选：/chat/stream 新建会话时用首条消息截断生成标题，
    未提供时为 NULL（历史行为不变）。
    """
    conn = _connect()
    try:
        _ensure_schema(conn)
        cursor = conn.execute(
            "INSERT INTO sessions (title, created_at, updated_at) VALUES (?, ?, ?)",
            (title, _utc_now_iso(), _utc_now_iso()),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def get_session(session_id: int) -> Optional[Dict[str, Any]]:
    """按 id 取回会话信息；不存在返回 None。"""
    conn = _connect()
    try:
        _ensure_schema(conn)
        row = conn.execute(
            "SELECT id, title, created_at, updated_at FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "title": row["title"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
    finally:
        conn.close()


def list_sessions() -> List[Dict[str, Any]]:
    """列出全部会话，按最近活动排序（updated_at 降序，同刻按 id 降序）。"""
    conn = _connect()
    try:
        _ensure_schema(conn)
        rows = conn.execute(
            "SELECT id, title, created_at, updated_at FROM sessions "
            "ORDER BY updated_at DESC, id DESC"
        ).fetchall()
        return [
            {
                "id": row["id"],
                "title": row["title"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]
    finally:
        conn.close()


def save_run(
    question: str,
    answer: str,
    tool_calls: List[Dict[str, Any]],
    tool_rounds: int,
    max_rounds_reached: bool,
    error: Optional[str],
    session_id: Optional[int] = None,
) -> int:
    """插入一次 Agent Run 快照，返回自增 id（run_id）。

    session_id 可选：关联会话并同步刷新 sessions.updated_at。
    tool_calls 保持 AgentResult 原始结构（round/name/arguments/result）整体
    序列化为 JSON TEXT，get_run 读回时原样还原。
    """
    conn = _connect()
    try:
        _ensure_schema(conn)
        cursor = conn.execute(
            "INSERT INTO agent_runs "
            "(question, answer, tool_calls, tool_rounds, max_rounds_reached, "
            "error, created_at, session_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                question,
                answer,
                json.dumps(tool_calls, ensure_ascii=False),
                int(tool_rounds),
                int(bool(max_rounds_reached)),
                error,
                _utc_now_iso(),
                session_id,
            ),
        )
        if session_id is not None:
            conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (_utc_now_iso(), session_id),
            )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "session_id": row["session_id"],
        "question": row["question"],
        "answer": row["answer"],
        "tool_calls": json.loads(row["tool_calls"]),
        "tool_rounds": row["tool_rounds"],
        "max_rounds_reached": bool(row["max_rounds_reached"]),
        "error": row["error"],
        "created_at": row["created_at"],
    }


def get_run(run_id: int) -> Optional[Dict[str, Any]]:
    """按 id 取回记录；tool_calls 反序列化还原为 list[dict]；不存在返回 None。"""
    conn = _connect()
    try:
        _ensure_schema(conn)
        row = conn.execute(
            "SELECT id, question, answer, tool_calls, tool_rounds, "
            "max_rounds_reached, error, created_at, session_id "
            "FROM agent_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        return _row_to_dict(row)
    finally:
        conn.close()


def list_runs(session_id: int) -> List[Dict[str, Any]]:
    """按会话列出全部 Agent Run（按 id 升序），返回结构同 get_run。"""
    conn = _connect()
    try:
        _ensure_schema(conn)
        rows = conn.execute(
            "SELECT id, question, answer, tool_calls, tool_rounds, "
            "max_rounds_reached, error, created_at, session_id "
            "FROM agent_runs WHERE session_id = ? ORDER BY id",
            (session_id,),
        ).fetchall()
        return [_row_to_dict(row) for row in rows]
    finally:
        conn.close()


__all__ = [
    "init_db",
    "create_session",
    "get_session",
    "list_sessions",
    "save_run",
    "get_run",
    "list_runs",
]
