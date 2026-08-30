"""技术指标计算模块（pandas / numpy 本地实现）。

所有数学计算均由 Python 完成，模型（DeepSeek）只负责解读工具返回的结果。
不依赖 ta / pandas-ta / TA-Lib 等第三方指标库。

指标约定：
- MA: 收盘价简单移动平均（pandas rolling）。
- RSI: 标准 RSI，基于价格变化 gain/loss 的 Wilder 平滑，默认周期 14。
- MACD: DIF = EMA12 - EMA26，Signal(DEA) = EMA9(DIF)，
  Histogram = 2 * (DIF - Signal)，即国内常用 MACD 柱。
- ATR: True Range 的 Wilder 平滑，默认周期 14。
"""

from __future__ import annotations

from typing import Any, Dict, List

import pandas as pd


def _to_dataframe(rows: List[Dict[str, Any]]) -> pd.DataFrame:
    """把历史行情 dict 列表转为 DataFrame。"""
    return pd.DataFrame(rows)


def calculate_ma(df: pd.DataFrame, period: int) -> pd.Series:
    """简单移动平均 MA，使用收盘价，pandas rolling 实现。"""
    return df["close"].rolling(window=period).mean()


def calculate_rsi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """标准 RSI（Wilder 平滑）。

    步骤：
    1. 收盘价变化 delta = close.diff()
    2. gain = max(delta, 0)，loss = max(-delta, 0)
    3. 平均上涨 avg_gain、平均下跌 avg_loss（Wilder 指数平滑）
    4. RS = avg_gain / avg_loss
    5. RSI = 100 - 100 / (1 + RS)

    平均下跌为 0 时 RS 无定义，按惯例 RSI = 100。
    前期不足 period 个观测值时返回 NaN（数据不足，不编造）。
    """
    delta = df["close"].diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)

    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()

    rs = avg_gain / avg_loss
    rsi = 100.0 - 100.0 / (1.0 + rs)
    # avg_loss 为 0 的交易日 RSI 恒为 100；NaN 观测仍保持 NaN
    rsi = rsi.where(avg_loss != 0, other=100.0)
    return rsi


def calculate_macd(
    df: pd.DataFrame,
    fast_period: int = 12,
    slow_period: int = 26,
    signal_period: int = 9,
) -> Dict[str, pd.Series]:
    """MACD 指标。

    Returns:
        {"macd": DIF, "signal": DEA/Signal, "histogram": 2 * (DIF - DEA)}
    """
    close = df["close"]
    ema_fast = close.ewm(span=fast_period, adjust=False).mean()
    ema_slow = close.ewm(span=slow_period, adjust=False).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=signal_period, adjust=False).mean()
    histogram = 2.0 * (dif - dea)
    return {"macd": dif, "signal": dea, "histogram": histogram}


def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """平均真实波幅 ATR（Wilder 平滑）。

    True Range = max(high - low, |high - prev_close|, |low - prev_close|)
    ATR = TR 的 Wilder 指数平滑。
    """
    high = df["high"]
    low = df["low"]
    prev_close = df["close"].shift(1)

    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()


def _safe(value: Any) -> Any:
    """NaN / None 统一转为 None，保证结果 JSON-compatible。"""
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result:  # NaN
        return None
    return round(result, 4)


def calculate_technical_indicators(
    rows: List[Dict[str, Any]], symbol: str
) -> Dict[str, Any]:
    """根据历史行情计算技术指标，返回统一结果结构。

    Args:
        rows: 已按日期升序排列的历史行情 dict 列表。
        symbol: 美股代码（用于结果中的 symbol 字段）。

    Returns:
        技术指标结果 dict（JSON-compatible），结构见模块文档。

    Raises:
        ValueError: rows 为空时抛出。
    """
    if not rows:
        raise ValueError("没有历史数据，无法计算技术指标")

    df = _to_dataframe(rows)
    ma5 = calculate_ma(df, 5)
    ma20 = calculate_ma(df, 20)
    ma60 = calculate_ma(df, 60)
    rsi14 = calculate_rsi(df, 14)
    macd = calculate_macd(df)
    atr14 = calculate_atr(df, 14)

    latest = rows[-1]  # 调用方保证已按日期升序
    return {
        "symbol": symbol,
        "data_source": "AKShare",
        "latest": {
            "date": latest.get("date"),
            "close": _safe(latest.get("close")),
            "volume": _safe(latest.get("volume")),
        },
        "trend": {
            "ma5": _safe(ma5.iloc[-1]),
            "ma20": _safe(ma20.iloc[-1]),
            "ma60": _safe(ma60.iloc[-1]),
        },
        "momentum": {
            "rsi14": _safe(rsi14.iloc[-1]),
        },
        "macd": {
            "macd": _safe(macd["macd"].iloc[-1]),
            "signal": _safe(macd["signal"].iloc[-1]),
            "histogram": _safe(macd["histogram"].iloc[-1]),
        },
        "volatility": {
            "atr14": _safe(atr14.iloc[-1]),
        },
    }
