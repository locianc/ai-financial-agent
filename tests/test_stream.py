"""Phase 18：SSE 流式接口 P0 确定性测试。

覆盖：
- 同步生成器 _stream_agent_events 事件顺序：tool_call 先于 tool_result、
  全部工具事件先于 token、__result__ 收尾；token 逐片、arguments 增量拼接；
  工具调用原始结构透传；create(stream=True) 透传；
- API 失败：error 事件 + __result__ 携带 error；
- 异步包装 run_agent_streaming：与同步事件序列一致；
- POST /chat/stream（TestClient stream=True）：
  - 完整流程 SSE 事件序列 tool_call/tool_result/token/done；
    save_run 收到 __result__ 快照（question/answer/tool_calls/.../session_id）；
    done 回显 session_id/run_id；
  - 无 session_id -> create_session(title=消息前 30 字) 且仅新建一次；
  - 有 session_id 且存在 -> 复用，不新建；
  - client 创建失败 -> error + done 事件（HTTP 仍 200）；
  - save_run 失败 -> done.run_id 为 null，token 流不受影响；
  - 零 stdout 副作用；
- GET /sessions：列表 + SessionInfo schema 校验。

依赖：TestClient + mock.patch 注入 fake 流式 client（stream_mod 的模块级引用：
_get_client / run_agent_streaming / save_run / create_session / get_session），
零联网、不落库。

运行：cd E:/github/ai-financial-agent && .venv/Scripts/python.exe tests/test_stream.py
"""

from __future__ import annotations

import asyncio
import io
import json
import sys
import threading
import time
import types
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from fastapi.testclient import TestClient  # noqa: E402

import app.api.routes as routes_mod  # noqa: E402
import app.api.stream as stream_mod  # noqa: E402
import app.agent.orchestrator as orchestrator_mod  # noqa: E402
from app.agent import run_agent_streaming  # noqa: E402
from app.agent.orchestrator import _stream_agent_events  # noqa: E402
from app.api.routes import app  # noqa: E402
from app.api.schemas import SessionInfo  # noqa: E402

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
# fake 流式 OpenAI client（逐片产出 delta.content / delta.tool_calls）
# ---------------------------------------------------------------------------

class _FakeDelta:
    def __init__(self, content: Optional[str] = None, tool_calls: Optional[List[Any]] = None) -> None:
        self.content = content
        self.tool_calls = tool_calls


class _FakeStreamChunk:
    def __init__(self, delta: Any, has_choices: bool = True) -> None:
        self.choices = [types.SimpleNamespace(delta=delta)] if has_choices else []


def _tool_chunk(index: int, call_id: Optional[str] = None, name: Optional[str] = None, arguments: Optional[str] = None) -> Any:
    return types.SimpleNamespace(
        index=index,
        id=call_id,
        function=types.SimpleNamespace(name=name, arguments=arguments),
    )


def _chunk(content: Optional[str] = None, tool_calls: Optional[List[Any]] = None) -> Any:
    return _FakeStreamChunk(_FakeDelta(content=content, tool_calls=tool_calls))


def _non_stream_response(content: str) -> Any:
    """Phase 20A：Router 为非流式调用，返回带 content 的普通响应结构。"""
    return types.SimpleNamespace(
        choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=content, tool_calls=None))]
    )


class _FakeStreamClient:
    def __init__(self, responses: List[List[Any]], fail: bool = False) -> None:
        self._responses = list(responses)
        self.calls: List[Dict[str, Any]] = []
        self._fail = fail
        # 与真实 OpenAI SDK 相同的 client.chat.completions.create 访问路径
        self.chat = types.SimpleNamespace(completions=self)

    def create(self, **kwargs: Any) -> Any:
        if kwargs.get("response_format") == {"type": "json_object"}:
            # Phase 20A：Router 内部非流式调用，不记录、不进响应队列
            return _non_stream_response(json.dumps(_DEFAULT_ROUTE))
        self.calls.append(kwargs)
        if self._fail:
            raise RuntimeError("stream boom")
        assert kwargs.get("stream") is True, "流式路径必须 stream=True"
        return self._responses.pop(0)


