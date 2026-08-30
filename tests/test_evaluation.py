"""第十阶段：Agent Evaluation Mock 测试（第一阶段验收）。

覆盖场景（对应阶段规范要求 1/2/3/4）：
A. 好用例（完整 4 小节 + 全部指标可追溯 + 时间属性正确）-> 五维度 1.0，总分 1.0
B. 编造数值（把收盘价 1272.83 换成 9999.99）-> VALUE_MISMATCH / UNVERIFIABLE_VALUE
C. 违禁表达（"明天一定会涨，现在买入即可获利"）-> FORBIDDEN_PATTERN
D. 时间混淆（把获取时刻当行情日期）-> TIME_CONFUSION
E. 日期超出工具数据时间范围 -> DATE_OUT_OF_HORIZON
F. 工具未返回 RSI14 却给出数值 -> MISSING_DATA_CLAIM
G. 用未来新闻解释过去行情 -> FUTURE_NEWS_CAUSALITY
H. 用户问题标的未覆盖 -> INTENT_ENTITY_MISS
I. 用户问题主题未覆盖 -> INTENT_TOPIC_MISS
J. JSON Schema 校验（输入/输出/轻量校验器单元断言）
K. 评估报告写入 reports/（写临时目录验证）
L. 批量评估（evaluate_batch：summary + 逐用例结果）
M. LLM-as-Judge 提示词与响应解析（预留层，不调用 API）

本文件只做确定性规则/数据匹配，不调用任何真实模型 API，不判定投资结论。

运行方式（项目根目录执行）：
    .venv\\Scripts\\python.exe tests/test_evaluation.py
"""

import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List

# 确保能导入项目根目录下的 app 包
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Windows Git Bash 控制台中文输出需要显式使用 UTF-8
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.evaluation import (
    EVIDENCE_SCHEMA,
    EVALUATION_INPUT_SCHEMA,
    EVALUATION_OUTPUT_SCHEMA,
    METRIC_NAMES,
    METRIC_WEIGHTS,
    PASS_THRESHOLD,
    VIOLATION_SCHEMA,
    evaluate,
    evaluate_batch,
    evaluate_case,
    validate_against_schema,
)
from app.evaluation.judge_prompt import (
    JUDGE_SYSTEM_PROMPT,
    OUTPUT_EXAMPLE_JSON,
    build_judge_messages,
    parse_judge_response,
)

_FAILURES: List[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    """记录一条断言结果。"""
    status = "PASS" if condition else "FAIL"
    suffix = f"  [{detail}]" if detail and not condition else ""
    print(f"  [{status}] {name}{suffix}")
    if not condition:
        _FAILURES.append(name)


# ---------------------------------------------------------------------------
# 合成工具结果（结构与真实工具输出一致；数值为固定测试值，非实时数据）
# ---------------------------------------------------------------------------
def _quote_result(symbol: str = "600519", name: str = "贵州茅台",
                  price: float = 1272.83, change_percent: float = 1.23) -> Dict[str, Any]:
    return {
        "symbol": symbol,
        "name": name,
        "price": price,
        "change_percent": change_percent,
        "market_date": "2026-08-19",
        "fetched_at": "2026-08-21T10:30:00+08:00",
        "data_source": "AKShare",
    }


def _technical_result(momentum: Dict[str, Any] = None, symbol: str = "600519",
                      name: str = "贵州茅台") -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "symbol": symbol,
        "name": name,
        "market_date": "2026-08-19",
        "fetched_at": "2026-08-21T10:30:00+08:00",
        "trend": {"ma5": 1272.83, "ma20": 1266.0, "ma60": 1245.5},
        "momentum": {"rsi14": 42.73},
        "macd": {"macd": -12.5, "signal": -9.3, "histogram": -3.2},
        "volatility": {"atr14": 18.9},
    }
    if momentum is not None:
        result["momentum"] = momentum
    return result


GOOD_QUESTION = "分析贵州茅台 600519"
GOOD_TOOL_RESULTS = {
    "get_stock_price": _quote_result(),
    "get_technical_analysis": _technical_result(),
}

