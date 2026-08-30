"""第十三阶段：Agent 执行逻辑服务化确定性测试。

覆盖：
- A 组 配置管理：AgentSettings 默认值与常量一致、from_env 读取环境变量、
  create_client 无 key 抛错 / 有 key 正确 / 无参调用兼容；
- B 组 Schema 与 Dispatch：TOOL_SCHEMAS 5 工具与 TOOL_DISPATCH 一一对应、结构完整；
- C 组 run_agent 编排（fake client + 打桩 TOOL_DISPATCH，不触网）：
  无工具调用、单轮并行多工具 + messages 序列、未知工具兜底、JSON 参数容错、
  TypeError / 通用异常兜底、API 异常、max_rounds、默认静默、progress 回调、settings 透传；
- D 组 main.py 再导出兼容：from main import 8 个名字全部可用、对象同一性、
  sampling 从 app.agent 导入、不再依赖 main.py；
- E 组 settings 注入（Phase 14）：system_prompt / tool_schemas 默认值与常量一致
  且 tool_schemas 为独立深拷贝；自定义 system_prompt / tool_schemas 透传到 API 调用。

运行：cd E:/github/ai-financial-agent && .venv/Scripts/python.exe tests/test_agent_orchestrator.py
"""

from __future__ import annotations

import io
import json
import os
import sys
import types
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import mock

# 确保能导入项目根目录下的 app 包
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.agent import (  # noqa: E402
    MAX_TOOL_ROUNDS,
    MODEL,
    SYSTEM_PROMPT,
    TOOL_DISPATCH,
    TOOL_SCHEMAS,
    AgentSettings,
    create_client,
    run_agent,
)
import app.agent.orchestrator as orchestrator_mod  # noqa: E402

_FAILURES: List[str] = []


def _run(name: str, fn) -> None:
    try:
        fn()
        print(f"  PASS  {name}")
    except AssertionError as exc:
        print(f"  FAIL  {name}: {exc}")
        _FAILURES.append(f"{name}: {exc}")
    except Exception as exc:  # noqa: BLE001 - 测试脚本捕获所有异常并计数
        print(f"  ERROR {name}: {type(exc).__name__}: {exc}")
        _FAILURES.append(f"{name}: {type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# fake OpenAI client（仅实现 run_agent 用到的接口）
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


# 打桩工具表：保证 run_agent 测试不触网、可确定断言
_FAKE_TOOLS = {
    "get_stock_price": lambda symbol="": {"symbol": symbol, "price": 100.0},
    "get_technical_analysis": lambda symbol="": {"symbol": symbol, "rsi": 50.0},
}

# Phase 20A：Router 默认全维度关闭，避免吞掉 fake 响应队列、干扰既有断言
_DEFAULT_ROUTE = {"needs_fundamental": False, "needs_quant": False, "needs_event": False}


# ---------------------------------------------------------------------------
# A. 配置管理
# ---------------------------------------------------------------------------

def test_constants_values() -> None:
    assert MODEL == "deepseek-v4-pro"
    assert MAX_TOOL_ROUNDS == 8


def test_settings_defaults_match_constants() -> None:
    cfg = AgentSettings()
    assert cfg.model == MODEL
    assert cfg.max_tool_rounds == MAX_TOOL_ROUNDS
    assert cfg.api_key is None


def test_settings_from_env() -> None:
    with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "env-key"}):
        assert AgentSettings.from_env().api_key == "env-key"
    with mock.patch.dict(os.environ):
        os.environ.pop("DEEPSEEK_API_KEY", None)
        assert AgentSettings.from_env().api_key is None


def test_create_client_no_key_raises() -> None:
    try:
        create_client(AgentSettings())
    except RuntimeError as exc:
        assert "DEEPSEEK_API_KEY" in str(exc)
    else:
        raise AssertionError("无 key 时应抛 RuntimeError")


def test_create_client_with_key() -> None:
    client = create_client(AgentSettings(api_key="test-key"))
    assert client.api_key == "test-key"
    assert str(client.base_url).startswith("https://api.deepseek.com")


def test_create_client_noarg_compat() -> None:
    # 兼容既有 from main import create_client 的无参调用（test_llm_output_quality 等）
    with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "env-key"}):
        client = create_client()
    assert client.api_key == "env-key"


