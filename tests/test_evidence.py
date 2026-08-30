"""Phase 20B：Evidence Context Builder 确定性测试。

覆盖：
- 领域映射：5 个工具 -> fundamental/quant/event；未知工具 -> other；
- 字段保留：source / data_time / fetched_at 提取，且 data_time 与 fetched_at
  各归各位、不混淆；
- 渲染：空结果占位符、多领域分节共存、data 原样进入文本；
- 不修改原始数据：build_evidence_context 只读输入，不改动传入的 tool result；
- LLM-free：模块源码无 openai/requests/akshare 等依赖，纯函数可独立调用；
- 编排接入（run_agent / _stream_agent_events）：证据 user 消息只在首次 Tool
  Calling 完成后注入一次，位于 tool 消息之后，原始 tool 消息保留；
- 只构建一次：mock 计数验证 build_evidence_context 每轮 Agent 恰好调用 1 次。

运行：cd E:/github/ai-financial-agent && .venv/Scripts/python.exe tests/test_evidence.py
"""

from __future__ import annotations

import inspect
import json
import sys
import types
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import app.agent.orchestrator as orchestrator_mod  # noqa: E402
import app.agent.evidence as evidence_mod  # noqa: E402
from app.agent.evidence import (  # noqa: E402
    EvidenceContext,
    EvidenceItem,
    build_evidence_context,
    render_evidence_context,
)
from app.agent.orchestrator import _stream_agent_events  # noqa: E402
from app.agent import run_agent  # noqa: E402

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
# 基础数据
# ---------------------------------------------------------------------------

_FUNDAMENTAL_RESULT = {
    "symbol": "600519",
    "report_period": "2025-12-31",
    "roe": 25.0,
    "source": "东方财富",
    "fetched_at": "2026-08-25T09:00:00+00:00",
}

_QUANT_RESULT = {
    "symbol": "600519",
    "price": 100.0,
    "trade_date": "2026-08-25",
    "fetched_at": "2026-08-25T09:00:00+00:00",
}

_NEWS_RESULT = {
    "symbol": "600519",
    "news": [{"title": "贵州茅台发布2026年半年度报告", "publish_date": "2026-08-20 09:30:00"}],
    "news_source": "东方财富",
    "fetched_at": "2026-08-25T09:00:00+00:00",
}


# ---------------------------------------------------------------------------
# 1. 领域映射：5 工具 + 未知工具 -> other
# ---------------------------------------------------------------------------

def test_domain_fundamental_tools() -> None:
    """get_stock_fundamentals / get_valuation_analysis -> fundamental。"""
    context = build_evidence_context([
        {"tool_name": "get_stock_fundamentals", "result": _FUNDAMENTAL_RESULT},
        {"tool_name": "get_valuation_analysis", "result": {"symbol": "600519", "pe": 20.0}},
    ])
    assert len(context.fundamental) == 2
    assert context.quant == [] and context.event == [] and context.other == []
    assert {i.tool_name for i in context.fundamental} == {
        "get_stock_fundamentals", "get_valuation_analysis"}


def test_domain_quant_tools() -> None:
    """get_stock_price / get_technical_analysis -> quant。"""
    context = build_evidence_context([
        {"tool_name": "get_stock_price", "result": _QUANT_RESULT},
        {"tool_name": "get_technical_analysis", "result": {"symbol": "600519", "rsi": 42.0}},
    ])
    assert len(context.quant) == 2
    assert context.fundamental == [] and context.event == [] and context.other == []
    assert {i.tool_name for i in context.quant} == {"get_stock_price", "get_technical_analysis"}


def test_domain_event_tool() -> None:
    """get_stock_news -> event。"""
    context = build_evidence_context([{"tool_name": "get_stock_news", "result": _NEWS_RESULT}])
    assert len(context.event) == 1
    assert context.event[0].tool_name == "get_stock_news"
    assert context.fundamental == [] and context.quant == [] and context.other == []


def test_domain_unknown_tool_other() -> None:
    """未登记工具 -> other，且所有 domain 都能取到。"""
    context = build_evidence_context([
        {"tool_name": "custom_screener", "result": {"symbol": "600519", "score": 80}},
    ])
    assert len(context.other) == 1
    assert context.other[0].tool_name == "custom_screener"
    assert context.fundamental == [] and context.quant == [] and context.event == []