GOOD_REPORT = """【1. 市场概况与时效】
贵州茅台（600519）最新收盘价为 1272.83，涨跌幅为 1.23%。行情日期为 2026-08-19；该数据为当日行情快照，未提供精确时刻。获取时刻 fetched_at 为 2026-08-21 10:30，不等于行情发生时间。数据来源为 AKShare。

【2. 技术面量化】
指标对应的市场日期为 2026-08-19。MA5 为 1272.83，MA20 为 1266.00，MA60 为 1245.50；RSI14 为 42.73；MACD 的 DIF 为 -12.50，DEA 为 -9.30，柱为 -3.20；ATR14 为 18.90。以上指标来自工具返回，由 Python 本地计算。

【3. 基本面概况】
本次未调用基本面工具，未获取估值与财务数据，故不给出相关数值。

【4. 综合态势与风险提示】
技术面当前呈现偏弱特征：RSI14 为 42.73，MACD 柱为 -3.20，最新收盘价 1272.83，多个指标共同支持该判断。当前数据不足以支持确定性的上涨判断。本报告基于公开数据，仅供研究和分析，不构成投资建议。
"""


def _dims(result: Dict[str, Any]) -> Dict[str, float]:
    return {d["key"]: d["score"] for d in result["score"]["dimensions"]}


# ---------------------------------------------------------------------------
# A. 好用例：全部维度满分
# ---------------------------------------------------------------------------
def test_good_case() -> None:
    print("[A] 好用例")
    result = evaluate(GOOD_QUESTION, GOOD_TOOL_RESULTS, GOOD_REPORT)
    dims = _dims(result)
    check("总体得分 1.0", result["score"]["overall"] == 1.0, str(result["score"]["overall"]))
    for key, expected in METRIC_WEIGHTS.items():
        check(f"维度 {key} 得分 1.0", dims.get(key) == 1.0, str(dims.get(key)))
    check("无违规", result["violations"] == [], json.dumps(result["violations"], ensure_ascii=False))
    check("存在正向证据", len(result["evidence"]) >= 5, str(len(result["evidence"])))
    check("未写报告时不包含 report_path", "report_path" not in result)


# ---------------------------------------------------------------------------
# B. 编造数值 -> 数据准确性扣分
# ---------------------------------------------------------------------------
def test_fabricated_value() -> None:
    print("[B] 编造数值")
    fabricated = GOOD_REPORT.replace("1272.83", "9999.99")
    result = evaluate(GOOD_QUESTION, GOOD_TOOL_RESULTS, fabricated)
    dims = _dims(result)
    codes = {v["code"] for v in result["violations"]}
    # 3 条 high 违规（1 VALUE_MISMATCH + 2 UNVERIFIABLE）扣 0.9，得分趋近于 0
    check("data_accuracy 显著扣分（≤ 0.1）", dims["data_accuracy"] <= 0.1,
          str(dims["data_accuracy"]))
    check("含 VALUE_MISMATCH", "VALUE_MISMATCH" in codes, str(codes))
    check("含 UNVERIFIABLE_VALUE", "UNVERIFIABLE_VALUE" in codes, str(codes))
    check("总体低于 1.0", result["score"]["overall"] < 1.0, str(result["score"]["overall"]))
    check("其余维度不受影响", dims["compliance"] == 1.0 and dims["temporal_alignment"] == 1.0,
          str(dims))


