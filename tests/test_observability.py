"""Phase 23：Observability & Evaluation Infrastructure 观测性测试。

覆盖（对应规格第 11 节）：
- LLMUsage：默认值 / dict / object / 缺失（None、空 dict、非法值）/ 累加；
- ToolTrace：字段与 snapshot 结构；
- RunTrace：默认值与 add_usage；
- RuntimeState：snapshot 保持 8 字段（不破坏既有契约），snapshot_with_trace
  组合 trace 且无无限递归；
- run_agent / _stream_agent_events 接入：Router usage、Final LLM usage
  （非流式 + 流式 chunk usage）、usage 缺失保持 0、工具耗时埋点、
  工具失败 trace、success/degraded/error/timeout/cancelled 状态；
- 隐私：trace 中不记录工具参数 / API Key / 结果内容；
- 契约不变性：SSE event type 集合不变、Validator 仍执行、Evidence 只构建一次、
  Router 分类仍用于工具 Schema 裁剪。

全部使用 fake/mock（打桩 TOOL_DISPATCH / 注入 RuntimeState spy / fake OpenAI client），
零联网、不触库。

运行：cd E:/github/ai-financial-agent && .venv/Scripts/python.exe -m pytest tests/test_observability.py -q
"""

from __future__ import annotations

import json
import sys
import threading
import types
from pathlib import Path
from time import monotonic
from typing import Any, Dict, List, Optional
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import app.agent.orchestrator as orchestrator_mod  # noqa: E402
from app.agent import run_agent  # noqa: E402
from app.agent.observability import LLMUsage, RunTrace, ToolTrace  # noqa: E402
from app.agent.orchestrator import _select_tool_schemas, _stream_agent_events  # noqa: E402
from app.agent.runtime import RuntimeState  # noqa: E402

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
# fake usage / OpenAI client（非流式，供 run_agent）
# ---------------------------------------------------------------------------

class _Usage:
    def __init__(self, prompt_tokens: int = 0, completion_tokens: int = 0, total_tokens: int = 0) -> None:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = total_tokens


class _Msg:
    def __init__(self, content: Optional[str] = None, tool_calls: Optional[List[Any]] = None) -> None:
        self.content = content
        self.tool_calls = tool_calls


class _Resp:
    def __init__(self, message: _Msg, usage: Any = None) -> None:
        self.choices = [types.SimpleNamespace(message=message)]
        self.usage = usage


def _tool_call(call_id: str, name: str, arguments: str) -> Any:
    return types.SimpleNamespace(
        id=call_id, function=types.SimpleNamespace(name=name, arguments=arguments)
    )