_FAKE_TOOLS = {
    "get_stock_price": lambda symbol="": {"symbol": symbol, "price": 100.0},
    "get_technical_analysis": lambda symbol="": {"symbol": symbol, "rsi": 50.0},
}

# Phase 20A：Router 默认全维度关闭，避免吞掉 fake 响应队列、干扰既有断言
_DEFAULT_ROUTE = {"needs_fundamental": False, "needs_quant": False, "needs_event": False}


def _tool_round_client(answer: str = "流式回答") -> _FakeStreamClient:
    """第一轮并行两个工具，第二轮输出最终回答。"""
    rounds = [
        [
            _chunk(tool_calls=[_tool_chunk(0, call_id="c1", name="get_stock_price", arguments='{"symbol": "6005')]),
            _chunk(tool_calls=[_tool_chunk(0, arguments='19"}')]),
            _chunk(tool_calls=[_tool_chunk(1, call_id="c2", name="get_technical_analysis", arguments='{"symbol": "600519"}')]),
        ],
        [_chunk(content="第一段"), _chunk(content="第二段"), _chunk(content=answer)],
    ]
    return _FakeStreamClient(rounds)


def _direct_client(answer: str = "直接回答") -> _FakeStreamClient:
    return _FakeStreamClient([[_chunk(content=answer)]])


# ---------------------------------------------------------------------------
# 同步生成器 _stream_agent_events
# ---------------------------------------------------------------------------

def test_sync_events_order() -> None:
    client = _tool_round_client()
    with mock.patch.dict(orchestrator_mod.TOOL_DISPATCH, _FAKE_TOOLS, clear=True):
        events = list(_stream_agent_events(client, "分析 600519"))
    kinds = [event_type for event_type, _ in events]

    # tool_call 先于对应 tool_result；工具事件先于 token；__result__ 收尾
    assert kinds.index("tool_call") < kinds.index("tool_result")
    assert kinds.index("tool_result") < kinds.index("token")
    assert kinds[-1] == "__result__"

    calls = [p for e, p in events if e == "tool_call"]
    assert calls == [
        {"tool": "get_stock_price", "args": {"symbol": "600519"}},
        {"tool": "get_technical_analysis", "args": {"symbol": "600519"}},
    ]
    results = [p for e, p in events if e == "tool_result"]
    assert results == [
        {"tool": "get_stock_price", "status": "ok"},
        {"tool": "get_technical_analysis", "status": "ok"},
    ]
    tokens = [p["content"] for e, p in events if e == "token"]
    assert tokens == ["第一段", "第二段", "流式回答"]

    # __result__：完整 AgentResult 快照，tool_calls 原始结构 4 字段
    payload = events[-1][1]
    assert payload["answer"] == "第一段第二段流式回答"
    assert payload["tool_rounds"] == 1
    assert payload["max_rounds_reached"] is False
    assert payload["error"] is None
    assert len(payload["tool_calls"]) == 2
    assert all(set(c.keys()) == {"round", "name", "arguments", "result"} for c in payload["tool_calls"])
    assert payload["tool_calls"][0]["result"]["price"] == 100.0

    # stream=True 透传；第二轮 messages 携带 role=tool 工具结果
    assert client.calls[0]["stream"] is True
    tool_msgs = [m for m in client.calls[1]["messages"] if m["role"] == "tool"]
    assert len(tool_msgs) == 2
    assert json.loads(tool_msgs[0]["content"])["price"] == 100.0


def test_sync_api_error() -> None:
    client = _FakeStreamClient([], fail=True)
    events = list(_stream_agent_events(client, "q"))
    assert events[0][0] == "error"
    assert "DeepSeek API 调用失败" in events[0][1]["message"]
    assert events[1][0] == "__result__"
    assert events[1][1]["error"] == events[0][1]["message"]
    assert events[1][1]["answer"] == ""


def test_sync_generator_stops_when_stop_event_set() -> None:
    """stop 在生成器启动前已置位：首轮入口检查点即返回，不发起任何 LLM 调用。"""
    stop = threading.Event()
    stop.set()
    client = _direct_client("不应到达")
    with mock.patch.dict(orchestrator_mod.TOOL_DISPATCH, _FAKE_TOOLS, clear=True):
        events = list(_stream_agent_events(client, "q", stop_event=stop))
    assert events == []  # 不产出任何事件（含 __result__，避免持久化半成品）
    assert client.calls == []  # 零 LLM 调用 = 零 Token 消耗