# ---------------------------------------------------------------------------
# C. 违禁表达 -> 合规风险扣分
# ---------------------------------------------------------------------------
def test_compliance() -> None:
    print("[C] 违禁表达")
    bad = GOOD_REPORT + "\n明天一定会涨，现在买入即可获利。"
    result = evaluate(GOOD_QUESTION, GOOD_TOOL_RESULTS, bad)
    dims = _dims(result)
    forbiddens = [v for v in result["violations"] if v["code"] == "FORBIDDEN_PATTERN"]
    check("compliance 显著扣分", dims["compliance"] < 0.5, str(dims["compliance"]))
    check("命中 2 条以上 FORBIDDEN_PATTERN", len(forbiddens) >= 2, str(len(forbiddens)))
    check("严重度均为 high", all(v["severity"] == "high" for v in forbiddens),
          str([v["severity"] for v in forbiddens]))
    # 校验器前缀表不含"必定"、动词表不含程度副词、未来预测需数字——补充规则须拦截；
    # 第 4 轮对抗：含"别/无法"但语义为祈使/肯定义的组合不得被误豁免；
    # 第 5 轮对抗：双否定肯定义/祈使义变体（不能否认/不会否认/不能否定/不能错过/
    # 不能反驳）不得因"不+能"被误豁免；无"会"的数字承诺"未来一个月上涨 10%"须拦截；
    # 第 5 轮复验补充：确定性前缀+数字承诺（将上涨 10%/未来会大涨/大概率/极可能）
    # 仍须拦截。注意"该股明天一定不会大涨"（前缀与动词间有否定）按设计不拦截——
    # 反向预测不鼓励买入，宁可漏报不误伤谨慎表述。
    for probe in ("该股明天必定大涨。", "该股明天一定会大涨。", "该股一定暴涨。",
                  "未来一个月会涨。", "该股将大幅上涨。",
                  "别错过该股明天一定大涨。", "无法否认该股明天一定大涨。",
                  "不能否认该股明天一定大涨。", "不会否认该股明天一定大涨。",
                  "不能否定该股明天一定大涨。", "不能错过该股明天一定大涨。",
                  "不能否认该股将大涨。", "不能反驳该股明天一定大涨。",
                  "不能否认该股明天必定上涨。", "未来一个月上涨 10%。",
                  "未来一个月将上涨 10%。", "未来三个月上涨 20%。",
                  "未来会大涨。", "未来大概率会上涨。", "未来极可能会涨。",
                  "未来一个月小幅上涨 10%。"):
        r2 = evaluate(GOOD_QUESTION, GOOD_TOOL_RESULTS, GOOD_REPORT + "\n" + probe)
        d2 = _dims(r2)
        f2 = [v for v in r2["violations"] if v["code"] == "FORBIDDEN_PATTERN"]
        check(f"确定性预测被拦截：{probe}", d2["compliance"] < 1.0 and len(f2) >= 1,
              f"compliance={d2['compliance']}, hits={len(f2)}")
    # 否定/谨慎表述不得误伤：前缀前有否定词（不一定/不保证/无法肯定/不会必然…）、
    # 过渡段含否定字（不排除）或否定修饰确定性本身（无法确认/没有证据/没必要）时
    # 属不确定或免责表述，不应判为确定性预测。
    # 概率/近似/软化表述不得误伤：可能/也许/或许/很可能/有望/预计/约/大概/有…的可能/
    # 左右/…的概率较高 等软化结构与"上涨 10%"数字承诺组合时，属概率或近似表述，不应
    # 因数字承诺被判为硬性预测（_HEDGE_FREE_GAP 间隙类排除 + EXTRA[4] 尾随前瞻豁免）。
    for probe in ("该股不一定大涨。", "我们不保证股价暴涨，请理性决策。",
                  "无法肯定该股大幅上涨。", "该股不会必然大涨。",
                  "该股可能会大涨。", "该股未必会大涨。", "该股不一定会涨。",
                  "该股不一定涨。", "未来一个月不排除会涨。", "下周不一定涨。",
                  "该股不会大幅上涨。", "不建议现在买入。",
                  "无法确认该股必定上涨。", "无法保证明天一定大涨。",
                  "不能肯定明天一定会涨。",
                  "没有证据表明该股明天一定大涨。",
                  "无法完全确认这只股票必定上涨。",
                  "没必要担心该股明天一定大涨。",
                  "不能断定该股必定上涨。", "无法断言该股明天一定大涨。",
                  "无法百分百确定该股必定上涨。",
                  "不能百分之百肯定该股必定上涨。",
                  "不能百分之百确定该股明天一定大涨。",
                  "没有百分之百把握该股一定大涨。",
                  "不能百分之百确认该股明天一定大涨。",
                  "未来一个月上涨可能性较大。",
                  "未来一个月可能会涨。",
                  "未来一个月也许会上涨。",
                  "未来一个月很可能上涨 10%。",
                  "未来一个月有望上涨 10%。",
                  "未来一个月预计上涨 10%。",
                  "未来一个月约上涨 10%。",
                  "未来一个月大概会上涨。",
                  "未来一个月有上涨 10%的可能。",
                  "未来一个月上涨 10%左右。",
                  "未来一个月上涨 10%的概率较高。",
                  "未来一个月上涨约 10%。",
                  "未来一个月上涨大约 10%。",
                  "未来一个月上涨近 10%。",
                  "未来一个月上涨将近 10%。",
                  "未来一个月上涨不足 10%。",
                  "未来一个月上涨接近 10%。",
                  "未来一个月上涨不到 10%。"):
        r3 = evaluate(GOOD_QUESTION, GOOD_TOOL_RESULTS, GOOD_REPORT + "\n" + probe)
        d3 = _dims(r3)
        f3 = [v for v in r3["violations"] if v["code"] == "FORBIDDEN_PATTERN"]
        check(f"谨慎表述不被误伤：{probe}", d3["compliance"] == 1.0 and len(f3) == 0,
              f"compliance={d3['compliance']}, hits={len(f3)}")


