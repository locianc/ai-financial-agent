"""Phase 20A：Supervisor-Worker 多 Agent 底层框架 —— Router 路由与 Worker 工具隔离测试。

覆盖：
- _route_question 三分类意图路由：fundamental / quant / event 的开启与关闭；
  缺失字段默认 False；非法 JSON / API 异常安全回退全 True（宁可多调用工具，
  不能因路由错误导致数据缺失）；
- Worker 域隔离结构：WORKER_TOOLS / WORKER_TOOL_DISPATCH 正确派生，
  TOOL_DISPATCH 完整保留 5 个工具（不复制实现）；
- _select_tool_schemas：按路由裁剪 Schema，自定义注入 Schema 不受路由影响；
- ROUTER_PROMPT 关键约束文本；
- run_agent / _stream_agent_events wiring：路由结果决定传入 LLM 的 tools 参数。

运行：cd E:/github/ai-financial-agent && .venv/Scripts/python.exe tests/test_router.py
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.agent import (  # noqa: E402
    TOOL_DISPATCH,
    TOOL_SCHEMAS,
    AgentSettings,
    run_agent,
)
import app.agent.orchestrator as orchestrator_mod  # noqa: E402
from app.agent.workers import (  # noqa: E402
    WORKER_PROMPTS,
    WORKER_TOOLS,
    get_worker_prompt,
    get_worker_tools,
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


def _resp(content: str) -> Any:
    """非流式响应：Router 只读取 choices[0].message.content。"""
    return types.SimpleNamespace(
        choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=content, tool_calls=None))]
    )


def _schema_names(schemas: List[Dict[str, Any]]) -> List[str]:
    return [s["function"]["name"] for s in schemas]


# ---------------------------------------------------------------------------
# fake OpenAI client（Router 识别 + 主循环响应）
# ---------------------------------------------------------------------------

class _RouterClient:
    """按 user 问题返回对应路由 JSON 的 fake client。

    routes: {问题: 路由 JSON 字符串}；未命中返回 "not json"（触发非法 JSON 回退）。
    fail=True 时 Router 调用直接抛异常（触发 API 异常回退）。
    主循环固定返回一条无工具调用的最终回答。
    """

    def __init__(self, routes: Dict[str, str], fail: bool = False, answer: str = "最终回答") -> None:
        self._routes = routes
        self._fail = fail
        self._answer = answer
        self.calls: List[Dict[str, Any]] = []
        # Router 通过 client.chat.completions.create 调用
        self.chat = types.SimpleNamespace(completions=self)

    def create(self, **kwargs: Any) -> Any:
        if kwargs.get("response_format") == {"type": "json_object"}:
            if self._fail:
                raise RuntimeError("router boom")
            user_msgs = [m for m in kwargs["messages"] if m["role"] == "user"]
            question = user_msgs[-1]["content"] if user_msgs else ""
            return _resp(self._routes.get(question, "not json"))
        self.calls.append(kwargs)
        return _resp(self._answer)


# ---------------------------------------------------------------------------
# 1. _route_question 三分类与安全回退
# ---------------------------------------------------------------------------

def test_route_fundamental_and_quant() -> None:
    """Case 1：基本面 + 技术面 -> fundamental=True, quant=True, event=False。"""
    client = _RouterClient({"分析贵州茅台的基本面和技术面": json.dumps(
        {"needs_fundamental": True, "needs_quant": True, "needs_event": False})})
    route = orchestrator_mod._route_question(client, "分析贵州茅台的基本面和技术面")
    assert route == {"needs_fundamental": True, "needs_quant": True, "needs_event": False}


def test_route_event_only() -> None:
    """Case 2：新闻事件 -> 仅 event=True。"""
    client = _RouterClient({"贵州茅台最近有什么新闻？": json.dumps(
        {"needs_fundamental": False, "needs_quant": False, "needs_event": True})})
    route = orchestrator_mod._route_question(client, "贵州茅台最近有什么新闻？")
    assert route == {"needs_fundamental": False, "needs_quant": False, "needs_event": True}


def test_route_fundamental_only() -> None:
    """Case 3：PE/ROE -> 仅 fundamental=True。"""
    client = _RouterClient({"贵州茅台现在的PE、ROE怎么样？": json.dumps(
        {"needs_fundamental": True, "needs_quant": False, "needs_event": False})})
    route = orchestrator_mod._route_question(client, "贵州茅台现在的PE、ROE怎么样？")
    assert route == {"needs_fundamental": True, "needs_quant": False, "needs_event": False}


def test_route_invalid_json_fallback_all_true() -> None:
    """Case 4：非法 JSON -> 安全回退全 True（宁可多调用工具，不能数据缺失）。"""
    client = _RouterClient({})  # 默认返回 "not json"
    route = orchestrator_mod._route_question(client, "任意问题")
    assert route == {"needs_fundamental": True, "needs_quant": True, "needs_event": True}


def test_route_missing_fields_default_false() -> None:
    client = _RouterClient({"q": json.dumps({"needs_fundamental": True})})
    route = orchestrator_mod._route_question(client, "q")
    assert route == {"needs_fundamental": True, "needs_quant": False, "needs_event": False}


def test_route_api_exception_fallback_all_true() -> None:
    client = _RouterClient({}, fail=True)
    route = orchestrator_mod._route_question(client, "q")
    assert route == {"needs_fundamental": True, "needs_quant": True, "needs_event": True}


# ---------------------------------------------------------------------------
# 2. Worker 域隔离结构
# ---------------------------------------------------------------------------

def test_worker_tools_mapping() -> None:
    assert WORKER_TOOLS == {
        "fundamental": ("get_stock_fundamentals", "get_valuation_analysis"),
        "quant": ("get_stock_price", "get_technical_analysis"),
        "event": ("get_stock_news",),
    }
    for worker, tools in WORKER_TOOLS.items():
        assert get_worker_tools(worker) == tools
        assert get_worker_prompt(worker), f"{worker} 缺少 Worker Prompt"
        assert "get_" in get_worker_prompt(worker) or len(get_worker_prompt(worker)) > 20
    assert get_worker_tools("unknown") == ()
    assert get_worker_prompt("unknown") == ""


def test_worker_tool_dispatch_derived() -> None:
    dispatch = orchestrator_mod.WORKER_TOOL_DISPATCH
    assert set(dispatch) == {"fundamental", "quant", "event"}
    for worker, tools in WORKER_TOOLS.items():
        assert set(dispatch[worker]) == set(tools)
        for name in tools:
            # 引用同一实现，不复制工具函数
            assert dispatch[worker][name] is TOOL_DISPATCH[name]


def test_tool_dispatch_preserved() -> None:
    # 完整 TOOL_DISPATCH / TOOL_SCHEMAS 保留 5 个工具，未被 Worker 隔离破坏
    names = _schema_names(TOOL_SCHEMAS)
    assert names == [
        "get_stock_price",
        "get_technical_analysis",
        "get_stock_fundamentals",
        "get_valuation_analysis",
        "get_stock_news",
    ]
    assert set(TOOL_DISPATCH) == set(names)
    assert len(TOOL_DISPATCH) == 5
    assert len(TOOL_SCHEMAS) == 5


def test_router_prompt_key_phrases() -> None:
    prompt = orchestrator_mod.ROUTER_PROMPT
    for phrase in ("needs_fundamental", "needs_quant", "needs_event", "严格 JSON", "不要输出 JSON 之外的任何内容"):
        assert phrase in prompt, f"ROUTER_PROMPT 缺少：{phrase}"


def test_worker_prompts_are_short() -> None:
    # Worker Prompt 短小，不重复完整 SYSTEM_PROMPT（最终回答由 SYSTEM_PROMPT 负责）
    for worker, prompt in WORKER_PROMPTS.items():
        assert len(prompt) < len(orchestrator_mod.SYSTEM_PROMPT)
        assert "投资建议" in prompt or "买入" in prompt  # 均含不荐股约束


# ---------------------------------------------------------------------------
# 3. _select_tool_schemas 按路由裁剪
# ---------------------------------------------------------------------------

def test_select_tool_schemas_all_true() -> None:
    schemas = orchestrator_mod._select_tool_schemas(
        {"needs_fundamental": True, "needs_quant": True, "needs_event": True})
    assert _schema_names(schemas) == [
        "get_stock_price",
        "get_technical_analysis",
        "get_stock_fundamentals",
        "get_valuation_analysis",
        "get_stock_news",
    ]


def test_select_tool_schemas_fundamental_only() -> None:
    schemas = orchestrator_mod._select_tool_schemas(
        {"needs_fundamental": True, "needs_quant": False, "needs_event": False})
    assert _schema_names(schemas) == ["get_stock_fundamentals", "get_valuation_analysis"]


def test_select_tool_schemas_quant_only() -> None:
    schemas = orchestrator_mod._select_tool_schemas(
        {"needs_fundamental": False, "needs_quant": True, "needs_event": False})
    assert _schema_names(schemas) == ["get_stock_price", "get_technical_analysis"]


def test_select_tool_schemas_event_only() -> None:
    schemas = orchestrator_mod._select_tool_schemas(
        {"needs_fundamental": False, "needs_quant": False, "needs_event": True})
    assert _schema_names(schemas) == ["get_stock_news"]


def test_select_tool_schemas_custom_injected_kept() -> None:
    # 自定义注入 Schema 不在任何 Worker 域内：路由全关也保留（E.3 注入契约）
    custom = [{"type": "function", "function": {"name": "custom_tool"}}]
    schemas = orchestrator_mod._select_tool_schemas(
        {"needs_fundamental": False, "needs_quant": False, "needs_event": False}, custom)
    assert schemas == custom


def test_select_tool_schemas_does_not_mutate_original() -> None:
    original = _schema_names(TOOL_SCHEMAS)
    orchestrator_mod._select_tool_schemas(
        {"needs_fundamental": True, "needs_quant": False, "needs_event": False})
    assert _schema_names(TOOL_SCHEMAS) == original
    assert len(TOOL_SCHEMAS) == 5


# ---------------------------------------------------------------------------
# 4. run_agent / _stream_agent_events wiring：路由 -> 裁剪后的 tools
# ---------------------------------------------------------------------------

def test_run_agent_forwards_routed_tools() -> None:
    client = _RouterClient({"分析贵州茅台的基本面和技术面": json.dumps(
        {"needs_fundamental": True, "needs_quant": True, "needs_event": False})})
    with mock.patch.dict(orchestrator_mod.TOOL_DISPATCH, {
        "get_stock_price": lambda symbol="": {"symbol": symbol, "price": 100.0},
        "get_technical_analysis": lambda symbol="": {"symbol": symbol, "rsi": 50.0},
        "get_stock_fundamentals": lambda symbol="": {"symbol": symbol, "roe": 20.0},
        "get_valuation_analysis": lambda symbol="": {"symbol": symbol, "pe": 20.0},
        "get_stock_news": lambda symbol="", limit=3: {"symbol": symbol, "news": []},
    }, clear=True):
        result = run_agent(client, "分析贵州茅台的基本面和技术面")
    assert result.answer == "最终回答"
    assert result.error is None
    # 主循环收到的 tools 按路由裁剪：fundamental + quant -> 4 个，event 排除
    assert _schema_names(client.calls[0]["tools"]) == [
        "get_stock_price",
        "get_technical_analysis",
        "get_stock_fundamentals",
        "get_valuation_analysis",
    ]


def test_run_agent_router_fallback_all_tools() -> None:
    # Router 非法 JSON -> 全 True 回退 -> 5 个工具全部保留
    client = _RouterClient({})
    with mock.patch.dict(orchestrator_mod.TOOL_DISPATCH, {
        "get_stock_price": lambda symbol="": {"symbol": symbol, "price": 100.0},
        "get_technical_analysis": lambda symbol="": {"symbol": symbol, "rsi": 50.0},
        "get_stock_fundamentals": lambda symbol="": {"symbol": symbol, "roe": 20.0},
        "get_valuation_analysis": lambda symbol="": {"symbol": symbol, "pe": 20.0},
        "get_stock_news": lambda symbol="", limit=3: {"symbol": symbol, "news": []},
    }, clear=True):
        result = run_agent(client, "任意问题")
    assert result.answer == "最终回答"
    assert _schema_names(client.calls[0]["tools"]) == [
        "get_stock_price",
        "get_technical_analysis",
        "get_stock_fundamentals",
        "get_valuation_analysis",
        "get_stock_news",
    ]


class _StreamingClient:
    """流式 fake：Router 返回路由 JSON，主循环返回单片文本（无工具调用）。"""

    def __init__(self, route_json: str, final_answer: str = "流式最终回答") -> None:
        self._route_json = route_json
        self._final_answer = final_answer
        self.calls: List[Dict[str, Any]] = []
        self.chat = types.SimpleNamespace(completions=self)

    def create(self, **kwargs: Any) -> Any:
        if kwargs.get("response_format") == {"type": "json_object"}:
            return _resp(self._route_json)
        self.calls.append(kwargs)
        return [types.SimpleNamespace(
            choices=[types.SimpleNamespace(
                delta=types.SimpleNamespace(content=self._final_answer, tool_calls=None))])]


def test_stream_events_route_filters_tools() -> None:
    client = _StreamingClient(json.dumps(
        {"needs_fundamental": True, "needs_quant": False, "needs_event": False}))
    events = list(orchestrator_mod._stream_agent_events(client, "贵州茅台现在的PE、ROE怎么样？"))
    assert [event_type for event_type, _ in events] == ["token", "__result__"]
    assert events[0][1]["content"] == "流式最终回答"
    assert events[1][1]["answer"] == "流式最终回答"
    assert events[1][1]["error"] is None
    # 主循环 tools 按路由裁剪为 fundamental 域
    assert _schema_names(client.calls[0]["tools"]) == [
        "get_stock_fundamentals",
        "get_valuation_analysis",
    ]


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def main() -> None:
    print("=== tests/test_router.py Phase 20A Router 路由与 Worker 工具隔离测试 ===")
    tests = [
        ("R.1 _route_question：基本面+技术面", test_route_fundamental_and_quant),
        ("R.2 _route_question：新闻事件", test_route_event_only),
        ("R.3 _route_question：PE/ROE 基本面", test_route_fundamental_only),
        ("R.4 _route_question：非法 JSON 回退全 True", test_route_invalid_json_fallback_all_true),
        ("R.5 _route_question：缺失字段默认 False", test_route_missing_fields_default_false),
        ("R.6 _route_question：API 异常回退全 True", test_route_api_exception_fallback_all_true),
        ("S.1 WORKER_TOOLS 域映射与 helpers", test_worker_tools_mapping),
        ("S.2 WORKER_TOOL_DISPATCH 正确派生、引用同一实现", test_worker_tool_dispatch_derived),
        ("S.3 TOOL_DISPATCH/TOOL_SCHEMAS 完整保留 5 工具", test_tool_dispatch_preserved),
        ("S.4 ROUTER_PROMPT 关键约束文本", test_router_prompt_key_phrases),
        ("S.5 Worker Prompt 短小、不重复 SYSTEM_PROMPT", test_worker_prompts_are_short),
        ("T.1 _select_tool_schemas 全 True 保留 5 工具", test_select_tool_schemas_all_true),
        ("T.2 _select_tool_schemas fundamental 裁剪", test_select_tool_schemas_fundamental_only),
        ("T.3 _select_tool_schemas quant 裁剪", test_select_tool_schemas_quant_only),
        ("T.4 _select_tool_schemas event 裁剪", test_select_tool_schemas_event_only),
        ("T.5 _select_tool_schemas 自定义注入保留", test_select_tool_schemas_custom_injected_kept),
        ("T.6 _select_tool_schemas 不修改原始", test_select_tool_schemas_does_not_mutate_original),
        ("W.1 run_agent 按路由透传裁剪 tools", test_run_agent_forwards_routed_tools),
        ("W.2 run_agent 路由回退全工具", test_run_agent_router_fallback_all_tools),
        ("W.3 _stream_agent_events 按路由裁剪 tools", test_stream_events_route_filters_tools),
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