# ---------------------------------------------------------------------------
# B. Schema 与 Dispatch
# ---------------------------------------------------------------------------

def test_schemas_match_dispatch() -> None:
    names = [s["function"]["name"] for s in TOOL_SCHEMAS]
    assert names == [
        "get_stock_price",
        "get_technical_analysis",
        "get_stock_fundamentals",
        "get_valuation_analysis",
        "get_stock_news",
    ]
    assert set(names) == set(TOOL_DISPATCH)
    assert len(names) == 5


def test_schema_structure() -> None:
    for schema in TOOL_SCHEMAS:
        fn = schema["function"]
        assert schema["type"] == "function"
        assert "parameters" in fn
        assert fn["parameters"]["required"] == ["symbol"]


def test_dispatch_all_callable() -> None:
    for name, fn in TOOL_DISPATCH.items():
        assert callable(fn), f"{name} 不可调用"


# ---------------------------------------------------------------------------
# C. run_agent 编排
# ---------------------------------------------------------------------------

def test_no_tool_call_returns_answer() -> None:
    client = _final_client("直接回答")
    result = run_agent(client, "简单问题")
    assert result.answer == "直接回答"
    assert result.tool_rounds == 0
    assert result.tool_calls == []
    assert result.max_rounds_reached is False
    assert result.error is None


def test_parallel_tools_then_final() -> None:
    with mock.patch.dict(orchestrator_mod.TOOL_DISPATCH, _FAKE_TOOLS, clear=True):
        client = _tool_then_final_client()
        result = run_agent(client, "分析贵州茅台 600519")

    assert result.answer == "最终回答"
    assert result.tool_rounds == 1
    assert result.max_rounds_reached is False
    assert [c.name for c in result.tool_calls] == ["get_stock_price", "get_technical_analysis"]
    assert [c.arguments for c in result.tool_calls] == [{"symbol": "600519"}] * 2
    assert result.tool_calls[0].result["price"] == 100.0
    assert result.tool_calls[1].result["rsi"] == 50.0

    # messages 历史序列：system -> user -> assistant(tool_calls) -> tool -> tool
    #   -> user（Phase 20B 结构化证据，仅注入一次，原始 tool 消息保留）
    calls = client.chat.completions.calls
    assert [m["role"] for m in calls[0]["messages"]] == ["system", "user"]
    second_roles = [m["role"] for m in calls[1]["messages"]]
    assert second_roles == ["system", "user", "assistant", "tool", "tool", "user"]
    tool_msgs = [m for m in calls[1]["messages"] if m["role"] == "tool"]
    assert tool_msgs[0]["tool_call_id"] == "call_price"
    assert '"price": 100.0' in tool_msgs[0]["content"]
    assert tool_msgs[1]["tool_call_id"] == "call_tech"
    assert '"rsi": 50.0' in tool_msgs[1]["content"]
    # 工具结果以 JSON 回传
    assert calls[1]["messages"][2]["tool_calls"][0]["id"] == "call_price"
    # 证据 user 消息：渲染结构化证据且只注入一次
    evidence_msgs = [m for m in calls[1]["messages"] if m["role"] == "user" and m["content"].startswith("以下是本轮工具调用得到的结构化证据")]
    assert len(evidence_msgs) == 1
    assert "【量化证据】" in evidence_msgs[0]["content"]
    assert "get_stock_price" in evidence_msgs[0]["content"]