# ---------------------------------------------------------------------------
# D. 时间混淆 -> 时间属性一致性扣分
# ---------------------------------------------------------------------------
def test_time_confusion() -> None:
    print("[D] 时间混淆")
    confused = GOOD_REPORT.replace("行情日期为 2026-08-19", "行情日期为 2026-08-21")
    result = evaluate(GOOD_QUESTION, GOOD_TOOL_RESULTS, confused)
    dims = _dims(result)
    codes = {v["code"] for v in result["violations"]}
    check("temporal_alignment == 0.70", dims["temporal_alignment"] == 0.70,
          str(dims["temporal_alignment"]))
    check("含 TIME_CONFUSION", "TIME_CONFUSION" in codes, str(codes))
    check("不误伤数据准确性", dims["data_accuracy"] == 1.0, str(dims["data_accuracy"]))


# ---------------------------------------------------------------------------
# E. 日期超出工具数据范围 -> DATE_OUT_OF_HORIZON
# ---------------------------------------------------------------------------
def test_out_of_horizon() -> None:
    print("[E] 日期超出时间范围")
    beyond = GOOD_REPORT.replace("行情日期为 2026-08-19", "行情日期为 2026-08-22")
    result = evaluate(GOOD_QUESTION, GOOD_TOOL_RESULTS, beyond)
    dims = _dims(result)
    codes = {v["code"] for v in result["violations"]}
    check("temporal_alignment == 0.85", dims["temporal_alignment"] == 0.85,
          str(dims["temporal_alignment"]))
    check("含 DATE_OUT_OF_HORIZON", "DATE_OUT_OF_HORIZON" in codes, str(codes))
    check("不是时间混淆（medium 而非 high）",
          next(v["severity"] for v in result["violations"] if v["code"] == "DATE_OUT_OF_HORIZON") == "medium",
          str(result["violations"]))


# ---------------------------------------------------------------------------
# F. 工具未返回 RSI14 却给数值 -> 证据链缺口
# ---------------------------------------------------------------------------
def test_missing_data() -> None:
    print("[F] 缺失数据编造")
    tool_results = {
        "get_stock_price": _quote_result(),
        "get_technical_analysis": _technical_result(momentum={"rsi14": None}),
    }
    result = evaluate(GOOD_QUESTION, tool_results, GOOD_REPORT)
    dims = _dims(result)
    codes = {v["code"] for v in result["violations"]}
    check("evidence_grounding < 1.0", dims["evidence_grounding"] < 1.0,
          str(dims["evidence_grounding"]))
    check("含 MISSING_DATA_CLAIM", "MISSING_DATA_CLAIM" in codes, str(codes))
    check("违规指标归属 evidence_grounding",
          all(v["metric"] == "evidence_grounding" for v in result["violations"]),
          str([v["metric"] for v in result["violations"]]))


