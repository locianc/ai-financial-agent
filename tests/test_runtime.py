"""Phase 21：Production Runtime Hardening 运行时保护层测试。

覆盖（对应规格第 16 节）：
- 默认 limits：8 轮 / 20 次 / 120 秒；
- check_limits 三种终止原因：tool_round_limit / tool_call_limit / request_timeout，
  以及对应标志位 limit_exceeded / timed_out；
- elapsed_seconds / snapshot 快照字段完整性；
- run_agent 接入：LLM 调用计数（Router + Final LLM 均计入 llm_calls）、
  超时停止工具与生成、轮数超限不请求 Final LLM、次数超限拦截后续 Tool、
  工具异常不崩溃 Runtime（记录 runtime.error）；
- streaming 接入：_stream_agent_events 与 run_agent_streaming 均接入运行时保护层、
  客户端停止置位 cancelled、超限产出既有 error 语义、SSE event type 集合不变、
  Validator 仍执行、Evidence 只构建一次。

全部使用 mock/fake（打桩 TOOL_DISPATCH / 注入 RuntimeState spy / fake OpenAI client），
零联网、不触库。

运行：cd E:/github/ai-financial-agent && .venv/Scripts/python.exe -m pytest tests/test_runtime.py -q
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import types
from pathlib import Path
from time import monotonic
from typing import Any, Dict, List, Optional
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import app.agent.orchestrator as orchestrator_mod  # noqa: E402
from app.agent import AgentSettings, run_agent, run_agent_streaming  # noqa: E402
from app.agent.orchestrator import _stream_agent_events  # noqa: E402
from app.agent.runtime import (  # noqa: E402
    DEFAULT_MAX_TOOL_CALLS,
    DEFAULT_MAX_TOOL_ROUNDS,
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
    RuntimeLimits,
    RuntimeState,
)

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
# fake OpenAI client（非流式，供 run_agent）
# ---------------------------------------------------------------------------

class _FakeMessage:
    def __init__(self, content: Optional[str] = None, tool_calls: Optional[List[Any]] = None) -> None:
        self.content = content
        self.tool_calls = tool_calls


class _FakeCompletions:
    def __init__(self, responses: List[Any]) -> None:
        self._queue = list(responses)
        self.calls: List[Dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        if kwargs.get("response_format") == {"type": "json_object"}:
            # Phase 20A：Router 内部非流式调用，不记录、不进响应队列
            return _resp(_msg(content=json.dumps(_DEFAULT_ROUTE)))
        self.calls.append(kwargs)
        return self._queue.pop(0)


class _FakeClient:
    def __init__(self, responses: List[Any]) -> None:
        self.chat = types.SimpleNamespace(completions=_FakeCompletions(responses))


def _tool_call(call_id: str, name: str, arguments: str) -> Any:
    return types.SimpleNamespace(
        id=call_id, function=types.SimpleNamespace(name=name, arguments=arguments)
    )


def _msg(content: Optional[str] = None, tool_calls: Optional[List[Any]] = None) -> _FakeMessage:
    return _FakeMessage(content=content, tool_calls=tool_calls)


def _resp(message: _FakeMessage) -> Any:
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])


def _final_client(answer: str) -> _FakeClient:
    return _FakeClient([_resp(_msg(content=answer))])


# ---------------------------------------------------------------------------
# fake 流式 OpenAI client（供 _stream_agent_events / run_agent_streaming）
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
    def __init__(self, responses: List[List[Any]]) -> None:
        self._responses = list(responses)
        self.calls: List[Dict[str, Any]] = []
        self.chat = types.SimpleNamespace(completions=self)

    def create(self, **kwargs: Any) -> Any:
        if kwargs.get("response_format") == {"type": "json_object"}:
            return _non_stream_response(json.dumps(_DEFAULT_ROUTE))
        self.calls.append(kwargs)
        assert kwargs.get("stream") is True, "流式路径必须 stream=True"
        return self._responses.pop(0)


# 打桩工具表：保证测试不触网、可确定断言
_FAKE_TOOLS = {
    "get_stock_price": lambda symbol="": {"symbol": symbol, "price": 100.0},
    "get_technical_analysis": lambda symbol="": {"symbol": symbol, "rsi": 50.0},
}

# Phase 20A：Router 默认全维度关闭，避免吞掉 fake 响应队列、干扰既有断言
_DEFAULT_ROUTE = {"needs_fundamental": False, "needs_quant": False, "needs_event": False}


def _tool_then_final_client() -> _FakeClient:
    """第一次响应请求两个并行工具，第二次返回最终回答。"""
    calls = [
        _tool_call("call_price", "get_stock_price", '{"symbol": "600519"}'),
        _tool_call("call_tech", "get_technical_analysis", '{"symbol": "600519"}'),
    ]
    return _FakeClient([
        _resp(_msg(content=None, tool_calls=calls)),
        _resp(_msg(content="最终回答")),
    ])


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
# 运行时 spy 辅助
# ---------------------------------------------------------------------------

def _patched_check_limits(spy: RuntimeState, trigger_call: int) -> Any:
    """返回替代 check_limits：在第 N 次调用时强制触发超时，其余委托真实逻辑。

    实例属性函数签名 def patched(limits)，run_agent / _stream_agent_events 以
    runtime.check_limits(limits) 调用时命中实例属性（优先于类方法）。
    """

    counter = {"n": 0}

    def patched(limits: RuntimeLimits) -> Optional[str]:
        counter["n"] += 1
        if counter["n"] == trigger_call:
            spy.timed_out = True
            return "request_timeout"
        return RuntimeState.check_limits(spy, limits)

    return patched


# ---------------------------------------------------------------------------
# RuntimeState / RuntimeLimits 纯单元
# ---------------------------------------------------------------------------

def test_default_limits() -> None:
    limits = RuntimeLimits()
    assert limits.max_tool_rounds == DEFAULT_MAX_TOOL_ROUNDS == 8
    assert limits.max_tool_calls == DEFAULT_MAX_TOOL_CALLS == 20
    assert limits.request_timeout_seconds == DEFAULT_REQUEST_TIMEOUT_SECONDS == 120.0


def test_round_limit_reason() -> None:
    state = RuntimeState()
    state.tool_rounds = 2
    reason = state.check_limits(RuntimeLimits(max_tool_rounds=2))
    assert reason == "tool_round_limit"
    assert state.limit_exceeded is True
    assert state.timed_out is False


def test_call_limit_reason() -> None:
    state = RuntimeState()
    state.tool_calls = 5
    reason = state.check_limits(RuntimeLimits(max_tool_calls=5))
    assert reason == "tool_call_limit"
    assert state.limit_exceeded is True
    assert state.timed_out is False


def test_timeout_reason() -> None:
    state = RuntimeState()
    state.started_at = monotonic() - 200.0  # 回拨模拟已超时
    reason = state.check_limits(RuntimeLimits(request_timeout_seconds=120.0))
    assert reason == "request_timeout"
    assert state.timed_out is True
    assert state.limit_exceeded is False


def test_elapsed_seconds() -> None:
    state = RuntimeState()
    assert state.elapsed_seconds >= 0.0


def test_snapshot_fields() -> None:
    state = RuntimeState()
    state.tool_rounds = 1
    state.tool_calls = 2
    state.llm_calls = 3
    state.cancelled = True
    state.error = "boom"
    snap = state.snapshot()
    assert set(snap) == {
        "elapsed_seconds", "tool_rounds", "tool_calls", "llm_calls",
        "cancelled", "timed_out", "limit_exceeded", "error",
    }
    assert snap["tool_rounds"] == 1
    assert snap["tool_calls"] == 2
    assert snap["llm_calls"] == 3
    assert snap["cancelled"] is True
    assert snap["timed_out"] is False
    assert snap["limit_exceeded"] is False
    assert snap["error"] == "boom"


def test_no_limits_no_reason() -> None:
    state = RuntimeState()
    assert state.check_limits(RuntimeLimits()) is None
    assert state.limit_exceeded is False
    assert state.timed_out is False


# ---------------------------------------------------------------------------
# run_agent 接入
# ---------------------------------------------------------------------------

def test_run_agent_counts_llm_calls() -> None:
    """Router + 每轮 Final LLM 均计入 llm_calls；工具轮/次计数正确。"""
    with mock.patch.dict(orchestrator_mod.TOOL_DISPATCH, _FAKE_TOOLS, clear=True):
        client = _tool_then_final_client()
        spy = RuntimeState()
        with mock.patch.object(orchestrator_mod, "RuntimeState", return_value=spy):
            result = run_agent(client, "分析 600519")
    assert result.answer == "最终回答"
    assert spy.llm_calls == 3  # Router + 第 1 轮 Final + 第 2 轮 Final
    assert spy.tool_rounds == 2
    assert spy.tool_calls == 2
    assert spy.timed_out is False
    assert spy.limit_exceeded is False


def test_run_agent_timeout_sets_error() -> None:
    """请求超时：首个检查点即终止，零 LLM 调用、零工具调用。"""
    spy = RuntimeState()
    spy.started_at = monotonic() - 200.0  # 回拨模拟超时
    client = _final_client("不应到达")
    with mock.patch.object(orchestrator_mod, "RuntimeState", return_value=spy):
        result = run_agent(client, "q")
    assert result.error is not None
    assert "超时" in result.error
    assert spy.timed_out is True
    assert result.answer == ""
    assert result.tool_calls == []
    assert client.chat.completions.calls == []  # 超时后不发起任何 LLM 请求


def test_run_agent_timeout_mid_tools() -> None:
    """超时发生在工具执行中途：第 1 个工具已执行，第 2 个被拦截，不再请求 Final LLM。"""
    with mock.patch.dict(orchestrator_mod.TOOL_DISPATCH, _FAKE_TOOLS, clear=True):
        client = _FakeClient([
            _resp(_msg(tool_calls=[
                _tool_call("c1", "get_stock_price", '{"symbol": "600519"}'),
                _tool_call("c2", "get_technical_analysis", '{"symbol": "600519"}'),
            ])),
            _resp(_msg(content="最终回答")),
        ])
        spy = RuntimeState()
        spy.check_limits = _patched_check_limits(spy, trigger_call=5)
        with mock.patch.object(orchestrator_mod, "RuntimeState", return_value=spy):
            result = run_agent(client, "q")
    assert "超时" in result.error
    assert spy.timed_out is True
    assert len(result.tool_calls) == 1  # 第 1 个工具已执行，第 2 个被拦截
    assert len(client.chat.completions.calls) == 1  # 未请求 Final LLM


def test_run_agent_round_limit_blocks_final_llm() -> None:
    """轮数超限：工具正常执行，但下一轮 LLM 请求前被拦截（不调用 Final LLM）。"""
    with mock.patch.dict(orchestrator_mod.TOOL_DISPATCH, _FAKE_TOOLS, clear=True):
        client = _FakeClient([
            _resp(_msg(tool_calls=[
                _tool_call("c1", "get_stock_price", '{"symbol": "600519"}'),
                _tool_call("c2", "get_technical_analysis", '{"symbol": "600519"}'),
            ])),
            _resp(_msg(content="最终回答")),
        ])
        limits = RuntimeLimits(max_tool_rounds=2)
        with mock.patch.object(orchestrator_mod, "RuntimeLimits", return_value=limits):
            result = run_agent(client, "q")
    assert "轮数超过上限" in result.error
    assert result.max_rounds_reached is False
    assert len(result.tool_calls) == 2  # 第 1 轮两个工具已执行
    assert len(client.chat.completions.calls) == 1  # 仅第 1 轮 LLM，Final LLM 被拦截


def test_run_agent_call_limit_stops_tools() -> None:
    """次数超限：第 1 个工具执行，第 2 个工具被拦截，不再请求 Final LLM。"""
    with mock.patch.dict(orchestrator_mod.TOOL_DISPATCH, _FAKE_TOOLS, clear=True):
        client = _FakeClient([
            _resp(_msg(tool_calls=[
                _tool_call("c1", "get_stock_price", '{"symbol": "600519"}'),
                _tool_call("c2", "get_technical_analysis", '{"symbol": "600519"}'),
            ])),
            _resp(_msg(content="最终回答")),
        ])
        limits = RuntimeLimits(max_tool_calls=1)
        with mock.patch.object(orchestrator_mod, "RuntimeLimits", return_value=limits):
            result = run_agent(client, "q")
    assert "次数超过上限" in result.error
    assert result.max_rounds_reached is False
    assert len(result.tool_calls) == 1  # 第 1 个工具执行，第 2 个被拦截
    assert len(client.chat.completions.calls) == 1  # 未请求 Final LLM


def test_tool_exception_isolated_in_runtime() -> None:
    """工具抛异常：不崩溃 Runtime，工具结果走 error 机制，runtime.error 记录异常。"""
    def _boom(**kwargs: Any) -> Dict[str, Any]:
        raise ValueError("boom")

    with mock.patch.dict(orchestrator_mod.TOOL_DISPATCH, {"get_stock_price": _boom}, clear=True):
        client = _FakeClient([_resp(_msg(tool_calls=[_tool_call("c1", "get_stock_price", '{"symbol": "600519"}')]))])
        spy = RuntimeState()
        with mock.patch.object(orchestrator_mod, "RuntimeState", return_value=spy):
            result = run_agent(client, "q")
    assert "工具执行异常" in result.tool_calls[0].result["error"]
    assert "ValueError" in result.tool_calls[0].result["error"]
    assert "ValueError" in spy.error  # 异常被记录到 runtime.error，不中断流程


def test_evidence_built_once_in_run_agent() -> None:
    """Evidence 只构建一次：多轮工具调用下 build_evidence_context 仅调用 1 次。"""
    with mock.patch.dict(orchestrator_mod.TOOL_DISPATCH, _FAKE_TOOLS, clear=True):
        client = _FakeClient([
            _resp(_msg(tool_calls=[_tool_call("c1", "get_stock_price", '{"symbol": "600519"}')])),
            _resp(_msg(tool_calls=[_tool_call("c2", "get_technical_analysis", '{"symbol": "600519"}')])),
            _resp(_msg(content="最终回答")),
        ])
        with mock.patch.object(
            orchestrator_mod, "build_evidence_context", wraps=orchestrator_mod.build_evidence_context
        ) as wrapped:
            result = run_agent(client, "q")
    assert result.answer == "最终回答"
    assert result.tool_rounds == 2
    assert wrapped.call_count == 1  # 结构化证据只在首次 Tool Calling 完成后构建一次


# ---------------------------------------------------------------------------
# streaming 接入
# ---------------------------------------------------------------------------

def test_streaming_runtime_integration() -> None:
    """_stream_agent_events 接入运行时保护层：计数与事件顺序均正确。"""
    client = _tool_round_client()
    spy = RuntimeState()
    with mock.patch.dict(orchestrator_mod.TOOL_DISPATCH, _FAKE_TOOLS, clear=True), mock.patch.object(
        orchestrator_mod, "RuntimeState", return_value=spy
    ):
        events = list(_stream_agent_events(client, "分析 600519"))
    kinds = [event_type for event_type, _ in events]
    assert kinds.index("tool_call") < kinds.index("token")
    assert kinds[-1] == "__result__"
    assert spy.llm_calls == 3  # Router + 2 轮 Final LLM
    assert spy.tool_rounds == 2
    assert spy.tool_calls == 2
    assert spy.timed_out is False
    assert spy.limit_exceeded is False
    assert events[-1][1]["error"] is None


def test_streaming_round_limit_yields_error() -> None:
    """流式超限：走现有 error + __result__ 语义，不新增 event type。"""
    client = _FakeStreamClient([[
        _chunk(tool_calls=[_tool_chunk(0, call_id="c1", name="get_stock_price", arguments='{"symbol": "600519"}')]),
    ]])
    limits = RuntimeLimits(max_tool_rounds=1)
    with mock.patch.object(orchestrator_mod, "RuntimeLimits", return_value=limits):
        events = list(_stream_agent_events(client, "q"))
    kinds = [event_type for event_type, _ in events]
    assert kinds == ["error", "__result__"]
    assert "轮数超过上限" in events[0][1]["message"]
    assert events[1][1]["error"] == events[0][1]["message"]
    assert client.calls == []  # 超限后不发起任何 LLM 请求


def test_stream_sets_cancelled_on_stop() -> None:
    """stop 前置置位：零事件零调用，runtime.cancelled 被置位。"""
    stop = threading.Event()
    stop.set()
    client = _direct_client("不应到达")
    spy = RuntimeState()
    with mock.patch.object(orchestrator_mod, "RuntimeState", return_value=spy):
        events = list(_stream_agent_events(client, "q", stop_event=stop))
    assert events == []
    assert client.calls == []
    assert spy.cancelled is True


def test_stream_stop_mid_chunk_sets_cancelled() -> None:
    """客户端断连（chunk 循环中途置位 stop）：立即终止，cancelled 被置位。"""
    stop = threading.Event()

    class _SelfStopStream:
        def __iter__(self):
            yield _chunk(content="一")
            stop.set()  # 首片后模拟断连
            yield _chunk(content="二")

    client = _FakeStreamClient([_SelfStopStream()])
    spy = RuntimeState()
    with mock.patch.object(orchestrator_mod, "RuntimeState", return_value=spy):
        events = list(_stream_agent_events(client, "q", stop_event=stop))
    assert events == []
    assert stop.is_set()
    assert spy.cancelled is True


def test_async_streaming_wrapper_integrates_runtime() -> None:
    """run_agent_streaming 异步包装：运行时保护层在 worker 线程同样生效。"""
    client = _tool_round_client()
    spy = RuntimeState()

    async def _collect() -> List[Any]:
        return [(t, p) async for t, p in run_agent_streaming(client, "分析 600519")]

    with mock.patch.dict(orchestrator_mod.TOOL_DISPATCH, _FAKE_TOOLS, clear=True), mock.patch.object(
        orchestrator_mod, "RuntimeState", return_value=spy
    ):
        events = asyncio.run(_collect())
    kinds = [event_type for event_type, _ in events]
    assert kinds == ["tool_call", "tool_result", "tool_call", "tool_result", "token", "token", "token", "__result__"]
    assert spy.llm_calls == 3
    assert spy.tool_rounds == 2
    assert spy.tool_calls == 2


# ---------------------------------------------------------------------------
# 契约不变性
# ---------------------------------------------------------------------------

def test_sse_event_types_unchanged() -> None:
    """SSE event type 集合保持不变：无 runtime_start/end、timeout、limit、cancelled 事件。"""
    allowed = {"tool_call", "tool_result", "token", "degraded", "error", "__result__"}
    with mock.patch.dict(orchestrator_mod.TOOL_DISPATCH, _FAKE_TOOLS, clear=True):
        client = _tool_round_client()
        events = list(_stream_agent_events(client, "分析 600519"))
    kinds = {event_type for event_type, _ in events}
    assert kinds <= allowed
    assert not (kinds & {"runtime_start", "runtime_end", "timeout", "limit", "cancelled"})


def test_validator_still_enforced_in_streaming() -> None:
    """最终回答仍经 Validator：命中高危违禁 → degraded + __result__（拦截原始结论）。"""
    client = _direct_client("建议买入该股，明天一定大涨")
    events = list(_stream_agent_events(client, "q"))
    kinds = [event_type for event_type, _ in events]
    assert kinds == ["degraded", "__result__"]
    assert "【回答受限：风险提示】" in events[0][1]["message"]
    assert events[0][1]["violations"]
    # 落库的是降级回答，而非被拦截的原始结论
    assert events[1][1]["answer"] == events[0][1]["message"]


def main() -> None:
    print("=== tests/test_runtime.py Phase 21 运行时保护层测试 ===")
    tests = [
        ("1. 默认 limits（8/20/120）", test_default_limits),
        ("2. 轮数超限 reason + limit_exceeded", test_round_limit_reason),
        ("3. 次数超限 reason + limit_exceeded", test_call_limit_reason),
        ("4. 超时 reason + timed_out", test_timeout_reason),
        ("5. elapsed_seconds 非负", test_elapsed_seconds),
        ("6. snapshot 字段完整性", test_snapshot_fields),
        ("7. 无超限无 reason", test_no_limits_no_reason),
        ("8. run_agent 计数（Router+Final 计入 llm_calls）", test_run_agent_counts_llm_calls),
        ("9. run_agent 超时置 error 且零调用", test_run_agent_timeout_sets_error),
        ("10. run_agent 工具中途超时拦截", test_run_agent_timeout_mid_tools),
        ("11. 轮数超限不调用 Final LLM", test_run_agent_round_limit_blocks_final_llm),
        ("12. 次数超限拦截后续工具", test_run_agent_call_limit_stops_tools),
        ("13. 工具异常不崩溃 Runtime", test_tool_exception_isolated_in_runtime),
        ("14. Evidence 只构建一次", test_evidence_built_once_in_run_agent),
        ("15. 流式接入运行时保护层", test_streaming_runtime_integration),
        ("16. 流式超限 error + __result__", test_streaming_round_limit_yields_error),
        ("17. stop 前置置位 cancelled", test_stream_sets_cancelled_on_stop),
        ("18. 断连中断 chunk 置位 cancelled", test_stream_stop_mid_chunk_sets_cancelled),
        ("19. 异步包装接入运行时", test_async_streaming_wrapper_integrates_runtime),
        ("20. SSE event type 集合不变", test_sse_event_types_unchanged),
        ("21. Validator 仍执行（流式）", test_validator_still_enforced_in_streaming),
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