def test_domain_all_five_tools() -> None:
    """5 个工具混合输入：各自落到正确 domain。"""
    context = build_evidence_context([
        {"tool_name": "get_stock_price", "result": _QUANT_RESULT},
        {"tool_name": "get_technical_analysis", "result": {"rsi": 42.0}},
        {"tool_name": "get_stock_fundamentals", "result": _FUNDAMENTAL_RESULT},
        {"tool_name": "get_valuation_analysis", "result": {"pe": 20.0}},
        {"tool_name": "get_stock_news", "result": _NEWS_RESULT},
    ])
    assert len(context.fundamental) == 2
    assert len(context.quant) == 2
    assert len(context.event) == 1
    assert context.other == []
    # all_items 按 fundamental -> quant -> event -> other 顺序聚合
    assert [i.tool_name for i in context.all_items()] == [
        "get_stock_fundamentals", "get_valuation_analysis",
        "get_stock_price", "get_technical_analysis",
        "get_stock_news",
    ]


# ---------------------------------------------------------------------------
# 2. 字段保留：source / data_time / fetched_at
# ---------------------------------------------------------------------------

def test_source_extraction() -> None:
    """source 取 source / provider / news_source 第一个非空值（按 _first_value 顺序）。"""
    item = build_evidence_context([
        {"tool_name": "get_stock_news", "result": {"news_source": "财联社", "source": "东方财富"}},
    ]).event[0]
    assert item.source == "东方财富"  # source 键优先于 news_source

    item2 = build_evidence_context([
        {"tool_name": "get_stock_price", "result": {"provider": "AKShare"}},
    ]).quant[0]
    assert item2.source == "AKShare"

    item3 = build_evidence_context([
        {"tool_name": "get_stock_news", "result": {"news_source": "财联社"}},
    ]).event[0]
    assert item3.source == "财联社"


def test_data_time_extraction() -> None:
    """data_time 取 report_period / data_date / trade_date / published_at 等。"""
    item = build_evidence_context([
        {"tool_name": "get_stock_fundamentals", "result": {"report_period": "2025-12-31"}},
    ]).fundamental[0]
    assert item.data_time == "2025-12-31"

    item2 = build_evidence_context([
        {"tool_name": "get_stock_news", "result": {"published_at": "2026-08-20 09:30:00"}},
    ]).event[0]
    assert item2.data_time == "2026-08-20 09:30:00"

    item3 = build_evidence_context([
        {"tool_name": "get_stock_price", "result": {"trade_date": "2026-08-25"}},
    ]).quant[0]
    assert item3.data_time == "2026-08-25"


def test_fetched_at_extraction() -> None:
    """fetched_at 取 fetched_at / fetch_time / retrieved_at。"""
    item = build_evidence_context([
        {"tool_name": "get_stock_price", "result": {"fetched_at": "2026-08-25T09:00:00+00:00"}},
    ]).quant[0]
    assert item.fetched_at == "2026-08-25T09:00:00+00:00"

    item2 = build_evidence_context([
        {"tool_name": "get_stock_price", "result": {"fetch_time": "T+0"}},
    ]).quant[0]
    assert item2.fetched_at == "T+0"


def test_data_time_fetched_at_not_confused() -> None:
    """data_time 与 fetched_at 各归各位，互不串用。"""
    item = build_evidence_context([
        {
            "tool_name": "get_stock_price",
            "result": {"trade_date": "2026-08-25", "fetched_at": "2026-08-25T09:00:00+00:00"},
        },
    ]).quant[0]
    assert item.data_time == "2026-08-25"
    assert item.fetched_at == "2026-08-25T09:00:00+00:00"
    # 反向：缺 fetched_at 不借用 data_time，缺 data_time 不借用 fetched_at
    item2 = build_evidence_context([
        {"tool_name": "get_stock_price", "result": {"trade_date": "2026-08-25"}},
    ]).quant[0]
    assert item2.data_time == "2026-08-25"
    assert item2.fetched_at is None

    item3 = build_evidence_context([
        {"tool_name": "get_stock_price", "result": {"fetched_at": "2026-08-25T09:00:00+00:00"}},
    ]).quant[0]
    assert item3.fetched_at == "2026-08-25T09:00:00+00:00"
    assert item3.data_time is None