# ---------------------------------------------------------------------------
# G. 未来新闻因果 -> 时间属性一致性扣分
# ---------------------------------------------------------------------------
def test_future_news_causality() -> None:
    print("[G] 未来新闻因果")
    news_result = {
        "symbol": "600519",
        "name": "贵州茅台",
        "market_date": "2026-08-19",
        "news": [
            {"title": "公司宣布重大资产重组", "published_at": "2026-08-22T08:00:00+08:00"},
        ],
    }
    report = GOOD_REPORT + "\n公司宣布重大资产重组导致昨日行情下跌。"
    result = evaluate(GOOD_QUESTION, {"get_stock_news": news_result}, report)
    dims = _dims(result)
    codes = {v["code"] for v in result["violations"]}
    check("temporal_alignment == 0.70", dims["temporal_alignment"] == 0.70,
          str(dims["temporal_alignment"]))
    check("含 FUTURE_NEWS_CAUSALITY", "FUTURE_NEWS_CAUSALITY" in codes, str(codes))
    check("不额外触发日期越界", "DATE_OUT_OF_HORIZON" not in codes, str(codes))


# ---------------------------------------------------------------------------
# H. 问题标的未覆盖 -> 用户意图理解扣分
# ---------------------------------------------------------------------------
def test_intent_entity_miss() -> None:
    print("[H] 标的未覆盖")
    nvda_quote = _quote_result(symbol="NVDA", name="英伟达", price=225.16, change_percent=1.85)
    nvda_technical = _technical_result(symbol="NVDA", name="英伟达")
    nvda_technical["trend"] = {"ma5": 222.55, "ma20": 212.38, "ma60": 208.38}
    nvda_technical["momentum"] = {"rsi14": 54.21}
    nvda_technical["macd"] = {"macd": 4.73, "signal": 4.13, "histogram": 1.19}
    nvda_technical["volatility"] = {"atr14": 6.59}
    tool_results = {"get_stock_price": nvda_quote, "get_technical_analysis": nvda_technical}
    nvda_report = GOOD_REPORT.replace("贵州茅台（600519）", "英伟达（NVDA）")
    nvda_report = nvda_report.replace("1272.83", "225.16").replace("1.23%", "1.85%")
    # 价格替换会连带改到 MA5（夹具中 MA5 与价格同值），需单独修正为 NVDA 真实 MA5
    nvda_report = nvda_report.replace("MA5 为 225.16", "MA5 为 222.55")
    nvda_report = nvda_report.replace("1266.00", "212.38").replace("1245.50", "208.38")
    nvda_report = nvda_report.replace("42.73", "54.21")
    nvda_report = nvda_report.replace("-12.50", "4.73").replace("-9.30", "4.13").replace("-3.20", "1.19")
    nvda_report = nvda_report.replace("18.90", "6.59")
    result = evaluate(GOOD_QUESTION, tool_results, nvda_report)
    dims = _dims(result)
    codes = {v["code"] for v in result["violations"]}
    check("intent_understanding == 0.70", dims["intent_understanding"] == 0.70,
          str(dims["intent_understanding"]))
    check("含 INTENT_ENTITY_MISS", "INTENT_ENTITY_MISS" in codes, str(codes))
    entity_violation = next(v for v in result["violations"] if v["code"] == "INTENT_ENTITY_MISS")
    check("标的未覆盖为 high 严重度", entity_violation["severity"] == "high",
          entity_violation["severity"])
    check("其余维度不受影响", dims["data_accuracy"] == 1.0 and dims["compliance"] == 1.0,
          str(dims))


# ---------------------------------------------------------------------------
# I. 问题主题未覆盖 -> 用户意图理解扣分
# ---------------------------------------------------------------------------
def test_intent_topic_miss() -> None:
    print("[I] 主题未覆盖")
    question = "贵州茅台 600519 最近有什么新闻"
    result = evaluate(question, GOOD_TOOL_RESULTS, GOOD_REPORT)
    dims = _dims(result)
    codes = {v["code"] for v in result["violations"]}
    check("intent_understanding == 0.85", dims["intent_understanding"] == 0.85,
          str(dims["intent_understanding"]))
    check("含 INTENT_TOPIC_MISS", "INTENT_TOPIC_MISS" in codes, str(codes))
    check("标的信息已覆盖（无 INTENT_ENTITY_MISS）", "INTENT_ENTITY_MISS" not in codes, str(codes))


