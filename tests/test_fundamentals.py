"""第七阶段测试：A 股基本面数据（Mock 纯逻辑 + LIVE 真实接口）。

Mock 部分覆盖用户要求的场景：
1. 贵州茅台完整数据        2. 正常股票（部分数据）
3. 无数据                 4. 缺 PE
5. 负 PE（保留+标记）       6. 缺 ROE
7. 多报告期               8. 重复数据去重
9. 非法代码               10. 接口无权限
11. 单位换算数值断言       12. 三时间严格区分（data_date/report_period/fetched_at）
13. 纯函数单元测试         14. Tool Schema 描述与参数
15. Tool 函数 Provider 注入

LIVE 部分（--live 或 FUNDAMENTALS_LIVE_TEST=1）：
- 探测 daily_basic / income / fina_indicator 真实权限
- 用真实接口获取贵州茅台基本面并如实报告权限状态
- 结论格式：Tushare fundamentals LIVE API：OK / PARTIAL / NO_PERMISSION（如实报告）

数据仅用于研究和分析，不构成投资建议。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from app.data.tushare_client import (  # noqa: E402
    TushareClient,
    TushareTokenMissingError,
)
from app.fundamentals.processing import (  # noqa: E402
    check_valuation_issues,
    extract_dividend,
    extract_income,
    extract_profitability,
    extract_valuation,
    normalize_daily_basic_rows,
    normalize_fina_rows,
    normalize_income_rows,
)
from app.fundamentals.providers import (  # noqa: E402
    FUNDAMENTALS_API_PERMISSION_REQUIRED,
    MockFundamentalProvider,
    TushareFundamentalProvider,
)
from app.tools.fundamentals_tool import (  # noqa: E402
    FUNDAMENTALS_TOOL_SCHEMA,
    get_stock_fundamentals,
)
from fixtures.fundamentals_mock import (  # noqa: E402
    DUPLICATE_FINA_ROWS,
    FakePermissionClient,
    INVALID_SYMBOLS,
    MAOTAI_DAILY_BASIC_ROWS,
    MAOTAI_FINA_ROWS,
    MAOTAI_INCOME_ROWS,
    MAOTAI_NAME,
    MAOTAI_TS_CODE,
    MISSING_PE_DAILY_BASIC_ROWS,
    MISSING_ROE_FINA_ROWS,
    MULTI_PERIOD_FINA_ROWS,
    NEGATIVE_PE_DAILY_BASIC_ROWS,
    NO_DATA_DAILY_BASIC_ROWS,
    NO_DATA_FINA_ROWS,
    NO_DATA_INCOME_ROWS,
    NORMAL_DAILY_BASIC_ROWS,
    NORMAL_NAME,
    NORMAL_TS_CODE,
)

FAILURES: list = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  [PASS] {name}")
    else:
        print(f"  [FAIL] {name} {detail}")
        FAILURES.append(name)


# ---------------------------------------------------------------------------
# 1. 贵州茅台完整数据
# ---------------------------------------------------------------------------
def test_maotai_full() -> None:
    print("\n[1] 贵州茅台完整数据")
    provider = MockFundamentalProvider(
        daily_basic_rows=MAOTAI_DAILY_BASIC_ROWS,
        income_rows=MAOTAI_INCOME_ROWS,
        fina_rows=MAOTAI_FINA_ROWS,
        name=MAOTAI_NAME,
    )
    result = provider.get_fundamentals(MAOTAI_TS_CODE)

    check("symbol", result.get("symbol") == MAOTAI_TS_CODE, str(result.get("symbol")))
    check("name", result.get("name") == MAOTAI_NAME, str(result.get("name")))
    check("market", result.get("market") == "A-share")
    check("asset_type", result.get("asset_type") == "stock")
    check("data_source", result.get("data_source") == "Mock")
    check("notice 存在", "notice" in result and "模拟数据" in result["notice"])

    val = result.get("valuation", {})
    check("pe=24.5", val.get("pe") == 24.5)
    check("pb=7.9", val.get("pb") == 7.9)
    check("ps=9.2", val.get("ps") == 9.2)
    check("total_market_cap=18968.0", val.get("total_market_cap") == 18968.0)
    check("float_market_cap=18968.0", val.get("float_market_cap") == 18968.0)

    prof = result.get("profitability", {})
    check("roe=9.6", prof.get("roe") == 9.6)
    check("eps=16.6", prof.get("eps") == 16.6)

    grow = result.get("growth", {})
    check("revenue=418.0", grow.get("revenue") == 418.0)
    check("revenue_growth=12.3", grow.get("revenue_growth") == 12.3)
    check("net_profit=208.0", grow.get("net_profit") == 208.0)
    check("net_profit_growth=13.8", grow.get("net_profit_growth") == 13.8)

    div = result.get("dividend", {})
    check("dividend_yield=2.05", div.get("dividend_yield") == 2.05)

    check("data_date=20260819", result.get("data_date") == "20260819")
    check("report_period=20260331", result.get("report_period") == "20260331")
    check("report_periods 列表",
          result.get("data_quality", {}).get("report_periods") == ["20260331", "20251231"])
    dq = result.get("data_quality", {})
    check("issues 为空", dq.get("issues") == [])
    check("dedupe 全 0", dq.get("dedupe") == {
        "daily_basic_removed": 0, "income_removed": 0, "fina_indicator_removed": 0})
    check("units 含市值单位", dq.get("units", {}).get("total_market_cap") == "亿元")
    check("units 含营收单位", dq.get("units", {}).get("revenue") == "亿元")


# ---------------------------------------------------------------------------
# 2. 正常股票（部分数据）
# ---------------------------------------------------------------------------
def test_normal_stock() -> None:
    print("\n[2] 正常股票（部分数据，pe/dv 回退）")
    provider = MockFundamentalProvider(
        daily_basic_rows=NORMAL_DAILY_BASIC_ROWS,
        income_rows=[],
        fina_rows=[],
        name=NORMAL_NAME,
    )
    result = provider.get_fundamentals(NORMAL_TS_CODE)

    val = result.get("valuation", {})
    check("pe 回退=5.2", val.get("pe") == 5.2)
    check("pb=0.55", val.get("pb") == 0.55)
    check("ps=None", val.get("ps") is None)
    check("total_market_cap=2300.0", val.get("total_market_cap") == 2300.0)
    div = result.get("dividend", {})
    check("dividend_yield 回退=6.1", div.get("dividend_yield") == 6.1)

    prof = result.get("profitability", {})
    check("无 fina 数据 roe=None", prof.get("roe") is None)
    check("无 fina 数据 eps=None", prof.get("eps") is None)
    grow = result.get("growth", {})
    check("无 income/fina 数据 revenue=None", grow.get("revenue") is None)
    check("无 income/fina 数据 net_profit=None", grow.get("net_profit") is None)
    check("report_period=None", result.get("report_period") is None)


# ---------------------------------------------------------------------------
# 3. 无数据
# ---------------------------------------------------------------------------
def test_no_data() -> None:
    print("\n[3] 无数据")
    provider = MockFundamentalProvider(
        daily_basic_rows=NO_DATA_DAILY_BASIC_ROWS,
        income_rows=NO_DATA_INCOME_ROWS,
        fina_rows=NO_DATA_FINA_ROWS,
        name=MAOTAI_NAME,
    )
    result = provider.get_fundamentals(MAOTAI_TS_CODE)
    check("error=no_data", result.get("error") == "no_data", str(result.get("error")))
    check("带 symbol", result.get("symbol") == MAOTAI_TS_CODE)


# ---------------------------------------------------------------------------
# 4. 缺 PE
# ---------------------------------------------------------------------------
def test_missing_pe() -> None:
    print("\n[4] 缺 PE（pe/ps 为 None，不伪造）")
    provider = MockFundamentalProvider(
        daily_basic_rows=MISSING_PE_DAILY_BASIC_ROWS,
        income_rows=MAOTAI_INCOME_ROWS,
        fina_rows=MAOTAI_FINA_ROWS,
        name=MAOTAI_NAME,
    )
    result = provider.get_fundamentals(MAOTAI_TS_CODE)
    val = result.get("valuation", {})
    check("pe=None", val.get("pe") is None)
    check("ps=None", val.get("ps") is None)
    check("pb 保留=7.9", val.get("pb") == 7.9)
    check("issues 无异常", result.get("data_quality", {}).get("issues") == [])


# ---------------------------------------------------------------------------
# 5. 负 PE（保留原值并在 issues 标记）
# ---------------------------------------------------------------------------
def test_negative_pe() -> None:
    print("\n[5] 负 PE（保留原值 + issues 标记）")
    provider = MockFundamentalProvider(
        daily_basic_rows=NEGATIVE_PE_DAILY_BASIC_ROWS,
        income_rows=MAOTAI_INCOME_ROWS,
        fina_rows=MAOTAI_FINA_ROWS,
        name=MAOTAI_NAME,
    )
    result = provider.get_fundamentals(MAOTAI_TS_CODE)
    val = result.get("valuation", {})
    check("pe=-8.6 保留", val.get("pe") == -8.6)
    issues = result.get("data_quality", {}).get("issues", [])
    check("issues 含非正 PE", any(
        i.get("field") == "pe" and i.get("issue") == "non_positive" for i in issues),
        str(issues))


# ---------------------------------------------------------------------------
# 6. 缺 ROE
# ---------------------------------------------------------------------------
def test_missing_roe() -> None:
    print("\n[6] 缺 ROE（roe=None，eps 保留）")
    provider = MockFundamentalProvider(
        daily_basic_rows=MAOTAI_DAILY_BASIC_ROWS,
        income_rows=MAOTAI_INCOME_ROWS,
        fina_rows=MISSING_ROE_FINA_ROWS,
        name=MAOTAI_NAME,
    )
    result = provider.get_fundamentals(MAOTAI_TS_CODE)
    prof = result.get("profitability", {})
    check("roe=None", prof.get("roe") is None)
    check("eps=16.6", prof.get("eps") == 16.6)


# ---------------------------------------------------------------------------
# 7. 多报告期
# ---------------------------------------------------------------------------
def test_multi_period() -> None:
    print("\n[7] 多报告期（取最新 + report_periods 列表）")
    provider = MockFundamentalProvider(
        daily_basic_rows=MAOTAI_DAILY_BASIC_ROWS,
        income_rows=[],
        fina_rows=MULTI_PERIOD_FINA_ROWS,
        name=MAOTAI_NAME,
    )
    result = provider.get_fundamentals(MAOTAI_TS_CODE)
    prof = result.get("profitability", {})
    check("取最新报告期 roe=9.6", prof.get("roe") == 9.6)
    check("取最新报告期 eps=16.6", prof.get("eps") == 16.6)
    check("report_period=20260331", result.get("report_period") == "20260331")
    periods = result.get("data_quality", {}).get("report_periods", [])
    check("report_periods 降序完整",
          periods == ["20260331", "20250630", "20250331"], str(periods))
    dedupe = result.get("data_quality", {}).get("dedupe", {})
    check("无重复故 removed=0", dedupe.get("fina_indicator_removed") == 0)


# ---------------------------------------------------------------------------
# 8. 重复数据
# ---------------------------------------------------------------------------
def test_duplicate_rows() -> None:
    print("\n[8] 重复数据（同一报告期保留 ann_date 最新）")
    provider = MockFundamentalProvider(
        daily_basic_rows=MAOTAI_DAILY_BASIC_ROWS,
        income_rows=MAOTAI_INCOME_ROWS,
        fina_rows=DUPLICATE_FINA_ROWS,
        name=MAOTAI_NAME,
    )
    result = provider.get_fundamentals(MAOTAI_TS_CODE)
    prof = result.get("profitability", {})
    check("保留 ann_date 最新 roe=9.9", prof.get("roe") == 9.9)
    check("保留 ann_date 最新 eps=16.7", prof.get("eps") == 16.7)
    dedupe = result.get("data_quality", {}).get("dedupe", {})
    check("fina_indicator_removed=1", dedupe.get("fina_indicator_removed") == 1)


# ---------------------------------------------------------------------------
# 9. 非法代码
# ---------------------------------------------------------------------------
def test_invalid_symbols() -> None:
    print("\n[9] 非法代码")
    provider = MockFundamentalProvider(
        daily_basic_rows=MAOTAI_DAILY_BASIC_ROWS,
        income_rows=MAOTAI_INCOME_ROWS,
        fina_rows=MAOTAI_FINA_ROWS,
        name=MAOTAI_NAME,
    )
    bad = 0
    for symbol in INVALID_SYMBOLS:
        result = provider.get_fundamentals(symbol)
        if result.get("error") == "invalid_symbol":
            bad += 1
        else:
            print(f"  [FAIL] 非法代码未被拒绝: {symbol!r}")
            FAILURES.append("invalid_symbol")
    check(f"全部 {len(INVALID_SYMBOLS)} 个非法代码返回 invalid_symbol",
          bad == len(INVALID_SYMBOLS))


# ---------------------------------------------------------------------------
# 10. 接口无权限
# ---------------------------------------------------------------------------
def test_permission_denied() -> None:
    print("\n[10] 接口无权限（不得绕过、不得用 Mock 冒充）")
    provider = TushareFundamentalProvider(client=FakePermissionClient())
    result = provider.get_fundamentals(MAOTAI_TS_CODE)
    check("error=fundamentals_api_permission_required",
          result.get("error") == FUNDAMENTALS_API_PERMISSION_REQUIRED,
          str(result.get("error")))
    check("不返回任何数据字段", "valuation" not in result)
    check("不带 notice（非 Mock）", "notice" not in result)


# ---------------------------------------------------------------------------
# 11. 单位换算数值断言（已内嵌于 1/2 中，此处再对纯函数断言）
# ---------------------------------------------------------------------------
def test_units_pure() -> None:
    print("\n[11] 单位换算纯函数断言")
    val = extract_valuation(MAOTAI_DAILY_BASIC_ROWS[0])
    check("total_mv 万元->亿元", val["total_market_cap"] == 18968.0)
    check("circ_mv 万元->亿元", val["float_market_cap"] == 18968.0)
    inc = extract_income(MAOTAI_INCOME_ROWS[0])
    check("revenue 元->亿元", inc["revenue"] == 418.0)
    check("net_profit 元->亿元", inc["net_profit"] == 208.0)


# ---------------------------------------------------------------------------
# 12. 三时间严格区分
# ---------------------------------------------------------------------------
def test_timeline_distinction() -> None:
    print("\n[12] 三时间区分（data_date / report_period / fetched_at）")
    provider = MockFundamentalProvider(
        daily_basic_rows=MAOTAI_DAILY_BASIC_ROWS,
        income_rows=MAOTAI_INCOME_ROWS,
        fina_rows=MAOTAI_FINA_ROWS,
        name=MAOTAI_NAME,
    )
    result = provider.get_fundamentals(MAOTAI_TS_CODE)
    data_date = result.get("data_date")
    report_period = result.get("report_period")
    fetched_at = result.get("fetched_at")
    check("data_date=20260819", data_date == "20260819")
    check("report_period=20260331", report_period == "20260331")
    check("data_date != report_period", data_date != report_period)
    check("fetched_at 为 ISO 时间（含 T）", isinstance(fetched_at, str) and "T" in fetched_at)
    check("fetched_at 与 data_date 不同", fetched_at.startswith(str(data_date)) is False)
    check("report_period 保留 API 原始格式(YYYYMMDD)",
          len(report_period) == 8 and report_period.isdigit())


# ---------------------------------------------------------------------------
# 13. 其他纯函数
# ---------------------------------------------------------------------------
def test_pure_functions() -> None:
    print("\n[13] 其他纯函数（NaN 安全 / 空输入 / 归一化）")
    row = {"trade_date": "20260819", "pe_ttm": float("nan"), "pb": 7.9}
    val = extract_valuation(row)
    check("NaN pe 安全转 None", val["pe"] is None)
    check("NaN 不影响 pb", val["pb"] == 7.9)

    empty = extract_valuation(None)
    check("空行估值全 None", all(v is None for v in empty.values()))
    empty_prof = extract_profitability(None)
    check("空行盈利全 None", empty_prof == {"roe": None, "eps": None})
    empty_div = extract_dividend(None)
    check("空行股息 None", empty_div == {"dividend_yield": None})

    norm = normalize_daily_basic_rows([])
    check("空 daily_basic 归一化", norm == {"latest": None, "removed": 0})
    fn = normalize_fina_rows(DUPLICATE_FINA_ROWS)
    check("fina 去重后 latest 为 20260515 版本", fn["latest"]["ann_date"] == "20260515")
    check("fina 去重 removed=1", fn["removed"] == 1)
    inc = normalize_income_rows(MAOTAI_INCOME_ROWS)
    check("income 最新为 20260331", inc["latest"]["end_date"] == "20260331")

    check("正常估值无 issues", check_valuation_issues(val) == [])
    neg = check_valuation_issues({"pe": -1.0, "pb": 0.0, "ps": 5.0})
    check("非正 PE/PB 被标记", len(neg) == 2, str(neg))


# ---------------------------------------------------------------------------
# 14. Tool Schema
# ---------------------------------------------------------------------------
def test_tool_schema() -> None:
    print("\n[14] Tool Schema")
    func = FUNDAMENTALS_TOOL_SCHEMA.get("function", {})
    check("name=get_stock_fundamentals", func.get("name") == "get_stock_fundamentals")
    expected_desc = "获取指定 A 股公司的公开基本面、估值及财务指标，用于辅助投资研究，不构成投资建议。"
    check("description 精确匹配", func.get("description") == expected_desc)
    params = func.get("parameters", {})
    check("params.type=object", params.get("type") == "object")
    check("required=['symbol']", params.get("required") == ["symbol"])
    props = params.get("properties", {})
    check("symbol 为 string", props.get("symbol", {}).get("type") == "string")


# ---------------------------------------------------------------------------
# 15. Tool 函数 Provider 注入
# ---------------------------------------------------------------------------
def test_tool_function() -> None:
    print("\n[15] Tool 函数 Provider 注入")
    provider = MockFundamentalProvider(
        daily_basic_rows=MAOTAI_DAILY_BASIC_ROWS,
        income_rows=MAOTAI_INCOME_ROWS,
        fina_rows=MAOTAI_FINA_ROWS,
        name=MAOTAI_NAME,
    )
    result = get_stock_fundamentals("600519", provider=provider)
    check("无后缀 600519 归一化为 600519.SH",
          result.get("symbol") == "600519.SH", str(result.get("symbol")))
    check("估值数据存在", result.get("valuation", {}).get("pe") == 24.5)

    invalid = get_stock_fundamentals("12345", provider=provider)
    check("非法代码经 Tool 返回 invalid_symbol",
          invalid.get("error") == "invalid_symbol")


# ---------------------------------------------------------------------------
# LIVE 测试
# ---------------------------------------------------------------------------
def run_live_tests() -> None:
    print("\n========== LIVE 测试（真实 Tushare 接口）==========")
    try:
        client = TushareClient()
    except TushareTokenMissingError as exc:
        print(f"  [FAIL] token 未配置：{exc}")
        FAILURES.append("live_token")
        return

    # income/fina_indicator 单独探测；daily_basic 状态取 Provider 实际调用结果，
    # 避免重复调用消耗 daily_basic 当日调用额度。
    probes = (
        ("income", {"ts_code": "600519.SH", "start_date": "20250101", "end_date": "20260630"}),
        ("fina_indicator", {"ts_code": "600519.SH", "start_date": "20250101", "end_date": "20260630"}),
    )
    statuses = {}
    print("\n接口权限探测：")
    for api_name, kwargs in probes:
        res = client.check_interface_permission(api_name, **kwargs)
        statuses[api_name] = res["status"]
        print(f"  {api_name:<14} -> {res['status']} (count={res['count']})")

    provider = TushareFundamentalProvider(client=client)
    result = provider.get_fundamentals("600519.SH")
    print("\nTushareFundamentalProvider 结果：")
    print(f"  error={result.get('error')}")
    print(f"  symbol={result.get('symbol')} name={result.get('name')} "
          f"data_source={result.get('data_source')}")
    print(f"  valuation.pe={result.get('valuation', {}).get('pe')} "
          f"pb={result.get('valuation', {}).get('pb')} "
          f"total_market_cap={result.get('valuation', {}).get('total_market_cap')}")
    print(f"  profitability.roe={result.get('profitability', {}).get('roe')} "
          f"eps={result.get('profitability', {}).get('eps')}")
    print(f"  growth.revenue={result.get('growth', {}).get('revenue')} "
          f"net_profit={result.get('growth', {}).get('net_profit')}")
    print(f"  data_date={result.get('data_date')} "
          f"report_period={result.get('report_period')}")
    sources = result.get("data_quality", {}).get("sources") or result.get("sources_status")
    print(f"  sources={sources}")

    for item in sources or []:
        statuses[item["source"]] = item["status"]
    daily_status = statuses.get("daily_basic")
    income_ok = statuses.get("income") == "ok"
    fina_ok = statuses.get("fina_indicator") == "ok"

    if daily_status == "ok":
        check("LIVE daily_basic 有权限且返回真实估值",
              result.get("valuation", {}).get("pe") is not None)
        check("LIVE data_date 来自真实接口", result.get("data_date") is not None)
        check("LIVE data_source=Tushare", result.get("data_source") == "Tushare")
        check("LIVE 无 notice（非 Mock）", "notice" not in result)
    elif daily_status == "rate_limited":
        check("LIVE daily_basic 频率超限时如实报错（不伪造估值）",
              result.get("error") is not None
              and result.get("valuation", {}).get("pe") is None)
    else:
        check("LIVE daily_basic 无权限时如实报错", "error" in result)

    if not income_ok:
        check("LIVE income 非 ok 时 growth.revenue=None（不伪造）",
              result.get("growth", {}).get("revenue") is None)
        check("LIVE income 非 ok 时 growth.net_profit=None",
              result.get("growth", {}).get("net_profit") is None)
    if not fina_ok:
        check("LIVE fina 非 ok 时 roe/eps=None（不伪造）",
              result.get("profitability", {}).get("roe") is None
              and result.get("profitability", {}).get("eps") is None)
        check("LIVE fina 非 ok 时增速=None",
              result.get("growth", {}).get("revenue_growth") is None)

    if daily_status == "ok" and not income_ok and not fina_ok:
        verdict = ("PARTIAL（daily_basic 有权限，估值真实；"
                   "income/fina_indicator 无权限，盈利/成长为 None）")
    elif daily_status == "ok":
        verdict = "OK（daily_basic 有权限）"
    elif daily_status == "rate_limited":
        verdict = ("RATE_LIMITED（daily_basic 有权限但本次调用频率超限；"
                   "income/fina_indicator 无权限）")
    else:
        verdict = "NO_PERMISSION（如实报告）"
    print(f"\nTushare fundamentals LIVE API：{verdict}")


def main() -> None:
    print("第七阶段测试：A 股基本面数据（Mock 纯逻辑）")
    test_maotai_full()
    test_normal_stock()
    test_no_data()
    test_missing_pe()
    test_negative_pe()
    test_missing_roe()
    test_multi_period()
    test_duplicate_rows()
    test_invalid_symbols()
    test_permission_denied()
    test_units_pure()
    test_timeline_distinction()
    test_pure_functions()
    test_tool_schema()
    test_tool_function()

    if FAILURES:
        print(f"\nMock 基本面处理：FAIL（{len(FAILURES)} 项失败）")
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    print("\nMock 基本面处理：PASS")
    return 0


if __name__ == "__main__":
    live = "--live" in sys.argv or os.getenv("FUNDAMENTALS_LIVE_TEST") == "1"
    exit_code = main()
    if exit_code == 0 and live:
        run_live_tests()
        exit_code = 1 if FAILURES else 0
    elif live:
        print("Mock 失败，跳过 LIVE 测试。")
    sys.exit(exit_code)
