"""Phase 16/17：Agent Persistence Layer P0 确定性测试。

覆盖：
- init_db 建表幂等（重复调用不报错，表可用）；
- save_run 插入并返回自增 id（多记录递增）；
- save_run -> get_run 全字段 round-trip（question/answer/tool_rounds/
  max_rounds_reached/error/id）；
- tool_calls JSON 序列化往返（AgentResult 原始结构：
  round/name/arguments/result 四字段原样保留）；
- max_rounds_reached bool -> SQLite 0/1 存储 -> 读回 bool；
- error 边界：None 与字符串；
- created_at 为 UTC ISO8601 且含 Z 时区标记；
- question NOT NULL 约束（直接 SQL 违反时 IntegrityError）；
- get_run 未命中返回 None；
- Phase 17 会话：
  - create_session 返回自增 id（多会话递增）且 created_at/updated_at 合法；
  - 会话隔离：不同会话的 run 互不可见；
  - save_run 关联 session_id 后 get_run / list_runs 可读回；
  - list_runs 按 id 升序、空会话返回 []、无 session 的 run 不属于任何会话；
  - 不存在的 session_id 触发外键 IntegrityError（PRAGMA foreign_keys=ON）；
  - save_run 关联会话时刷新 sessions.updated_at；
  - 迁移：Phase 16 旧库（agent_runs 无 session_id）经 init_db 自动补列，
    旧数据保留且 session_id 为 NULL。

依赖：标准库 sqlite3 + tempfile，零联网。每个用例经 init_db(独立临时文件)
隔离数据库，不触碰仓库内 agent_runs.db。

运行：cd E:/github/ai-financial-agent && .venv/Scripts/python.exe tests/test_store.py
"""

from __future__ import annotations

import re
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.store import create_session, get_run, init_db, list_runs, save_run  # noqa: E402

_FAILURES: List[str] = []


def _run(name: str, fn) -> None:
    try:
        fn()
        print(f"  PASS  {name}")
    except AssertionError as exc:
        print(f"  FAIL  {name}: {exc}")
        _FAILURES.append(f"{name}: {exc}")
    except Exception as exc:  # noqa: BLE001
        print(f"  ERROR {name}: {type(exc).__name__}: {exc}")
        _FAILURES.append(f"{name}: {type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# 用例（每个用例独立的临时数据库文件）
# ---------------------------------------------------------------------------

def test_init_db_idempotent(tmp: str) -> None:
    db = Path(tmp) / "init_idem.db"
    init_db(db)
    init_db(db)  # 重复建表不报错
    rid = save_run("q", "a", [], 0, False, None)  # 表可用
    assert get_run(rid) is not None


def test_save_run_incrementing_id(tmp: str) -> None:
    db = Path(tmp) / "incr.db"
    init_db(db)
    first = save_run("问题一", "答案一", [], 0, False, None)
    second = save_run("问题二", "答案二", [], 1, True, "err")
    assert first == 1
    assert second == 2
    assert first != second


def test_round_trip(tmp: str) -> None:
    db = Path(tmp) / "rt.db"
    init_db(db)
    tool_calls = [
        {
            "round": 1,
            "name": "get_stock_price",
            "arguments": {"symbol": "600519"},
            "result": {"price": 100.0},
        }
    ]
    rid = save_run("分析贵州茅台", "最终回答", tool_calls, 2, True, "工具异常")
    row = get_run(rid)
    assert row is not None
    assert row["id"] == rid
    assert row["question"] == "分析贵州茅台"
    assert row["answer"] == "最终回答"
    assert row["tool_rounds"] == 2
    assert row["max_rounds_reached"] is True
    assert row["error"] == "工具异常"
    assert row["tool_calls"] == tool_calls


def test_tool_calls_round_trip(tmp: str) -> None:
    db = Path(tmp) / "tc.db"
    init_db(db)
    calls = [
        {
            "round": 1,
            "name": "get_stock_price",
            "arguments": {"symbol": "600519"},
            "result": {"price": 1680.0, "currency": "CNY"},
        },
        {
            "round": 1,
            "name": "get_technical_analysis",
            "arguments": {"symbol": "600519"},
            "result": {"rsi": 55.3},
        },
        {
            "round": 2,
            "name": "get_fundamentals",
            "arguments": {"symbol": "600519"},
            "result": {"pe": 28.5},
        },
    ]
    rid = save_run("q", "a", calls, 2, False, None)
    got = get_run(rid)["tool_calls"]
    assert got == calls
    # 原始结构：4 字段齐全，未简化
    assert all(set(c.keys()) == {"round", "name", "arguments", "result"} for c in got)


def test_bool_conversion(tmp: str) -> None:
    db = Path(tmp) / "bool.db"
    init_db(db)
    rid_true = save_run("q", "a", [], 3, True, None)
    rid_false = save_run("q", "a", [], 0, False, None)
    assert get_run(rid_true)["max_rounds_reached"] is True
    assert get_run(rid_false)["max_rounds_reached"] is False
    # SQLite 层存的是 0/1（整数）
    conn = sqlite3.connect(db)
    try:
        values = [r[0] for r in conn.execute("SELECT max_rounds_reached FROM agent_runs ORDER BY id")]
        assert values == [1, 0]
    finally:
        conn.close()


def test_error_boundary(tmp: str) -> None:
    db = Path(tmp) / "err.db"
    init_db(db)
    rid_none = save_run("q", "a", [], 0, False, None)
    rid_str = save_run("q", "a", [], 0, False, "boom")
    assert get_run(rid_none)["error"] is None
    assert get_run(rid_str)["error"] == "boom"


def test_created_at_utc_z(tmp: str) -> None:
    db = Path(tmp) / "ts.db"
    init_db(db)
    rid = save_run("q", "a", [], 0, False, None)
    created_at = get_run(rid)["created_at"]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", created_at), (
        f"created_at 应为 UTC ISO8601 含 Z：{created_at!r}"
    )