def test_news_tool_agent_flow() -> None:
    """“最近有什么新闻”场景：Agent 串行调用 get_stock_news，
    新闻事实与发布时间经工具结果进入最终回答（不触网）。"""
    news_rows = [
        {"title": "贵州茅台发布2026年半年度报告", "summary": "公司实现营收保持增长。",
         "publish_date": "2026-08-20 09:30:00", "source": "东方财富"},
        {"title": "600519 获多家机构上调目标价", "summary": "多家机构看好公司长期价值。",
         "publish_date": "2026-08-19 15:00:00", "source": "财联社"},
    ]
    fake_news_tool = {
        "get_stock_news": lambda symbol="", limit=3: {
            "symbol": "600519.SH", "asset_type": "stock", "market": "A-share",
            "data_source": "Akshare", "fetched_at": "2026-08-25T00:00:00+00:00",
            "news_count": len(news_rows), "news": news_rows,
        }
    }

    class _NewsAwareCompletions(_FakeCompletions):
        """第二次调用时从工具结果 JSON 中提取新闻事实，生成体现其内容的回答。"""

        def create(self, **kwargs: Any) -> Any:
            if kwargs.get("response_format") == {"type": "json_object"}:
                return _resp(_msg(content=json.dumps(_DEFAULT_ROUTE)))
            self.calls.append(kwargs)
            if len(self.calls) >= 2:
                tool_msgs = [m for m in kwargs["messages"] if m["role"] == "tool"]
                news = json.loads(tool_msgs[0]["content"])["news"]
                titles = "；".join(n["title"] for n in news)
                dates = "；".join(n["publish_date"] for n in news)
                return _resp(_msg(content=(
                    f"【新闻动态】{titles}。\n"
                    f"【发布时间】{dates}。\n"
                    "上述新闻仅反映公开信息，可能受到相关事件影响，不构成投资建议。"
                )))
            return self._queue.pop(0)

    client = _FakeClient([])
    client.chat.completions = _NewsAwareCompletions([
        _resp(_msg(content=None, tool_calls=[
            _tool_call("call_news", "get_stock_news", '{"symbol": "600519", "limit": 3}'),
        ])),
    ])

    with mock.patch.dict(orchestrator_mod.TOOL_DISPATCH, fake_news_tool, clear=True):
        result = run_agent(client, "贵州茅台 600519 最近有什么新闻？")

    assert result.error is None
    assert result.tool_rounds == 1
    assert [c.name for c in result.tool_calls] == ["get_stock_news"]
    assert result.tool_calls[0].arguments == {"symbol": "600519", "limit": 3}
    assert result.tool_calls[0].result["news_count"] == 2
    # 最终回答体现新闻事实与发布时间
    assert "贵州茅台发布2026年半年度报告" in result.answer
    assert "2026-08-20 09:30:00" in result.answer
    assert "2026-08-19 15:00:00" in result.answer
    assert "可能受到相关事件影响" in result.answer
    # 工具结果以 JSON 回传给模型；Phase 20B 证据 user 消息在 tool 消息后注入一次
    calls = client.chat.completions.calls
    roles = [m["role"] for m in calls[1]["messages"]]
    assert roles == ["system", "user", "assistant", "tool", "user"]
    assert roles.count("tool") == 1
    assert roles.count("user") == 2  # 原始问题 + 证据上下文，未复制完整 Tool Result
    assert "以下是本轮工具调用得到的结构化证据" in calls[1]["messages"][-1]["content"]
    assert "【事件证据】" in calls[1]["messages"][-1]["content"]


def test_unknown_tool_fallback() -> None:
    client = _FakeClient([_resp(_msg(tool_calls=[_tool_call("c1", "nonexistent_tool", '{"symbol": "600519"}')]))])
    result = run_agent(client, "q")
    assert result.tool_calls[0].result == {
        "error": "未知工具: nonexistent_tool",
        "symbol": "600519",
    }


def test_invalid_json_arguments_fallback() -> None:
    with mock.patch.dict(orchestrator_mod.TOOL_DISPATCH, _FAKE_TOOLS, clear=True):
        client = _FakeClient([_resp(_msg(tool_calls=[_tool_call("c1", "get_stock_price", "{bad json")]))])
        result = run_agent(client, "q")
    assert result.tool_calls[0].arguments == {}
    # 空参 {} 落入 fake 工具默认参数 symbol=""，返回 {"symbol": "", "price": 100.0}
    assert result.tool_calls[0].result["price"] == 100.0


def test_type_error_fallback() -> None:
    def _strict(symbol: str) -> Dict[str, Any]:
        return {"symbol": symbol}

    with mock.patch.dict(orchestrator_mod.TOOL_DISPATCH, {"get_stock_price": _strict}, clear=True):
        client = _FakeClient([_resp(_msg(tool_calls=[_tool_call("c1", "get_stock_price", '{"unknown": 1}')]))])
        result = run_agent(client, "q")
    assert "工具参数错误" in result.tool_calls[0].result["error"]


