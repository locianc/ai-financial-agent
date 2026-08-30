"""第十五阶段：FastAPI Service Layer P0 确定性测试。

覆盖：
- GET /health：200 + schema；
- POST /chat 正常路径：直接回答（无工具）、并行工具 + 最终回答（打桩 TOOL_DISPATCH），
  响应含 answer / tool_calls / tool_rounds / max_rounds_reached / error 且无 CLI 文本；
- 参数错误：question 缺失 / 空串 -> 422；
- Agent 异常处理：模型 create 抛异常 -> 200 + error 字段；未配置 key -> 503；
- Service 层零终端副作用：/chat 调用不产生 stdout 输出；
- ChatResponse schema round_trip 与可变字段 default_factory 隔离；
- 持久化：保存成功 -> run_id 返回且 save_run 收到完整快照（含原始 tool_calls）；
  保存失败 -> 200 + run_id=null，Agent 回复不受影响；
- 会话（Phase 17）：无 session_id -> 新建会话并关联 run；有 session_id -> 复用
  且不新建；保存失败 -> run_id null 但 session_id 仍回显；
- 历史：GET /sessions/{session_id}/runs -> 200 + RunRecord 列表；空会话 -> [].

依赖：TestClient + mock.patch 注入 fake OpenAI client（routes 的 _get_client 为
模块级函数，路由函数体内调用，body 校验 422 优先于 client 创建 503），零联网。
save_run / create_session 由 main() 统一打桩（固定返回值），list_runs 在用例内
patch，用例内嵌套 patch 覆盖成功/失败路径，全程不落库、不产生数据库文件。

运行：cd E:/github/ai-financial-agent && .venv/Scripts/python.exe tests/test_api.py
"""

from __future__ import annotations

import io
import json
import sys
import types
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from fastapi.testclient import TestClient  # noqa: E402

import app.api.routes as routes_mod  # noqa: E402
import app.agent.orchestrator as orchestrator_mod  # noqa: E402
from app.api.routes import app  # noqa: E402
from app.api.schemas import ChatResponse, ToolCallInfo  # noqa: E402

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
# fake OpenAI client（与 test_agent_orchestrator 相同模式）
# ---------------------------------------------------------------------------

class _FakeMessage:
    def __init__(self, content: Optional[str] = None, tool_calls: Optional[List[Any]] = None) -> None:
        self.content = content
        self.tool_calls = tool_calls


class _FakeCompletions:
    def __init__(self, responses: List[Any], fail_on_create: bool = False) -> None:
        self._queue = list(responses)
        self.calls: List[Dict[str, Any]] = []
        self._fail_on_create = fail_on_create

    def create(self, **kwargs: Any) -> Any:
        if kwargs.get("response_format") == {"type": "json_object"}:
            # Phase 20A：Router 内部非流式调用，不记录、不进响应队列
            return _resp(content=json.dumps(_DEFAULT_ROUTE))
        self.calls.append(kwargs)
        if self._fail_on_create:
            raise RuntimeError("boom")
        return self._queue.pop(0)


class _FakeClient:
    def __init__(self, responses: Optional[List[Any]] = None, fail_on_create: bool = False) -> None:
        self.chat = types.SimpleNamespace(
            completions=_FakeCompletions(responses or [], fail_on_create=fail_on_create)
        )


def _tool_call(call_id: str, name: str, arguments: str) -> Any:
    return types.SimpleNamespace(
        id=call_id, function=types.SimpleNamespace(name=name, arguments=arguments)
    )


def _resp(content: Optional[str] = None, tool_calls: Optional[List[Any]] = None) -> Any:
    return types.SimpleNamespace(
        choices=[types.SimpleNamespace(message=_FakeMessage(content=content, tool_calls=tool_calls))]
    )


def _final_client(answer: str = "直接回答") -> _FakeClient:
    return _FakeClient([_resp(content=answer)])


