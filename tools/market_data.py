"""真实市场行情数据模块（基于 AKShare / 东方财富公开接口）。

从 AKShare 获取美股与国际期货的真实行情数据，A 股实时行情经东方财富
qt/stock/get 公开接口直连获取，统一转换为 Python dict 返回给上层 Agent 使用。

注意：数据来自公开行情源，可能存在延迟，不保证零延迟，
数据仅用于研究和分析。
"""

from typing import Any, Dict, Optional

# 本机网络环境适配必须在本模块发起任何网络请求前生效
from tools import network_adapter  # noqa: F401

import akshare as ak
import requests

from app.data.akshare_client import resolve_a_share_secid
from app.news.processing import normalize_a_share_symbol


def _to_float(value: Any) -> Optional[float]:
    """安全转换为 float，转换失败或为 NaN 时返回 None，不编造数据。"""
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    # NaN（如 "-"、停牌合约等占位值）同样视为缺失
    if result != result:
        return None
    return result


def _get(row: Any, name: str) -> Any:
    """安全读取行字段，列不存在时返回 None。"""
    try:
        return row[name]
    except KeyError:
        return None


def get_us_stock_quote(symbol: str) -> Dict[str, Any]:
    """获取指定美股的真实行情数据（AKShare: stock_us_spot_em）。

    Args:
        symbol: 美股代码，例如 NVDA、AAPL、TSLA。

    Returns:
        统一结构行情 dict；失败时返回包含 "error" 字段的 dict。
    """
    symbol = symbol.strip().upper()

    try:
        df = ak.stock_us_spot_em()
    except Exception as exc:
        return {"error": f"AKShare 网络请求失败: {type(exc).__name__}: {exc}", "symbol": symbol}

    if df is None or df.empty:
        return {"error": "AKShare 返回空 DataFrame", "symbol": symbol}

    # 代码列形如 "105.NVDA"，按代码后缀精确匹配
    code_col = df["代码"].astype(str).str.upper()
    matched = df[code_col.str.endswith(symbol)]
    if matched.empty:
        return {"error": "Unknown symbol", "symbol": symbol}

    row = matched.iloc[0]
    return {
        "symbol": symbol,
        "asset_type": "stock",
        "name": _get(row, "名称"),
        "price": _to_float(_get(row, "最新价")),
        "change_percent": _to_float(_get(row, "涨跌幅")),
        "open": _to_float(_get(row, "开盘价")),
        "high": _to_float(_get(row, "最高价")),
        "low": _to_float(_get(row, "最低价")),
        "previous_close": _to_float(_get(row, "昨收价")),
        "volume": _to_float(_get(row, "成交量")),
        "currency": "USD",
        "market": "US Stock",
        "data_source": "AKShare",
        "data_quality": "market-data-source",
        "timestamp": None,  # 该接口快照不含时间字段，不编造
    }


def get_global_futures_quote(symbol: str) -> Dict[str, Any]:
    """获取指定国际期货的真实行情数据（AKShare: futures_global_spot_em）。

    国际期货列表没有代码列，按名称包含匹配。

    Args:
        symbol: 期货名称关键字，例如 黄金、原油。

    Returns:
        统一结构行情 dict；失败时返回包含 "error" 字段的 dict。
    """
    symbol = symbol.strip()

    try:
        df = ak.futures_global_spot_em()
    except Exception as exc:
        return {"error": f"AKShare 网络请求失败: {type(exc).__name__}: {exc}", "symbol": symbol}

    if df is None or df.empty:
        return {"error": "AKShare 返回空 DataFrame", "symbol": symbol}

    name_col = df["名称"].astype(str)
    matched = df[name_col.str.contains(symbol, regex=False)]
    if matched.empty:
        return {"error": "Unknown symbol", "symbol": symbol}

    # 名称匹配可能命中未挂牌的远期合约（如"迷你黄金2806"，无最新价）。
    # 优先选取首个有最新价的活跃合约；全部无行情时才退回首条匹配。
    def _has_price(value: Any) -> bool:
        return _to_float(value) is not None

    active = matched[[_has_price(v) for v in matched["最新价"]]]
    row = active.iloc[0] if not active.empty else matched.iloc[0]
    return {
        "symbol": symbol,
        "asset_type": "futures",
        "name": _get(row, "名称"),
        "price": _to_float(_get(row, "最新价")),
        "change_percent": _to_float(_get(row, "涨跌幅")),
        # 期货接口列名为 今开/最高/最低/昨结，语义对应 开盘/最高/最低/昨收
        "open": _to_float(_get(row, "今开")),
        "high": _to_float(_get(row, "最高")),
        "low": _to_float(_get(row, "最低")),
        "previous_close": _to_float(_get(row, "昨结")),
        "volume": _to_float(_get(row, "成交量")),
        "currency": None,  # 该接口未提供币种，不编造
        "market": "Global Futures",
        "data_source": "AKShare",
        "data_quality": "market-data-source",
        "timestamp": None,  # 该接口列表不含时间字段，不编造
    }