def test_sync_generator_stops_mid_chunk_loop() -> None:
    """stop 在 chunk 迭代中途置位：下一个检查点立即终止，不产出 __result__。"""
    stop = threading.Event()

    class _SelfStopStream:
        def __iter__(self):
            yield _chunk(content="一")
            stop.set()  # 首片后模拟断连
            yield _chunk(content="二")
            yield _chunk(content="三")

    client = _FakeStreamClient([_SelfStopStream()])
    with mock.patch.dict(orchestrator_mod.TOOL_DISPATCH, _FAKE_TOOLS, clear=True):
        events = list(_stream_agent_events(client, "q", stop_event=stop))
    assert events == []  # 缓冲中的内容不落盘、不产出 __result__
    assert stop.is_set()


def test_sync_final_answer_degraded_on_violation() -> None:
    """最终回答命中违禁荐股：拦截原始结论，改发 degraded + __result__（降级答案落库）。"""
    client = _direct_client("建议买入该股，明天一定大涨")
    events = list(_stream_agent_events(client, "q"))
    kinds = [event_type for event_type, _ in events]
    assert kinds == ["degraded", "__result__"]
    degraded = events[0][1]
    assert "【回答受限：风险提示】" in degraded["message"]
    assert degraded["violations"], "应检出违禁荐股"
    assert any("建议" in v for v in degraded["violations"])
    # __result__ 持久化的是降级回答，而非被拦截的原始结论
    assert events[1][1]["answer"] == degraded["message"]
    assert events[1][1]["error"] is None


# ---------------------------------------------------------------------------
# 异步包装 run_agent_streaming
# ---------------------------------------------------------------------------

def test_async_wrapper_matches_sync() -> None:
    client = _tool_round_client()

    async def _collect() -> List[Any]:
        return [(t, p) async for t, p in run_agent_streaming(client, "分析 600519")]

    with mock.patch.dict(orchestrator_mod.TOOL_DISPATCH, _FAKE_TOOLS, clear=True):
        events = asyncio.run(_collect())
    kinds = [event_type for event_type, _ in events]
    # 与同步生成器一致：按工具逐个交错（tool_call -> 执行 -> tool_result -> 下一工具）
    assert kinds == ["tool_call", "tool_result", "tool_call", "tool_result", "token", "token", "token", "__result__"]
    assert events[-1][1]["answer"] == "第一段第二段流式回答"


def test_async_wrapper_api_error() -> None:
    client = _FakeStreamClient([], fail=True)

    async def _collect() -> List[Any]:
        return [(t, p) async for t, p in run_agent_streaming(client, "q")]

    events = asyncio.run(_collect())
    assert events[0][0] == "error"
    assert events[1][0] == "__result__"