def test_generic_exception_fallback() -> None:
    def _boom(**kwargs: Any) -> Dict[str, Any]:
        raise ValueError("boom")

    with mock.patch.dict(orchestrator_mod.TOOL_DISPATCH, {"get_stock_price": _boom}, clear=True):
        client = _FakeClient([_resp(_msg(tool_calls=[_tool_call("c1", "get_stock_price", '{"symbol": "600519"}')]))])
        result = run_agent(client, "q")
    assert "工具执行异常" in result.tool_calls[0].result["error"]
    assert "ValueError" in result.tool_calls[0].result["error"]


def test_api_exception_sets_error() -> None:
    class _BoomCompletions:
        def create(self, **kwargs: Any) -> Any:
            raise RuntimeError("boom")

    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=_BoomCompletions()))
    result = run_agent(client, "q")
    assert "DeepSeek API 调用失败" in result.error
    assert "RuntimeError" in result.error
    assert "boom" in result.error
    assert result.answer == ""


def test_max_rounds_reached() -> None:
    with mock.patch.dict(orchestrator_mod.TOOL_DISPATCH, _FAKE_TOOLS, clear=True):
        client = _FakeClient([
            _resp(_msg(tool_calls=[_tool_call(f"c{i}", "get_stock_price", '{"symbol": "600519"}')]))
            for i in range(2)
        ])
        result = run_agent(client, "q", settings=AgentSettings(max_tool_rounds=2))
    assert result.max_rounds_reached is True
    assert result.tool_rounds == 2
    assert len(client.chat.completions.calls) == 2
    assert result.answer == ""


def test_run_agent_silent_by_default() -> None:
    with mock.patch.dict(orchestrator_mod.TOOL_DISPATCH, _FAKE_TOOLS, clear=True):
        client = _tool_then_final_client()
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            run_agent(client, "分析贵州茅台 600519")
    assert buffer.getvalue() == ""


def test_progress_callback_lines() -> None:
    with mock.patch.dict(orchestrator_mod.TOOL_DISPATCH, _FAKE_TOOLS, clear=True):
        client = _tool_then_final_client()
        lines: List[str] = []
        result = run_agent(client, "分析贵州茅台 600519", progress=lines.append)
    assert lines == [
        "[第 1 轮] 模型请求调用 2 个工具",
        "  调用 get_stock_price{'symbol': '600519'}",
        "  调用 get_technical_analysis{'symbol': '600519'}",
    ]
    assert result.answer == "最终回答"


def test_settings_model_forwarded() -> None:
    client = _final_client("ok")
    result = run_agent(client, "q", settings=AgentSettings(model="test-model"))
    assert client.chat.completions.calls[0]["model"] == "test-model"
    assert result.answer == "ok"


# ---------------------------------------------------------------------------
# D. main.py 再导出兼容
# ---------------------------------------------------------------------------

def test_main_reexports() -> None:
    import main

    for name in (
        "run_agent",
        "create_client",
        "SYSTEM_PROMPT",
        "TOOL_SCHEMAS",
        "TOOL_DISPATCH",
        "MODEL",
        "MAX_TOOL_ROUNDS",
    ):
        assert hasattr(main, name), f"main 缺少 {name}"


def test_main_reexports_same_objects() -> None:
    import main

    assert main.SYSTEM_PROMPT is SYSTEM_PROMPT
    assert main.TOOL_SCHEMAS is TOOL_SCHEMAS
    assert main.TOOL_DISPATCH is TOOL_DISPATCH
    assert main.run_agent is run_agent
    assert main.MODEL == MODEL
    assert main.MAX_TOOL_ROUNDS == MAX_TOOL_ROUNDS
    # 唯一真源：run_agent 定义在 orchestrator，而非 main.py 副本
    assert run_agent.__module__ == "app.agent.orchestrator"


def test_main_import_preserves_system_prompt() -> None:
    # SYSTEM_PROMPT 逐字平移：既有关键短语必须仍在（test_output_quality 依赖）
    import main

    for phrase in ("工具使用规则", "不构成投资建议", "get_valuation_analysis"):
        assert phrase in main.SYSTEM_PROMPT


