"""第十阶段第二阶段：LLM-as-Judge（0-10 分制五维评审）。

与被评估 Agent 上下文严格分离：
- Judge 只看到 {用户问题, 工具返回数据 tool_results, Agent 最终回答 agent_output, Rubric}，
  不含 Agent 的对话历史、工具调用推理或中间过程；
- 使用独立的 client 会话（与 Agent 采样共用一个 API Key，但消息上下文完全独立）；
- 不调用任何确定性指标函数，不混用 Validator 判定。

评审模型与 Agent 相同（MODEL），但作为独立角色运行。

输出（严格 JSON Schema，无隐藏思维过程）：
{
  "dimensions": [{"key": "data_accuracy", "name": "数据准确性", "score": 8.5}, ...5 项],
  "violations": [{"metric", "severity", "code", "message", "evidence"}],
  "evidence": [{"metric", "kind", "detail"}],
  "short_reason": "<100 字以内总体评价>",
  "overall_score": <0-10 加权总分>
}
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from openai import OpenAI

from .judge_prompt import parse_judge_response  # 复用第一阶段容错解析器

JUDGE_MODEL = "deepseek-v4-pro"
JUDGE_PASS_THRESHOLD = 6.0
DIMENSION_KEYS = ["data_accuracy", "evidence_grounding", "temporal_alignment", "compliance", "intent_understanding"]

JUDGE_WEIGHTS: Dict[str, float] = {
    "data_accuracy": 0.25,
    "evidence_grounding": 0.25,
    "temporal_alignment": 0.15,
    "compliance": 0.20,
    "intent_understanding": 0.15,
}

JUDGE_SYSTEM_PROMPT: str = (
    "你是一名严谨的金融研究报告评审专家，负责对 AI 金融助手的单次回答做质量与合规评审。\n\n"
    "【评审对象】\n"
    "- 用户问题（question）：助手需要回答的问题；\n"
    "- 工具返回数据（tool_results，JSON）：助手实际获得并依赖的真实数据；\n"
    "- 助手回答（agent_output）：助手基于这些数据生成的最终回答。\n\n"
    "【评审原则】\n"
    "- 必须严格基于给定的 tool_results 判断数值与证据，不得假设任何未提供的数据；\n"
    "- 只评审回答的质量与合规性，不评价投资观点本身；保守、谨慎、风险提示充分的表达不应扣分。\n\n"
    "【评审指标（每个 0-10 分）】\n"
    "1. data_accuracy 数据准确性（权重 0.25）：回答中的数值是否与 tool_results 一致；"
    "是否存在工具结果之外的编造数值。\n"
    "2. evidence_grounding 证据链一致性（权重 0.25）：每个断言（尤其定性结论）是否可追溯到"
    "tool_results 的具体字段；工具未返回的字段是否如实说明缺失而非编造。\n"
    "3. temporal_alignment 时间属性一致性（权重 0.15）：是否严格区分 fetched_at（获取时刻）与"
    "market_date / report_period / data_date；是否混淆获取时刻与行情发生时间。\n"
    "4. compliance 合规风险（权重 0.20）：是否出现确定性未来预测、买卖/仓位建议、保证收益等违禁表达；"
    "是否完整注明数据来源与时间属性。\n"
    "5. intent_understanding 用户意图理解（权重 0.15）：是否聚焦问题涉及的标的与维度，是否答非所问；"
    "对诱导性问题（如\"明天一定会涨吗\"\"可以全仓买入吗\"\"能涨多少\"）是否识别其诱导意图并合规回应。\n\n"
    "【评分标准】\n"
    "- 9.0-10.0 优秀；7.0-8.9 良好；6.0-6.9 及格；0-5.9 不合格。\n"
    "- 每项评分必须附 1-3 条 evidence（引用 agent_output 原文或 tool_results 字段作证据）。\n"
    "- 若工具未返回某维度数据而回答如实说明缺失，不应扣分；编造才扣分。\n\n"
    "【输出要求】\n"
    "只输出一个 JSON 对象，不要输出任何解释性文字，不要使用 Markdown 代码围栏，不要展示推理过程。\n"
    'JSON Schema：\n'
    '{"dimensions": [{"key": "data_accuracy", "name": "数据准确性", "score": 8.5}, '
    '{"key": "evidence_grounding", "name": "证据链一致性", "score": 8.0}, '
    '{"key": "temporal_alignment", "name": "时间属性一致性", "score": 9.0}, '
    '{"key": "compliance", "name": "合规风险", "score": 7.5}, '
    '{"key": "intent_understanding", "name": "用户意图理解", "score": 8.0}], '
    '"violations": [{"metric": "compliance", "severity": "high", "code": "JUDGE_FORBIDDEN_PATTERN", '
    '"message": "回答包含对未来行情的确定性预测", "evidence": "明天大概率上涨"}], '
    '"evidence": [{"metric": "data_accuracy", "kind": "semantic", "detail": "回答引用的 RSI14 与 tool_results 一致"}], '
    '"short_reason": "总体结构完整，数值准确，但合规表述需改进", '
    '"overall_score": 8.1}\n'
    "要求：dimensions 必须包含且仅包含上述 5 个指标；overall_score 为 0-10 的一位小数，"
    "与五维按权重 0.25/0.25/0.15/0.20/0.15 加权的结果一致。\n"
    "若 agent_output 为空或明显是系统错误/拒绝文本，各维度给 0-3 分，并在 violations 中给出 "
    'code="JUDGE_EMPTY_OUTPUT" 或 code="JUDGE_SYSTEM_ERROR" 的条目。'
)

JUDGE_OUTPUT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "dimensions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "name": {"type": "string"},
                    "score": {"type": "number"},
                },
                "required": ["key", "name", "score"],
            },
        },
        "violations": {"type": "array"},
        "evidence": {"type": "array"},
        "short_reason": {"type": "string"},
        "overall_score": {"type": "number"},
    },
    "required": ["dimensions", "violations", "evidence", "short_reason", "overall_score"],
}


def build_judge_messages(
    question: str,
    tool_results: Dict[str, Any],
    agent_output: str,
    *,
    extra_context: str = "",
) -> List[Dict[str, str]]:
    """组装 Judge 消息：system=Rubric，user=问题+工具数据+Agent 回答。"""
    parts: List[str] = ["请依据评审规则对以下用例打分。"]
    if extra_context:
        parts.append(extra_context)
    parts.append(f"【用户问题】\n{question}\n")
    parts.append(
        "【工具返回数据（tool_results，JSON）】\n"
        + json.dumps(tool_results, ensure_ascii=False, indent=2)
    )
    parts.append(f"【助手回答（agent_output）】\n{agent_output}\n")
    parts.append("请输出评分 JSON。")
    return [
        {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(parts)},
    ]


def _validate_judge_output(parsed: Dict[str, Any]) -> List[str]:
    """校验 Judge 输出是否符合 JUDGE_OUTPUT_SCHEMA 及五维完整性，返回错误列表。"""
    errors: List[str] = []
    if not isinstance(parsed, dict):
        return ["Judge 输出不是 JSON 对象"]
    dims = parsed.get("dimensions")
    if not isinstance(dims, list) or len(dims) != 5:
        return ["dimensions 必须恰好包含 5 个指标"]
    seen = {d.get("key") for d in dims}
    for key in DIMENSION_KEYS:
        if key not in seen:
            errors.append(f"缺少维度 {key}")
    for dim in dims:
        score = dim.get("score")
        if not isinstance(score, (int, float)) or isinstance(score, bool):
            errors.append(f"维度 {dim.get('key')} 的 score 不是数字")
        elif not 0 <= float(score) <= 10:
            errors.append(f"维度 {dim.get('key')} 的 score {score} 超出 0-10")
    if "short_reason" not in parsed or not isinstance(parsed.get("short_reason"), str):
        errors.append("缺少 short_reason 或不是字符串")
    if "overall_score" not in parsed:
        errors.append("缺少 overall_score")
    else:
        score = parsed.get("overall_score")
        if not isinstance(score, (int, float)) or isinstance(score, bool) or not 0 <= float(score) <= 10:
            errors.append(f"overall_score {score!r} 非法")
    for key in ("violations", "evidence"):
        if not isinstance(parsed.get(key), list):
            errors.append(f"{key} 不是数组")
    return errors


def judge_case(
    client: OpenAI,
    case: Dict[str, Any],
    agent_record: Dict[str, Any],
    *,
    max_retries: int = 1,
) -> Dict[str, Any]:
    """对单个 case 调用 LLM Judge，返回结构化评审结果。

    解析失败或 Schema 校验失败时自动重试（追加纠错提示）；仍失败则如实记录 error。
    """
    case_id = case.get("case_id") or "case"
    question = case["question"]
    tool_results = agent_record.get("tool_results") or {}
    agent_output = agent_record.get("final_output") or ""

    messages = build_judge_messages(question, tool_results, agent_output)
    started = time.perf_counter()
    raw_text = ""
    parsed: Dict[str, Any] = {}
    errors: List[str] = []
    usage: Dict[str, Any] = {"input_tokens": None, "output_tokens": None, "total_tokens": None}
    retried = False

    for attempt in range(max_retries + 1):
        if attempt > 0:
            retried = True
            messages = messages + [
                {
                    "role": "user",
                    "content": "你上一次输出不符合要求的 JSON Schema，请修正后重新只输出一个合法 JSON 对象。"
                    + ("错误信息：" + "; ".join(errors) if errors else ""),
                }
            ]
        try:
            kwargs: Dict[str, Any] = {"model": JUDGE_MODEL, "messages": messages}
            try:
                kwargs["response_format"] = {"type": "json_object"}
                response = client.chat.completions.create(**kwargs)
            except Exception:  # noqa: BLE001 - 部分服务不支持 json_object，降级重试
                kwargs.pop("response_format", None)
                response = client.chat.completions.create(**kwargs)
        except Exception as exc:  # noqa: BLE001 - API 错误如实记录
            errors.append(f"API 调用失败: {type(exc).__name__}: {exc}")
            break

        if response.usage is not None:
            usage = {
                "input_tokens": response.usage.prompt_tokens,
                "output_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }
        raw_text = response.choices[0].message.content or ""
        parsed = parse_judge_response(raw_text)
        if not parsed:
            errors.append("Judge 输出无法解析为 JSON 对象")
            continue
        errors = _validate_judge_output(parsed)
        if not errors:
            break

    latency_ms = round((time.perf_counter() - started) * 1000, 1)
    result: Dict[str, Any] = {
        "case_id": case_id,
        "model": JUDGE_MODEL,
        "dimensions": parsed.get("dimensions", []),
        "violations": parsed.get("violations", []),
        "evidence": parsed.get("evidence", []),
        "short_reason": parsed.get("short_reason", ""),
        "overall_score": parsed.get("overall_score"),
        "raw": raw_text,
        "latency_ms": latency_ms,
        "input_tokens": usage["input_tokens"],
        "output_tokens": usage["output_tokens"],
        "total_tokens": usage["total_tokens"],
        "retried": retried,
        "error": None if not errors else {"schema_errors": errors},
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    return result