def test_async_stream_cancellation_stops_producer() -> None:
    """客户端断连（消费端取消生成器）→ stop 置位 → producer 不再发起第 2 轮推理。

    断连发生在工具执行中途：fake 工具放慢（sleep），消费者收到 tool_call 后立即
    aclose() 注入 GeneratorExit，run_agent_streaming 的 finally 置位 stop；
    工具返回后 producer 在下一轮入口检查点终止——零额外 LLM 调用（防 Token 泄漏）。
    """

    class _SlowChunks:
        def __init__(self, chunks: List[Any], delay: float) -> None:
            self._chunks = chunks
            self._delay = delay

        def __iter__(self):
            for chunk in self._chunks:
                time.sleep(self._delay)
                yield chunk

    class _SlowClient:
        def __init__(self) -> None:
            self.chat = types.SimpleNamespace(completions=self)
            self.calls: List[Dict[str, Any]] = []

        def create(self, **kwargs: Any) -> Any:
            if kwargs.get("response_format") == {"type": "json_object"}:
                return _non_stream_response(json.dumps(_DEFAULT_ROUTE))
            self.calls.append(kwargs)
            assert kwargs.get("stream") is True
            return _SlowChunks(
                [_chunk(tool_calls=[_tool_chunk(0, call_id="c1", name="get_stock_price", arguments='{"symbol": "600519"}')])],
                0.05,
            )

    client = _SlowClient()
    stop = threading.Event()
    slow_tools = {
        # 工具执行放慢 0.3s：给消费者置位 stop 留出确定窗口
        "get_stock_price": lambda symbol="": (
            time.sleep(0.3), {"symbol": symbol, "price": 100.0}
        )[1],
    }

    async def _consume() -> List[Any]:
        events: List[Any] = []
        stream = run_agent_streaming(client, "分析 600519", stop_event=stop)
        try:
            async for event_type, payload in stream:
                events.append((event_type, payload))
                if len(events) == 1:
                    await stream.aclose()  # 模拟客户端断连：注入 GeneratorExit
                    break
        except asyncio.CancelledError:
            pass
        return events

    with mock.patch.dict(orchestrator_mod.TOOL_DISPATCH, slow_tools, clear=True):
        events = asyncio.run(_consume())

    assert [event_type for event_type, _ in events] == ["tool_call"]
    assert stop.is_set()  # run_agent_streaming 的 finally 已置位停止标记
    assert len(client.calls) == 1  # 仅第 1 轮推理；断连后不再发起第 2 轮 LLM 调用


# ---------------------------------------------------------------------------
# POST /chat/stream（SSE 解析）
# ---------------------------------------------------------------------------

def _parse_sse(lines: str) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    current: Dict[str, Any] = {}
    for line in lines.splitlines():
        if not line.strip():
            if current:
                events.append(current)
                current = {}
            continue
        if line.startswith("event:"):
            current["event"] = line[len("event:"):].strip()
        elif line.startswith("data:"):
            current["data"] = json.loads(line[len("data:"):].strip())
    if current:
        events.append(current)
    return events


def test_chat_stream_full_flow() -> None:
    client = _tool_round_client("最终流式回答")
    captured: Dict[str, Any] = {}

    def _fake_save(**kwargs: Any) -> int:
        captured.update(kwargs)
        return 42

    with mock.patch.dict(orchestrator_mod.TOOL_DISPATCH, _FAKE_TOOLS, clear=True):
        with mock.patch.object(stream_mod, "_get_client", return_value=client), mock.patch.object(
            stream_mod, "save_run", side_effect=_fake_save
        ), mock.patch.object(
            stream_mod, "create_session", return_value=101
        ), mock.patch.object(stream_mod, "get_session", return_value=None), TestClient(app) as tc:
            with tc.stream("POST", "/chat/stream", json={"message": "分析 600519"}) as resp:
                assert resp.status_code == 200
                assert resp.headers["content-type"].startswith("text/event-stream")
                events = _parse_sse("\n".join(resp.iter_lines()))

    assert [e["event"] for e in events] == ["tool_call", "tool_result", "tool_call", "tool_result", "token", "token", "token", "done"]
    assert events[-1]["data"] == {"session_id": 101, "run_id": 42}

    # save_run 收到 __result__ 完整快照（含 session_id）
    assert captured["question"] == "分析 600519"
    assert captured["answer"] == "第一段第二段最终流式回答"
    assert captured["tool_rounds"] == 1
    assert captured["max_rounds_reached"] is False
    assert captured["error"] is None
    assert captured["session_id"] == 101
    assert len(captured["tool_calls"]) == 2
    assert all(set(c.keys()) == {"round", "name", "arguments", "result"} for c in captured["tool_calls"])


def test_chat_stream_creates_session_with_title() -> None:
    client = _direct_client("新会话回答")
    titles: List[Optional[str]] = []
    created: List[int] = []

    def _fake_create(title: Optional[str] = None) -> int:
        titles.append(title)
        created.append(1)
        return 202

    def _fake_save(**kwargs: Any) -> int:
        return 7

    with mock.patch.object(stream_mod, "_get_client", return_value=client), mock.patch.object(
        stream_mod, "save_run", side_effect=_fake_save
    ), mock.patch.object(
        stream_mod, "create_session", side_effect=_fake_create
    ), mock.patch.object(stream_mod, "get_session", return_value=None), TestClient(app) as tc:
        with tc.stream("POST", "/chat/stream", json={"message": "帮我分析一下贵州茅台这家公司的情况"}) as resp:
            events = _parse_sse("\n".join(resp.iter_lines()))
    assert len(created) == 1  # 仅新建一次
    # 消息仅 17 字，不足 30 字时取全文
    assert titles == ["帮我分析一下贵州茅台这家公司的情况"]
    assert events[-1]["data"]["session_id"] == 202