# ---------------------------------------------------------------------------
# A 股实时行情（东方财富 qt/stock/get 公开接口）
# ---------------------------------------------------------------------------
# f162=市盈率(动态), f167=市净率, f116=总市值(元), f117=流通市值(元)
_QUOTE_FIELDS = "f43,f57,f58,f169,f170,f46,f44,f45,f60,f47,f48,f168,f50,f116,f117,f162,f167"
# 本机网络实测：适配器将 *.push2.eastmoney.com 固定到 61.129.129.48 后，
# 裸主机 push2 被重置，但编号子域 / push2delay 可用；故依次尝试。
_QUOTE_HOSTS = (
    "push2delay.eastmoney.com",
    "63.push2.eastmoney.com",
    "52.push2.eastmoney.com",
)


def get_a_share_quote(symbol: str) -> Dict[str, Any]:
    """获取指定 A 股实时行情（东方财富 qt/stock/get 公开接口）。

    Args:
        symbol: A 股代码，支持 600519 / 600519.SH / 600519.sh。

    Returns:
        统一结构行情 dict；失败时返回包含 "error" 字段的 dict。
    """
    try:
        code = normalize_a_share_symbol(symbol)[:6]
        secid = resolve_a_share_secid(code)
    except ValueError:
        return {"error": f"不是有效的 A 股代码: {symbol}", "symbol": symbol}

    params = {
        "secid": secid,
        "fields": _QUOTE_FIELDS,
        "invt": "2",
        "fltt": "2",
    }
    last_error: Optional[Exception] = None
    data: Optional[Dict[str, Any]] = None
    for host in _QUOTE_HOSTS:
        try:
            resp = requests.get(
                f"https://{host}/api/qt/stock/get",
                params=params,
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json().get("data")
            if data:
                break
        except Exception as exc:
            last_error = exc

    if data is None:
        return {
            "error": (
                f"AKShare 网络请求失败: {type(last_error).__name__}: {last_error}"
                if last_error
                else "AKShare 返回空数据"
            ),
            "symbol": code,
        }

    return {
        "symbol": code,
        "asset_type": "stock",
        "name": data.get("f58"),
        "price": _to_float(data.get("f43")),
        "change_percent": _to_float(data.get("f170")),
        "open": _to_float(data.get("f46")),
        "high": _to_float(data.get("f44")),
        "low": _to_float(data.get("f45")),
        "previous_close": _to_float(data.get("f60")),
        "volume": _to_float(data.get("f47")),
        "amount": _to_float(data.get("f48")),
        "volume_unit": "手",  # 东方财富 A 股成交量单位为手（1手=100股）
        # 估值字段（元）；实时快照的"动态"口径，供基本面估值参考
        "pe": _to_float(data.get("f162")),
        "pb": _to_float(data.get("f167")),
        "total_market_cap": _to_float(data.get("f116")),
        "float_market_cap": _to_float(data.get("f117")),
        "currency": "CNY",
        "market": "A-share",
        "data_source": "AKShare",
        "data_quality": "market-data-source",
        "timestamp": None,  # 该接口快照不含时间字段，不编造
    }