class _Completions:
    def __init__(self, responses: List[Any], router_usage: Any = None) -> None:
        self._queue = list(responses)
        self.router_usage = router_usage
        self.calls: List[Dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        if kwargs.get("response_format") == {"type": "json_object"}:
            # Phase 20A：Router 内部非流式调用，携带可选的 usage
            return _Resp(
                _Msg(content=json.dumps(_DEFAULT_ROUTE)), self.router_usage
            )
        self.calls.append(kwargs)
        return self._queue.pop(0)


class _FakeClient:
    def __init__(self, responses: List[Any], router_usage: Any = None) -> None:
        self.chat = types.SimpleNamespace(completions=_Completions(responses, router_usage))


class _RaisingClient:
    """非 Router 的 Final LLM 调用抛网络异常，用于 error 状态测试。"""

    def __init__(self) -> None:
        self.chat = types.SimpleNamespace(completions=self)

    def create(self, **kwargs: Any) -> Any:
        if kwargs.get("response_format") == {"type": "json_object"}:
            return _Resp(_Msg(content=json.dumps(_DEFAULT_ROUTE)))
        raise ConnectionError("网络故障")


# ---------------------------------------------------------------------------
# fake 流式 OpenAI client（供 _stream_agent_events）
# ---------------------------------------------------------------------------

class _Delta:
    def __init__(self, content: Optional[str] = None, tool_calls: Optional[List[Any]] = None) -> None:
        self.content = content
        self.tool_calls = tool_calls


class _Chunk:
    def __init__(self, content: Optional[str] = None, tool_calls: Optional[List[Any]] = None, usage: Any = None) -> None:
        self.choices = [types.SimpleNamespace(delta=_Delta(content=content, tool_calls=tool_calls))]
        self.usage = usage


def _tool_chunk(index: int, call_id: str, name: str, arguments: str) -> Any:
    return types.SimpleNamespace(
        index=index,
        id=call_id,
        function=types.SimpleNamespace(name=name, arguments=arguments),
    )


class _StreamClient:
    def __init__(self, rounds: List[List[Any]], router_usage: Any = None) -> None:
        self._rounds = list(rounds)
        self.router_usage = router_usage
        self.calls: List[Dict[str, Any]] = []
        self.chat = types.SimpleNamespace(completions=self)

    def create(self, **kwargs: Any) -> Any:
        if kwargs.get("response_format") == {"type": "json_object"}:
            return _Resp(_Msg(content=json.dumps(_DEFAULT_ROUTE)), self.router_usage)
        self.calls.append(kwargs)
        return self._rounds.pop(0)


# Phase 20A：Router 默认全维度关闭，避免吞掉 fake 响应队列
_DEFAULT_ROUTE = {"needs_fundamental": False, "needs_quant": False, "needs_event": False}

# 打桩工具表：保证测试不触网、可确定断言
_FAKE_TOOLS = {
    "get_stock_price": lambda symbol="": {"symbol": symbol, "price": 100.0},
    "get_technical_analysis": lambda symbol="": {"symbol": symbol, "rsi": 50.0},
}

_ROUTE_TRUE = {"needs_fundamental": True, "needs_quant": True, "needs_event": True}


# ---------------------------------------------------------------------------
# LLMUsage / ToolTrace / RunTrace 纯单元
# ---------------------------------------------------------------------------

def test_llm_usage_defaults() -> None:
    usage = LLMUsage()
    assert usage.snapshot() == {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }


def test_llm_usage_from_dict() -> None:
    usage = LLMUsage()
    usage.add({"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
    assert usage.snapshot() == {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
    }


def test_llm_usage_from_object() -> None:
    usage = LLMUsage()
    usage.add(_Usage(prompt_tokens=1, completion_tokens=2, total_tokens=3))
    assert usage.snapshot() == {
        "prompt_tokens": 1,
        "completion_tokens": 2,
        "total_tokens": 3,
    }


def test_llm_usage_missing_stays_zero() -> None:
    usage = LLMUsage()
    usage.add(None)  # 响应无 usage
    usage.add({})  # 空 dict
    usage.add(_Usage())  # 全零 object
    usage.add({"prompt_tokens": "非法", "completion_tokens": None})  # 非法值
    assert usage.snapshot() == {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }


def test_llm_usage_accumulates() -> None:
    usage = LLMUsage()
    usage.add({"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4})
    usage.add(_Usage(prompt_tokens=7, completion_tokens=9, total_tokens=16))
    assert usage.snapshot() == {
        "prompt_tokens": 10,
        "completion_tokens": 10,
        "total_tokens": 20,
    }


def test_tool_trace_snapshot() -> None:
    trace = ToolTrace(name="get_stock_price")
    snap = trace.snapshot()
    assert set(snap) == {"name", "elapsed_seconds", "success"}
    assert snap["name"] == "get_stock_price"
    assert snap["elapsed_seconds"] == 0.0
    assert snap["success"] is True
    failed = ToolTrace(name="x", elapsed_seconds=1.23456, success=False)
    assert failed.snapshot()["elapsed_seconds"] == 1.235
    assert failed.snapshot()["success"] is False


def test_run_trace_defaults_and_add_usage() -> None:
    trace = RunTrace()
    assert trace.status == "running"
    assert trace.tools == []
    assert trace.usage.snapshot() == {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }
    trace.add_usage({"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3})
    assert trace.usage.prompt_tokens == 1
    assert trace.usage.completion_tokens == 2
    assert trace.usage.total_tokens == 3


def test_runtime_snapshot_no_recursion() -> None:
    """snapshot() 保持 8 字段既有契约；snapshot_with_trace() 组合 trace 无无限递归。"""
    state = RuntimeState()
    state.tool_rounds = 1
    snap = state.snapshot()
    assert set(snap) == {
        "elapsed_seconds", "tool_rounds", "tool_calls", "llm_calls",
        "cancelled", "timed_out", "limit_exceeded", "error",
    }
    assert "trace" not in snap
    full = state.snapshot_with_trace()
    assert "trace" in full
    trace = full["trace"]
    assert set(trace) == {"runtime", "llm_usage", "tools", "status"}
    # 内层 runtime 是基础快照，不再嵌套 trace → 无无限递归
    assert "trace" not in trace["runtime"]
    assert trace["status"] == "running"
    assert trace["tools"] == []
    assert trace["llm_usage"]["total_tokens"] == 0


# ---------------------------------------------------------------------------
# Token usage 观测（Router / Final LLM / 流式）
# ---------------------------------------------------------------------------

def test_router_usage_recorded() -> None:
    """Router 调用成功后记录 usage 到 trace。"""
    client = _FakeClient(
        [_Resp(_Msg(content="最终回答"))], router_usage=_Usage(10, 5, 15)
    )
    spy = RuntimeState()
    with mock.patch.object(orchestrator_mod, "RuntimeState", return_value=spy):
        result = run_agent(client, "分析 600519")
    assert result.answer == "最终回答"
    assert spy.trace.usage.snapshot() == {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
    }


def test_final_llm_usage_recorded() -> None:
    """Final LLM（非流式）成功后记录 usage，并与 Router usage 累加。"""
    client = _FakeClient(
        [_Resp(_Msg(content="最终回答"), usage=_Usage(20, 8, 28))],
        router_usage=_Usage(2, 3, 5),
    )
    spy = RuntimeState()
    with mock.patch.object(orchestrator_mod, "RuntimeState", return_value=spy):
        result = run_agent(client, "q")
    assert result.answer == "最终回答"
    assert spy.trace.usage.snapshot() == {
        "prompt_tokens": 22,
        "completion_tokens": 11,
        "total_tokens": 33,
    }


def test_streaming_final_llm_usage_recorded() -> None:
    """流式路径：chunk 级 usage（末片携带）与 Router usage 均记录。"""
    client = _StreamClient(
        [[_Chunk(content="流式回答", usage=_Usage(30, 7, 37))]],
        router_usage=_Usage(4, 2, 6),
    )
    spy = RuntimeState()
    with mock.patch.object(orchestrator_mod, "RuntimeState", return_value=spy):
        events = list(_stream_agent_events(client, "q"))
    kinds = [event_type for event_type, _ in events]
    assert kinds == ["token", "__result__"]
    assert spy.trace.usage.snapshot() == {
        "prompt_tokens": 34,
        "completion_tokens": 9,
        "total_tokens": 43,
    }


def test_missing_usage_no_error_stays_zero() -> None:
    """usage 缺失：不报错、不估算，保持 0。"""
    client = _FakeClient([_Resp(_Msg(content="最终回答"))])
    spy = RuntimeState()
    with mock.patch.object(orchestrator_mod, "RuntimeState", return_value=spy):
        result = run_agent(client, "q")
    assert result.answer == "最终回答"
    assert result.error is None
    assert spy.trace.usage.snapshot() == {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }


# ---------------------------------------------------------------------------
# 工具耗时埋点
# ---------------------------------------------------------------------------

def test_tool_latency_recorded() -> None:
    """工具执行记录 name/elapsed_seconds/success，与 tool_calls 计数一致。"""
    client = _FakeClient(
        [
            _Resp(_Msg(tool_calls=[_tool_call("c1", "get_stock_price", '{"symbol": "600519"}')])),
            _Resp(_Msg(content="最终回答")),
        ],
        router_usage=_Usage(0, 0, 0),
    )
    spy = RuntimeState()
    with mock.patch.dict(orchestrator_mod.TOOL_DISPATCH, _FAKE_TOOLS, clear=True), mock.patch.object(
        orchestrator_mod, "RuntimeState", return_value=spy
    ):
        result = run_agent(client, "q")
    assert result.answer == "最终回答"
    assert len(spy.trace.tools) == 1
    trace = spy.trace.tools[0]
    assert trace.name == "get_stock_price"
    assert trace.success is True
    assert trace.elapsed_seconds >= 0.0
    assert spy.tool_calls == 1


def test_tool_failure_trace_success_false() -> None:
    """工具执行抛异常：trace 记录 success=False 并 re-raise（Phase 23 埋点模式）。"""
    client = _FakeClient(
        [_Resp(_Msg(tool_calls=[_tool_call("c1", "get_stock_price", '{"symbol": "600519"}')]))]
    )
    spy = RuntimeState()
    with mock.patch.object(orchestrator_mod, "_execute_tool", side_effect=ValueError("boom")), mock.patch.object(
        orchestrator_mod, "RuntimeState", return_value=spy
    ):
        with pytest.raises(ValueError):
            run_agent(client, "q")
    assert len(spy.trace.tools) == 1
    assert spy.trace.tools[0].name == "get_stock_price"
    assert spy.trace.tools[0].success is False
    assert spy.trace.tools[0].elapsed_seconds >= 0.0


# ---------------------------------------------------------------------------
# Run Status：success / degraded / error / timeout / cancelled
# ---------------------------------------------------------------------------

def test_run_status_success() -> None:
    client = _FakeClient([_Resp(_Msg(content="最终回答"))])
    spy = RuntimeState()
    with mock.patch.object(orchestrator_mod, "RuntimeState", return_value=spy):
        result = run_agent(client, "q")
    assert result.error is None
    assert spy.trace.status == "success"


def test_run_status_degraded_query() -> None:
    """查询级合规门禁：命中违禁提问 → degraded，且不发起任何 LLM 请求。"""
    client = _FakeClient([])
    spy = RuntimeState()
    with mock.patch.object(orchestrator_mod, "RuntimeState", return_value=spy):
        result = run_agent(client, "明天一定会涨吗")
    assert "受限" in result.answer
    assert client.chat.completions.calls == []
    assert spy.trace.status == "degraded"


def test_run_status_degraded_validator_streaming() -> None:
    """最终回答命中高危违禁：Validator 拦截 → degraded，trace 状态为 degraded。"""
    client = _StreamClient([[_Chunk(content="建议买入该股，明天一定大涨")]])
    spy = RuntimeState()
    with mock.patch.object(orchestrator_mod, "RuntimeState", return_value=spy):
        events = list(_stream_agent_events(client, "q"))
    kinds = [event_type for event_type, _ in events]
    assert kinds == ["degraded", "__result__"]
    assert "【回答受限：风险提示】" in events[0][1]["message"]
    assert events[0][1]["violations"]
    assert spy.trace.status == "degraded"


def test_run_status_error() -> None:
    """Final LLM API 调用失败 → result.error 置位，trace 状态为 error。"""
    client = _RaisingClient()
    spy = RuntimeState()
    with mock.patch.object(orchestrator_mod, "RuntimeState", return_value=spy):
        result = run_agent(client, "q")
    assert result.error is not None
    assert "API 调用失败" in result.error
    assert spy.trace.status == "error"


def test_run_status_timeout() -> None:
    """运行时超时 → 终止并置 error，trace 状态为 timeout。"""
    client = _FakeClient([_Resp(_Msg(content="不应到达"))])
    spy = RuntimeState()
    spy.started_at = monotonic() - 200.0  # 回拨模拟超时
    with mock.patch.object(orchestrator_mod, "RuntimeState", return_value=spy):
        result = run_agent(client, "q")
    assert "超时" in result.error
    assert spy.timed_out is True
    assert spy.trace.status == "timeout"


def test_run_status_cancelled() -> None:
    """客户端停止 → cancelled 置位，trace 状态为 cancelled，零事件。"""
    stop = threading.Event()
    stop.set()
    client = _StreamClient([[_Chunk(content="不应到达")]])
    spy = RuntimeState()
    with mock.patch.object(orchestrator_mod, "RuntimeState", return_value=spy):
        events = list(_stream_agent_events(client, "q", stop_event=stop))
    assert events == []
    assert spy.cancelled is True
    assert spy.trace.status == "cancelled"


# ---------------------------------------------------------------------------
# 隐私：Secret / 参数 / 结果不出现在 trace
# ---------------------------------------------------------------------------

def test_secret_and_args_not_in_trace() -> None:
    """trace 只含 name/elapsed/success，不含工具参数、结果内容或密钥。"""
    client = _FakeClient(
        [
            _Resp(_Msg(tool_calls=[_tool_call("c1", "get_stock_price", '{"symbol": "600519"}')])),
            _Resp(_Msg(content="最终回答")),
        ]
    )
    spy = RuntimeState()
    with mock.patch.dict(orchestrator_mod.TOOL_DISPATCH, _FAKE_TOOLS, clear=True), mock.patch.object(
        orchestrator_mod, "RuntimeState", return_value=spy
    ):
        run_agent(client, "q")
    serialized = json.dumps(spy.trace.snapshot(spy), ensure_ascii=False)
    assert "600519" not in serialized  # 工具参数不记录
    assert "最终回答" not in serialized  # 结果内容不记录
    assert "sk-" not in serialized  # 密钥不记录
    assert "100.0" not in serialized  # 工具结果数据不记录


# ---------------------------------------------------------------------------
# 契约不变性：SSE / Validator / Evidence / Router
# ---------------------------------------------------------------------------

def test_sse_event_set_unchanged() -> None:
    """SSE event type 集合保持不变：无 trace/usage/metrics/runtime_* 新事件。"""
    allowed = {"tool_call", "tool_result", "token", "degraded", "error", "__result__"}
    rounds = [
        [_Chunk(tool_calls=[_tool_chunk(0, "c1", "get_stock_price", '{"symbol": "600519"}')])],
        [_Chunk(content="流式回答")],
    ]
    client = _StreamClient(rounds)
    with mock.patch.dict(orchestrator_mod.TOOL_DISPATCH, _FAKE_TOOLS, clear=True):
        events = list(_stream_agent_events(client, "分析 600519"))
    kinds = {event_type for event_type, _ in events}
    assert kinds <= allowed
    assert not (kinds & {"runtime_start", "runtime_end", "trace", "usage", "metrics", "timeout", "limit", "cancelled"})


def test_validator_still_executes() -> None:
    """Validator 未被绕过：违禁最终回答仍被拦截为 degraded（含违规明细）。"""
    client = _StreamClient([[_Chunk(content="现在可以买入，明天一定大涨")]])
    events = list(_stream_agent_events(client, "q"))
    kinds = [event_type for event_type, _ in events]
    assert kinds == ["degraded", "__result__"]
    assert events[0][1]["violations"]


def test_evidence_built_once() -> None:
    """Evidence 只构建一次：多轮工具调用下 build_evidence_context 仅调用 1 次。"""
    client = _FakeClient(
        [
            _Resp(_Msg(tool_calls=[_tool_call("c1", "get_stock_price", '{"symbol": "600519"}')])),
            _Resp(_Msg(tool_calls=[_tool_call("c2", "get_technical_analysis", '{"symbol": "600519"}')])),
            _Resp(_Msg(content="最终回答")),
        ]
    )
    with mock.patch.dict(orchestrator_mod.TOOL_DISPATCH, _FAKE_TOOLS, clear=True), mock.patch.object(
        orchestrator_mod, "build_evidence_context", wraps=orchestrator_mod.build_evidence_context
    ) as wrapped:
        result = run_agent(client, "q")
    assert result.answer == "最终回答"
    assert result.tool_rounds == 2
    assert wrapped.call_count == 1


def test_router_logic_unchanged() -> None:
    """Router 分类结果仍驱动工具 Schema 裁剪（Router 路由逻辑未被改动）。"""
    active_quant = {"needs_fundamental": False, "needs_quant": True, "needs_event": False}
    schemas = _select_tool_schemas(active_quant, list(orchestrator_mod.TOOL_SCHEMAS))
    names = {s["function"]["name"] for s in schemas}
    assert "get_stock_price" in names
    assert "get_technical_analysis" in names
    assert "get_stock_fundamentals" not in names
    assert "get_stock_news" not in names
    all_off = {"needs_fundamental": False, "needs_quant": False, "needs_event": False}
    names_all_off = {s["function"]["name"] for s in _select_tool_schemas(all_off, list(orchestrator_mod.TOOL_SCHEMAS))}
    assert "get_stock_price" not in names_all_off
    assert "get_technical_analysis" not in names_all_off


def main() -> None:
    print("=== tests/test_observability.py Phase 23 观测性测试 ===")
    tests = [
        ("1. LLMUsage 默认值", test_llm_usage_defaults),
        ("2. LLMUsage 从 dict 读取", test_llm_usage_from_dict),
        ("3. LLMUsage 从 object 读取", test_llm_usage_from_object),
        ("4. LLMUsage 缺失/非法保持 0", test_llm_usage_missing_stays_zero),
        ("5. LLMUsage 累加", test_llm_usage_accumulates),
        ("6. ToolTrace snapshot", test_tool_trace_snapshot),
        ("7. RunTrace 默认与 add_usage", test_run_trace_defaults_and_add_usage),
        ("8. Runtime snapshot 无递归", test_runtime_snapshot_no_recursion),
        ("9. Router usage 记录", test_router_usage_recorded),
        ("10. Final LLM usage 记录", test_final_llm_usage_recorded),
        ("11. 流式 Final LLM usage 记录", test_streaming_final_llm_usage_recorded),
        ("12. usage 缺失不报错保持 0", test_missing_usage_no_error_stays_zero),
        ("13. 工具耗时埋点", test_tool_latency_recorded),
        ("14. 工具失败 trace success=False", test_tool_failure_trace_success_false),
        ("15. 状态 success", test_run_status_success),
        ("16. 状态 degraded（查询级）", test_run_status_degraded_query),
        ("17. 状态 degraded（Validator 拦截）", test_run_status_degraded_validator_streaming),
        ("18. 状态 error", test_run_status_error),
        ("19. 状态 timeout", test_run_status_timeout),
        ("20. 状态 cancelled", test_run_status_cancelled),
        ("21. Secret/参数不出现在 trace", test_secret_and_args_not_in_trace),
        ("22. SSE event 集合不变", test_sse_event_set_unchanged),
        ("23. Validator 仍执行", test_validator_still_executes),
        ("24. Evidence 只构建一次", test_evidence_built_once),
        ("25. Router 路由逻辑不变", test_router_logic_unchanged),
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
