"""A 股新闻工具层（第六阶段基础设施 + AKShare 真实数据接入）。

get_stock_news() 是 Agent 可调用的新闻工具入口：默认走 AKShare 东方财富
个股新闻搜索（免鉴权、真实数据），由 Python 做文本清洗、去重与摘要截断，
返回标准 JSON 结构。工具返回的每条新闻字段遵循统一契约：

    title        标题
    summary      摘要（硬限制 100 字符内，含省略号）
    publish_date 发布时间
    source       来源

失败（接口限流、网络超时、无数据等）时返回明确的 Error JSON，供 Agent 优雅降级。

数据仅用于研究和分析，不构成投资建议。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from app.news.processing import clean_news_text
from app.news.providers import (
    AkshareNewsProvider,
    BaseNewsProvider,
    normalize_a_share_symbol,
)

_SUMMARY_MAX_LEN = 100
_SUMMARY_ELLIPSIS = "..."


def _short_summary(text: str) -> str:
    """清洗并把摘要硬截断到 100 字符内（含省略号），绝不超限。"""
    cleaned = clean_news_text(text)
    if len(cleaned) <= _SUMMARY_MAX_LEN:
        return cleaned
    return cleaned[: _SUMMARY_MAX_LEN - len(_SUMMARY_ELLIPSIS)] + _SUMMARY_ELLIPSIS


# ---------------------------------------------------------------------------
# Tool Schema（Agent 据此得知工具存在、参数与用途）
# ---------------------------------------------------------------------------
NEWS_TOOL_SCHEMA: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "get_stock_news",
        "description": (
            "获取指定A股股票近期相关新闻资讯。新闻来自AKShare东方财富公开财经"
            "资讯/快讯数据，Python会进行文本清洗、去重并限制摘要长度。"
            "用于辅助分析可能影响股价的近期事件；但仅凭新闻无法确认绝对因果关系。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "A股股票代码，例如 600519、600519.SH、000001.SZ",
                },
                "limit": {
                    "type": "integer",
                    "description": "返回相关新闻数量，默认3，最大5",
                    "default": 3,
                    "minimum": 1,
                    "maximum": 5,
                },
            },
            "required": ["symbol"],
        },
    },
}


def get_stock_news(
    symbol: str, limit: int = 3, provider: Optional[BaseNewsProvider] = None
) -> Dict[str, Any]:
    """获取指定 A 股股票近期相关新闻（默认 AKShare 东方财富数据源）。

    Args:
        symbol: A 股代码，例如 600519、600519.SH、000001.SZ。
        limit: 返回条数（钳制到 1-5）。
        provider: 新闻 Provider，默认 AkshareNewsProvider；
                  测试时可注入 MockNewsProvider / TushareNewsProvider。

    Returns:
        标准 JSON 结构 dict（news 内每条含 title/summary/publish_date/source）；
        失败时返回包含 "error" 字段的 dict：
        - invalid_symbol：代码非法（如非 6 位数字、暂不支持的板块）；
        - akshare_news_failed：AKShare 接口限流/网络超时/无数据等；
        - news_api_permission_required / token_missing：注入的 Tushare Provider 返回。
    """
    try:
        ts_code = normalize_a_share_symbol(symbol)
    except ValueError:
        return {"error": "invalid_symbol", "symbol": symbol}

    if provider is None:
        provider = AkshareNewsProvider()

    result = provider.get_news(ts_code, limit=limit)
    if "error" in result:
        return result

    news = [_standard_item(row) for row in result["news"]]
    return {
        "symbol": result["symbol"],
        "asset_type": result.get("asset_type", "stock"),
        "market": result.get("market", "A-share"),
        "data_source": result.get("data_source", "Akshare"),
        "fetched_at": result.get("fetched_at"),
        "news_count": len(news),
        "news": news,
    }


def _standard_item(row: Dict[str, Any]) -> Dict[str, Any]:
    """把 Provider 的新闻行映射为统一标准字段（title/summary/publish_date/source）。"""
    item: Dict[str, Any] = {
        "title": clean_news_text(row.get("title")),
        "summary": _short_summary(row.get("summary") or row.get("content") or ""),
        "publish_date": row.get("published_at"),
        "source": row.get("source"),
    }
    if row.get("relevance"):
        item["relevance"] = row["relevance"]
    return item
