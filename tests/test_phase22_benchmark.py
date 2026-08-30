"""Phase 22：Production Audit & Benchmark —— Token/Runtime Benchmark + Failure Matrix 补漏。

对应 Phase 22 规格第 4、5 节。

Part A：Token/Runtime Benchmark（第 4 节）
- 5 个固定场景，每场景 ≥3 次运行（RUNS=3）：
  A 单领域基本面（get_stock_fundamentals + get_valuation_analysis）
  B 单领域技术面（get_stock_price + get_technical_analysis）
  C 单领域新闻（get_stock_news）
  D 三领域综合（全部 5 个工具）
  E 合规敏感请求（查询级合规门禁拦截，零 LLM / 零工具）
- 逐次记录 llm_calls / tool_calls / tool_rounds / elapsed_seconds /
  timed_out / limit_exceeded，并计算平均值；
- Token（prompt/completion/total）：orchestrator 不读取 API usage 字段，
  无用量埋点，fake client 无法提供 → 如实记录 N/A，不编造（记为观测缺口）。

Part B：Runtime Failure Matrix 补漏（第 5 节）
test_runtime.py / test_router.py / test_agent_orchestrator.py / test_stream.py
已覆盖 7 项故障的绝大部分（Router 异常、Final LLM 异常、轮数超限、次数超限、
超时、CancelledError/断连均已有非流式与/或流式用例）。本文件仅补齐流式路径
缺口，并断言：无未捕获异常、无死循环、无终止后继续执行、无非法 SSE 事件：
- 流式 Router 异常 → 回退全维度（fallback all-true），工具照常执行；
- 流式 Tool 异常 → tool_result status=error，流程继续到最终回答，
  runtime.error 记录，不崩溃；
- 流式请求超时 → error + __result__，无结果泄漏；
- 流式次数超限（max_tool_calls=1）→ 第 2 个工具被拦截，error 语义。

全部 mock/fake（打桩 TOOL_DISPATCH / 注入 RuntimeState spy / fake OpenAI client），
零联网、不触库。

运行：cd E:/github/ai-financial-agent && .venv/Scripts/python.exe -m pytest tests/test_phase22_benchmark.py -q -s
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import app.agent.orchestrator as orchestrator_mod  # noqa: E402
from app.agent import run_agent  # noqa: E402
from app.agent.orchestrator import _stream_agent_events  # noqa: E402
from app.agent.runtime import RuntimeLimits, RuntimeState  # noqa: E402

RUNS = 3
ALL_TOOL_NAMES = {
    "get_stock_price",
    "get_technical_analysis",
    "get_stock_fundamentals",
    "get_valuation_analysis",
    "get_stock_news",
}

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
# 通用 fake 构件（与 test_runtime.py 同源模式）
# ---------------------------------------------------------------------------

class _FakeMessage:
    def __init__(self, content: Optional[str] = None, tool_calls: Optional[List[Any]] = None) -> None:
        self.content = content
        self.tool_calls = tool_calls


class _FakeDelta:
    def __init__(self, content: Optional[str] = None, tool_calls: Optional[List[Any]] = None) -> None:
        self.content = content
        self.tool_calls = tool_calls


class _FakeStreamChunk:
    def __init__(self, delta: Any, has_choices: bool = True) -> None:
        self.choices = [types.SimpleNamespace(delta=delta)] if has_choices else []


def _tool_call(call_id: str, name: str, arguments: str) -> Any:
    return types.SimpleNamespace(
        id=call_id, function=types.SimpleNamespace(name=name, arguments=arguments)
    )


def _msg(content: Optional[str] = None, tool_calls: Optional[List[Any]] = None) -> _FakeMessage:
    return _FakeMessage(content=content, tool_calls=tool_calls)


def _resp(message: _FakeMessage) -> Any:
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])


def _non_stream_response(content: str) -> Any:
    return types.SimpleNamespace(
        choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=content, tool_calls=None))]
    )


def _tool_chunk(index: int, call_id: Optional[str] = None, name: Optional[str] = None, arguments: Optional[str] = None) -> Any:
    return types.SimpleNamespace(
        index=index,
        id=call_id,
        function=types.SimpleNamespace(name=name, arguments=arguments),
    )


def _chunk(content: Optional[str] = None, tool_calls: Optional[List[Any]] = None) -> Any:
    return _FakeStreamChunk(_FakeDelta(content=content, tool_calls=tool_calls))


# 打桩工具表：5 个工具全量替换，保证不触网、可确定断言
_FAKE_TOOLS = {
    "get_stock_price": lambda symbol="": {"symbol": symbol, "price": 100.0, "pct_change": 0.5},
    "get_technical_analysis": lambda symbol="": {
        "symbol": symbol,
        "trend": {"ma5": 100.0, "ma20": 99.0, "ma60": 98.0},
        "momentum": {"rsi14": 55.0},
        "macd": {"dif": 0.1, "dea": 0.05, "histogram": 0.05},
        "volatility": {"atr14": 1.0},
    },
    "get_stock_fundamentals": lambda symbol="": {
        "symbol": symbol,
        "profitability": {"roe": 15.0, "eps": 3.5, "gross_margin": 40.0},
    },
    "get_valuation_analysis": lambda symbol="": {
        "symbol": symbol,
        "valuation": {"pe": 25.0, "pb": 5.0},
    },
    "get_stock_news": lambda symbol="": {
        "symbol": symbol,
        "news": [{"title": "公司发布中报", "published_at": "2026-08-01", "source": "测试源"}],
    },
}

_DEFAULT_ROUTE = {"needs_fundamental": False, "needs_quant": False, "needs_event": False}


class _BenchClient:
    """非流式 fake：Router（response_format=json_object）返回场景路由；
    随后的 Final LLM 调用依次返回 tool_rounds 中的工具请求，最后返回最终回答。"""

    def __init__(self, route: Dict[str, bool], tool_rounds: List[List[Tuple[str, str]]], answer: str) -> None:
        self._route = route
        self._tool_rounds = list(tool_rounds)
        self._answer = answer
        self.calls: List[Dict[str, Any]] = []
        self.chat = types.SimpleNamespace(completions=self)

    def create(self, **kwargs: Any) -> Any:
        if kwargs.get("response_format") == {"type": "json_object"}:
            return _resp(_msg(content=json.dumps(self._route)))
        self.calls.append(kwargs)
        if self._tool_rounds:
            calls = self._tool_rounds.pop(0)
            tcs = [
                _tool_call(f"c{i}", name, arguments)
                for i, (name, arguments) in enumerate(calls)
            ]
            return _resp(_msg(content=None, tool_calls=tcs))
        return _resp(_msg(content=self._answer))


class _FMStreamClient:
    """流式 fake：可配置 Router 抛异常；Final LLM 调用必须 stream=True。"""

    def __init__(self, rounds: List[List[Any]], router_error: bool = False) -> None:
        self._rounds = list(rounds)
        self._router_error = router_error
        self.calls: List[Dict[str, Any]] = []
        self.chat = types.SimpleNamespace(completions=self)

    def create(self, **kwargs: Any) -> Any:
        if kwargs.get("response_format") == {"type": "json_object"}:
            if self._router_error:
                raise RuntimeError("router boom")
            return _non_stream_response(json.dumps(_DEFAULT_ROUTE))
        self.calls.append(kwargs)
        assert kwargs.get("stream") is True, "流式路径必须 stream=True"
        return self._rounds.pop(0)


def _fm_tool_round_client(answer: str = "流式最终回答", router_error: bool = False) -> _FMStreamClient:
    rounds = [
        [
            _chunk(tool_calls=[_tool_chunk(0, call_id="c1", name="get_stock_price", arguments='{"symbol": "600519"}')]),
            _chunk(tool_calls=[_tool_chunk(1, call_id="c2", name="get_technical_analysis", arguments='{"symbol": "600519"}')]),
        ],
        [_chunk(content=answer)],
    ]
    return _FMStreamClient(rounds, router_error=router_error)


def _patched_check_limits(spy: RuntimeState, trigger_call: int) -> Any:
    """第 N 次 check_limits 调用时强制触发超时，其余委托真实逻辑。"""
    counter = {"n": 0}

    def patched(limits: RuntimeLimits) -> Optional[str]:
        counter["n"] += 1
        if counter["n"] == trigger_call:
            spy.timed_out = True
            return "request_timeout"
        return RuntimeState.check_limits(spy, limits)

    return patched


# ---------------------------------------------------------------------------
# Part A：Token/Runtime Benchmark（第 4 节）
# ---------------------------------------------------------------------------

_SCENARIOS = {
    "A": {
        "label": "A 单领域基本面",
        "question": "分析贵州茅台的基本面：营收、净利润、ROE 与当前估值水平",
        "route": {"needs_fundamental": True, "needs_quant": False, "needs_event": False},
        "tools": [("get_stock_fundamentals", '{"symbol": "600519"}'),
                  ("get_valuation_analysis", '{"symbol": "600519"}')],
        "answer": "基于基本面与估值工具返回数据完成分析。数据仅用于研究和分析，不构成投资建议。",
        "expected": {"llm_calls": 3, "tool_calls": 2, "tool_rounds": 2,
                     "tools": {"get_stock_fundamentals", "get_valuation_analysis"}},
    },
    "B": {
        "label": "B 单领域技术面",
        "question": "分析贵州茅台的技术面：最新价格、均线、RSI 与 MACD",
        "route": {"needs_fundamental": False, "needs_quant": True, "needs_event": False},
        "tools": [("get_stock_price", '{"symbol": "600519"}'),
                  ("get_technical_analysis", '{"symbol": "600519"}')],
        "answer": "基于行情与技术面工具返回数据完成分析。数据仅用于研究和分析，不构成投资建议。",
        "expected": {"llm_calls": 3, "tool_calls": 2, "tool_rounds": 2,
                     "tools": {"get_stock_price", "get_technical_analysis"}},
    },
    "C": {
        "label": "C 单领域新闻",
        "question": "贵州茅台最近有什么新闻？",
        "route": {"needs_fundamental": False, "needs_quant": False, "needs_event": True},
        "tools": [("get_stock_news", '{"symbol": "600519"}')],
        "answer": "已获取贵州茅台近期新闻并完成事件梳理。数据仅用于研究和分析，不构成投资建议。",
        "expected": {"llm_calls": 3, "tool_calls": 1, "tool_rounds": 2,
                     "tools": {"get_stock_news"}},
    },
    "D": {
        "label": "D 三领域综合",
        "question": "综合分析贵州茅台的行情、技术面、基本面、估值与近期新闻",
        "route": {"needs_fundamental": True, "needs_quant": True, "needs_event": True},
        "tools": [("get_stock_price", '{"symbol": "600519"}'),
                  ("get_technical_analysis", '{"symbol": "600519"}'),
                  ("get_stock_fundamentals", '{"symbol": "600519"}'),
                  ("get_valuation_analysis", '{"symbol": "600519"}'),
                  ("get_stock_news", '{"symbol": "600519"}')],
        "answer": "已综合行情、技术面、基本面、估值与新闻数据完成跨领域分析。数据仅用于研究和分析，不构成投资建议。",
        "expected": {"llm_calls": 3, "tool_calls": 5, "tool_rounds": 2, "tools": ALL_TOOL_NAMES},
    },
    "E": {
        "label": "E 合规敏感请求",
        "question": "现在可以全仓买入贵州茅台吗？",
        "route": _DEFAULT_ROUTE,
        "tools": [],
        "answer": "不应到达",
        "expected": {"llm_calls": 0, "tool_calls": 0, "tool_rounds": 0, "tools": set()},
    },
}

# 模块级结果收集：{场景: {"rows": [...], "avg": {...}, "expected": {...}}}
_RESULTS: Dict[str, Dict[str, Any]] = {}


def _run_benchmark_once(scenario: str) -> Dict[str, Any]:
    cfg = _SCENARIOS[scenario]
    client = _BenchClient(cfg["route"], [list(cfg["tools"])], cfg["answer"])
    spy = RuntimeState()
    with mock.patch.object(orchestrator_mod, "RuntimeState", return_value=spy), mock.patch.dict(
        orchestrator_mod.TOOL_DISPATCH, _FAKE_TOOLS, clear=True
    ):
        result = run_agent(client, cfg["question"])
    snap = spy.snapshot()
    return {
        "llm_calls": snap["llm_calls"],
        "tool_calls": snap["tool_calls"],
        "tool_rounds": snap["tool_rounds"],
        "elapsed_seconds": snap["elapsed_seconds"],
        "timed_out": snap["timed_out"],
        "limit_exceeded": snap["limit_exceeded"],
        "error": snap["error"],
        "answer": result.answer,
        "tools_sent": _sent_tool_names(client),
        "client_calls": len(client.calls),
    }


def _sent_tool_names(client: _BenchClient) -> List[str]:
    """首次 Final LLM 请求实际下发的工具 Schema 名称（路由裁剪结果）。"""
    if not client.calls:
        return []
    schemas = client.calls[0].get("tools", [])
    return [s["function"]["name"] for s in schemas]


def test_benchmark_scenario_a() -> None:
    _run_scenario("A")


def test_benchmark_scenario_b() -> None:
    _run_scenario("B")


def test_benchmark_scenario_c() -> None:
    _run_scenario("C")


def test_benchmark_scenario_d() -> None:
    _run_scenario("D")


def test_benchmark_scenario_e() -> None:
    _run_scenario("E")


def _run_scenario(scenario: str) -> None:
    cfg = _SCENARIOS[scenario]
    exp = cfg["expected"]
    rows = [_run_benchmark_once(scenario) for _ in range(RUNS)]
    for row in rows:
        assert row["timed_out"] is False, "基准场景不应触发超时"
        assert row["limit_exceeded"] is False, "基准场景不应触发超限"
        assert row["llm_calls"] == exp["llm_calls"], f"llm_calls 期望 {exp['llm_calls']}"
        assert row["tool_calls"] == exp["tool_calls"], f"tool_calls 期望 {exp['tool_calls']}"
        assert row["tool_rounds"] == exp["tool_rounds"], f"tool_rounds 期望 {exp['tool_rounds']}"
        assert set(row["tools_sent"]) == exp["tools"], "路由裁剪的工具清单与场景不符"
        if scenario == "E":
            assert row["client_calls"] == 0, "合规拦截后不应发起任何 LLM 调用"
            assert "【回答受限：风险提示】" in row["answer"], "应返回合规降级回答"
            assert row["error"] is None
        else:
            assert row["error"] is None
            assert "【回答受限" not in row["answer"]
    avg = {
        "llm_calls": sum(r["llm_calls"] for r in rows) / RUNS,
        "tool_calls": sum(r["tool_calls"] for r in rows) / RUNS,
        "tool_rounds": sum(r["tool_rounds"] for r in rows) / RUNS,
        "elapsed_seconds": sum(r["elapsed_seconds"] for r in rows) / RUNS,
        "timeout": sum(1 for r in rows if r["timed_out"]),
        "limit_exceeded": sum(1 for r in rows if r["limit_exceeded"]),
        "tokens": None,  # orchestrator 无 usage 埋点，fake client 无法提供 → N/A
    }
    _RESULTS[scenario] = {"rows": rows, "avg": avg, "expected": exp, "label": cfg["label"]}


def test_benchmark_summary() -> None:
    """汇总断言 + 输出平均指标表 + 落盘 JSON 证据文件。"""
    assert set(_RESULTS) == set(_SCENARIOS), f"全部 5 个场景均应完成基准，缺 {set(_SCENARIOS) - set(_RESULTS)}"
    for scenario, entry in _RESULTS.items():
        assert len(entry["rows"]) >= RUNS, f"{scenario} 运行次数不足 {RUNS}"
    header = f"{'场景':<12}{'Runs':<5}{'LLM Calls':<10}{'Tool Calls':<11}{'Tool Rounds':<12}{'Avg Latency(s)':<14}{'Timeout':<8}{'Limit':<6}{'Tokens'}"
    print("\n[Phase 22 Benchmark] Token/Runtime 基准（fake，无联网；Token 无 usage 埋点 → N/A）")
    print(header)
    for scenario in sorted(_RESULTS):
        avg = _RESULTS[scenario]["avg"]
        print(
            f"{_RESULTS[scenario]['label']:<12}{RUNS:<5}"
            f"{avg['llm_calls']:<10.1f}{avg['tool_calls']:<11.1f}{avg['tool_rounds']:<12.1f}"
            f"{avg['elapsed_seconds']:<14.4f}{avg['timeout']:<8}{avg['limit_exceeded']:<6}N/A"
        )
    artifact = {
        scenario: {
            "label": entry["label"],
            "runs": entry["rows"],
            "avg": entry["avg"],
        }
        for scenario, entry in sorted(_RESULTS.items())
    }
    with open("phase22_benchmark_results.json", "w", encoding="utf-8") as fh:
        json.dump(artifact, fh, ensure_ascii=False, indent=2)
    print("[Phase 22 Benchmark] 原始逐次数据已落盘 phase22_benchmark_results.json")


# ---------------------------------------------------------------------------
# Part B：Runtime Failure Matrix 流式缺口补漏（第 5 节）
# ---------------------------------------------------------------------------

_ALLOWED_EVENTS = {"tool_call", "tool_result", "token", "degraded", "error", "__result__"}


def _assert_legal_events(events: List[Tuple[str, Dict[str, Any]]]) -> None:
    kinds = {event_type for event_type, _ in events}
    assert kinds <= _ALLOWED_EVENTS, f"出现非法 SSE 事件: {kinds - _ALLOWED_EVENTS}"
    assert not (kinds & {"runtime_start", "runtime_end", "timeout", "limit", "cancelled"})


def test_fm_streaming_router_exception_fallback() -> None:
    """流式 Router 异常 → _route_question 回退全维度，工具照常执行，无崩溃。"""
    client = _fm_tool_round_client(router_error=True)
    spy = RuntimeState()
    with mock.patch.object(orchestrator_mod, "RuntimeState", return_value=spy), mock.patch.dict(
        orchestrator_mod.TOOL_DISPATCH, _FAKE_TOOLS, clear=True
    ):
        events = list(_stream_agent_events(client, "分析 600519"))
    kinds = [event_type for event_type, _ in events]
    assert kinds == ["tool_call", "tool_result", "tool_call", "tool_result", "token", "__result__"]
    assert spy.llm_calls == 3  # Router 调用尝试已计数 + 两轮 Final LLM
    assert spy.tool_calls == 2
    assert spy.tool_rounds == 2
    assert events[-1][1]["error"] is None
    _assert_legal_events(events)


def test_fm_streaming_tool_exception_continues() -> None:
    """流式 Tool 异常 → tool_result status=error，流程继续到最终回答，runtime.error 记录。"""
    def _boom(**kwargs: Any) -> Dict[str, Any]:
        raise ValueError("tool boom")

    dispatch = {
        "get_stock_price": _boom,
        "get_technical_analysis": lambda symbol="": {"symbol": symbol, "price": 100.0},
    }
    client = _fm_tool_round_client()
    spy = RuntimeState()
    with mock.patch.object(orchestrator_mod, "RuntimeState", return_value=spy), mock.patch.dict(
        orchestrator_mod.TOOL_DISPATCH, dispatch, clear=True
    ):
        events = list(_stream_agent_events(client, "q"))
    statuses = [payload["status"] for event_type, payload in events if event_type == "tool_result"]
    assert statuses == ["error", "ok"]
    assert "ValueError" in spy.error and "tool boom" in spy.error
    kinds = [event_type for event_type, _ in events]
    assert kinds == ["tool_call", "tool_result", "tool_call", "tool_result", "token", "__result__"]
    assert events[-1][1]["error"] is None  # 工具异常不终止整个流程
    _assert_legal_events(events)


def test_fm_streaming_timeout_aborts_without_result_leak() -> None:
    """流式超时 → error + __result__，终止后不再产出任何内容，无结果泄漏。"""
    client = _fm_tool_round_client()
    spy = RuntimeState()
    spy.check_limits = _patched_check_limits(spy, trigger_call=5)
    with mock.patch.object(orchestrator_mod, "RuntimeState", return_value=spy), mock.patch.dict(
        orchestrator_mod.TOOL_DISPATCH, _FAKE_TOOLS, clear=True
    ):
        events = list(_stream_agent_events(client, "q"))
    kinds = [event_type for event_type, _ in events]
    assert kinds == ["tool_call", "tool_result", "error", "__result__"]
    assert "超时上限" in events[-2][1]["message"]
    assert events[-1][1]["error"] == events[-2][1]["message"]
    assert spy.timed_out is True
    assert not any(event_type == "token" for event_type, _ in events)  # 无内容泄漏
    _assert_legal_events(events)


def test_fm_streaming_call_limit_blocks_second_tool() -> None:
    """流式次数超限（max_tool_calls=1）→ 第 2 个工具被拦截，error 语义。"""
    client = _fm_tool_round_client()
    limits = RuntimeLimits(max_tool_calls=1)
    with mock.patch.object(orchestrator_mod, "RuntimeLimits", return_value=limits), mock.patch.dict(
        orchestrator_mod.TOOL_DISPATCH, _FAKE_TOOLS, clear=True
    ):
        events = list(_stream_agent_events(client, "q"))
    kinds = [event_type for event_type, _ in events]
    assert kinds == ["tool_call", "tool_result", "error", "__result__"]
    assert "次数超过上限" in events[-2][1]["message"]
    assert kinds.count("tool_call") == 1  # 第 2 个工具未执行
    assert not any(event_type == "token" for event_type, _ in events)
    _assert_legal_events(events)


# ---------------------------------------------------------------------------
# main() 直跑入口（与既有测试文件一致）
# ---------------------------------------------------------------------------

def main() -> None:
    print("=== tests/test_phase22_benchmark.py Phase 22 Benchmark + Failure Matrix ===")
    tests = [
        ("A 单领域基本面 Benchmark", test_benchmark_scenario_a),
        ("B 单领域技术面 Benchmark", test_benchmark_scenario_b),
        ("C 单领域新闻 Benchmark", test_benchmark_scenario_c),
        ("D 三领域综合 Benchmark", test_benchmark_scenario_d),
        ("E 合规敏感请求 Benchmark", test_benchmark_scenario_e),
        ("汇总表 + 落盘", test_benchmark_summary),
        ("FM 流式 Router 异常回退", test_fm_streaming_router_exception_fallback),
        ("FM 流式 Tool 异常继续", test_fm_streaming_tool_exception_continues),
        ("FM 流式超时终止", test_fm_streaming_timeout_aborts_without_result_leak),
        ("FM 流式次数超限拦截", test_fm_streaming_call_limit_blocks_second_tool),
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
