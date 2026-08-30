"""A 股估值历史分位分析 Tool（第十一阶段）。

基于 Tushare daily_basic 历史序列，返回指定 A 股公司当前 PE/PB 在
回看窗口（默认近 5 年）内的历史分位（percentile），用于判断估值所处
历史区间高低。Tool Schema 供 DeepSeek Tool Calling 使用；Provider
通过参数注入，Tool 不依赖具体数据源。

数据仅用于研究和分析，不构成投资建议。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from app.data.tushare_client import TushareTokenMissingError
from app.fundamentals.providers import BaseFundamentalProvider
from app.fundamentals.valuation import ValuationAnalysisProvider

VALUATION_TOOL_SCHEMA: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "get_valuation_analysis",
        "description": (
            "获取指定 A 股公司当前 PE/PB 在历史回看窗口（默认近 5 年）内的历史分位"
            "（percentile，0~1）及历史区间统计，用于判断估值所处历史区间高低，"
            "辅助投资研究，不构成投资建议。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "A 股代码，如 600519 或 600519.SH。",
                },
            },
            "required": ["symbol"],
        },
    },
}


def get_valuation_analysis(
    symbol: str, provider: Optional[BaseFundamentalProvider] = None
) -> Dict[str, Any]:
    """获取指定 A 股公司当前 PE/PB 的历史分位分析。

    Args:
        symbol: A 股代码（支持 600519 / 600519.SH / 600519.sh 等形式）。
        provider: 数据源 Provider；默认使用基于 Tushare daily_basic 历史序列的
            ValuationAnalysisProvider。

    Returns:
        统一结构 JSON dict；失败时返回包含 "error" 字段的 dict。
    """
    if provider is None:
        provider = ValuationAnalysisProvider()
    try:
        return provider.get_fundamentals(symbol)
    except TushareTokenMissingError:
        return {"error": "token_missing", "symbol": symbol}