# ---------------------------------------------------------------------------
# 3. 渲染：空占位 / 多领域 / data 原样
# ---------------------------------------------------------------------------

def test_empty_context_placeholder() -> None:
    assert render_evidence_context(EvidenceContext()) == "【证据上下文】\n暂无工具证据。"
    assert render_evidence_context(build_evidence_context([])) == "【证据上下文】\n暂无工具证据。"


def test_multi_domain_render_sections() -> None:
    text = render_evidence_context(build_evidence_context([
        {"tool_name": "get_stock_fundamentals", "result": _FUNDAMENTAL_RESULT},
        {"tool_name": "get_stock_price", "result": _QUANT_RESULT},
        {"tool_name": "get_stock_news", "result": _NEWS_RESULT},
    ]))
    assert "【基本面证据】" in text
    assert "【量化证据】" in text
    assert "【事件证据】" in text
    assert "【其他证据】" not in text
    # 每个分节内部从 [Evidence 1] 编号（三个分节各一条）
    assert text.count("[Evidence 1]") == 3
    # 字段行：tool / source / data_time / fetched_at / data
    assert "tool=get_stock_fundamentals" in text
    assert "source=东方财富" in text
    assert "data_time=2025-12-31" in text
    assert "fetched_at=2026-08-25T09:00:00+00:00" in text


def test_render_includes_data_repr() -> None:
    """渲染文本包含原始 data 的 repr，证据原样进入最终上下文。"""
    text = render_evidence_context(build_evidence_context([
        {"tool_name": "get_stock_price", "result": _QUANT_RESULT},
    ]))
    assert "data=" in text
    assert "'price': 100.0" in text
    assert "'symbol': '600519'" in text


def test_original_input_not_mutated() -> None:
    """build_evidence_context 只读输入：不改动传入的 tool result。"""
    inputs = [
        {"tool_name": "get_stock_price", "result": deepcopy(_QUANT_RESULT)},
        {"tool_name": "get_stock_fundamentals", "result": deepcopy(_FUNDAMENTAL_RESULT)},
    ]
    snapshot = deepcopy(inputs)
    build_evidence_context(inputs)
    assert inputs == snapshot


# ---------------------------------------------------------------------------
# 4. LLM-free：模块无 LLM / 金融 API 依赖，纯函数独立可用
# ---------------------------------------------------------------------------

def test_build_render_llm_free() -> None:
    """evidence.py 不含任何 LLM / HTTP / 金融 API 调用点。"""
    source = inspect.getsource(evidence_mod)
    for forbidden in ("openai", "requests", "http.client", "urllib", "httpx", "akshare", "tushare"):
        assert forbidden not in source, f"evidence.py 不应依赖：{forbidden}"
    # 行为验证：无需任何 client，直接构建并渲染
    context = build_evidence_context([{"tool_name": "get_stock_price", "result": _QUANT_RESULT}])
    assert isinstance(context, EvidenceContext)
    assert isinstance(context.quant[0], EvidenceItem)
    assert "【量化证据】" in render_evidence_context(context)


# ---------------------------------------------------------------------------
# 5. 编排接入：run_agent / _stream_agent_events / 只构建一次
# ---------------------------------------------------------------------------

class _FakeMessage:
    def __init__(self, content: Optional[str] = None, tool_calls: Optional[List[Any]] = None) -> None:
        self.content = content
        self.tool_calls = tool_calls


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
    return types.SimpleNamespace(
        choices=[types.SimpleNamespace(delta=types.SimpleNamespace(content=content, tool_calls=tool_calls))]
    )


_FAKE_TOOLS = {
    "get_stock_price": lambda symbol="": {"symbol": symbol, "price": 100.0},
    "get_technical_analysis": lambda symbol="": {"symbol": symbol, "rsi": 50.0},
}


