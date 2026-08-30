"""技术指标与历史数据单元测试。

覆盖：
1. MA 计算（数值断言）
2. RSI 计算（数值断言）
3. MACD 计算（数值断言 + 结构恒等式）
4. ATR 计算（数值断言）
5. 数据为空
6. 数据不足
7. 缺失值
8. 历史数据排序与去重

运行方式（项目根目录执行）：
    .venv\\Scripts\\python.exe tests/test_technical.py
"""

import math
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# 确保能导入项目根目录下的 app / tools 包
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Windows Git Bash 控制台中文输出需要显式使用 UTF-8
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd

from app.analysis.technical import (
    calculate_atr,
    calculate_ma,
    calculate_macd,
    calculate_rsi,
    calculate_technical_indicators,
)
from app.data.akshare_client import inspect_history, sort_and_dedupe

_FAILURES: List[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    """记录一条断言结果。"""
    status = "PASS" if condition else "FAIL"
    suffix = f"  [{detail}]" if detail and not condition else ""
    print(f"  [{status}] {name}{suffix}")
    if not condition:
        _FAILURES.append(name)


def assert_series_close(
    name: str, actual: pd.Series, expected: List[Optional[float]]
) -> None:
    """逐元素断言 Series 与期望列表接近，NaN 对应期望的 None/NaN。"""
    actual_list = list(actual)
    ok = len(actual_list) == len(expected)
    if ok:
        for a, e in zip(actual_list, expected):
            if e is None or (isinstance(e, float) and math.isnan(e)):
                if not (a is None or (isinstance(a, float) and math.isnan(a))):
                    ok = False
                    break
            else:
                if not (
                    a is not None
                    and not (isinstance(a, float) and math.isnan(a))
                    and math.isclose(float(a), float(e), abs_tol=1e-9)
                ):
                    ok = False
                    break
    check(name, ok, f"actual={actual_list}, expected={expected}")


def _ohlc_df(closes: List[float], pad: float = 0.5) -> pd.DataFrame:
    """根据收盘价构造包含 high/low 的 DataFrame。"""
    return pd.DataFrame(
        {
            "close": [float(c) for c in closes],
            "high": [float(c) + pad for c in closes],
            "low": [float(c) - pad for c in closes],
        }
    )


def _make_rows(
    closes: List[float],
    start_date: str = "2026-01-01",
    pad: float = 0.5,
) -> List[Dict[str, Any]]:
    """构造历史行情 dict 列表，日期从 start_date 起按天递增。"""
    from datetime import date, timedelta

    start = date.fromisoformat(start_date)
    rows = []
    for i, close in enumerate(closes):
        day = start + timedelta(days=i)
        rows.append(
            {
                "date": day.isoformat(),
                "open": float(close),
                "high": float(close) + pad,
                "low": float(close) - pad,
                "close": float(close),
                "volume": 1000000,
                "amount": None,
                "change_percent": None,
            }
        )
    return rows


# ---------------------------------------------------------------------------
# 1. MA 计算
# ---------------------------------------------------------------------------
def test_ma() -> None:
    print("测试 1：MA 计算")
    df = _ohlc_df([1, 2, 3, 4, 5])
    ma3 = calculate_ma(df, 3)
    assert_series_close("MA3 of [1,2,3,4,5]", ma3, [None, None, 2.0, 3.0, 4.0])
    check("MA5 最后值", math.isclose(float(ma3.iloc[-1]), 4.0, abs_tol=1e-12))


# ---------------------------------------------------------------------------
# 2. RSI 计算
# ---------------------------------------------------------------------------
def test_rsi() -> None:
    print("测试 2：RSI 计算")
    # 手工可推导：RSI(2)，[1,2,3,2] -> avg_gain/avg_loss -> 100 / 50
    df = _ohlc_df([1, 2, 3, 2])
    rsi = calculate_rsi(df, period=2)
    assert_series_close("RSI(2) of [1,2,3,2]", rsi, [None, None, 100.0, 50.0])

    # 严格上涨：全部为上涨，平均下跌为 0 -> RSI = 100
    up = _ohlc_df(list(range(1, 18)))
    check("RSI(5) 严格上涨=100", math.isclose(float(calculate_rsi(up, 5).iloc[-1]), 100.0))

    # 严格下跌：平均上涨为 0 -> RSI = 0
    down = _ohlc_df(list(range(17, 0, -1)))
    check("RSI(5) 严格下跌=0", math.isclose(float(calculate_rsi(down, 5).iloc[-1]), 0.0))


# ---------------------------------------------------------------------------
# 3. MACD 计算
# ---------------------------------------------------------------------------
def test_macd() -> None:
    print("测试 3：MACD 计算")
    df = _ohlc_df(list(range(1, 31)))
    macd = calculate_macd(df)
    dif, dea, hist = macd["macd"], macd["signal"], macd["histogram"]

    # 首日 EMA12=EMA26=close0 -> DIF=0；DEA 以 DIF 初始化 -> 0；柱=0
    check("MACD 首日 DIF=0", math.isclose(float(dif.iloc[0]), 0.0, abs_tol=1e-12))
    check("MACD 首日 Signal=0", math.isclose(float(dea.iloc[0]), 0.0, abs_tol=1e-12))
    check("MACD 首日 Histogram=0", math.isclose(float(hist.iloc[0]), 0.0, abs_tol=1e-12))

    # 恒等式：Histogram = 2 * (DIF - Signal)
    expected = (2.0 * (dif - dea)).round(12)
    actual = hist.round(12)
    check(
        "Histogram == 2*(DIF-Signal)",
        bool((expected == actual).all()),
        f"max_diff={float((expected - actual).abs().max())}",
    )

    # 结构断言：严格上涨序列从第 2 根K线起 DIF 应为正值（首日 EMA 初始化故为 0）
    check("严格上涨时 DIF>0", bool((dif.iloc[1:].dropna() > 0).all()))


# ---------------------------------------------------------------------------
# 4. ATR 计算
# ---------------------------------------------------------------------------
def test_atr() -> None:
    print("测试 4：ATR 计算")
    df = pd.DataFrame(
        {
            "high": [10.0, 11.0, 13.0],
            "low": [8.0, 9.0, 11.0],
            "close": [9.0, 10.0, 12.0],
        }
    )
    # TR = [max(10-8)=2, max(11-9,|11-9|,|9-9|)=2, max(13-11,|13-10|,|11-10|)=3]
    # ATR(2) Wilder 平滑 -> [NaN, 2.0, 2.5]
    atr = calculate_atr(df, period=2)
    assert_series_close("ATR(2) of 3 根K线", atr, [None, 2.0, 2.5])


# ---------------------------------------------------------------------------
# 5. 数据为空
# ---------------------------------------------------------------------------
def test_empty_data() -> None:
    print("测试 5：数据为空")
    empty = pd.DataFrame({"close": [], "high": [], "low": []})
    check("MA 空数据返回空 Series", len(calculate_ma(empty, 5)) == 0)

    try:
        calculate_technical_indicators([], "NVDA")
        check("空 rows 应抛出 ValueError", False)
    except ValueError:
        check("空 rows 应抛出 ValueError", True)


# ---------------------------------------------------------------------------
# 6. 数据不足
# ---------------------------------------------------------------------------
def test_insufficient_data() -> None:
    print("测试 6：数据不足（10 根K线）")
    rows = _make_rows([float(i) for i in range(1, 11)])
    result = calculate_technical_indicators(rows, "NVDA")
    check("MA60 数据不足为 None", result["trend"]["ma60"] is None)
    check("RSI14 数据不足为 None", result["momentum"]["rsi14"] is None)
    check("MA5 数据充足不为 None", result["trend"]["ma5"] is not None)
    check("ATR14 数据不足为 None", result["volatility"]["atr14"] is None)


# ---------------------------------------------------------------------------
# 7. 缺失值
# ---------------------------------------------------------------------------
def test_missing_values() -> None:
    print("测试 7：缺失值")
    closes: List[float] = [1, 2, float("nan"), 4, 5, 6, 7, 8, 9, 10]
    rows = _make_rows(closes)
    # 计算不应崩溃；缺失值附近窗口为 NaN，后续窗口正常
    result = calculate_technical_indicators(rows, "NVDA")
    check("缺失值下 MA5 最后值=8", math.isclose(float(result["trend"]["ma5"]), 8.0))
    check("缺失值下不产出假数据", result["latest"]["close"] == 10.0)


# ---------------------------------------------------------------------------
# 8. 历史数据排序与去重
# ---------------------------------------------------------------------------
def test_sort_and_dedupe() -> None:
    print("测试 8：历史数据排序与去重")
    rows = [
        {"date": "2026-01-03", "close": 3.0},
        {"date": "2026-01-01", "close": 1.0},
        {"date": "2026-01-02", "close": 2.0},
        {"date": "2026-01-02", "close": 2.5},  # 重复日期
    ]
    sorted_rows, dropped = sort_and_dedupe(rows)
    check("去重后保留 3 条", len(sorted_rows) == 3)
    check("删除 1 条重复", dropped == 1)
    dates = [r["date"] for r in sorted_rows]
    check("按日期升序", dates == sorted(dates), f"dates={dates}")
    check("重复日期保留最后一条", [r["close"] for r in sorted_rows] == [1.0, 2.5, 3.0])

    # 数据质量检查：异常数据必须报告，不静默修复
    bad = [
        {"date": "2026-01-01", "close": 1.0, "high": 1.5, "low": 0.5, "volume": 100},
        {"date": "2026-01-02", "close": None, "high": 1.0, "low": 0.5, "volume": -5},
        {"date": "2026-01-03", "close": 3.0, "high": 0.5, "low": 2.0, "volume": 100},
    ]
    quality = inspect_history(bad)
    check("报告收盘价缺失", quality["missing_close_count"] == 1)
    check("报告最高价低于最低价", quality["high_below_low_count"] == 1)
    check("报告成交量为负", quality["negative_volume_count"] == 1)
    check("存在问题时 clean=False", not quality["clean"])
    check("问题描述非空", len(quality["issues"]) == 3)


def main() -> None:
    print("=" * 50)
    print("Technical Analysis Unit Tests")
    print("=" * 50)
    print()
    test_ma()
    print()
    test_rsi()
    print()
    test_macd()
    print()
    test_atr()
    print()
    test_empty_data()
    print()
    test_insufficient_data()
    print()
    test_missing_values()
    print()
    test_sort_and_dedupe()
    print()
    print("-" * 50)
    if _FAILURES:
        print(f"结果：{len(_FAILURES)} 项失败 -> {_FAILURES}")
        sys.exit(1)
    print("结果：全部通过")
    sys.exit(0)


if __name__ == "__main__":
    main()