def test_question_not_null(tmp: str) -> None:
    db = Path(tmp) / "nn.db"
    init_db(db)
    conn = sqlite3.connect(db)
    try:
        try:
            conn.execute(
                "INSERT INTO agent_runs (question, created_at) VALUES (NULL, '2026-01-01T00:00:00Z')"
            )
            conn.commit()
            raise AssertionError("question=NULL 应触发 IntegrityError")
        except sqlite3.IntegrityError:
            pass
    finally:
        conn.close()


def test_get_run_missing(tmp: str) -> None:
    db = Path(tmp) / "miss.db"
    init_db(db)
    rid = save_run("q", "a", [], 0, False, None)
    assert get_run(rid) is not None
    assert get_run(rid + 100) is None


# ---------------------------------------------------------------------------
# Phase 17：会话（sessions / session_id / list_runs / 迁移）
# ---------------------------------------------------------------------------

def test_create_session_incrementing(tmp: str) -> None:
    db = Path(tmp) / "sess.db"
    init_db(db)
    first = create_session()
    second = create_session()
    assert first == 1
    assert second == 2
    assert first != second
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute("SELECT id, created_at, updated_at FROM sessions ORDER BY id").fetchall()
        assert [r[0] for r in rows] == [1, 2]
        for _, created_at, updated_at in rows:
            assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", created_at)
            assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", updated_at)
    finally:
        conn.close()


def test_session_isolation(tmp: str) -> None:
    db = Path(tmp) / "iso.db"
    init_db(db)
    sid_a = create_session()
    sid_b = create_session()
    save_run("q1", "a1", [], 0, False, None, session_id=sid_a)
    save_run("q2", "a2", [], 0, False, None, session_id=sid_b)
    assert len(list_runs(sid_a)) == 1
    assert list_runs(sid_a)[0]["question"] == "q1"
    assert len(list_runs(sid_b)) == 1
    assert list_runs(sid_b)[0]["question"] == "q2"


def test_run_session_association(tmp: str) -> None:
    db = Path(tmp) / "assoc.db"
    init_db(db)
    sid = create_session()
    rid = save_run("q", "a", [], 1, True, None, session_id=sid)
    row = get_run(rid)
    assert row is not None
    assert row["session_id"] == sid
    assert list_runs(sid)[0]["id"] == rid


def test_list_runs_ascending_order(tmp: str) -> None:
    db = Path(tmp) / "order.db"
    init_db(db)
    sid = create_session()
    r1 = save_run("第一问", "第一答", [], 0, False, None, session_id=sid)
    r2 = save_run("第二问", "第二答", [], 1, True, "err", session_id=sid)
    rows = list_runs(sid)
    assert [r["id"] for r in rows] == [r1, r2]
    assert rows[0]["question"] == "第一问"
    assert rows[1]["tool_rounds"] == 1
    assert rows[1]["max_rounds_reached"] is True
    assert rows[1]["error"] == "err"


def test_list_runs_empty(tmp: str) -> None:
    db = Path(tmp) / "empty.db"
    init_db(db)
    sid = create_session()
    assert list_runs(sid) == []
    assert list_runs(sid + 100) == []  # 不存在的会话同样返回空列表


def test_save_run_without_session(tmp: str) -> None:
    db = Path(tmp) / "nosess.db"
    init_db(db)
    rid = save_run("q", "a", [], 0, False, None)
    row = get_run(rid)
    assert row is not None
    assert row["session_id"] is None
    assert list_runs(999) == []  # 无 session 的记录不出现在任何会话列表