class _RunAgentClient:
    """Router 返回路由 JSON；主循环第一轮并行调用 2 个工具，第二轮返回最终回答。"""

    def __init__(self, route_json: str, final_answer: str = "最终回答") -> None:
        self._route_json = route_json
        self._final_answer = final_answer
        self.calls: List[Dict[str, Any]] = []
        self.chat = types.SimpleNamespace(completions=self)

    def create(self, **kwargs: Any) -> Any:
        if kwargs.get("response_format") == {"type": "json_object"}:
            return _resp(_msg(content=self._route_json))
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            return _resp(_msg(content=None, tool_calls=[
                _tool_call("call_price", "get_stock_price", '{"symbol": "600519"}'),
                _tool_call("call_tech", "get_technical_analysis", '{"symbol": "600519"}'),
            ]))
        return _resp(_msg(content=self._final_answer))


_ROUTE = json.dumps({"needs_fundamental": False, "needs_quant": True, "needs_event": False})


def test_run_agent_injects_evidence_once() -> None:
    """run_agent：证据 user 消息在 tool 消息之后注入且仅一次。"""
    client = _RunAgentClient(_ROUTE)
    with mock.patch.dict(orchestrator_mod.TOOL_DISPATCH, _FAKE_TOOLS, clear=True):
        result = run_agent(client, "分析 600519")
    assert result.answer == "最终回答"
    assert result.error is None

    # 第二轮（最终回答）调用携带的证据 user 消息
    second_msgs = client.calls[1]["messages"]
    roles = [m["role"] for m in second_msgs]
    assert roles == ["system", "user", "assistant", "tool", "tool", "user"]

    evidence_msgs = [m for m in second_msgs if (m.get("content") or "").startswith("以下是本轮工具调用得到的结构化证据")]
    assert len(evidence_msgs) == 1  # 只注入一次
    evidence = evidence_msgs[0]
    assert evidence["role"] == "user"
    # 原始 tool 消息保留，且证据不是完整 Tool Result 的逐条复制
    tool_msgs = [m for m in second_msgs if m["role"] == "tool"]
    assert len(tool_msgs) == 2
    assert json.loads(tool_msgs[0]["content"])["price"] == 100.0
    assert json.loads(tool_msgs[1]["content"])["rsi"] == 50.0
    # 证据位于所有 tool 消息之后
    assert second_msgs.index(evidence) > second_msgs.index(tool_msgs[-1])
    # 证据内容为结构化上下文：量化分节 + 工具名
    assert "【量化证据】" in evidence["content"]
    assert "get_stock_price" in evidence["content"]
    assert "不得补充工具未提供的金融事实" in evidence["content"]


def test_stream_injects_evidence_once() -> None:
    """流式：SSE 事件协议不变，第二轮 messages 携带证据 user 消息且仅一次。"""

    class _StreamClient:
        def __init__(self, route_json: str, final_answer: str = "流式最终回答") -> None:
            self._route_json = route_json
            self._final_answer = final_answer
            self.calls: List[Dict[str, Any]] = []
            self.chat = types.SimpleNamespace(completions=self)

        def create(self, **kwargs: Any) -> Any:
            if kwargs.get("response_format") == {"type": "json_object"}:
                return _non_stream_response(self._route_json)
            self.calls.append(kwargs)
            assert kwargs.get("stream") is True
            if len(self.calls) == 1:
                return [
                    _chunk(tool_calls=[_tool_chunk(0, call_id="c1", name="get_stock_price", arguments='{"symbol": "600519"}')]),
                    _chunk(tool_calls=[_tool_chunk(1, call_id="c2", name="get_technical_analysis", arguments='{"symbol": "600519"}')]),
                ]
            return [_chunk(content=self._final_answer)]

    client = _StreamClient(_ROUTE)
    with mock.patch.dict(orchestrator_mod.TOOL_DISPATCH, _FAKE_TOOLS, clear=True):
        events = list(_stream_agent_events(client, "分析 600519"))

    # SSE 事件协议保持不变（不新增证据事件）
    assert [event_type for event_type, _ in events] == [
        "tool_call", "tool_result", "tool_call", "tool_result", "token", "__result__",
    ]
    assert events[-1][1]["answer"] == "流式最终回答"
    assert events[-1][1]["error"] is None

    # 第二轮（最终回答）messages：tool 消息后注入一条证据 user 消息
    second_msgs = client.calls[1]["messages"]
    roles = [m["role"] for m in second_msgs]
    assert roles == ["system", "user", "assistant", "tool", "tool", "user"]
    evidence_msgs = [m for m in second_msgs if (m.get("content") or "").startswith("以下是本轮工具调用得到的结构化证据")]
    assert len(evidence_msgs) == 1
    assert "【量化证据】" in evidence_msgs[0]["content"]
    # 原始 tool 消息保留
    tool_msgs = [m for m in second_msgs if m["role"] == "tool"]
    assert len(tool_msgs) == 2


