"""输出质量确定性测试（第九阶段 Part 6/7 的确定性部分）。

覆盖场景（Tests A-G 对应阶段规范 Part 6）：
A. 规范报告通过全部校验（4 小节 / 证据链 / 时间属性 / 无违禁表达 / 来源注明）
B. 确定性未来预测（"明天一定会涨"）被违禁表达规则拦截
C. 买卖/仓位建议（"现在可以全仓买入"）被违禁表达规则拦截
D. 工具未返回 RSI14 却给出数值 -> 缺失数据诚实性拦截
E. market_date != fetched_at 时把获取时刻当作行情/交易时间 -> 时间混淆拦截
F. report_period != fetched_at 时把获取时刻当作财务数据日期 -> 拦截
G. 用发布时间晚于行情日期的新闻解释过去行情 -> 未来新闻因果拦截

另有静态校验：SYSTEM_PROMPT 四小节规范、违禁表达清单、时间属性区分、
诚实缺失要求、证据链要求；以及缺失数据如实表述（权限限制无法获得 ROE）
不触发误报的正向用例。

本文件只做确定性文本/数据匹配，不调用任何真实 API，不判定投资结论。

运行方式（项目根目录执行）：
    .venv\\Scripts\\python.exe tests/test_output_quality.py
"""

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# 确保能导入项目根目录下的 app / tools 包
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Windows Git Bash 控制台中文输出需要显式使用 UTF-8
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.output_quality.validator import (
    MANDATORY_SECTIONS,
    TIER1_ACCESSORS,
    check_forbidden_patterns,
    check_future_news_causality,
    check_indicator_claims,
    check_mandatory_sections,
    check_missing_indicator_claims,
    check_time_confusion,
    find_indicator_claims,
    validate_report,
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
def _quote_result() -> Dict[str, Any]:
    """get_stock_price 结果：timestamp 为 None（该接口不提供精确时刻，如实缺失）。"""
    return {
        "symbol": "600519",
        "name": "贵州茅台",
        "price": 1272.83,
        "change_percent": 2.31,
        "open": 1260.0,
        "high": 1280.0,
        "low": 1255.0,
        "previous_close": 1244.1,
        "volume": 3200000,
        "amount": 40.5e9,
        "pe": 17.87,
        "pb": 6.1,
        "total_market_cap": 1.6e12,
        "float_market_cap": 1.6e12,
        "currency": "CNY",
        "market": "A股",
        "data_source": "东方财富",
        "data_quality": {"source": "东方财富", "clean": True},
        "timestamp": None,
    }


def _technical_result(rsi14: Optional[float] = 42.73) -> Dict[str, Any]:
    """get_technical_analysis 结果。"""
    return {
        "symbol": "600519.SH",
        "name": "贵州茅台",
        "market": "A股",
        "data_source": "AKShare",
        "latest": {"date": "2026-08-19", "close": 1272.83, "volume": 3200000},
        "trend": {"ma5": 1280.5, "ma20": 1290.1, "ma60": 1271.51},
        "momentum": {"rsi14": rsi14},
        "macd": {"macd": -8.2, "signal": -7.6, "histogram": -1.2},
        "volatility": {"atr14": 18.5},
        "fetched_at": "2026-08-21T03:00:00+00:00",
        "market_date": "2026-08-19",
        "data_quality": {"clean": True, "issues": []},
    }


def _fundamentals_result(roe: Optional[float] = 30.4) -> Dict[str, Any]:
    """get_stock_fundamentals 结果。"""
    return {
        "symbol": "600519.SH",
        "name": "贵州茅台",
        "market": "A股",
        "asset_type": "stock",
        "valuation": {
            "pe": 19.54,
            "pb": 6.33,
            "ps": 9.2,
            "total_market_cap": 15989.2,
            "float_market_cap": 15989.2,
        },
        "profitability": {
            "roe": roe,
            "eps": 16.6,
            "gross_margin": 89.56,
            "book_value_per_share": 138.4,
            "operating_cash_flow_per_share": 12.3,
        },
        "growth": {"revenue_growth": 15.5, "net_profit_growth": 19.3},
        "dividend": {"dividend_yield": 4.09},
        "data_date": "20260819",
        "report_period": "2026-03-31",
        "fetched_at": "2026-08-21T03:00:00+00:00",
        "data_source": "Composite(AKShare + Tushare)",
        "data_quality": {
            "sources": [{"source": "daily_basic", "status": "ok"},
                        {"source": "yjbb", "status": "ok"}],
            "report_periods": ["2026-03-31"],
            "issues": [],
        },
    }


def _tool_results(
    rsi14: Optional[float] = 42.73,
    roe: Optional[float] = 30.4,
    news: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """组装 {工具名: 结果} 容器（与 validate_report 约定一致）。"""
    container: Dict[str, Any] = {
        "get_stock_price": _quote_result(),
        "get_technical_analysis": _technical_result(rsi14),
        "get_stock_fundamentals": _fundamentals_result(roe),
    }
    if news is not None:
        container["get_stock_news"] = news
    return container


# ---------------------------------------------------------------------------
# A. 规范报告：应通过全部校验
# ---------------------------------------------------------------------------
GOOD_REPORT_A = """【1. 市场概况与时效】
贵州茅台（600519）最新价格为 1272.83 元，涨跌幅 2.31%。数据来源为东方财富公开行情（AKShare）。行情快照对应的市场交易日（market_date）为 2026-08-19；本次获取数据时刻（fetched_at）为 2026-08-21。由于该行情接口未提供精确快照时刻（timestamp 为 None），本报告只提供该交易日的行情/快照数据，未提供精确时刻。
【2. 技术面量化】
已调用 get_technical_analysis 工具（数据来源 AKShare，行情交易日 2026-08-19）：MA5 为 1280.5，MA20 为 1290.1，MA60 为 1271.51，RSI14 为 42.73，DIF 为 -8.2，DEA 为 -7.6，MACD 柱为 -1.2，ATR14 为 18.5。所有指标均来自工具返回的数据。
【3. 基本面概况】
已调用 get_stock_fundamentals 工具（数据来源 Composite：AKShare + Tushare）：估值数据日期（data_date）为 2026-08-19，财务报告期（report_period）为 2026-03-31，获取时刻（fetched_at）为 2026-08-21。PE 为 19.54，PB 为 6.33，ROE 为 30.4，EPS 为 16.6，毛利率为 89.56，股息率为 4.09，营收同比增长 15.5，净利润同比增长 19.3。
【4. 综合态势与风险提示】
技术面当前呈现偏弱特征：RSI14 为 42.73，MACD 柱为 -1.2，最新收盘价 1272.83，均低于 MA20（1290.1）。基本面估值处于中性区间：PE 为 19.54，股息率为 4.09。当前数据不足以支持确定性的上涨判断。风险提示：本报告基于公开数据，仅供研究和分析，不构成投资建议。
"""


def test_good_report_passes_all_checks() -> None:
    print("测试 A：规范报告通过全部校验")
    violations = validate_report(GOOD_REPORT_A, _tool_results())
    check("规范报告无任何违规", violations == [], f"violations={violations}")


# ---------------------------------------------------------------------------
# B. 确定性未来预测
# ---------------------------------------------------------------------------
BAD_REPORT_B = """【1. 市场概况与时效】
贵州茅台（600519）最新价格为 1272.83 元。行情交易日为 2026-08-19。
【2. 技术面量化】
RSI14 为 42.73，MA5 为 1280.5。
【3. 基本面概况】
PE 为 19.54。
【4. 综合态势与风险提示】
明天一定会涨，现在买入即可获利。风险提示：本报告仅用于研究。
"""


def test_forbidden_future_prediction() -> None:
    print("测试 B：确定性未来预测被拦截")
    violations = validate_report(BAD_REPORT_B, _tool_results())
    hits = [v for v in violations if "明天一定会涨" in v]
    check("“明天一定会涨”被拦截", len(hits) >= 1, f"violations={violations}")


# ---------------------------------------------------------------------------
# C. 买卖/仓位建议
# ---------------------------------------------------------------------------
BAD_REPORT_C = """【1. 市场概况与时效】
贵州茅台（600519）最新价格 1272.83 元，涨跌幅 2.31%。行情交易日为 2026-08-19。
【2. 技术面量化】
MA5 为 1280.5，RSI14 为 42.73。
【3. 基本面概况】
PE 为 19.54，ROE 为 30.4。
【4. 综合态势与风险提示】
现在可以全仓买入贵州茅台。风险提示：本报告仅用于研究。
"""


def test_forbidden_buy_advice() -> None:
    print("测试 C：买卖/仓位建议被拦截")
    violations = validate_report(BAD_REPORT_C, _tool_results())
    hits = [v for v in violations if "全仓" in v]
    check("“现在可以全仓买入”被拦截", len(hits) >= 1, f"violations={violations}")


# ---------------------------------------------------------------------------
# D. 缺失数据诚实性：工具未返回 RSI14，报告却给数值
# ---------------------------------------------------------------------------
BAD_REPORT_D = """【1. 市场概况与时效】
贵州茅台（600519）最新价格 1272.83 元。行情交易日为 2026-08-19。
【2. 技术面量化】
MA5 为 1280.5，RSI14 为 45.2。
【3. 基本面概况】
PE 为 19.54。
【4. 综合态势与风险提示】
技术面偏中性。风险提示：本报告仅用于研究。
"""


def test_missing_rsi_fabrication() -> None:
    print("测试 D：工具未返回 RSI14 却给出数值被拦截")
    violations = validate_report(BAD_REPORT_D, _tool_results(rsi14=None))
    hits = [v for v in violations if "RSI14" in v and "45.2" in v]
    check("缺失 RSI14 仍给数值被拦截", len(hits) >= 1, f"violations={violations}")


# ---------------------------------------------------------------------------
# E. 时间混淆：把 fetched_at 当作行情/交易时间
# ---------------------------------------------------------------------------
BAD_REPORT_E = """【1. 市场概况与时效】
贵州茅台（600519）最新价格 1272.83 元。行情交易日为 2026-08-21，当日上涨。
【2. 技术面量化】
MA5 为 1280.5，RSI14 为 42.73。
【3. 基本面概况】
PE 为 19.54。
【4. 综合态势与风险提示】
风险提示：本报告仅用于研究。
"""


def test_time_confusion_market_date() -> None:
    print("测试 E：fetched_at 被当作行情/交易时间被拦截")
    violations = validate_report(BAD_REPORT_E, _tool_results())
    hits = [v for v in violations if "行情/交易时间" in v]
    check("时间混淆（行情时间）被拦截", len(hits) >= 1, f"violations={violations}")


# ---------------------------------------------------------------------------
# F. 时间混淆：把 fetched_at 当作财务数据日期（旧报告期 + 当前获取时刻）
# ---------------------------------------------------------------------------
BAD_REPORT_F = """【1. 市场概况与时效】
贵州茅台（600519）最新价格 1272.83 元。行情交易日为 2026-08-19。
【2. 技术面量化】
MA5 为 1280.5，RSI14 为 42.73。
【3. 基本面概况】
本报告期财务数据为 2026-08-21 当天的财务指标。
【4. 综合态势与风险提示】
风险提示：本报告仅用于研究。
"""


def test_time_confusion_report_period() -> None:
    print("测试 F：fetched_at 被当作财务数据日期被拦截")
    violations = validate_report(BAD_REPORT_F, _tool_results())
    hits = [v for v in violations if "财务数据日期" in v]
    check("时间混淆（财务数据日期）被拦截", len(hits) >= 1, f"violations={violations}")


# ---------------------------------------------------------------------------
# G. 未来新闻因果：用发布时间晚于行情日期的新闻解释过去行情
# ---------------------------------------------------------------------------
BAD_REPORT_G = """【1. 市场概况与时效】
贵州茅台（600519）最新价格 1272.83 元。行情交易日为 2026-08-19。
【2. 技术面量化】
MA5 为 1280.5，RSI14 为 42.73。
【3. 基本面概况】
PE 为 19.54。
【4. 综合态势与风险提示】
公司发布半年报业绩超预期，导致当日股价上涨。风险提示：本报告仅用于研究。
"""

_FUTURE_NEWS = {
    "fetched_at": "2026-08-21T03:00:00+00:00",
    "news": [
        {
            "title": "公司发布半年报业绩超预期",
            "summary": "公司发布半年报，业绩超预期。",
            "published_at": "2026-08-22",
            "source": "测试新闻源",
            "relevance": 0.8,
        }
    ],
}


def test_future_news_causality() -> None:
    print("测试 G：未来新闻解释过去行情被拦截")
    violations = validate_report(BAD_REPORT_G, _tool_results(news=_FUTURE_NEWS))
    hits = [v for v in violations if "晚于行情日期" in v]
    check("未来新闻因果被拦截", len(hits) >= 1, f"violations={violations}")


# ---------------------------------------------------------------------------
# H. 静态校验：SYSTEM_PROMPT 与工具 Schema 完整
# ---------------------------------------------------------------------------
def test_system_prompt_spec() -> None:
    print("测试 H：SYSTEM_PROMPT 四小节研究分析规范")
    try:
        import main as main_module
    except Exception as exc:
        check("可导入 main 模块", False, f"{type(exc).__name__}: {exc}")
        return

    prompt = main_module.SYSTEM_PROMPT
    check("是字符串且非空", isinstance(prompt, str) and len(prompt) > 500)

    for section in MANDATORY_SECTIONS:
        check(f"SYSTEM_PROMPT 含 {section}", section in prompt)

    forbidden_examples = [
        "建议买入", "现在可以买", "现在应该卖", "可以全仓",
        "建议重仓", "明天一定上涨", "明天大概率上涨", "保证盈利", "稳赚", "一定会跌",
    ]
    for expr in forbidden_examples:
        check(f"SYSTEM_PROMPT 列出违禁表达 {expr}", expr in prompt)

    time_attrs = ["market_date", "timestamp", "report_period", "data_date",
                  "publish_date", "published_at", "fetched_at"]
    for attr in time_attrs:
        check(f"SYSTEM_PROMPT 区分时间属性 {attr}", attr in prompt)
    news_phrases = ["get_stock_news", "仅凭新闻无法确认绝对因果关系",
                    "可能受到…影响", "不得将新闻与股价变动直接等同为因果"]
    for phrase in news_phrases:
        check(f"SYSTEM_PROMPT 新闻因果防御 {phrase}", phrase in prompt)
    check("fetched_at 不能被解释为行情发生时间",
          "fetched_at 不能被解释为行情发生时间" in prompt)
    check("不得把 fetched_at 当作行情时间或交易时间",
          "fetched_at 当作行情时间或交易时间" in prompt)

    evidence_phrases = ["结论→指标→工具", "当前数据不足以支持该判断",
                        "必须能追溯到具体的工具数据"]
    for phrase in evidence_phrases:
        check(f"SYSTEM_PROMPT 证据链要求 {phrase}", phrase in prompt)

    honesty_phrases = ["工具未返回的指标不得给出数值", "不得编造", "如实反映",
                       "不构成投资建议"]
    for phrase in honesty_phrases:
        check(f"SYSTEM_PROMPT 诚实缺失要求 {phrase}", phrase in prompt)

    schema_names = [s["function"]["name"] for s in main_module.TOOL_SCHEMAS]
    check("Tool Schema 注册 5 个工具",
          schema_names == ["get_stock_price", "get_technical_analysis",
                           "get_stock_fundamentals", "get_valuation_analysis",
                           "get_stock_news"],
          f"names={schema_names}")
    check("TOOL_DISPATCH 与 Schema 一一对应",
          all(n in main_module.TOOL_DISPATCH for n in schema_names))
    for s in main_module.TOOL_SCHEMAS:
        check(
            f"Schema 结构完整（{s['function']['name']}）",
            s["type"] == "function" and "parameters" in s["function"],
        )


# ---------------------------------------------------------------------------
# I. 缺失数据如实表述不误报（正向用例）
# ---------------------------------------------------------------------------
HONEST_ROE_REPORT = """【1. 市场概况与时效】
贵州茅台（600519）最新价格为 1272.83 元，涨跌幅 2.31%。行情交易日为 2026-08-19，获取时刻为 2026-08-21。
【2. 技术面量化】
MA5 为 1280.5，RSI14 为 42.73。
【3. 基本面概况】
PE 为 19.54。由于 Tushare 当前接口权限限制，本次无法获得 ROE 数据，因此本报告不提供 ROE 数值。
【4. 综合态势与风险提示】
技术面偏中性。风险提示：本报告基于公开数据，仅供研究和分析，不构成投资建议。
"""


def test_honest_missing_roe() -> None:
    print("测试 I：缺失数据如实表述不误报")
    violations = validate_report(HONEST_ROE_REPORT, _tool_results(roe=None))
    check("如实说明无法获得 ROE 不误报", violations == [], f"violations={violations}")


# ---------------------------------------------------------------------------
# J. 校验器基本行为
# ---------------------------------------------------------------------------
def test_validator_basics() -> None:
    print("测试 J：校验器基本行为")
    check("空报告被拒", validate_report("", _tool_results()) == ["报告为空"])
    check("空报告被拒（空白）", validate_report("   \n", _tool_results()) == ["报告为空"])

    short = "当前数据不足，无法分析。"
    check("简短回答可不要求小节", validate_report(short, _tool_results(),
                                             require_sections=False) == [])

    missing_sec = "【1. 市场概况与时效】\n【3. 基本面概况】\n【4. 综合态势与风险提示】"
    sec_violations = check_mandatory_sections(missing_sec)
    check("缺少【2. 技术面量化】被报告",
          any("技术面量化" in v for v in sec_violations), f"violations={sec_violations}")

    forbidden = check_forbidden_patterns("明天大概率上涨")
    check("“明天大概率上涨”被拦截", len(forbidden) >= 1, f"violations={forbidden}")

    # 直接检查单项时间混淆与未来新闻函数可独立调用
    tc = check_time_confusion(BAD_REPORT_E, _tool_results())
    check("check_time_confusion 独立调用生效",
          any("行情/交易时间" in v for v in tc), f"violations={tc}")
    fc = check_future_news_causality(BAD_REPORT_G, _tool_results(news=_FUTURE_NEWS))
    check("check_future_news_causality 独立调用生效",
          any("晚于行情日期" in v for v in fc), f"violations={fc}")

    miss = check_missing_indicator_claims(
        "RSI14 为 99.9", _tool_results(rsi14=None))
    check("check_missing_indicator_claims 独立调用生效", len(miss) >= 1, f"violations={miss}")

    check("TIER1 指标清单覆盖核心指标",
          {"MA5", "MA20", "MA60", "RSI14", "RSI", "MACD", "DIF", "DEA", "ATR14",
           "PE", "PB", "ROE", "毛利率", "股息率"}.issubset(set(TIER1_ACCESSORS)))


# ---------------------------------------------------------------------------
# K. 校验器准确性修复回归（修复 A-D：针对真实输出误报项）
# ---------------------------------------------------------------------------
# 基准报告：仅含与合成工具结果一致的真实数值。
K1_REPORT = """【1. 市场概况与时效】
贵州茅台（600519）最新价格为 1272.83 元，涨跌幅 2.31%。行情交易日为 2026-08-19，获取时刻为 2026-08-21。数据来源为东方财富公开行情（AKShare）。
【2. 技术面量化】
MA5 为 1280.5，MA20 为 1290.1，MA60 为 1271.51，RSI14 为 42.73，MACD 柱为 -1.2，ATR14 为 18.5。成交量：3,200,000 手，成交额：405.0 亿。
【3. 基本面概况】
PE 为 19.54，PB 为 6.33，ROE 为 30.4，EPS 为 16.6，毛利率为 89.56，股息率为 4.09。总市值：约 15,989.2 亿元。
【4. 综合态势与风险提示】
技术面偏中性。风险提示：本报告基于公开数据，仅供研究和分析，不构成投资建议。
"""


def test_fix_a_comma_grouping_and_units() -> None:
    print("测试 K1：千分位与中文单位合法数值不误报（修复项 A）")
    violations = validate_report(K1_REPORT, _tool_results())
    check("千分位/单位换算数值通过校验", violations == [], f"violations={violations}")

    forged = K1_REPORT.replace("3,200,000", "9,999,999").replace("15,989.2", "20,000")
    violations = validate_report(forged, _tool_results())
    hits = [v for v in violations if "9,999,999" in v or "20,000" in v]
    check("伪造千分位/单位数值被拦截", len(hits) >= 2, f"violations={violations}")


K2_REPORT = """【1. 市场概况与时效】
贵州茅台（600519）最新价格为 1272.83 元，涨跌幅 2.31%。该价格数据仅能确认是 2026-08-21 交易日的行情/快照数据。
【2. 技术面量化】
MA5 为 1280.5，RSI14 为 42.73。
【3. 基本面概况】
PE 为 19.54。
【4. 综合态势与风险提示】
技术面偏中性。风险提示：本报告基于公开数据，仅供研究和分析，不构成投资建议。
"""


def test_fix_a_date_continuation_and_alpha_prefix() -> None:
    print("测试 K2：日期续写与指标参数前字母不误报（修复项 A）")
    violations = validate_report(K2_REPORT, _tool_results())
    check("日期续写年份不被当作数值", violations == [], f"violations={violations}")

    alpha = K2_REPORT.replace(
        "该价格数据仅能确认是 2026-08-21 交易日的行情/快照数据",
        "收盘价略高于 MA60（1271.51）")
    violations = validate_report(alpha, _tool_results())
    check("MA60 的参数 60 不被当作数值", violations == [], f"violations={violations}")


def test_fix_b_indicator_parameter_echo() -> None:
    print("测试 K3：指标参数回显不误报（修复项 B）")
    claims = check_indicator_claims(
        "MA5（5日均线）为 1280.5，ATR14（14日波动）为 18.5", _tool_results())
    check("MA5/ATR14 参数回显不产生违规", claims == [], f"violations={claims}")

    bad = check_indicator_claims("MA5 为 9999.99", _tool_results())
    check("MA5 伪造值仍被拦截", len(bad) >= 1, f"violations={bad}")


def test_fix_c_attributive_time_confusion() -> None:
    print("测试 K4：属性词定中结构匹配时间混淆（修复项 C）")
    data_date_report = K2_REPORT.replace(
        "该价格数据仅能确认是 2026-08-21 交易日的行情/快照数据",
        "估值数据日期（data_date）为 20260821。")
    violations = validate_report(data_date_report, _tool_results())
    check("data_date 估值数据日期不误报", violations == [], f"violations={violations}")

    confusion = K2_REPORT.replace(
        "该价格数据仅能确认是 2026-08-21 交易日的行情/快照数据",
        "本报告期财务数据为 2026-08-21 当天的财务指标。")
    violations = validate_report(confusion, _tool_results())
    hits = [v for v in violations if "财务数据日期" in v]
    check("报告期混淆仍被拦截", len(hits) >= 1, f"violations={violations}")

    dedupe = K2_REPORT.replace(
        "该价格数据仅能确认是 2026-08-21 交易日的行情/快照数据",
        "报告期为 2026-08-21，财务数据为 2026-08-21。")
    violations = validate_report(dedupe, _tool_results())
    dup = [v for v in violations if "财务数据日期" in v]
    check("同类时间混淆去重为 1 条", len(dup) == 1, f"violations={dup}")


def test_fix_d_quoted_citation_skip() -> None:
    print("测试 K5：引号内转述不判违禁（修复项 D）")
    unquoted = check_forbidden_patterns("明天一定会涨")
    check("未加引号违禁仍被拦截", len(unquoted) >= 1, f"violations={unquoted}")

    quoted_cn = check_forbidden_patterns("关于“明天一定会涨”这一问题")
    check("中文引号内转述不误报", quoted_cn == [], f"violations={quoted_cn}")

    quoted_cn2 = check_forbidden_patterns("不提供“可以全仓买入”的买卖结论")
    check("中文引号内买卖措辞不误报", quoted_cn2 == [], f"violations={quoted_cn2}")

    quoted_ascii = check_forbidden_patterns('关于"明天一定会涨"这一问题')
    check("ASCII 引号内转述不误报", quoted_ascii == [], f"violations={quoted_ascii}")


# Phase 20C 调优：新闻文本数字豁免 + 指标名间隔排除字母（修复跨指标污染）
K6_NEWS = {
    "symbol": "600519",
    "name": "贵州茅台",
    "items": [
        {
            "title": "茅台预计上半年营收 812 亿元",
            "content": "贵州茅台发布经营预告，预计上半年实现营收约 812 亿元，"
                      "同比增长约 15%，但渠道库存压力有所显现，部分经销商反馈动销放缓。",
            "summary": "上半年营收预计 812 亿元，动销放缓或对增速形成拖累",
            "published_at": "2026-08-18T10:00:00+00:00",
            "source": "财联社",
        }
    ],
    "fetched_at": "2026-08-21T03:00:00+00:00",
}

K6_REPORT = """【1. 市场概况与时效】
贵州茅台（600519）最新价格为 1272.83 元，涨跌幅 2.31%。该价格数据仅能确认是 2026-08-21 交易日的行情/快照数据。
【2. 技术面量化】
MA5 为 1280.5，价格仍在 MA5 与 MA60 之上，RSI14 为 42.73，MACD 柱为 -1.2，ATR14 为 18.5。
【3. 基本面概况】
PE 为 19.54。
【4. 综合态势与风险提示】
据新闻所述，公司上半年营收约 812 亿元，同比增长约 15%；结合上述新闻，公司可能面临动销放缓、销量下滑的风险。技术面偏中性。风险提示：本报告基于公开数据，仅供研究和分析，不构成投资建议。
"""


def test_fix_e_news_restatement_and_indicator_gap() -> None:
    print("测试 K6：新闻文本数字豁免 + 指标名间隔排除字母（Phase 20C 调优）")

    # (1) 基于新闻原文的事件复述 + 客观风险推演：不应被判定"无证据编造"
    violations = validate_report(K6_REPORT, _tool_results(news=K6_NEWS))
    check("新闻复述与风险推演通过全部校验", violations == [], f"violations={violations}")

    # (2) 同一报告在无新闻工具结果时，"营收 812" 仍是编造 → 验证豁免确实由新闻数据驱动
    no_news = validate_report(K6_REPORT, _tool_results(news=None))
    hits = [v for v in no_news if "营收" in v]
    check("无新闻数据时营收数值仍被拦截", len(hits) >= 1, f"violations={no_news}")

    # (3) 跨指标污染：MA5 不得吞掉 MA60 的 60，罗列式也不得串扰
    claims = check_indicator_claims("价格仍在 MA5 与 MA60 之上", _tool_results())
    check("MA5 与 MA60 不跨指标误配", claims == [], f"claims={claims}")

    listed = check_indicator_claims(
        "MA5/MA20/MA60、RSI14、MACD（DIF/DEA/柱）、ATR14 均来自工具返回",
        _tool_results(),
    )
    check("指标罗列不产生跨指标污染", listed == [], f"claims={listed}")

    # (4) 伪造值仍被拦截（放宽仅针对跨指标污染，不放松证据链本身）
    forged = check_indicator_claims("MA5 为 9999.99", _tool_results())
    check("MA5 伪造值仍被拦截", len(forged) >= 1, f"violations={forged}")

    # (5) 风险推演表述不误判为违禁表达
    risk = check_forbidden_patterns("结合上述新闻，公司可能面临销量下滑的风险")
    check("客观风险推演不误报", risk == [], f"violations={risk}")

    # (6) 底线重申：绝对性预测与直接操作建议仍被严格拦截
    bottom = check_forbidden_patterns("明天一定会涨，建议全仓买入")
    check("绝对性预测+操作建议仍被拦截", len(bottom) >= 1, f"violations={bottom}")
    check(
        "绝对性预测示例被拦截",
        len(check_forbidden_patterns("明天一定会涨")) >= 1,
    )
    check(
        "直接操作建议示例被拦截",
        len(check_forbidden_patterns("建议全仓买入")) >= 1,
    )


# Phase 20C 调优：均线周期参数（"N日均线"）不当作指标数值，避免技术面误报
K7_REPORT = """【1. 市场概况与时效】
贵州茅台（600519）最新价格 1272.83 元，涨跌幅 2.31%。行情交易日为 2026-08-19。
【2. 技术面量化】
MA5 为 1280.5，MA20 为 1290.1，MA60 为 1271.51，价格仍在 20 日均线之上；
RSI14 为 42.73，DIF 为 -8.2，DEA 为 -7.6，MACD 为 -8.2，MACD 柱为 -1.2，ATR14 为 18.5。
MACD 位于 20 日均线上方，短期均线 5 日均线与 10 日均线交叉后向上，60 日线趋势平稳。
【3. 基本面概况】
PE 为 19.54。
【4. 综合态势与风险提示】
价格处于 20 日均线与 MA60 之间，整体偏强震荡，当前数据不足以支持确定性的上涨判断。
风险提示：本报告基于公开数据，仅供研究和分析，不构成投资建议。
"""


def test_fix_f_period_parameter_exemption() -> None:
    print("测试 K7：均线周期参数（N日均线）不当作指标数值（Phase 20C 调优）")

    # (1) 含"20日均线/60日线"措辞的技术报告通过全部校验
    violations = validate_report(K7_REPORT, _tool_results())
    check("周期参数措辞通过全部校验", violations == [], f"violations={violations}")

    # (2) MACD 不把"20 日均线"的 20 当作 MACD 数值
    claims = find_indicator_claims("MACD 位于 20 日均线上方", "MACD")
    check("MACD 不把 20 日均线的 20 当作数值", claims == [], f"claims={claims}")

    # (3) 价格不把周期参数 20 当作数值
    tier2 = check_indicator_claims("价格仍在 20 日均线之上", _tool_results())
    check("价格不把周期参数 20 当作数值", tier2 == [], f"violations={tier2}")

    # (4) 伪造值仍被拦截（豁免仅针对周期参数，不放松证据链本身）
    forged = check_indicator_claims("MACD 为 9999.99", _tool_results())
    check("MACD 伪造值仍被拦截", len(forged) >= 1, f"violations={forged}")


# Phase 20C 调优：新闻数值按绝对值豁免——"净利润下降 1.95%"复述为"净利同比 -1.95"
# 不算无证据编造；但工具数值符号仍严格（DIF -8.2 不得写成 +8.2）。
K8_NEWS = {
    "symbol": "600519",
    "name": "贵州茅台",
    "items": [
        {
            "title": "贵州茅台(600519.SH)：2026年中报净利润为445.17亿元、同比较去年同期下降1.95%",
            "content": "2026年8月15日，贵州茅台(600519.SH)发布2026年中报。"
                       "公司营业总收入为922.78亿元，较去年同报告期营业总收入增加11.84亿元。",
            "summary": "中报净利润 445.17 亿元，同比下降 1.95%",
            "published_at": "2026-08-15T10:11:51+00:00",
            "source": "界面新闻",
        },
        {
            "title": "分析人士指出，贵州茅台当前滚动市盈率约 24.5 倍",
            "content": "有分析人士表示，贵州茅台估值处于历史中枢附近，滚动市盈率约 24.5 倍。",
            "summary": "滚动市盈率约 24.5 倍",
            "published_at": "2026-08-20T09:30:00+00:00",
            "source": "证券时报",
        },
    ],
    "fetched_at": "2026-08-21T03:00:00+00:00",
}

K8_REPORT = """【1. 市场概况与时效】
贵州茅台（600519）近期发布 2026 年中报，相关报道来源为界面新闻。
【2. 技术面量化】
本次未获取技术面指标数据（未调用 get_technical_analysis 工具）。
【3. 基本面概况】
据新闻所述，公司 2026 年中报净利润为 445.17 亿元，同比去年下降 1.95%（净利同比 -1.95%）。
【4. 综合态势与风险提示】
据新闻报道，公司营收延续增长，但净利润出现小幅下滑，相关报道指出市场情绪或偏谨慎。
风险提示：本报告基于公开数据，仅供研究和分析，不构成投资建议。
"""


def test_fix_g_news_restatement_sign_exemption() -> None:
    print("测试 K8：新闻数值按绝对值豁免（下降 1.95% 复述为 -1.95 不算编造，Phase 20C 调优）")

    news_only = {"get_stock_news": K8_NEWS}

    # (1) 基于新闻原文的"净利同比 -1.95%"复述通过全部校验
    violations = validate_report(K8_REPORT, news_only)
    check("新闻复述（负号转写）通过全部校验", violations == [], f"violations={violations}")

    # (2) 同一报告无新闻数据时仍被拦截 → 豁免确实由新闻证据驱动
    no_news = validate_report(K8_REPORT, _tool_results(news=None))
    hits = [v for v in no_news if "净利" in v]
    check("无新闻数据时净利数值仍被拦截", len(hits) >= 1, f"violations={no_news}")

    # (3) 工具数值符号严格：DIF 工具值 -8.2 写成 +8.2 仍被拦截（豁免不覆盖工具数据）
    strict = check_indicator_claims("DIF 为 8.2", _tool_results())
    check("工具数值符号仍严格（DIF 8.2 拦截）", len(strict) >= 1, f"violations={strict}")

    # (4) 不在新闻中的编造净利数值仍被拦截
    forged = check_indicator_claims("净利同比 -9999.99", news_only)
    check("净利编造值仍被拦截", len(forged) >= 1, f"violations={forged}")

    # (5) TIER1 缺失数据检查同样受新闻豁免：新闻中的 PE 数字不算编造
    missing = check_missing_indicator_claims("据新闻所述，公司当前 PE 为 24.5", news_only)
    check("新闻中 PE 数字通过缺失数据检查", missing == [], f"violations={missing}")
    missing_fake = check_missing_indicator_claims("据新闻所述，公司当前 PE 为 99.99", news_only)
    check("新闻中不存在的 PE 数字仍被拦截", len(missing_fake) >= 1, f"violations={missing_fake}")


def main() -> None:
    print("=" * 50)
    print("Output Quality Deterministic Tests (Phase 9)")
    print("=" * 50)
    print()
    test_good_report_passes_all_checks()
    print()
    test_forbidden_future_prediction()
    print()
    test_forbidden_buy_advice()
    print()
    test_missing_rsi_fabrication()
    print()
    test_time_confusion_market_date()
    print()
    test_time_confusion_report_period()
    print()
    test_future_news_causality()
    print()
    test_system_prompt_spec()
    print()
    test_honest_missing_roe()
    print()
    test_validator_basics()
    print()
    test_fix_a_comma_grouping_and_units()
    print()
    test_fix_a_date_continuation_and_alpha_prefix()
    print()
    test_fix_b_indicator_parameter_echo()
    print()
    test_fix_c_attributive_time_confusion()
    print()
    test_fix_d_quoted_citation_skip()
    print()
    test_fix_e_news_restatement_and_indicator_gap()
    print()
    test_fix_f_period_parameter_exemption()
    test_fix_g_news_restatement_sign_exemption()
    print()
    print("-" * 50)
    if _FAILURES:
        print(f"结果：{len(_FAILURES)} 项失败 -> {_FAILURES}")
        sys.exit(1)
    print("结果：全部通过")
    sys.exit(0)


if __name__ == "__main__":
    main()
