"""技术分析工具。

get_technical_analysis() 是 Agent 可调用的入口：
获取指定股票（A 股 / 美股自动识别）最近 250 个自然日（约 170 个交易日）
的历史日线行情（AKShare / 东方财富），由 Python 本地计算 MA/RSI/MACD/ATR
技术指标，返回 JSON-compatible dict。

历史数据来自 AKShare 公开接口（东方财富），技术指标由 Python 计算，
数据仅用于研究和分析，不构成投资建议。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from app.analysis.technical import calculate_technical_indicators
from app.data.akshare_client import (
    HistoryDataError,
    get_a_share_stock_history_with_quality,
    get_us_stock_history_with_quality,
)
from app.news.processing import normalize_a_share_symbol

# 250 个自然日窗口，约覆盖 170+ 个交易日，保证 MA60/MACD 有足够样本
_LOOKBACK_CALENDAR_DAYS = 250


def get_technical_analysis(symbol: str) -> Dict[str, Any]:
    """获取指定股票最近 250 个自然日（约 170 个交易日）历史行情并计算技术指标。

    Args:
        symbol: A 股代码（600519 / 600519.SH）或美股代码（NVDA、AAPL、TSLA）。

    Returns:
        技术指标结果 dict（JSON-compatible）；失败时返回包含 "error" 字段的 dict。
    """
    symbol = symbol.strip()
    try:
        normalize_a_share_symbol(symbol)
        is_a_share = True
    except ValueError:
        is_a_share = False
        symbol = symbol.upper()

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=_LOOKBACK_CALENDAR_DAYS)

    fetch = (
        get_a_share_stock_history_with_quality
        if is_a_share
        else get_us_stock_history_with_quality
    )
    try:
        result = fetch(
            symbol,
            start_date=start.strftime("%Y-%m-%d"),
            end_date=end.strftime("%Y-%m-%d"),
            adjust="",
        )
    except HistoryDataError as exc:
        return {"error": str(exc), "symbol": symbol}

    history = result["history"]

    try:
        indicators = calculate_technical_indicators(history, symbol)
    except Exception as exc:
        return {
            "error": f"技术指标计算失败: {type(exc).__name__}: {exc}",
            "symbol": symbol,
        }

    # fetched_at：Python 本地获取数据的 UTC 时间（ISO 8601），不等于行情日期
    indicators["fetched_at"] = result["fetched_at"]
    # market_date：最新一根K线的市场日期（原始数据自带的市场日期，予以保留）
    indicators["market_date"] = history[-1]["date"]
    indicators["history_rows"] = len(history)
    indicators["market"] = "A-share" if is_a_share else "US Stock"
    indicators["data_quality"] = result["data_quality"]
    return indicators