def test_evidence_built_exactly_once() -> None:
    """Agent 一次运行中 Evidence Builder 只构建一次（不随轮次重复）。"""
    client = _RunAgentClient(_ROUTE)
    call_count = {"n": 0}
    original = evidence_mod.build_evidence_context

    def _spy(tool_results: Any) -> Any:
        call_count["n"] += 1
        return original(tool_results)

    with mock.patch.object(orchestrator_mod, "build_evidence_context", side_effect=_spy), \
            mock.patch.dict(orchestrator_mod.TOOL_DISPATCH, _FAKE_TOOLS, clear=True):
        result = run_agent(client, "分析 600519")
    assert result.answer == "最终回答"
    assert call_count["n"] == 1


def test_no_evidence_when_no_tool_called() -> None:
    """直接回答路径（无工具调用）：不注入证据 user 消息。"""

    class _DirectClient:
        def __init__(self, route_json: str, answer: str = "直接回答") -> None:
            self._route_json = route_json
            self._answer = answer
            self.calls: List[Dict[str, Any]] = []
            self.chat = types.SimpleNamespace(completions=self)

        def create(self, **kwargs: Any) -> Any:
            if kwargs.get("response_format") == {"type": "json_object"}:
                return _resp(_msg(content=self._route_json))
            self.calls.append(kwargs)
            return _resp(_msg(content=self._answer))

    client = _DirectClient(_ROUTE)
    with mock.patch.dict(orchestrator_mod.TOOL_DISPATCH, _FAKE_TOOLS, clear=True):
        result = run_agent(client, "q")
    assert result.answer == "直接回答"
    assert result.tool_calls == []
    first_msgs = client.calls[0]["messages"]
    assert [m["role"] for m in first_msgs] == ["system", "user"]
    assert not any(m["content"].startswith("以下是本轮工具调用得到的结构化证据") for m in first_msgs)


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def main() -> None:
    print("=== tests/test_evidence.py Phase 20B Evidence Context Builder 测试 ===")
    tests = [
        ("D.1 领域映射：fundamental 工具", test_domain_fundamental_tools),
        ("D.2 领域映射：quant 工具", test_domain_quant_tools),
        ("D.3 领域映射：event 工具", test_domain_event_tool),
        ("D.4 领域映射：未知工具 -> other", test_domain_unknown_tool_other),
        ("D.5 领域映射：5 工具混合", test_domain_all_five_tools),
        ("F.1 字段保留：source", test_source_extraction),
        ("F.2 字段保留：data_time", test_data_time_extraction),
        ("F.3 字段保留：fetched_at", test_fetched_at_extraction),
        ("F.4 data_time 与 fetched_at 不混淆", test_data_time_fetched_at_not_confused),
        ("R.1 渲染：空结果占位符", test_empty_context_placeholder),
        ("R.2 渲染：多领域分节共存", test_multi_domain_render_sections),
        ("R.3 渲染：data 原样进入文本", test_render_includes_data_repr),
        ("I.1 不修改原始数据", test_original_input_not_mutated),
        ("L.1 build/render 不调用 LLM（LLM-free）", test_build_render_llm_free),
        ("W.1 run_agent 接入证据（一次）", test_run_agent_injects_evidence_once),
        ("W.2 _stream_agent_events 接入证据（一次）", test_stream_injects_evidence_once),
        ("W.3 Evidence Builder 只构建一次", test_evidence_built_exactly_once),
        ("W.4 无工具调用不注入证据", test_no_evidence_when_no_tool_called),
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