def test_chat_stream_reuses_session() -> None:
    client = _direct_client("复用回答")
    captured: Dict[str, Any] = {}

    def _fake_save(**kwargs: Any) -> int:
        captured.update(kwargs)
        return 5

    with mock.patch.object(stream_mod, "_get_client", return_value=client), mock.patch.object(
        stream_mod, "get_session", return_value={"id": 7, "title": None, "created_at": "", "updated_at": ""}
    ), mock.patch.object(
        stream_mod, "create_session", side_effect=AssertionError("有 session_id 且存在时不应新建会话")
    ), mock.patch.object(
        stream_mod, "save_run", side_effect=_fake_save
    ), TestClient(app) as tc:
        with tc.stream("POST", "/chat/stream", json={"message": "q", "session_id": 7}) as resp:
            events = _parse_sse("\n".join(resp.iter_lines()))
    assert [e["event"] for e in events] == ["token", "done"]
    assert events[-1]["data"] == {"session_id": 7, "run_id": 5}
    assert captured["session_id"] == 7  # run 关联到既有会话


def test_chat_stream_client_error() -> None:
    def _boom() -> None:
        raise RuntimeError("未配置 DEEPSEEK_API_KEY，请在 .env 中配置后重试。")

    with mock.patch.object(stream_mod, "_get_client", side_effect=_boom), TestClient(app) as tc:
        with tc.stream("POST", "/chat/stream", json={"message": "q"}) as resp:
            assert resp.status_code == 200  # 错误走 error 事件，不抛 503
            events = _parse_sse("\n".join(resp.iter_lines()))
    assert [e["event"] for e in events] == ["error", "done"]
    assert "DEEPSEEK_API_KEY" in events[0]["data"]["message"]
    assert events[-1]["data"] == {"session_id": None, "run_id": None}


def test_chat_stream_save_failure_degrades() -> None:
    client = _direct_client("降级回答")
    with mock.patch.object(stream_mod, "_get_client", return_value=client), mock.patch.object(
        stream_mod, "save_run", side_effect=RuntimeError("db down")
    ), mock.patch.object(
        stream_mod, "create_session", return_value=101
    ), mock.patch.object(stream_mod, "get_session", return_value=None), TestClient(app) as tc:
        with tc.stream("POST", "/chat/stream", json={"message": "q"}) as resp:
            events = _parse_sse("\n".join(resp.iter_lines()))
    assert [e["event"] for e in events] == ["token", "done"]
    assert events[0]["data"]["content"] == "降级回答"  # 回答流不受落库失败影响
    assert events[-1]["data"]["session_id"] == 101
    assert events[-1]["data"]["run_id"] is None


def test_chat_stream_degraded_on_violation() -> None:
    """SSE 层面：最终回答命中高危违禁 -> degraded 事件 + 降级回答落库（拦截原始结论）。"""
    client = _direct_client("现在可以全仓买入")
    captured: Dict[str, Any] = {}

    def _fake_save(**kwargs: Any) -> int:
        captured.update(kwargs)
        return 42

    with mock.patch.object(stream_mod, "_get_client", return_value=client), mock.patch.object(
        stream_mod, "save_run", side_effect=_fake_save
    ), mock.patch.object(
        stream_mod, "create_session", return_value=101
    ), mock.patch.object(stream_mod, "get_session", return_value=None), TestClient(app) as tc:
        with tc.stream("POST", "/chat/stream", json={"message": "q"}) as resp:
            events = _parse_sse("\n".join(resp.iter_lines()))
    assert [e["event"] for e in events] == ["degraded", "done"]
    degraded = events[0]["data"]
    assert "【回答受限：风险提示】" in degraded["message"]
    assert degraded["violations"], "应检出违禁荐股"
    # 落库的是降级回答，而非被拦截的原始结论
    assert captured["answer"] == degraded["message"]
    assert events[-1]["data"] == {"session_id": 101, "run_id": 42}