def test_sampling_imports_agent_not_main() -> None:
    # Phase 14：sampling 从 app.agent 复用常量/工具表，不再依赖 main.py
    import inspect

    import app.evaluation.sampling  # noqa: F401
    source = inspect.getsource(app.evaluation.sampling)
    assert "from app.agent import" in source
    assert "from main" not in source


# ---------------------------------------------------------------------------
# E. settings 注入（Phase 14：prompt / tool_schemas 解耦）
# ---------------------------------------------------------------------------

def test_settings_prompt_schema_defaults_isolated() -> None:
    cfg = AgentSettings()
    assert cfg.system_prompt == SYSTEM_PROMPT
    assert cfg.tool_schemas == TOOL_SCHEMAS
    # tool_schemas 为独立深拷贝，不共享可变全局对象
    assert cfg.tool_schemas is not TOOL_SCHEMAS
    cfg.tool_schemas.append({"type": "function", "function": {"name": "mutated"}})
    cfg.tool_schemas[0]["function"]["name"] = "mutated_inner"
    assert len(TOOL_SCHEMAS) == 5
    assert TOOL_SCHEMAS[0]["function"]["name"] == "get_stock_price"


def test_settings_system_prompt_injected() -> None:
    client = _final_client("ok")
    result = run_agent(client, "q", settings=AgentSettings(system_prompt="custom-prompt"))
    assert result.answer == "ok"
    assert client.chat.completions.calls[0]["messages"][0]["content"] == "custom-prompt"


def test_settings_tool_schemas_injected() -> None:
    custom_schemas = [{"type": "function", "function": {"name": "custom_tool"}}]
    client = _final_client("ok")
    result = run_agent(client, "q", settings=AgentSettings(tool_schemas=custom_schemas))
    assert result.answer == "ok"
    assert client.chat.completions.calls[0]["tools"] == custom_schemas


def main() -> None:
    print("=== tests/test_agent_orchestrator.py Agent 执行服务化测试 ===")
    tests = [
        ("A.1 常量值", test_constants_values),
        ("A.2 AgentSettings 默认值与常量一致", test_settings_defaults_match_constants),
        ("A.3 from_env 读取环境变量", test_settings_from_env),
        ("A.4 create_client 无 key 抛错", test_create_client_no_key_raises),
        ("A.5 create_client 有 key 正确", test_create_client_with_key),
        ("A.6 create_client 无参调用兼容", test_create_client_noarg_compat),
        ("B.1 Schema 与 Dispatch 一一对应", test_schemas_match_dispatch),
        ("B.2 Schema 结构完整", test_schema_structure),
        ("B.3 Dispatch 全部可调用", test_dispatch_all_callable),
        ("C.1 无工具调用直接返回", test_no_tool_call_returns_answer),
        ("C.2 并行多工具 + messages 序列", test_parallel_tools_then_final),
        ("C.3 未知工具兜底", test_unknown_tool_fallback),
        ("C.4 JSON 参数容错", test_invalid_json_arguments_fallback),
        ("C.5 TypeError 兜底", test_type_error_fallback),
        ("C.6 通用异常兜底", test_generic_exception_fallback),
        ("C.7 API 异常设置 error", test_api_exception_sets_error),
        ("C.8 max_rounds 触顶", test_max_rounds_reached),
        ("C.9 默认静默（服务化）", test_run_agent_silent_by_default),
        ("C.10 progress 回调与现状一致", test_progress_callback_lines),
        ("C.11 settings.model 透传", test_settings_model_forwarded),
        ("C.12 新闻工具串行调用 + 回答体现新闻事实与发布时间", test_news_tool_agent_flow),
        ("D.1 main 再导出 8 名字", test_main_reexports),
        ("D.2 再导出同一对象（唯一真源）", test_main_reexports_same_objects),
        ("D.3 SYSTEM_PROMPT 关键短语保留", test_main_import_preserves_system_prompt),
        ("D.4 sampling 从 app.agent 导入、不再依赖 main", test_sampling_imports_agent_not_main),
        ("E.1 settings 默认 prompt/schema 独立深拷贝", test_settings_prompt_schema_defaults_isolated),
        ("E.2 自定义 system_prompt 注入", test_settings_system_prompt_injected),
        ("E.3 自定义 tool_schemas 注入", test_settings_tool_schemas_injected),
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
