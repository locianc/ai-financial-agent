"""股票行情工具。

get_stock_price() 是默认入口，返回真实行情数据（A 股 / 美股自动识别）；
get_stock_price_mock() 保留第三阶段的模拟数据，仅用于离线测试。
"""

from typing import Any, Dict

from app.news.processing import normalize_a_share_symbol
from tools.market_data import get_a_share_quote, get_us_stock_quote

# 预定义的模拟行情数据，data_source 统一标记为 "MOCK"
_MOCK_STOCKS: Dict[str, Dict[str, Any]] = {
    "NVDA": {
        "symbol": "NVDA",
        "name": "NVIDIA",
        "price": 182.50,
        "change_percent": 2.31,
        "volume": 12500000,
        "market": "NASDAQ",
        "data_source": "MOCK",
    },
    "AAPL": {
        "symbol": "AAPL",
        "name": "Apple Inc.",
        "price": 232.45,
        "change_percent": -0.68,
        "volume": 48000000,
        "market": "NASDAQ",
        "data_source": "MOCK",
    },
    "TSLA": {
        "symbol": "TSLA",
        "name": "Tesla Inc.",
        "price": 248.90,
        "change_percent": 3.12,
        "volume": 90000000,
        "market": "NASDAQ",
        "data_source": "MOCK",
    },
}


def get_stock_price(symbol: str) -> Dict[str, Any]:
    """获取指定股票的实时行情数据（A 股 / 美股自动识别）。

    Args:
        symbol: A 股代码（600519 / 600519.SH）或美股代码（NVDA、AAPL）。

    Returns:
        真实行情数据字典；失败时返回包含 "error" 字段的字典。
    """
    symbol = symbol.strip()
    try:
        normalize_a_share_symbol(symbol)
    except ValueError:
        return get_us_stock_quote(symbol)
    return get_a_share_quote(symbol)


def get_stock_price_mock(symbol: str) -> Dict[str, Any]:
    """获取指定股票的模拟行情数据（仅用于离线测试）。

    Args:
        symbol: 股票代码，例如 NVDA、AAPL、TSLA。

    Returns:
        模拟行情数据字典；未知代码返回错误信息字典。
    """
    symbol = symbol.strip().upper()
    if symbol not in _MOCK_STOCKS:
        return {"error": "Unknown symbol", "symbol": symbol}
    return dict(_MOCK_STOCKS[symbol])