def _tool_then_final_client() -> _FakeClient:
    calls = [
        _tool_call("call_price", "get_stock_price", '{"symbol": "600519"}'),
        _tool_call("call_tech", "get_technical_analysis", '{"symbol": "600519"}'),
    ]
    return _FakeClient([_resp(tool_calls=calls), _resp(content="最终回答")])


_FAKE_TOOLS = {
    "get_stock_price": lambda symbol="": {"symbol": symbol, "price": 100.0},
    "get_technical_analysis": lambda symbol="": {"symbol": symbol, "rsi": 50.0},
}

# Phase 20A：Router 默认全维度关闭，避免吞掉 fake 响应队列、干扰既有断言
_DEFAULT_ROUTE = {"needs_fundamental": False, "needs_quant": False, "needs_event": False}


# ---------------------------------------------------------------------------
# helper：注入 fake client（mock.patch routes._get_client）
# ---------------------------------------------------------------------------

def _patch_client(factory) -> mock._patch:
    return mock.patch.object(routes_mod, "_get_client", factory)


# ---------------------------------------------------------------------------
# GET /health
# ---------------------------------------------------------------------------

def test_health_ok() -> None:
    with TestClient(app) as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# POST /chat 正常路径
# ---------------------------------------------------------------------------

def test_chat_direct_answer() -> None:
    with _patch_client(lambda: _final_client("直接回答")), TestClient(app) as client:
        resp = client.post("/chat", json={"question": "简单问题"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["answer"] == "直接回答"
    assert body["tool_calls"] == []
    assert body["tool_rounds"] == 0
    assert body["max_rounds_reached"] is False
    assert body["error"] is None


def test_chat_parallel_tools() -> None:
    with mock.patch.dict(orchestrator_mod.TOOL_DISPATCH, _FAKE_TOOLS, clear=True):
        with _patch_client(lambda: _tool_then_final_client()), TestClient(app) as client:
            resp = client.post("/chat", json={"question": "分析贵州茅台 600519"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["answer"] == "最终回答"
    assert body["tool_rounds"] == 1
    assert [c["name"] for c in body["tool_calls"]] == ["get_stock_price", "get_technical_analysis"]
    assert [c["arguments"] for c in body["tool_calls"]] == [{"symbol": "600519"}] * 2
    assert body["tool_calls"][0]["result"]["price"] == 100.0
    assert body["tool_calls"][1]["result"]["rsi"] == 50.0
    assert body["error"] is None


# ---------------------------------------------------------------------------
# 参数错误
# ---------------------------------------------------------------------------

def test_chat_missing_question_422() -> None:
    with TestClient(app) as client:
        resp = client.post("/chat", json={})
    assert resp.status_code == 422


def test_chat_empty_question_422() -> None:
    with TestClient(app) as client:
        resp = client.post("/chat", json={"question": ""})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Agent 异常处理
# ---------------------------------------------------------------------------

def test_chat_model_api_error_sets_error_field() -> None:
    with _patch_client(lambda: _FakeClient(fail_on_create=True)), TestClient(app) as client:
        resp = client.post("/chat", json={"question": "q"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["answer"] == ""
    assert body["error"] is not None
    assert "DeepSeek API 调用失败" in body["error"]
    assert "RuntimeError" in body["error"]


def test_chat_no_api_key_returns_503() -> None:
    def _no_key() -> None:
        raise RuntimeError("未配置 DEEPSEEK_API_KEY，请在 .env 中配置后重试。")

    with _patch_client(_no_key), TestClient(app) as client:
        resp = client.post("/chat", json={"question": "q"})
    assert resp.status_code == 503
    assert "DEEPSEEK_API_KEY" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# 持久化（run_id）
# ---------------------------------------------------------------------------

def test_chat_save_success_returns_run_id() -> None:
    captured: Dict[str, Any] = {}

    def _fake_save(**kwargs: Any) -> int:
        captured.update(kwargs)
        return 7

    with _patch_client(lambda: _final_client("持久化回答")), mock.patch.object(
        routes_mod, "save_run", side_effect=_fake_save
    ), TestClient(app) as client:
        resp = client.post("/chat", json={"question": "分析 600519"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["run_id"] == 7
    assert body["answer"] == "持久化回答"
    # save_run 收到完整快照：question + AgentResult 字段
    assert captured["question"] == "分析 600519"
    assert captured["answer"] == "持久化回答"
    assert captured["tool_calls"] == []
    assert captured["tool_rounds"] == 0
    assert captured["max_rounds_reached"] is False
    assert captured["error"] is None


def test_chat_save_preserves_tool_calls() -> None:
    captured: Dict[str, Any] = {}

    def _fake_save(**kwargs: Any) -> int:
        captured.update(kwargs)
        return 7

    with mock.patch.dict(orchestrator_mod.TOOL_DISPATCH, _FAKE_TOOLS, clear=True):
        with _patch_client(lambda: _tool_then_final_client()), mock.patch.object(
            routes_mod, "save_run", side_effect=_fake_save
        ), TestClient(app) as client:
            resp = client.post("/chat", json={"question": "分析 600519"})
    assert resp.status_code == 200
    assert resp.json()["run_id"] == 7
    calls = captured["tool_calls"]
    assert len(calls) == 2
    assert [c["name"] for c in calls] == ["get_stock_price", "get_technical_analysis"]
    assert [c["arguments"] for c in calls] == [{"symbol": "600519"}] * 2
    assert calls[0]["result"]["price"] == 100.0
    # 原始结构：4 字段齐全，未简化
    assert all(set(c.keys()) == {"round", "name", "arguments", "result"} for c in calls)


def test_chat_save_failure_degrades_gracefully() -> None:
    with _patch_client(lambda: _final_client("降级回答")), mock.patch.object(
        routes_mod, "save_run", side_effect=RuntimeError("db down")
    ), TestClient(app) as client:
        resp = client.post("/chat", json={"question": "q"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["answer"] == "降级回答"  # Agent 回复不受持久化失败影响
    assert body["run_id"] is None
    assert body["error"] is None


# ---------------------------------------------------------------------------
# 会话（Phase 17）
# ---------------------------------------------------------------------------

def test_chat_new_session_created() -> None:
    created: List[int] = []
    captured: Dict[str, Any] = {}

    def _fake_create() -> int:
        created.append(1)
        return 101

    def _fake_save(**kwargs: Any) -> int:
        captured.update(kwargs)
        return 7

    with _patch_client(lambda: _final_client("新会话回答")), mock.patch.object(
        routes_mod, "create_session", side_effect=_fake_create
    ), mock.patch.object(
        routes_mod, "save_run", side_effect=_fake_save
    ), TestClient(app) as client:
        resp = client.post("/chat", json={"question": "分析 600519"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["session_id"] == 101
    assert len(created) == 1  # 无 session_id 时恰好新建一次会话
    assert captured["session_id"] == 101  # run 关联到新建会话


def test_chat_reuses_existing_session() -> None:
    created: List[int] = []
    captured: Dict[str, Any] = {}

    def _fake_create() -> int:
        created.append(1)
        return 101

    def _fake_save(**kwargs: Any) -> int:
        captured.update(kwargs)
        return 7

    with _patch_client(lambda: _final_client("复用会话回答")), mock.patch.object(
        routes_mod, "create_session", side_effect=_fake_create
    ), mock.patch.object(
        routes_mod, "save_run", side_effect=_fake_save
    ), TestClient(app) as client:
        resp = client.post("/chat", json={"question": "q", "session_id": 5})
    assert resp.status_code == 200
    body = resp.json()
    assert body["session_id"] == 5  # 回显请求中的 session_id
    assert created == []  # 有 session_id 时不创建新会话
    assert captured["session_id"] == 5  # run 关联到既有会话


def test_chat_save_failure_keeps_session_id() -> None:
    with _patch_client(lambda: _final_client("降级回答")), mock.patch.object(
        routes_mod, "create_session", return_value=101
    ), mock.patch.object(
        routes_mod, "save_run", side_effect=RuntimeError("db down")
    ), TestClient(app) as client:
        resp = client.post("/chat", json={"question": "q"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["answer"] == "降级回答"
    assert body["run_id"] is None
    assert body["session_id"] == 101  # 会话创建成功仍回显，run 落库失败单独降级


# ---------------------------------------------------------------------------
# 输出合规校验（任务一：Validator 接入真实链路）
# ---------------------------------------------------------------------------

def test_chat_degraded_on_violation() -> None:
    """最终回答命中高危违禁模式（违规荐股）时，原始结论被拦截并降级。"""
    captured: Dict[str, Any] = {}

    def _fake_save(**kwargs: Any) -> int:
        captured.update(kwargs)
        return 7

    with _patch_client(lambda: _final_client("建议买入该股，明天一定大涨")), mock.patch.object(
        routes_mod, "save_run", side_effect=_fake_save
    ), TestClient(app) as client:
        resp = client.post("/chat", json={"question": "分析 600519"})
    assert resp.status_code == 200
    body = resp.json()
    # 原始结论被拦截：answer 为受限降级答案而非 LLM 原话
    assert body["answer"] != "建议买入该股，明天一定大涨"
    assert "【回答受限：风险提示】" in body["answer"]
    assert "原始结论已被拦截" in body["answer"]
    # 落库的同样是降级答案（历史回放不泄露原始结论）
    assert captured["answer"] == body["answer"]
    assert body["error"] is None


def test_chat_clean_answer_not_degraded() -> None:
    """未命中高危违禁模式的正常回答保持原样，不降级。"""
    with _patch_client(lambda: _final_client("该股当前市盈率处于历史中位区间，请结合自身风险承受能力理性决策")), TestClient(
        app
    ) as client:
        resp = client.post("/chat", json={"question": "分析 600519"})
    assert resp.status_code == 200
    body = resp.json()
    assert "【回答受限：风险提示】" not in body["answer"]
    assert "该股当前市盈率" in body["answer"]


# ---------------------------------------------------------------------------
# 历史查询（Phase 17）
# ---------------------------------------------------------------------------

def test_session_runs_history() -> None:
    captured_session: List[int] = []
    fake_rows = [
        {
            "id": 3,
            "session_id": 7,
            "question": "第一问",
            "answer": "第一答",
            "tool_calls": [
                {
                    "round": 1,
                    "name": "get_stock_price",
                    "arguments": {"symbol": "600519"},
                    "result": {"price": 100.0},
                }
            ],
            "tool_rounds": 1,
            "max_rounds_reached": False,
            "error": None,
            "created_at": "2026-08-23T00:00:00Z",
        },
        {
            "id": 4,
            "session_id": 7,
            "question": "第二问",
            "answer": "第二答",
            "tool_calls": [],
            "tool_rounds": 0,
            "max_rounds_reached": False,
            "error": None,
            "created_at": "2026-08-23T00:01:00Z",
        },
    ]

    def _fake_list(session_id: int) -> List[Dict[str, Any]]:
        captured_session.append(session_id)
        return fake_rows

    with mock.patch.object(routes_mod, "list_runs", side_effect=_fake_list), TestClient(app) as client:
        resp = client.get("/sessions/7/runs")
    assert resp.status_code == 200
    body = resp.json()
    assert captured_session == [7]  # 路径参数透传
    assert len(body) == 2
    assert body[0]["id"] == 3
    assert body[0]["session_id"] == 7
    assert body[0]["question"] == "第一问"
    assert body[0]["tool_calls"][0]["name"] == "get_stock_price"
    assert body[0]["max_rounds_reached"] is False
    assert body[0]["created_at"] == "2026-08-23T00:00:00Z"
    assert body[1]["id"] == 4
    assert body[1]["question"] == "第二问"


def test_session_runs_empty() -> None:
    captured_session: List[int] = []

    def _fake_list(session_id: int) -> List[Dict[str, Any]]:
        captured_session.append(session_id)
        return []

    with mock.patch.object(routes_mod, "list_runs", side_effect=_fake_list), TestClient(app) as client:
        resp = client.get("/sessions/999/runs")
    assert resp.status_code == 200
    assert resp.json() == []
    assert captured_session == [999]


# ---------------------------------------------------------------------------
# Service 层零终端副作用
# ---------------------------------------------------------------------------

def test_chat_produces_no_stdout() -> None:
    buffer = io.StringIO()
    with _patch_client(lambda: _final_client("静默回答")), TestClient(app) as client, redirect_stdout(buffer):
        resp = client.post("/chat", json={"question": "测试问题"})
    assert resp.status_code == 200
    assert buffer.getvalue() == "", f"Service 层不应产生 stdout：{buffer.getvalue()!r}"


# ---------------------------------------------------------------------------
# schema round_trip 与 default_factory 隔离
# ---------------------------------------------------------------------------

def test_chat_response_schema_round_trip() -> None:
    model = ChatResponse(
        answer="a",
        tool_calls=[
            ToolCallInfo(round=1, name="t", arguments={"symbol": "600519"}, result={"price": 100.0})
        ],
        tool_rounds=1,
        max_rounds_reached=False,
        error=None,
    )
    data = model.model_dump()
    assert data["answer"] == "a"
    assert data["tool_calls"][0]["arguments"] == {"symbol": "600519"}
    assert data["error"] is None
    # 序列化 JSON 后结构保持
    import json

    dumped = json.loads(model.model_dump_json())
    assert dumped["tool_rounds"] == 1


def test_chat_response_mutable_field_isolated() -> None:
    c1, c2 = ChatResponse(), ChatResponse()
    c1.tool_calls.append(ToolCallInfo(round=1, name="x", arguments={}, result={}))
    assert c2.tool_calls == []
    assert len(c2.tool_calls) == 0


def main() -> None:
    print("=== tests/test_api.py FastAPI Service Layer P0 测试 ===")
    tests = [
        ("1. GET /health 200", test_health_ok),
        ("2. /chat 直接回答", test_chat_direct_answer),
        ("3. /chat 并行工具 + 最终回答", test_chat_parallel_tools),
        ("4. /chat question 缺失 422", test_chat_missing_question_422),
        ("5. /chat question 空串 422", test_chat_empty_question_422),
        ("6. 模型 API 异常 -> 200 + error 字段", test_chat_model_api_error_sets_error_field),
        ("7. 未配置 key -> 503", test_chat_no_api_key_returns_503),
        ("8. /chat 零 stdout 副作用", test_chat_produces_no_stdout),
        ("9. ChatResponse schema round_trip", test_chat_response_schema_round_trip),
        ("10. 可变列表字段 default_factory 隔离", test_chat_response_mutable_field_isolated),
        ("11. 保存成功：run_id 返回 + save_run 收到完整快照", test_chat_save_success_returns_run_id),
        ("12. 保存成功：tool_calls 原始结构透传", test_chat_save_preserves_tool_calls),
        ("13. 保存失败：200 + run_id=null 降级", test_chat_save_failure_degrades_gracefully),
        ("14. 无 session_id：新建会话并关联 run", test_chat_new_session_created),
        ("15. 有 session_id：复用会话不新建", test_chat_reuses_existing_session),
        ("16. 保存失败：run_id null + session_id 仍回显", test_chat_save_failure_keeps_session_id),
        ("17. GET /sessions/{id}/runs 历史列表", test_session_runs_history),
        ("18. GET /sessions/{id}/runs 空会话 []", test_session_runs_empty),
        ("19. 输出合规拦截：违规荐股降级 + 落库降级答案", test_chat_degraded_on_violation),
        ("20. 输出合规放行：正常回答不降级", test_chat_clean_answer_not_degraded),
    ]
    # 基础打桩：save_run / create_session 固定返回，避免测试真实落库（确定性、零 DB 文件）
    with mock.patch.object(routes_mod, "save_run", return_value=7), mock.patch.object(
        routes_mod, "create_session", return_value=101
    ):
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
