"""第十二阶段 evidence-aware reasoning：历史区间/分位断言确定性测试。

覆盖：
- A 组 check_boundary_claims（validator）：无边界词不触发、无可靠分位时裸断言触发、
  可靠分位放行、percentiles 存在但 reliable=False 仍触发、引号内转述豁免、
  免责/否定句子豁免、9 词全覆盖、多词多违规、validate_report 集成、
  _has_reliable_history_percentile 多结构；
- B 组 metrics.evidence_grounding：HISTORY_BOUNDARY_CLAIM 映射（high/0.70/建议）、
  可靠分位放行且补正证据、与 MISSING_DATA_CLAIM 共存、evaluate() 集成；
- C 组 P1 回归：以 honest-failure 回答为 fixture（原为 phase11_live_e2e.log 真实输出，
  该日志为本地生成产物已按发布准备要求移除，改用内嵌等价 fixture），
  断言不误报 HISTORY_BOUNDARY_CLAIM。

运行：cd E:/github/ai-financial-agent && .venv/Scripts/python.exe tests/test_evidence_boundary.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

# 确保能导入项目根目录下的 app 包
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.evaluation.evaluator import evaluate
from app.evaluation.metrics import metric_evidence_grounding
from app.output_quality.validator import (
    BOUNDARY_CLAIM_WORDS,
    _has_reliable_history_percentile,
    check_boundary_claims,
    validate_report,
)


# ---------------------------------------------------------------------------
# 测试辅助
# ---------------------------------------------------------------------------

def _rate_limited_tools() -> Dict[str, Any]:
    """Phase 11 honest-failure 场景：get_valuation_analysis 返回限流错误，无分位数据。"""
    return {"get_valuation_analysis": {"error": "rate_limited", "symbol": "600519"}}


def _reliable_tools() -> Dict[str, Any]:
    return {
        "get_valuation_analysis": {
            "symbol": "600519",
            "percentiles": {
                "pe": {"percentile": 0.3, "sample_count": 120, "reliable": True},
                "pb": {"percentile": 0.5, "sample_count": 120, "reliable": True},
                "horizon": {"start": "20210823", "end": "20260821", "trading_days": 120},
                "reliable": True,
            },
        }
    }


def _unreliable_tools() -> Dict[str, Any]:
    return {
        "get_valuation_analysis": {
            "symbol": "600519",
            "percentiles": {
                "pe": {"percentile": 0.3, "sample_count": 30, "reliable": False},
                "pb": {"percentile": 0.5, "sample_count": 30, "reliable": False},
                "horizon": {"start": "20260101", "end": "20260821", "trading_days": 30},
                "reliable": False,
            },
        }
    }


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


_FAILURES: List[str] = []


# ---------------------------------------------------------------------------
# A. validator.check_boundary_claims
# ---------------------------------------------------------------------------

def test_no_boundary_word_no_trigger() -> None:
    report = "贵州茅台当前 PE 为 17.87 倍，PB 为 6.33 倍。"
    assert check_boundary_claims(report, _rate_limited_tools()) == []


def test_unhedged_boundary_assertion_triggered() -> None:
    reports = [
        "贵州茅台当前估值处于历史低位。",
        "当前 PE 估值处于历史高位。",
        "该股估值处于历史中枢附近。",
    ]
    for report in reports:
        violations = check_boundary_claims(report, _rate_limited_tools())
        assert len(violations) == 1, f"应命中 1 条违规：{report} -> {violations}"
        assert "断言估值所处历史区间/分位" in violations[0]


def test_reliable_percentile_allows() -> None:
    report = "当前 PE 估值处于历史低位。"
    assert check_boundary_claims(report, _reliable_tools()) == []


def test_unreliable_percentile_still_triggers() -> None:
    report = "当前 PE 估值处于历史低位。"
    violations = check_boundary_claims(report, _unreliable_tools())
    assert len(violations) == 1


def test_quote_inside_skipped() -> None:
    # 转述用户问题/引用措辞（含 历史偏低/历史中枢/历史高位）
    report = '如"历史偏低""历史中枢附近""历史高位"的表述需要工具数据支撑。'
    assert check_boundary_claims(report, _rate_limited_tools()) == []


def test_hedged_sentences_exempted() -> None:
    sentences = [
        "本次未能获得 PE/PB 的历史分位。",
        "当前数据不足以支持对估值历史位置的判断。",
        "我不会对估值历史位置作出任何断言。",
        "估值历史位置证据不足，无法判断高低。",
        "报告中未对 PE/PB 是否处于历史低位或高位作出任何结论。",
        "估值历史分位接口本次因限流未能返回数据。",
    ]
    for sentence in sentences:
        assert check_boundary_claims(sentence, _rate_limited_tools()) == [], (
            f"免责/否定句不应命中：{sentence}"
        )


def test_all_nine_words_detected() -> None:
    assert len(BOUNDARY_CLAIM_WORDS) == 9
    for word in BOUNDARY_CLAIM_WORDS:
        report = f"该股估值{word}。"
        violations = check_boundary_claims(report, _rate_limited_tools())
        assert len(violations) == 1, f"词 {word} 应命中：{violations}"


def test_multi_word_multiple_violations() -> None:
    report = "当前估值处于历史低位，同时处于历史高位。"
    violations = check_boundary_claims(report, _rate_limited_tools())
    assert len(violations) == 2
    assert any("历史低位" in v for v in violations)
    assert any("历史高位" in v for v in violations)


def test_validate_report_integration() -> None:
    report = (
        "【1. 市场概况与时效】\n贵州茅台最新价格 100 元。\n"
        "【2. 技术面量化】\n未调用技术分析工具，无指标数据。\n"
        "【3. 基本面概况】\n当前 PE 估值处于历史低位。\n"
        "【4. 综合态势与风险提示】\n本报告基于公开数据，不构成投资建议。"
    )
    res = validate_report(report, _rate_limited_tools())
    assert any("断言估值所处历史区间/分位" in v for v in res)
    # 可靠分位数据下不产生边界断言违规
    res_ok = validate_report(report, _reliable_tools())
    assert not any("断言估值所处历史区间/分位" in v for v in res_ok)


def test_has_reliable_history_percentile() -> None:
    assert _has_reliable_history_percentile(_reliable_tools()) is True
    assert _has_reliable_history_percentile(_unreliable_tools()) is False
    assert _has_reliable_history_percentile(_rate_limited_tools()) is False
    # 顶层无 reliable 但 pe 子项可靠（旧结构兼容）
    pe_only = {
        "get_valuation_analysis": {
            "percentiles": {
                "pe": {"percentile": 0.3, "reliable": True},
                "pb": {"percentile": None, "reliable": False},
            }
        }
    }
    assert _has_reliable_history_percentile(pe_only) is True


# ---------------------------------------------------------------------------
# B. metrics.evidence_grounding 接入
# ---------------------------------------------------------------------------

def test_metric_maps_boundary_claim() -> None:
    res = metric_evidence_grounding(
        "贵州茅台估值历史位置如何",
        _rate_limited_tools(),
        "贵州茅台当前估值处于历史低位。",
    )
    hits = [v for v in res.violations if v.code == "HISTORY_BOUNDARY_CLAIM"]
    assert len(hits) == 1
    assert hits[0].metric == "evidence_grounding"
    assert hits[0].severity == "high"
    assert abs(res.score - 0.7) < 1e-9
    assert any("历史分位" in s for s in res.suggestions)


def test_metric_reliable_no_boundary_violation() -> None:
    res = metric_evidence_grounding(
        "贵州茅台估值历史位置如何",
        _reliable_tools(),
        "贵州茅台当前估值处于历史低位。",
    )
    assert not any(v.code == "HISTORY_BOUNDARY_CLAIM" for v in res.violations)
    assert any(e.kind == "missing_data" for e in res.evidence)


def test_metric_coexists_with_missing_data() -> None:
    report = "当前 PE 处于历史低位，ROE 为 25%。"
    res = metric_evidence_grounding("贵州茅台估值如何", _rate_limited_tools(), report)
    codes = [v.code for v in res.violations]
    assert "MISSING_DATA_CLAIM" in codes
    assert "HISTORY_BOUNDARY_CLAIM" in codes
    assert not any(e.kind == "missing_data" for e in res.evidence)


def test_evaluate_integration() -> None:
    question = "贵州茅台 600519 当前估值处于近 5 年历史估值的什么位置？"
    tools = _rate_limited_tools()
    result = evaluate(question, tools, "贵州茅台当前估值处于历史低位。")
    codes = [v["code"] for v in result["violations"]]
    assert "HISTORY_BOUNDARY_CLAIM" in codes

    result_hedged = evaluate(
        question,
        tools,
        "本次未能获得 PE/PB 的历史分位，数据不足以支持对估值历史位置的判断。",
    )
    codes_hedged = [v["code"] for v in result_hedged["violations"]]
    assert "HISTORY_BOUNDARY_CLAIM" not in codes_hedged


# ---------------------------------------------------------------------------
# C. P1 回归：honest-failure 输出不误报
# ---------------------------------------------------------------------------
# 原 fixture 为根目录 phase11_live_e2e.log（真实 CLI 会话输出，含 honest-failure 回答）。
# 该日志属本地生成产物，按发布准备要求移除（*.log 已入 .gitignore）。
# 改为内嵌等价 fixture：估值分位接口限流导致数据缺失时如实声明并回避断言的回答。

_P1_HONEST_FAILURE_ANSWER = (
    "【1. 市场概况与时效】\n"
    "贵州茅台（600519）最新收盘价约 1700 元，行情数据获取正常，"
    "报告基于最新交易日公开数据生成。\n"
    "【2. 技术面量化】\n"
    "未调用技术分析工具，无技术指标数据。\n"
    "【3. 基本面概况】\n"
    "本次未能获得 PE/PB 的历史分位数据（估值接口限流），"
    "当前数据不足以支持对估值历史位置的判断，我不会对估值历史位置作出任何断言。\n"
    "【4. 综合态势与风险提示】\n"
    "本报告基于公开数据，仅供信息分析与研究辅助，不构成投资建议。"
)


def test_p1_log_no_boundary_false_positive() -> None:
    answer = _P1_HONEST_FAILURE_ANSWER
    assert "历史分位" in answer  # fixture 确实包含边界词，确保测试有效
    assert check_boundary_claims(answer, _rate_limited_tools()) == []


def test_p1_log_evaluate_no_boundary_violation() -> None:
    answer = _P1_HONEST_FAILURE_ANSWER
    question = "贵州茅台 600519 当前 PE 和 PB 处于近 5 年历史估值的什么位置？请给出历史分位"
    result = evaluate(question, _rate_limited_tools(), answer)
    codes = [v["code"] for v in result["violations"]]
    assert "HISTORY_BOUNDARY_CLAIM" not in codes


def main() -> None:
    print("=== tests/test_evidence_boundary.py 历史区间/分位断言（evidence-aware reasoning）测试 ===")
    tests = [
        ("A.1 无边界词不触发", test_no_boundary_word_no_trigger),
        ("A.2 无可靠分位时裸断言触发", test_unhedged_boundary_assertion_triggered),
        ("A.3 可靠分位放行", test_reliable_percentile_allows),
        ("A.4 percentiles 存在但 reliable=False 仍触发", test_unreliable_percentile_still_triggers),
        ("A.5 引号内转述豁免", test_quote_inside_skipped),
        ("A.6 免责/否定句子豁免", test_hedged_sentences_exempted),
        ("A.7 9 词全覆盖", test_all_nine_words_detected),
        ("A.8 多词多违规计数", test_multi_word_multiple_violations),
        ("A.9 validate_report 集成", test_validate_report_integration),
        ("A.10 可靠分位判定多结构", test_has_reliable_history_percentile),
        ("B.1 映射 HISTORY_BOUNDARY_CLAIM(high/0.70)", test_metric_maps_boundary_claim),
        ("B.2 可靠分位不触发且补正证据", test_metric_reliable_no_boundary_violation),
        ("B.3 与 MISSING_DATA_CLAIM 共存", test_metric_coexists_with_missing_data),
        ("B.4 evaluate() 集成", test_evaluate_integration),
        ("C.1 P1 日志不误报（check_boundary_claims）", test_p1_log_no_boundary_false_positive),
        ("C.2 P1 日志不误报（evaluate）", test_p1_log_evaluate_no_boundary_violation),
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
