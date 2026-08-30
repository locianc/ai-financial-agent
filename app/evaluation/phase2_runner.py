"""第十阶段第二阶段：双重评估编排器（Deterministic Validator + LLM Judge）+ 报告生成。

每个真实 case 依次执行：
1. 真实 Agent 采样（sampling.sample_agent_run，真实 Tool Calling + 真实数据）；
2. Deterministic Validator（evaluator.evaluate，第一阶段框架，五维 0-1）；
3. LLM-as-Judge（judge_llm.judge_case，五维 0-10，上下文严格分离）。

判定规则（透明、非黑盒）：
- deterministic_passed := det.overall >= 0.6
- judge_passed       := judge.overall_score >= 6.0 且无解析错误
- final_status       := PASS 当且仅当两者同时通过，否则 FAIL
- final_score        := 0.5 * det.overall + 0.5 * (judge.overall_score / 10)
- validator_judge_conflict := 两者 PASS/FAIL 判定不一致

报告统计（≥10 项）：五维均分、每 case 分数、冲突数、违规类型统计、
Tool Calling 正确性、数据引用正确性、时间属性正确性、合规问题数、
API Token 消耗、平均响应耗时，外加失败 case 完整诊断与语义漏检专项。

约束：不修改任何金融数据工具核心逻辑；所有数据保留 data_source；错误如实记录。
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from openai import OpenAI

from .evaluator import PASS_THRESHOLD, evaluate
from .judge_llm import DIMENSION_KEYS, JUDGE_PASS_THRESHOLD, judge_case
from .sampling import sample_agent_run
from .semantic_gap import scan_semantic_gaps

DIMENSION_NAMES: Dict[str, str] = {
    "data_accuracy": "数据准确性",
    "evidence_grounding": "证据链一致性",
    "temporal_alignment": "时间属性一致性",
    "compliance": "合规风险",
    "intent_understanding": "用户意图理解",
}


# ---------------------------------------------------------------------------
# 双重评估
# ---------------------------------------------------------------------------
def run_dual_evaluation(
    agent_client: OpenAI,
    judge_client: OpenAI,
    case: Dict[str, Any],
) -> Dict[str, Any]:
    """对单个 case 执行 真实采样 → 确定性校验 → LLM Judge，返回合并结果。"""
    case_id = case.get("case_id") or "case"

    agent_record = sample_agent_run(agent_client, case)
    final_output = agent_record.get("final_output") or ""
    deterministic = evaluate(
        case["question"],
        agent_record.get("tool_results") or {},
        final_output,
    )
    deterministic["passed"] = deterministic["score"]["overall"] >= PASS_THRESHOLD

    judge = judge_case(judge_client, case, agent_record)

    det_passed = bool(deterministic["passed"])
    judge_passed = (
        judge.get("overall_score") is not None
        and judge["overall_score"] >= JUDGE_PASS_THRESHOLD
        and judge.get("error") is None
    )
    final_score = (
        round(
            0.5 * deterministic["score"]["overall"]
            + 0.5 * (float(judge["overall_score"]) / 10.0),
            4,
        )
        if judge.get("overall_score") is not None
        else None
    )
    final_status = "PASS" if (det_passed and judge_passed) else "FAIL"

    return {
        "case_id": case_id,
        "category": case.get("category"),
        "category_name": case.get("category_name"),
        "question": case["question"],
        "symbol": case.get("symbol"),
        "agent_record": agent_record,
        "deterministic_result": deterministic,
        "llm_judge_result": judge,
        "final_score": final_score,
        "final_status": final_status,
        "validator_judge_conflict": det_passed != judge_passed,
    }


# ---------------------------------------------------------------------------
# 统计聚合
# ---------------------------------------------------------------------------
def _judge_dim_scores(judge: Dict[str, Any]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for dim in judge.get("dimensions", []):
        score = dim.get("score")
        if isinstance(score, (int, float)) and not isinstance(score, bool):
            out[dim["key"]] = float(score)
    return out


def _det_dim_scores(det: Dict[str, Any]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for dim in det["score"]["dimensions"]:
        out[dim["key"]] = float(dim["score"])
    return out


def _tool_stats(result: Dict[str, Any]) -> Dict[str, Any]:
    record = result["agent_record"]
    calls = record.get("tools_called") or []
    called = [c["name"] for c in calls]
    unique_called = list(dict.fromkeys(called))
    unknown = [c for c in calls if c["status"] == "arg_parse_error" or (c.get("error") and "未知工具" in (c.get("error") or ""))]
    tool_errors = [c for c in calls if c.get("status") == "tool_error"]
    required = (result.get("__required_tools") or [])
    missing = [t for t in required if t not in unique_called] if required else []
    return {
        "case_id": result["case_id"],
        "rounds": len({c["round"] for c in calls}),
        "called": unique_called,
        "required": required,
        "missing": missing,
        "unknown_tools": len(unknown),
        "tool_errors": len(tool_errors),
        "tool_error_detail": [
            {"name": c["name"], "error": c["result"].get("error") if isinstance(c.get("result"), dict) else str(c.get("result"))}
            for c in tool_errors
        ],
    }


def build_report(
    results: List[Dict[str, Any]],
    gap_results: List[Dict[str, Any]],
    dataset: Dict[str, Any],
) -> Dict[str, Any]:
    """汇总全部 case 结果，构建含 ≥10 项统计与失败诊断的报告 dict。"""
    total = len(results)
    passed = sum(1 for r in results if r["final_status"] == "PASS")
    final_scores = [r["final_score"] for r in results if r["final_score"] is not None]
    det_overalls = [r["deterministic_result"]["score"]["overall"] for r in results]
    judge_overalls = [r["llm_judge_result"]["overall_score"] for r in results if r["llm_judge_result"]["overall_score"] is not None]
    conflicts = [r for r in results if r["validator_judge_conflict"]]

    # 五维统计（Judge 0-10 与 Deterministic 0-1 分开报告）
    dim_stats: List[Dict[str, Any]] = []
    for key in DIMENSION_KEYS:
        j_scores = [_judge_dim_scores(r["llm_judge_result"]).get(key) for r in results]
        j_scores = [s for s in j_scores if s is not None]
        d_scores = [_det_dim_scores(r["deterministic_result"]).get(key) for r in results]
        d_scores = [s for s in d_scores if s is not None]
        dim_stats.append(
            {
                "key": key,
                "name": DIMENSION_NAMES[key],
                "judge_mean": round(sum(j_scores) / len(j_scores), 4) if j_scores else None,
                "judge_min": round(min(j_scores), 4) if j_scores else None,
                "judge_max": round(max(j_scores), 4) if j_scores else None,
                "det_mean": round(sum(d_scores) / len(d_scores), 4) if d_scores else None,
            }
        )

    # 违规类型统计
    det_violations: List[Dict[str, Any]] = []
    judge_violations: List[Dict[str, Any]] = []
    for r in results:
        det_violations.extend(r["deterministic_result"].get("violations", []))
        judge_violations.extend(r["llm_judge_result"].get("violations", []))
    by_code = Counter(v["code"] for v in det_violations + judge_violations)
    by_metric = Counter(v["metric"] for v in det_violations + judge_violations)
    by_severity = Counter(v["severity"] for v in det_violations + judge_violations)
    compliance_violations = [v for v in det_violations + judge_violations if v["metric"] == "compliance"]

    # 工具调用统计
    tool_stats = [_tool_stats({**r, "__required_tools": _required_for(r, dataset)}) for r in results]
    unknown_tool_count = sum(t["unknown_tools"] for t in tool_stats)
    tool_error_count = sum(t["tool_errors"] for t in tool_stats)
    cases_missing_required = [t for t in tool_stats if t["missing"]]

    # Token 与耗时
    agent_tokens = {"input": 0, "output": 0, "total": 0, "per_case": {}}
    judge_tokens = {"input": 0, "output": 0, "total": 0, "per_case": {}}
    for r in results:
        a = r["agent_record"]
        j = r["llm_judge_result"]
        for bucket, rec in ((agent_tokens, a), (judge_tokens, j)):
            for k in ("input", "output", "total"):
                v = rec.get(f"{k}_tokens")
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    bucket[k] += int(v)
                    bucket["per_case"][r["case_id"]] = bucket["per_case"].get(r["case_id"], {})
                    bucket["per_case"][r["case_id"]][k] = int(v)

    agent_latencies = [r["agent_record"]["latency_ms"] for r in results]
    judge_latencies = [r["llm_judge_result"]["latency_ms"] for r in results]

    failures = [build_failure_diagnosis(r) for r in results if r["final_status"] == "FAIL"]

    report: Dict[str, Any] = {
        "report_id": f"phase2_eval_{time.strftime('%Y%m%d_%H%M%S')}",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "benchmark": dataset.get("benchmark", "phase2_real_evaluation"),
        "version": dataset.get("version", "0.1.0"),
        "rule": {
            "deterministic_pass_threshold": PASS_THRESHOLD,
            "judge_pass_threshold": JUDGE_PASS_THRESHOLD,
            "final_status_rule": "PASS 当且仅当 Deterministic overall >= 0.6 且 Judge overall_score >= 6.0 且 Judge 无解析错误",
            "final_score_formula": "0.5 * deterministic_overall + 0.5 * (judge_overall_score / 10)",
        },
        "summary": {
            "total": total,
            "passed": passed,
            "failed": total - passed,
            "pass_rate": round(passed / total, 4) if total else 0.0,
            "mean_final_score": round(sum(final_scores) / len(final_scores), 4) if final_scores else None,
            "mean_det_overall": round(sum(det_overalls) / len(det_overalls), 4) if det_overalls else None,
            "mean_judge_overall": round(sum(judge_overalls) / len(judge_overalls), 4) if judge_overalls else None,
            "conflicts_count": len(conflicts),
            "conflict_cases": [c["case_id"] for c in conflicts],
        },
        "dimension_stats": dim_stats,
        "per_case": [
            {
                "case_id": r["case_id"],
                "category": r["category"],
                "category_name": r["category_name"],
                "question": r["question"],
                "det_overall": r["deterministic_result"]["score"]["overall"],
                "det_passed": r["deterministic_result"]["passed"],
                "judge_overall": r["llm_judge_result"]["overall_score"],
                "judge_passed": (
                    r["llm_judge_result"]["overall_score"] is not None
                    and r["llm_judge_result"]["overall_score"] >= JUDGE_PASS_THRESHOLD
                    and r["llm_judge_result"].get("error") is None
                ),
                "final_score": r["final_score"],
                "final_status": r["final_status"],
                "conflict": r["validator_judge_conflict"],
                "agent_latency_ms": r["agent_record"]["latency_ms"],
                "agent_error": (r["agent_record"].get("error") or {}).get("type"),
            }
            for r in results
        ],
        "tool_calling": {
            "unknown_tools": unknown_tool_count,
            "tool_errors": tool_error_count,
            "cases_missing_required": [
                {"case_id": t["case_id"], "missing": t["missing"], "called": t["called"]}
                for t in cases_missing_required
            ],
            "per_case": tool_stats,
        },
        "violation_stats": {
            "total": len(det_violations) + len(judge_violations),
            "det_count": len(det_violations),
            "judge_count": len(judge_violations),
            "by_code": dict(by_code.most_common()),
            "by_metric": dict(by_metric.most_common()),
            "by_severity": dict(by_severity.most_common()),
        },
        "compliance": {
            "det_mean": _mean_dim("compliance", det_overalls_by_dim(results)),
            "judge_mean": _mean_judge_dim("compliance", results),
            "violations_count": len(compliance_violations),
            "violations_per_case": [
                {
                    "case_id": r["case_id"],
                    "count": sum(
                        1 for v in r["deterministic_result"].get("violations", [])
                        + r["llm_judge_result"].get("violations", [])
                        if v["metric"] == "compliance"
                    ),
                    "det_count": sum(1 for v in r["deterministic_result"].get("violations", []) if v["metric"] == "compliance"),
                    "judge_count": sum(1 for v in r["llm_judge_result"].get("violations", []) if v["metric"] == "compliance"),
                }
                for r in results
            ],
        },
        "data_accuracy": {
            "det_mean": _mean_dim("data_accuracy", det_overalls_by_dim(results)),
            "judge_mean": _mean_judge_dim("data_accuracy", results),
            "value_mismatch_violations": sum(
                1 for v in det_violations + judge_violations if v["code"] in ("VALUE_MISMATCH", "UNVERIFIABLE_VALUE", "MISSING_DATA_CLAIM")
            ),
        },
        "temporal": {
            "det_mean": _mean_dim("temporal_alignment", det_overalls_by_dim(results)),
            "judge_mean": _mean_judge_dim("temporal_alignment", results),
            "temporal_violations": sum(
                1 for v in det_violations + judge_violations if v["metric"] == "temporal_alignment"
            ),
        },
        "tokens": {
            "agent": agent_tokens,
            "judge": judge_tokens,
            "grand_total": agent_tokens["total"] + judge_tokens["total"],
        },
        "latency": {
            "agent_mean_ms": round(sum(agent_latencies) / len(agent_latencies), 1) if agent_latencies else None,
            "judge_mean_ms": round(sum(judge_latencies) / len(judge_latencies), 1) if judge_latencies else None,
            "per_case": [
                {
                    "case_id": r["case_id"],
                    "agent_ms": r["agent_record"]["latency_ms"],
                    "judge_ms": r["llm_judge_result"]["latency_ms"],
                }
                for r in results
            ],
        },
        "failures": failures,
        "semantic_gaps": build_gap_summary(gap_results),
    }
    return report


def _required_for(result: Dict[str, Any], dataset: Dict[str, Any]) -> List[str]:
    for case in dataset.get("cases", []):
        if case["case_id"] == result["case_id"]:
            return case.get("required_tools", [])
    return []


def det_overalls_by_dim(results: List[Dict[str, Any]]) -> Dict[str, List[float]]:
    out: Dict[str, List[float]] = {k: [] for k in DIMENSION_KEYS}
    for r in results:
        for dim in r["deterministic_result"]["score"]["dimensions"]:
            out[dim["key"]].append(float(dim["score"]))
    return out


def _mean_dim(key: str, by_dim: Dict[str, List[float]]) -> Optional[float]:
    vals = by_dim.get(key, [])
    return round(sum(vals) / len(vals), 4) if vals else None


def _mean_judge_dim(key: str, results: List[Dict[str, Any]]) -> Optional[float]:
    vals = [_judge_dim_scores(r["llm_judge_result"]).get(key) for r in results]
    vals = [v for v in vals if v is not None]
    return round(sum(vals) / len(vals), 4) if vals else None


def build_failure_diagnosis(result: Dict[str, Any]) -> Dict[str, Any]:
    """失败 case 的完整诊断：Agent / Det / Judge 三方信息。"""
    record = result["agent_record"]
    det = result["deterministic_result"]
    judge = result["llm_judge_result"]
    final_output = (record.get("final_output") or "").strip()
    return {
        "case_id": result["case_id"],
        "category": result["category_name"],
        "question": result["question"],
        "final_status": result["final_status"],
        "final_score": result["final_score"],
        "agent": {
            "error": record.get("error"),
            "reached_max_rounds": record.get("reached_max_rounds"),
            "tools_called_summary": [
                {"round": c["round"], "name": c["name"], "arguments": c["arguments"], "status": c.get("status")}
                for c in record.get("tools_called", [])
            ],
            "latency_ms": record.get("latency_ms"),
            "tokens": {
                "input": record.get("input_tokens"),
                "output": record.get("output_tokens"),
                "total": record.get("total_tokens"),
            },
            "final_output_excerpt": final_output[:1200],
        },
        "deterministic": {
            "overall": det["score"]["overall"],
            "passed": det["passed"],
            "dimensions": det["score"]["dimensions"],
            "violations": det.get("violations", []),
        },
        "judge": {
            "overall_score": judge.get("overall_score"),
            "dimensions": judge.get("dimensions"),
            "violations": judge.get("violations", []),
            "short_reason": judge.get("short_reason"),
            "error": judge.get("error"),
            "retried": judge.get("retried"),
        },
    }


def build_gap_summary(gap_results: List[Dict[str, Any]]) -> Dict[str, Any]:
    total_findings = 0
    by_category: Counter = Counter()
    by_severity: Counter = Counter()
    per_case: List[Dict[str, Any]] = []
    regex_hit_total = 0
    for g in gap_results:
        findings = g.get("findings", [])
        total_findings += len(findings)
        for f in findings:
            by_category[f["category"]] += 1
            by_severity[f["severity"]] += 1
        regex_hits = g.get("regex_hits", [])
        regex_hit_total += len(regex_hits)
        per_case.append(
            {
                "case_id": g["case_id"],
                "findings_count": len(findings),
                "findings": findings,
                "regex_hits": regex_hits,
                "error": g.get("error"),
            }
        )
    return {
        "total_findings": total_findings,
        "by_category": dict(by_category.most_common()),
        "by_severity": dict(by_severity.most_common()),
        "regex_hit_total": regex_hit_total,
        "per_case": per_case,
    }


# ---------------------------------------------------------------------------
# 报告写入
# ---------------------------------------------------------------------------
def write_reports(
    report: Dict[str, Any],
    results: List[Dict[str, Any]],
    *,
    report_dir: str = "app/evaluation/reports/phase2",
) -> Path:
    """写入 JSON 报告 + Markdown 报告 + 原始记录快照，返回报告目录。"""
    directory = Path(report_dir)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")

    json_path = directory / f"evaluation_report_{stamp}.json"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    md_path = directory / f"evaluation_report_{stamp}.md"
    md_path.write_text(_render_markdown(report), encoding="utf-8")

    raw_path = directory / f"raw_records_{stamp}.json"
    raw_path.write_text(
        json.dumps(
            {
                "generated_at": report["generated_at"],
                "results": [_strip_bulky(result) for result in results],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return directory


def _strip_bulky(result: Dict[str, Any]) -> Dict[str, Any]:
    """保留审计所需全部字段，仅截断过长 final_output 原始文本。"""
    record = result["agent_record"]
    output = record.get("final_output") or ""
    record = {**record}
    if len(output) > 4000:
        record["final_output"] = output[:4000] + f"\n...[截断，总长 {len(output)} 字符]"
    return {**result, "agent_record": record}


def _render_markdown(report: Dict[str, Any]) -> str:
    s = report["summary"]
    lines: List[str] = []
    lines.append(f"# 第十阶段第二阶段 Evaluation Report（真实 Agent + LLM-as-Judge）\n")
    lines.append(f"- report_id：`{report['report_id']}`")
    lines.append(f"- 生成时间：{report['generated_at']}")
    lines.append(f"- Benchmark：{report['benchmark']}（version {report['version']}）\n")
    lines.append("## 判定规则（非黑盒）\n")
    lines.append(f"- Deterministic Validator 通过阈值：overall >= {report['rule']['deterministic_pass_threshold']}（0-1 分制）")
    lines.append(f"- LLM Judge 通过阈值：overall_score >= {report['rule']['judge_pass_threshold']}（0-10 分制）")
    lines.append(f"- final_status：{report['rule']['final_status_rule']}")
    lines.append(f"- final_score：{report['rule']['final_score_formula']}\n")

    lines.append("## 1. 总体摘要\n")
    lines.append(f"- 用例总数：{s['total']}；通过 {s['passed']}；失败 {s['failed']}；通过率 {s['pass_rate']:.2%}")
    lines.append(f"- 平均 final_score：{s['mean_final_score']}")
    lines.append(f"- 平均 Deterministic overall：{s['mean_det_overall']}；平均 Judge overall：{s['mean_judge_overall']}")
    lines.append(f"- Validator 与 Judge 冲突数：{s['conflicts_count']}（case：{s['conflict_cases']}）\n")

    lines.append("## 2. 五维平均分\n")
    lines.append("| 维度 | Judge 平均(0-10) | Judge 区间 | Deterministic 平均(0-1) |")
    lines.append("|---|---|---|---|")
    for d in report["dimension_stats"]:
        lines.append(
            f"| {d['name']} {d['key']} | {d['judge_mean']} | {d['judge_min']} ~ {d['judge_max']} | {d['det_mean']} |"
        )
    lines.append("")

    lines.append("## 3. 每 case 分数\n")
    lines.append("| case_id | 类别 | det(0-1) | det PASS | judge(0-10) | judge PASS | final_score | 状态 | 冲突 |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for c in report["per_case"]:
        lines.append(
            f"| {c['case_id']} | {c['category_name']} | {c['det_overall']} | {c['det_passed']} | "
            f"{c['judge_overall']} | {c['judge_passed']} | {c['final_score']} | {c['final_status']} | {c['conflict']} |"
        )
    lines.append("")

    tc = report["tool_calling"]
    lines.append("## 4. Tool Calling 正确性\n")
    lines.append(f"- 未知工具调用数：{tc['unknown_tools']}；工具执行错误数：{tc['tool_errors']}")
    lines.append(f"- 缺少必选工具的 case：{len(tc['cases_missing_required'])}")
    for miss in tc["cases_missing_required"]:
        lines.append(f"  - {miss['case_id']}：必选 {miss['missing']}，实际调用 {miss['called']}")
    lines.append("")

    vs = report["violation_stats"]
    lines.append("## 5. 违规类型统计\n")
    lines.append(f"- 违规总数：{vs['total']}（Deterministic {vs['det_count']} / Judge {vs['judge_count']}）")
    lines.append(f"- 按严重度：{vs['by_severity']}")
    lines.append(f"- 按错误码：{vs['by_code']}")
    lines.append("")

    comp = report["compliance"]
    lines.append("## 6. 合规问题数\n")
    lines.append(f"- Deterministic compliance 平均：{comp['det_mean']}；Judge compliance 平均：{comp['judge_mean']}")
    lines.append(f"- compliance 违规总数：{comp['violations_count']}")
    for pc in comp["violations_per_case"]:
        lines.append(f"  - {pc['case_id']}：det {pc['det_count']} / judge {pc['judge_count']}")
    lines.append("")

    da = report["data_accuracy"]
    lines.append("## 7. 数据引用正确性\n")
    lines.append(f"- Deterministic data_accuracy 平均：{da['det_mean']}；Judge data_accuracy 平均：{da['judge_mean']}")
    lines.append(f"- 数值不一致/不可核实违规数：{da['value_mismatch_violations']}\n")

    tp = report["temporal"]
    lines.append("## 8. 时间属性正确性\n")
    lines.append(f"- Deterministic temporal 平均：{tp['det_mean']}；Judge temporal 平均：{tp['judge_mean']}")
    lines.append(f"- 时间属性违规数：{tp['temporal_violations']}\n")

    tok = report["tokens"]
    lines.append("## 9. API Token 消耗\n")
    lines.append(
        f"- Agent 采样：input {tok['agent']['input']} / output {tok['agent']['output']} / total {tok['agent']['total']}"
    )
    lines.append(
        f"- LLM Judge：input {tok['judge']['input']} / output {tok['judge']['output']} / total {tok['judge']['total']}"
    )
    lines.append(f"- 总计：{tok['grand_total']}")
    lines.append("")

    lat = report["latency"]
    lines.append("## 10. 平均响应耗时\n")
    lines.append(f"- Agent 平均：{lat['agent_mean_ms']} ms；Judge 平均：{lat['judge_mean_ms']} ms")
    for pc in lat["per_case"]:
        lines.append(f"  - {pc['case_id']}：agent {pc['agent_ms']} ms / judge {pc['judge_ms']} ms")
    lines.append("")

    lines.append("## 11. 失败 case 完整诊断\n")
    if not report["failures"]:
        lines.append("无失败 case。\n")
    for f in report["failures"]:
        lines.append(f"### {f['case_id']}（{f['category']}）")
        lines.append(f"- 问题：{f['question']}")
        lines.append(f"- final_status：{f['final_status']}；final_score：{f['final_score']}")
        lines.append(f"- Agent 错误：{f['agent']['error']}；达到最大轮数：{f['agent']['reached_max_rounds']}")
        lines.append(f"- Agent 工具调用：{json.dumps(f['agent']['tools_called_summary'], ensure_ascii=False)}")
        lines.append(f"- Agent token：{f['agent']['tokens']}")
        lines.append(f"- Deterministic：overall {f['deterministic']['overall']}，dimensions {json.dumps(f['deterministic']['dimensions'], ensure_ascii=False)}")
        if f["deterministic"]["violations"]:
            lines.append(f"- Deterministic 违规：{json.dumps(f['deterministic']['violations'], ensure_ascii=False, indent=1)}")
        lines.append(f"- Judge：overall_score {f['judge']['overall_score']}，short_reason：{f['judge']['short_reason']}")
        if f["judge"]["violations"]:
            lines.append(f"- Judge 违规：{json.dumps(f['judge']['violations'], ensure_ascii=False, indent=1)}")
        if f["judge"].get("error"):
            lines.append(f"- Judge 错误：{f['judge']['error']}")
        output = f["agent"]["final_output_excerpt"]
        if output:
            lines.append(f"- Agent 回答摘录：\n```\n{output}\n```")
        lines.append("")

    lines.append("## 12. 语义层面漏检专项\n")
    gaps = report["semantic_gaps"]
    lines.append(f"- 语义发现总数：{gaps['total_findings']}；按类别：{gaps['by_category']}；按严重度：{gaps['by_severity']}")
    lines.append(f"- 正则命中对照总数：{gaps['regex_hit_total']}\n")
    for pc in gaps["per_case"]:
        lines.append(f"### {pc['case_id']}")
        lines.append(f"- 语义发现 {pc['findings_count']} 条；正则命中 {len(pc['regex_hits'])} 条；扫描错误：{pc.get('error')}")
        for f in pc["findings"]:
            lines.append(
                f"  - [{f['severity']}] {f['category']}：{f['explanation']}"
                f"（原文：{f['excerpt'][:120]}）"
            )
        for hit in pc["regex_hits"]:
            lines.append(f"  - 正则[{hit['source']}]命中：{hit['excerpt'][:120]}")
        lines.append("")
    return "\n".join(lines)
