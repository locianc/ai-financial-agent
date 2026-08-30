"""A 股基本面数据 Tool（第七阶段）。

Tool Schema 供未来 DeepSeek Tool Calling 使用；本阶段只实现 Tool 函数与
Schema，不接入 main.py。Provider 通过参数注入，Tool 不依赖具体数据源。

数据仅用于研究和分析，不构成投资建议。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from app.data.tushare_client import TushareTokenMissingError
from app.fundamentals.providers import (
    BaseFundamentalProvider,
    CompositeFundamentalProvider,
)

FUNDAMENTALS_TOOL_SCHEMA: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "get_stock_fundamentals",
        "description": "获取指定 A 股公司的公开基本面、估值及财务指标，用于辅助投资研究，不构成投资建议。",
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


def get_stock_fundamentals(
    symbol: str, provider: Optional[BaseFundamentalProvider] = None
) -> Dict[str, Any]:
    """获取指定 A 股公司的公开基本面、估值及财务指标。

    Args:
        symbol: A 股代码（支持 600519 / 600519.SH / 600519.sh 等形式）。
        provider: 数据源 Provider；默认使用 Tushare 估值 + AKShare 财务补全的
            CompositeFundamentalProvider。

    Returns:
        统一结构 JSON dict；失败时返回包含 "error" 字段的 dict。
    """
    if provider is None:
        provider = CompositeFundamentalProvider()
    try:
        return provider.get_fundamentals(symbol)
    except TushareTokenMissingError:
        return {"error": "token_missing", "symbol": symbol}