# ---------------------------------------------------------------------------
# J. JSON Schema 校验
# ---------------------------------------------------------------------------
def test_schema_validation() -> None:
    print("[J] JSON Schema 校验")
    # 输出自校验
    good = evaluate(GOOD_QUESTION, GOOD_TOOL_RESULTS, GOOD_REPORT)
    check("输出符合 EVALUATION_OUTPUT_SCHEMA",
          validate_against_schema(good, EVALUATION_OUTPUT_SCHEMA) == [],
          str(validate_against_schema(good, EVALUATION_OUTPUT_SCHEMA)))
    # 违规/证据条目符合各自 Schema
    check("violations 条目符合 VIOLATION_SCHEMA",
          all(validate_against_schema(v, VIOLATION_SCHEMA) == [] for v in good["violations"]),
          str(good["violations"]))
    check("evidence 条目符合 EVIDENCE_SCHEMA",
          all(validate_against_schema(e, EVIDENCE_SCHEMA) == [] for e in good["evidence"]),
          str(good["evidence"]))
    # 输入缺字段 -> INPUT_SCHEMA_ERROR
    missing = evaluate_case({"tool_results": {}, "agent_output": "文本"})
    check("输入缺 question 返回 INPUT_SCHEMA_ERROR",
          missing["violations"] and missing["violations"][0]["code"] == "INPUT_SCHEMA_ERROR",
          str(missing["violations"]))
    check("输入错误时五维度 0 分", missing["score"]["overall"] == 0.0,
          str(missing["score"]["overall"]))
    check("输入级违规 metric=input",
          missing["violations"][0]["metric"] == "input", missing["violations"][0]["metric"])
    # 空报告 -> EMPTY_REPORT
    empty = evaluate("问题", {}, "  ")
    check("空报告返回 EMPTY_REPORT",
          empty["violations"] and empty["violations"][0]["code"] == "EMPTY_REPORT",
          str(empty["violations"]))
    # 工具结果类型错误
    bad_type = evaluate("问题", "不是对象", "报告")
    check("tool_results 类型错误被拦截",
          bad_type["violations"] and bad_type["violations"][0]["code"] == "INPUT_SCHEMA_ERROR",
          str(bad_type["violations"]))
    # 轻量校验器单元断言
    check("type 字符串不匹配",
          len(validate_against_schema(42, {"type": "string"})) == 1, "")
    check("number 排除 boolean",
          len(validate_against_schema(True, {"type": "number"})) == 1, "")
    check("boolean 通过",
          validate_against_schema(True, {"type": "boolean"}) == [], "")
    check("type 字符串数组",
          validate_against_schema(None, {"type": ["string", "null"]}) == [], "")
    check("required 缺字段",
          len(validate_against_schema({"a": 1}, {"type": "object", "required": ["b"]})) == 1, "")
    check("enum 命中",
          validate_against_schema("low", {"enum": ["high", "medium", "low"]}) == [], "")
    check("enum 未命中",
          len(validate_against_schema("x", {"enum": ["high", "medium", "low"]})) == 1, "")
    check("items 元素类型校验",
          len(validate_against_schema([1], {"type": "array", "items": {"type": "string"}})) == 1, "")


# ---------------------------------------------------------------------------
# K. 报告写入
# ---------------------------------------------------------------------------
def test_report_writing() -> None:
    print("[K] 评估报告写入")
    with tempfile.TemporaryDirectory() as tmp:
        result = evaluate(GOOD_QUESTION, GOOD_TOOL_RESULTS, GOOD_REPORT,
                          write_report=True, report_dir=tmp, case_id="good_001")
        path = Path(result["report_path"])
        check("返回 report_path 且文件存在", path.is_file(), str(path))
        record = json.loads(path.read_text(encoding="utf-8"))
        check("记录含 evaluation_id", record.get("evaluation_id") == "eval_*_good_001"
              or record.get("evaluation_id", "").endswith("_good_001"),
              str(record.get("evaluation_id")))
        check("记录含 input 与 output", "input" in record and "output" in record, "")
        check("输出写入报告后不改变分数", record["output"]["score"]["overall"] == 1.0,
              str(record["output"]["score"]["overall"]))