def test_save_run_invalid_session_fk(tmp: str) -> None:
    db = Path(tmp) / "fk.db"
    init_db(db)
    try:
        save_run("q", "a", [], 0, False, None, session_id=999)
        raise AssertionError("不存在的 session_id 应触发外键 IntegrityError")
    except sqlite3.IntegrityError:
        pass


def test_save_run_updates_session_updated_at(tmp: str) -> None:
    db = Path(tmp) / "upd.db"
    init_db(db)
    sid = create_session()
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "UPDATE sessions SET updated_at = '2000-01-01T00:00:00Z' WHERE id = ?", (sid,)
        )
        conn.commit()
    finally:
        conn.close()
    save_run("q", "a", [], 0, False, None, session_id=sid)
    conn = sqlite3.connect(db)
    try:
        updated_at = conn.execute(
            "SELECT updated_at FROM sessions WHERE id = ?", (sid,)
        ).fetchone()[0]
    finally:
        conn.close()
    assert updated_at != "2000-01-01T00:00:00Z"  # save_run 已刷新 updated_at
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", updated_at)


def test_migration_old_db_adds_session_id(tmp: str) -> None:
    db = Path(tmp) / "mig.db"
    conn = sqlite3.connect(db)
    try:
        # 手工构造 Phase 16 旧库：agent_runs 无 session_id 列
        conn.execute(
            """
            CREATE TABLE agent_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                question TEXT NOT NULL,
                answer TEXT NOT NULL DEFAULT '',
                tool_calls TEXT NOT NULL DEFAULT '[]',
                tool_rounds INTEGER NOT NULL DEFAULT 0,
                max_rounds_reached INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO agent_runs (question, answer, tool_calls, tool_rounds, "
            "max_rounds_reached, error, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("旧问题", "旧答案", "[]", 1, 0, None, "2026-01-01T00:00:00Z"),
        )
        conn.commit()
    finally:
        conn.close()
    init_db(db)  # 触发迁移：补齐 session_id 列
    old = get_run(1)
    assert old is not None
    assert old["question"] == "旧问题"
    assert old["session_id"] is None  # 旧记录 session_id 为 NULL
    new_rid = save_run("新问题", "新答案", [], 0, False, None)
    assert get_run(new_rid)["session_id"] is None  # 迁移后新写入正常
    sid = create_session()  # sessions 表可正常使用
    assert list_runs(sid) == []


def main() -> None:
    print("=== tests/test_store.py Agent Persistence Layer P0 测试 ===")
    with tempfile.TemporaryDirectory(prefix="test_store_") as tmp:
        tests = [
            ("1. init_db 建表幂等", lambda: test_init_db_idempotent(tmp)),
            ("2. save_run 自增 id（多记录）", lambda: test_save_run_incrementing_id(tmp)),
            ("3. save_run->get_run 全字段 round-trip", lambda: test_round_trip(tmp)),
            ("4. tool_calls JSON 原样往返（原始结构）", lambda: test_tool_calls_round_trip(tmp)),
            ("5. bool -> 0/1 存储与读回", lambda: test_bool_conversion(tmp)),
            ("6. error 边界（None / 字符串）", lambda: test_error_boundary(tmp)),
            ("7. created_at UTC ISO8601 含 Z", lambda: test_created_at_utc_z(tmp)),
            ("8. question NOT NULL 约束", lambda: test_question_not_null(tmp)),
            ("9. get_run 未命中返回 None", lambda: test_get_run_missing(tmp)),
            ("10. create_session 自增 id + 时间戳", lambda: test_create_session_incrementing(tmp)),
            ("11. 会话隔离（互不可见）", lambda: test_session_isolation(tmp)),
            ("12. run 关联 session 后可读回", lambda: test_run_session_association(tmp)),
            ("13. list_runs 按 id 升序 + 全字段", lambda: test_list_runs_ascending_order(tmp)),
            ("14. 空会话 / 不存在会话 -> []", lambda: test_list_runs_empty(tmp)),
            ("15. 无 session 的 run 不属任何会话", lambda: test_save_run_without_session(tmp)),
            ("16. 不存在 session_id -> 外键 IntegrityError", lambda: test_save_run_invalid_session_fk(tmp)),
            ("17. save_run 刷新 sessions.updated_at", lambda: test_save_run_updates_session_updated_at(tmp)),
            ("18. 旧库迁移：补 session_id 列且旧数据保留", lambda: test_migration_old_db_adds_session_id(tmp)),
        ]
        for name, fn in tests:
            _run(name, fn)
    total = len(tests)
    passed = total - len(_FAILURES)
    print(f"\n结果：{passed}/{total} 通过")
    if _FAILURES:
        print("失败明细：")
        for item in _FAILURES:
            print(f"  - {item}")
        sys.exit(1)
    print("全部通过。")


if __name__ == "__main__":
    main()
