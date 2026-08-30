"""第十阶段：Agent Evaluation 评估主入口与 Evaluation JSON Schema。

evaluate(question, tool_results, agent_output) 单用例评估流程：
1. 输入校验（EVALUATION_INPUT_SCHEMA）；
2. 依次运行五个指标（metrics.py）得到各维度分数；
3. 按 METRIC_WEIGHTS 加权得到总分；
4. 输出自校验（EVALUATION_OUTPUT_SCHEMA）；
5. 可选写入 JSON 报告（reports/）。

输入：{question, tool_results, agent_output}
输出：{score, violations, evidence, suggestions}

本模块不调用任何模型 API；LLM-as-Judge（judge_prompt.py）为后续阶段预留。
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .metrics import (
    METRIC_NAMES,
    METRIC_WEIGHTS,
    MetricResult,
    Violation,
    metric_compliance,
    metric_data_accuracy,
    metric_evidence_grounding,
    metric_intent_understanding,
    metric_temporal_alignment,
)


# ---------------------------------------------------------------------------
# Evaluation JSON Schema（draft 2020-12 子集）
# ---------------------------------------------------------------------------
EVALUATION_INPUT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "question": {"type": "string"},
        "tool_results": {"type": "object"},
        "agent_output": {"type": "string"},
        "meta": {"type": "object"},
    },
    "required": ["question", "tool_results", "agent_output"],
}

VIOLATION_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "metric": {
            "type": "string",
            "enum": ["data_accuracy", "evidence_grounding", "temporal_alignment",
                     "compliance", "intent_understanding", "input"],
        },
        "severity": {"type": "string", "enum": ["high", "medium", "low"]},
        "code": {"type": "string"},
        "message": {"type": "string"},
        "evidence": {"type": ["string", "array"]},
    },
    "required": ["metric", "severity", "code", "message"],
}

EVIDENCE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "metric": {"type": "string", "enum": list(METRIC_NAMES) + ["input"]},
        "kind": {"type": "string"},
        "detail": {"type": "string"},
    },
    "required": ["metric", "kind", "detail"],
}

EVALUATION_OUTPUT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "score": {
            "type": "object",
            "properties": {
                "overall": {"type": "number"},
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
            },
            "required": ["overall", "dimensions"],
        },
        "violations": {"type": "array", "items": VIOLATION_SCHEMA},
        "evidence": {"type": "array", "items": EVIDENCE_SCHEMA},
        "suggestions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["score", "violations", "evidence", "suggestions"],
}

PASS_THRESHOLD = 0.6


# ---------------------------------------------------------------------------
# 轻量 JSON Schema 校验器（无第三方依赖）
# ---------------------------------------------------------------------------
def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def validate_against_schema(value: Any, schema: Dict[str, Any], path: str = "$") -> List[str]:
    """轻量 JSON Schema（draft 2020-12 子集）校验器，返回错误列表。

    支持：type（字符串或字符串数组）、properties/required（对象）、
    items（数组）、enum；number 类型排除 bool。
    """
    errors: List[str] = []

    schema_type = schema.get("type")
    if schema_type is not None:
        types = schema_type if isinstance(schema_type, list) else [schema_type]
        actual = _json_type(value)
        if actual not in types:
            return [f"{path}: 期望类型 {types}，实际 {actual!r}"]
        if "number" in types and isinstance(value, bool):
            return [f"{path}: 期望 number，实际 boolean"]

    if isinstance(value, dict):
        properties = schema.get("properties", {})
        for key, sub in properties.items():
            if key in value:
                errors.extend(validate_against_schema(value[key], sub, f"{path}.{key}"))
        for required_key in schema.get("required", []):
            if required_key not in value:
                errors.append(f"{path}: 缺少必需字段 {required_key!r}")

    if isinstance(value, list) and schema.get("items") is not None:
        items = schema["items"]
        for index, item in enumerate(value):
            errors.extend(validate_against_schema(item, items, f"{path}[{index}]"))

    enum = schema.get("enum")
    if enum is not None and value not in enum:
        errors.append(f"{path}: 值 {value!r} 不在枚举 {enum} 中")

    return errors


# ---------------------------------------------------------------------------
# 评估入口
# ---------------------------------------------------------------------------
def _error_output(code: str, messages: List[str]) -> Dict[str, Any]:
    """输入级错误 / 空报告：五个维度全部 0 分，并记录 input 级违规。"""
    return {
        "score": {
            "overall": 0.0,
            "dimensions": [{"key": key, "name": METRIC_NAMES[key], "score": 0.0} for key in METRIC_WEIGHTS],
        },
        "violations": [Violation("input", "high", code, msg, "").to_dict() for msg in messages],
        "evidence": [],
        "suggestions": ["请检查评估输入是否符合 EVALUATION_INPUT_SCHEMA。"],
    }


def evaluate(
    question: str,
    tool_results: Dict[str, Any],
    agent_output: str,
    *,
    write_report: bool = False,
    report_dir: Optional[str] = None,
    case_id: Optional[str] = None,
    meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """对单个用例执行完整评估，返回符合 EVALUATION_OUTPUT_SCHEMA 的结果 dict。"""
    input_payload: Dict[str, Any] = {
        "question": question,
        "tool_results": tool_results,
        "agent_output": agent_output,
    }
    if meta is not None:
        input_payload["meta"] = meta
    input_errors = validate_against_schema(input_payload, EVALUATION_INPUT_SCHEMA)
    if input_errors:
        return _error_output("INPUT_SCHEMA_ERROR", input_errors)
    if not agent_output or not agent_output.strip():
        return _error_output("EMPTY_REPORT", ["报告为空"])

    results: List[MetricResult] = [
        metric_data_accuracy(question, tool_results, agent_output),
        metric_evidence_grounding(question, tool_results, agent_output),
        metric_temporal_alignment(question, tool_results, agent_output),
        metric_compliance(question, tool_results, agent_output),
        metric_intent_understanding(question, tool_results, agent_output),
    ]

    overall = sum(METRIC_WEIGHTS[result.key] * result.score for result in results)
    output: Dict[str, Any] = {
        "score": {
            "overall": round(overall, 4),
            "dimensions": [
                {"key": r.key, "name": r.name, "score": round(r.score, 4)} for r in results
            ],
        },
        "violations": [v.to_dict() for r in results for v in r.violations],
        "evidence": [e.to_dict() for r in results for e in r.evidence],
        "suggestions": list(dict.fromkeys(s for r in results for s in r.suggestions)),
    }

    output_errors = validate_against_schema(output, EVALUATION_OUTPUT_SCHEMA)
    if output_errors:
        raise RuntimeError(f"评估输出不符合 EVALUATION_OUTPUT_SCHEMA: {output_errors}")

    if write_report:
        output["report_path"] = _write_report(
            question, tool_results, agent_output, output, meta, report_dir, case_id
        )
    return output


def _write_report(
    question: str,
    tool_results: Dict[str, Any],
    agent_output: str,
    output: Dict[str, Any],
    meta: Optional[Dict[str, Any]],
    report_dir: Optional[str],
    case_id: Optional[str],
) -> str:
    """把评估输入与输出写入 JSON 报告文件，返回文件路径。"""
    directory = Path(report_dir) if report_dir else Path(__file__).resolve().parent / "reports"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    suffix = f"_{case_id}" if case_id else ""
    path = directory / f"evaluation_{stamp}{suffix}.json"
    record = {
        "evaluation_id": f"eval_{stamp}{'_' + case_id if case_id else ''}",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "input": {"question": question, "tool_results": tool_results, "agent_output": agent_output},
        "meta": meta or {},
        "output": output,
    }
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


def evaluate_case(case: Dict[str, Any], *, write_report: bool = False, report_dir: Optional[str] = None) -> Dict[str, Any]:
    """按 {question, tool_results, agent_output, case_id?, meta?} 用例 dict 评估。"""
    input_errors = validate_against_schema(case, EVALUATION_INPUT_SCHEMA)
    if input_errors:
        return _error_output("INPUT_SCHEMA_ERROR", input_errors)
    return evaluate(
        case["question"], case["tool_results"], case["agent_output"],
        write_report=write_report, report_dir=report_dir,
        case_id=case.get("case_id"), meta=case.get("meta"),
    )


def evaluate_batch(cases: List[Dict[str, Any]], *, report_dir: Optional[str] = None) -> Dict[str, Any]:
    """批量评估：返回 summary（总数/均分/最值/通过数）与逐用例结果。"""
    results: List[Dict[str, Any]] = []
    for index, case in enumerate(cases, start=1):
        case_id = case.get("case_id") or f"case_{index:02d}"
        item = evaluate_case({**case, "case_id": case_id}, report_dir=report_dir)
        results.append({
            "case_id": case_id,
            "overall": item["score"]["overall"],
            "violations_count": len(item["violations"]),
            "passed": item["score"]["overall"] >= PASS_THRESHOLD,
        })
    scores = [item["overall"] for item in results]
    summary = {
        "total": len(cases),
        "mean": round(sum(scores) / len(scores), 4) if scores else 0.0,
        "min": round(min(scores), 4) if scores else 0.0,
        "max": round(max(scores), 4) if scores else 0.0,
        "passed": sum(1 for item in results if item["passed"]),
    }
    return {"summary": summary, "results": results}