def test_chat_stream_empty_message_422() -> None:
    with TestClient(app) as tc:
        resp = tc.post("/chat/stream", json={"message": ""})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 零终端副作用
# ---------------------------------------------------------------------------

def test_chat_stream_no_stdout() -> None:
    client = _direct_client("静默回答")
    buffer = io.StringIO()
    with mock.patch.object(stream_mod, "_get_client", return_value=client), mock.patch.object(
        stream_mod, "create_session", return_value=101
    ), mock.patch.object(stream_mod, "get_session", return_value=None), TestClient(app) as tc, redirect_stdout(buffer):
        with tc.stream("POST", "/chat/stream", json={"message": "测试问题"}) as resp:
            list(resp.iter_lines())
    assert buffer.getvalue() == "", f"Service 层不应产生 stdout：{buffer.getvalue()!r}"


# ---------------------------------------------------------------------------
# GET /sessions
# ---------------------------------------------------------------------------

def test_sessions_list() -> None:
    fake = [
        {
            "id": 2,
            "title": "最新会话",
            "created_at": "2026-08-23T01:00:00Z",
            "updated_at": "2026-08-23T02:00:00Z",
        },
        {
            "id": 1,
            "title": None,
            "created_at": "2026-08-22T00:00:00Z",
            "updated_at": "2026-08-22T00:00:00Z",
        },
    ]
    with mock.patch.object(routes_mod, "list_sessions", return_value=fake), TestClient(app) as tc:
        resp = tc.get("/sessions")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 2
    assert body[0]["id"] == 2
    assert body[0]["title"] == "最新会话"
    assert body[1]["id"] == 1
    assert body[1]["title"] is None
    SessionInfo(**body[0])  # schema 校验通过


def test_sessions_empty() -> None:
    with mock.patch.object(routes_mod, "list_sessions", return_value=[]), TestClient(app) as tc:
        resp = tc.get("/sessions")
    assert resp.status_code == 200
    assert resp.json() == []


def main() -> None:
    print("=== tests/test_stream.py Phase 18 SSE 流式接口 P0 测试 ===")
    tests = [
        ("1. 同步生成器事件顺序与载荷", test_sync_events_order),
        ("2. 同步生成器 API 失败 -> error + __result__", test_sync_api_error),
        ("3. stop 前置 -> 零事件零 LLM 调用", test_sync_generator_stops_when_stop_event_set),
        ("4. stop 中断 chunk 循环 -> 不产出 __result__", test_sync_generator_stops_mid_chunk_loop),
        ("5. 最终回答违规 -> degraded + 降级答案落库", test_sync_final_answer_degraded_on_violation),
        ("6. 异步包装与同步事件序列一致", test_async_wrapper_matches_sync),
        ("7. 异步包装 API 失败", test_async_wrapper_api_error),
        ("8. 消费端断连 -> stop 置位且不再发起第 2 轮推理", test_async_stream_cancellation_stops_producer),
        ("9. /chat/stream 完整流程（tool/token/done + 落库快照）", test_chat_stream_full_flow),
        ("10. 无 session_id -> 新建会话（标题取消息前 30 字）", test_chat_stream_creates_session_with_title),
        ("11. 有 session_id -> 复用不新建", test_chat_stream_reuses_session),
        ("12. client 创建失败 -> error + done（HTTP 200）", test_chat_stream_client_error),
        ("13. save_run 失败 -> run_id null 降级", test_chat_stream_save_failure_degrades),
        ("14. /chat/stream 违规 -> degraded 事件 + 降级回答落库", test_chat_stream_degraded_on_violation),
        ("15. message 空串 422", test_chat_stream_empty_message_422),
        ("16. /chat/stream 零 stdout 副作用", test_chat_stream_no_stdout),
        ("17. GET /sessions 列表", test_sessions_list),
        ("18. GET /sessions 空列表", test_sessions_empty),
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