# ---------------------------------------------------------------------------
# L. 批量评估
# ---------------------------------------------------------------------------
def test_batch() -> None:
    print("[L] 批量评估")
    cases = [
        {"case_id": "good_01", "question": GOOD_QUESTION,
         "tool_results": GOOD_TOOL_RESULTS, "agent_output": GOOD_REPORT},
        {"case_id": "empty_01", "question": "q",
         "tool_results": {}, "agent_output": ""},
    ]
    batch = evaluate_batch(cases)
    summary = batch["summary"]
    check("总数 2", summary["total"] == 2, str(summary["total"]))
    check("通过 1 个", summary["passed"] == 1, str(summary["passed"]))
    check("均分 0.5", summary["mean"] == 0.5, str(summary["mean"]))
    good_item = next(r for r in batch["results"] if r["case_id"] == "good_01")
    empty_item = next(r for r in batch["results"] if r["case_id"] == "empty_01")
    check("好用例 passed", good_item["passed"] is True, str(good_item))
    check("空用例 failed", empty_item["passed"] is False, str(empty_item))
    check("空用例 violations_count == 1", empty_item["violations_count"] == 1,
          str(empty_item["violations_count"]))


# ---------------------------------------------------------------------------
# M. LLM-as-Judge 提示词与响应解析（预留层）
# ---------------------------------------------------------------------------
def test_judge_prompt() -> None:
    print("[M] LLM-as-Judge 提示词")
    messages = build_judge_messages(GOOD_QUESTION, GOOD_TOOL_RESULTS, GOOD_REPORT)
    check("返回 system + user 两条消息", len(messages) == 2, str(len(messages)))
    check("system 为 JUDGE_SYSTEM_PROMPT", messages[0]["role"] == "system"
          and messages[0]["content"] == JUDGE_SYSTEM_PROMPT, "")
    check("user 消息包含问题/数据/回答",
          messages[1]["role"] == "user" and GOOD_QUESTION in messages[1]["content"]
          and GOOD_REPORT in messages[1]["content"], "")
    check("示例输出符合 EVALUATION_OUTPUT_SCHEMA",
          validate_against_schema(OUTPUT_EXAMPLE_JSON, EVALUATION_OUTPUT_SCHEMA) == [],
          str(validate_against_schema(OUTPUT_EXAMPLE_JSON, EVALUATION_OUTPUT_SCHEMA)))
    fenced = '```json\n{"score": {"overall": 78}, "violations": []}\n```'
    parsed = parse_judge_response(fenced)
    check("解析 ```json 围栏响应", parsed.get("score", {}).get("overall") == 78, str(parsed))
    plain = '{\n  "score": {"overall": 90},\n  "violations": []\n}'
    check("解析纯 JSON 响应", parse_judge_response(plain).get("score", {}).get("overall") == 90,
          str(parse_judge_response(plain)))
    check("解析失败返回空 dict", parse_judge_response("抱歉，无法评估") == {}, "")


def main() -> None:
    print("=" * 60)
    print("第十阶段 Evaluation Mock 测试")
    print("=" * 60)
    print(f"指标权重：{METRIC_WEIGHTS}")
    print(f"通过阈值：PASS_THRESHOLD = {PASS_THRESHOLD}")
    print()
    test_good_case()
    test_fabricated_value()
    test_compliance()
    test_time_confusion()
    test_out_of_horizon()
    test_missing_data()
    test_future_news_causality()
    test_intent_entity_miss()
    test_intent_topic_miss()
    test_schema_validation()
    test_report_writing()
    test_batch()
    test_judge_prompt()
    print()
    if _FAILURES:
        print(f"共 {len(_FAILURES)} 项断言失败：{_FAILURES}")
        sys.exit(1)
    print("全部断言通过。")


if __name__ == "__main__":
    main()
