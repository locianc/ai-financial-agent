"""第十阶段第二阶段：真实 Agent 采样器。

使用真实 DeepSeek Agent + 真实 Tool Calling（TOOL_SCHEMAS / TOOL_DISPATCH /
SYSTEM_PROMPT 均从 app.agent 复用，不修改其逻辑）对单个 Evaluation case 采样，
返回结构化审计记录。

记录字段（与第一阶段验收要求一致）：
- case_id / question / category / symbol
- tools_called：有序的工具调用日志（round / name / arguments / result / status）
- tool_results：{tool_name: result_dict} 合并视图（同名工具取最后一次），
  供 Deterministic Validator 与 LLM Judge 消费；原始结果保留在 tools_called 中
- final_output：Agent 最终回答文本
- latency_ms：整次采样总耗时
- input_tokens / output_tokens / total_tokens：各轮 usage 求和；
  任一轮 API 未返回 usage 时记为 null（不估算）
- timestamp / usage_detail / reached_max_rounds / error

约束：
- 不修改 app.agent 与任何金融数据工具核心逻辑；
- API 错误、限流、权限错误、达到最大轮数均如实记录，不吞异常。
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from openai import OpenAI

from app.agent import MAX_TOOL_ROUNDS, MODEL, SYSTEM_PROMPT, TOOL_DISPATCH, TOOL_SCHEMAS


def _build_messages(question: str) -> List[Dict[str, Any]]:
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]


def _execute_tool(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """执行单个工具，任何异常都转成 {error, symbol} 结构，不向外抛。"""
    if name not in TOOL_DISPATCH:
        return {"error": f"未知工具: {name}", "symbol": arguments.get("symbol", "")}
    try:
        return TOOL_DISPATCH[name](**arguments)
    except TypeError as exc:
        return {"error": f"工具参数错误: {exc}", "symbol": arguments.get("symbol", "")}
    except Exception as exc:  # noqa: BLE001 - 如实记录工具异常
        return {
            "error": f"工具执行异常: {type(exc).__name__}: {exc}",
            "symbol": arguments.get("symbol", ""),
        }


def sample_agent_run(client: OpenAI, case: Dict[str, Any]) -> Dict[str, Any]:
    """对单个 case 运行真实 Agent 工具调用流程，返回结构化采样记录。"""
    case_id = case.get("case_id") or "case"
    question = case["question"]
    started = time.perf_counter()
    messages = _build_messages(question)

    tools_called: List[Dict[str, Any]] = []
    usage_list: List[Dict[str, Any]] = []
    final_output: Optional[str] = None
    error: Optional[Dict[str, Any]] = None
    reached_max_rounds = False

    for round_index in range(1, MAX_TOOL_ROUNDS + 1):
        try:
            response = client.chat.completions.create(
                model=MODEL,
                messages=messages,
                tools=TOOL_SCHEMAS,
            )
        except Exception as exc:  # noqa: BLE001 - API 错误如实记录
            error = {
                "phase": "api_call",
                "round": round_index,
                "type": type(exc).__name__,
                "message": str(exc),
            }
            break

        usage = response.usage
        if usage is not None:
            usage_list.append(
                {
                    "round": round_index,
                    "prompt_tokens": usage.prompt_tokens,
                    "completion_tokens": usage.completion_tokens,
                    "total_tokens": usage.total_tokens,
                }
            )

        message = response.choices[0].message
        if not message.tool_calls:
            final_output = message.content or ""
            break

        # 模型请求调用工具：assistant 消息入历史，随后执行全部工具。
        messages.append(message)
        for tool_call in message.tool_calls:
            name = tool_call.function.name
            try:
                arguments = json.loads(tool_call.function.arguments or "{}")
            except json.JSONDecodeError as exc:
                arguments = {}
                tools_called.append(
                    {
                        "round": round_index,
                        "name": name,
                        "arguments": arguments,
                        "result": None,
                        "status": "arg_parse_error",
                        "error": str(exc),
                    }
                )
            else:
                result = _execute_tool(name, arguments)
                tools_called.append(
                    {
                        "round": round_index,
                        "name": name,
                        "arguments": arguments,
                        "result": result,
                        "status": "ok" if "error" not in result else "tool_error",
                    }
                )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": json.dumps(tools_called[-1].get("result") or {}, ensure_ascii=False),
                }
            )
    else:
        # for 循环正常耗尽（未 break）：模型在最后一轮仍请求工具。
        reached_max_rounds = True
        error = {
            "phase": "max_rounds",
            "type": "MaxToolRoundsExceeded",
            "message": f"达到最大工具调用轮数 {MAX_TOOL_ROUNDS}，未得到最终回答",
        }

    # Token 统计：所有轮均有 usage 才求和；否则 null（不估算）。
    if usage_list and all("total_tokens" in u for u in usage_list):
        input_tokens = sum(u["prompt_tokens"] for u in usage_list)
        output_tokens = sum(u["completion_tokens"] for u in usage_list)
        total_tokens = sum(u["total_tokens"] for u in usage_list)
    else:
        input_tokens = output_tokens = total_tokens = None

    # 同名工具取最后一次结果，作为 Validator / Judge 的合并视图。
    merged_results: Dict[str, Any] = {}
    for call in tools_called:
        if call["status"] == "ok" and call["result"] is not None:
            merged_results[call["name"]] = call["result"]

    return {
        "case_id": case_id,
        "category": case.get("category"),
        "category_name": case.get("category_name"),
        "question": question,
        "symbol": case.get("symbol"),
        "tools_called": tools_called,
        "tool_results": merged_results,
        "final_output": final_output,
        "latency_ms": round((time.perf_counter() - started) * 1000, 1),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "usage_detail": usage_list,
        "reached_max_rounds": reached_max_rounds,
        "error": error,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
