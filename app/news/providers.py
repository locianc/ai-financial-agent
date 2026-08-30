"""新闻 Provider 抽象（第六阶段基础设施）。

设计（方案 2 第十条）：MockNewsProvider 与 TushareNewsProvider 遵循同一个
接口 get_news(symbol, limit=3)，返回相同结构的标准 JSON，未来从 Mock 换成
真实 Tushare 数据源时，无需修改 Agent 的 Tool Schema。

AkshareNewsProvider 同样遵循该接口：基于 AKShare 东方财富个股新闻搜索
（免鉴权）返回真实新闻数据，是本项目当前默认的新闻数据源。

契约：
- 任何 Provider 都不得在真实调用失败时返回模拟数据冒充成功；
- TushareNewsProvider 当前（2026-08-20）因 news 接口无权限，必须返回
  {"error": NEWS_API_PERMISSION_REQUIRED, ...}，绝不返回 Mock 数据；
- 数据仅用于研究和分析，不构成投资建议。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from app.data.tushare_client import (
    TushareAPIError,
    TushareClient,
    TushareNewsPermissionError,
    TushareTokenMissingError,
)
from app.news.processing import (
    build_news_result,
    build_stock_keywords,
    deduplicate_news,
    filter_relevant_news,
    normalize_a_share_symbol,
    utc_now_iso,
    validate_news_timeline,
)

# ---------------------------------------------------------------------------
# 错误契约
# ---------------------------------------------------------------------------
NEWS_API_PERMISSION_REQUIRED = "news_api_permission_required"
"""news 接口无权限时的统一错误码。任何 Provider 都不得绕过它。"""


def clamp_news_limit(limit: Any, default: int = 3) -> int:
    """把 limit 钳制到 [1, 5]，非法值回退到 default。"""
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return default
    return max(1, min(5, value))


def _invalid_symbol_result(symbol: str) -> Dict[str, Any]:
    return {"error": "invalid_symbol", "symbol": symbol}


def _token_missing_result(symbol: str) -> Dict[str, Any]:
    return {"error": "token_missing", "symbol": symbol}


# ---------------------------------------------------------------------------
# Provider 接口
# ---------------------------------------------------------------------------
class BaseNewsProvider(ABC):
    """新闻 Provider 统一接口。"""

    @abstractmethod
    def get_news(self, symbol: str, limit: int = 3) -> Dict[str, Any]:
        """获取指定 A 股股票近期相关新闻。

        Args:
            symbol: A 股代码（支持 600519 / 600519.SH / 600519.sh 等形式）。
            limit: 返回条数上限（钳制到 1-5）。

        Returns:
            标准 JSON 结构 dict；失败时返回包含 "error" 字段的 dict。
        """


# ---------------------------------------------------------------------------
# Mock News Provider（模拟数据，仅用于测试与演示）
# ---------------------------------------------------------------------------
class MockNewsProvider(BaseNewsProvider):
    """模拟新闻 Provider。

    只用于单元测试与演示，返回的每条数据都带 "source": "mock" 且顶层
    data_source="Mock"，并附 notice 说明，绝不冒充真实新闻。
    """

    def get_news(self, symbol: str, limit: int = 3) -> Dict[str, Any]:
        try:
            ts_code = normalize_a_share_symbol(symbol)
        except ValueError:
            return _invalid_symbol_result(symbol)

        limit = clamp_news_limit(limit)
        pure_code = ts_code.split(".")[0]
        now = datetime.now()

        def _ago(hours: int) -> str:
            return (now - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")

        raw_rows: List[Dict[str, Any]] = [
            {
                "title": f"{pure_code} 所属上市公司今日发布公告称经营情况正常",
                "content": f"公司公告显示，{pure_code} 对应上市公司目前经营正常，"
                           "不存在应披露而未披露的重大事项。",
                "published_at": _ago(3),
                "source": "mock",
            },
            {
                "title": "行业政策动态或影响相关板块表现",
                "content": f"本次政策调整涉及的上市公司中包括 {pure_code}，"
                           "具体影响有待后续财报验证。",
                "published_at": _ago(10),
                "source": "mock",
            },
            {
                "title": "宏观经济数据今日公布",
                "content": "国家统计局公布最新经济数据，与个股无关。",
                "published_at": _ago(20),
                "source": "mock",
            },
        ]

        keywords = build_stock_keywords(ts_code=ts_code)
        relevant = filter_relevant_news(raw_rows, keywords)
        unique, _removed = deduplicate_news(relevant)
        fetched_at = utc_now_iso()
        unique, _issues = validate_news_timeline(unique, fetched_at)

        result = build_news_result(
            symbol=ts_code,
            news_rows=unique[:limit],
            fetched_at=fetched_at,
            data_source="Mock",
        )
        result["notice"] = "模拟数据，仅用于测试与演示，不代表真实新闻。"
        return result


# ---------------------------------------------------------------------------
# AKShare News Provider（真实数据源，免鉴权：东方财富个股新闻搜索）
# ---------------------------------------------------------------------------
class AkshareNewsProvider(BaseNewsProvider):
    """AKShare 东方财富个股新闻源（免鉴权、真实数据）。

    基于 ak.stock_news_em() 按股票代码搜索东方财富新闻索引，返回与代码相关的
    个股新闻/快讯。字段映射：

        AKShare 列 新闻标题 -> title
        AKShare 列 新闻内容 -> content
        AKShare 列 发布时间 -> published_at
        AKShare 列 文章来源 -> source

    失败契约：网络超时、接口限流、无数据返回等一律返回
    {"error": "akshare_news_failed", ...}，绝不返回模拟数据冒充成功。
    """

    data_source = "Akshare"

    def get_news(self, symbol: str, limit: int = 3) -> Dict[str, Any]:
        try:
            ts_code = normalize_a_share_symbol(symbol)
        except ValueError:
            return _invalid_symbol_result(symbol)

        limit = clamp_news_limit(limit)
        try:
            raw_rows = self._fetch_raw_rows(ts_code)
        except Exception as exc:
            return {
                "error": "akshare_news_failed",
                "symbol": symbol,
                "ts_code": ts_code,
                "detail": (
                    f"AKShare 新闻接口请求失败（网络超时/接口限流等）："
                    f"{type(exc).__name__}: {exc}"
                ),
            }

        if not raw_rows:
            return {
                "error": "akshare_news_failed",
                "symbol": symbol,
                "ts_code": ts_code,
                "detail": f"AKShare 新闻接口未返回与 {symbol} 相关的数据。",
            }

        unique, _removed = deduplicate_news(raw_rows)
        fetched_at = utc_now_iso()
        unique, _issues = validate_news_timeline(unique, fetched_at)

        return build_news_result(
            symbol=ts_code,
            news_rows=unique[:limit],
            fetched_at=fetched_at,
            data_source=self.data_source,
        )

    def _fetch_raw_rows(self, ts_code: str) -> List[Dict[str, Any]]:
        """调用 ak.stock_news_em 抓取原始新闻行（列名映射）。

        只负责网络层与列名映射；失败（抛异常）与空数据交由 get_news 兜底。
        """
        from tools import network_adapter  # noqa: F401  # 本机网络适配必须先于任何网络请求生效
        import akshare as ak

        pure_code = ts_code.split(".")[0]
        df = ak.stock_news_em(symbol=pure_code)
        if df is None or df.empty:
            return []

        rows: List[Dict[str, Any]] = []
        for _, row in df.iterrows():
            rows.append(
                {
                    "title": row.get("新闻标题"),
                    "content": row.get("新闻内容"),
                    "published_at": row.get("发布时间"),
                    "source": row.get("文章来源"),
                }
            )
        return rows


# ---------------------------------------------------------------------------
# Tushare News Provider（真实数据源；当前接口无权限）
# ---------------------------------------------------------------------------
class TushareNewsProvider(BaseNewsProvider):
    """Tushare 新闻 Provider。

    当前（2026-08-20）news 接口因账户积分不足被拒绝访问，因此
    get_news() 必须返回 {"error": NEWS_API_PERMISSION_REQUIRED, ...}，
    绝不返回模拟数据、绝不假定调用成功。
    """

    def __init__(self, client: Optional[TushareClient] = None) -> None:
        self._client = client or TushareClient()

    def get_news(self, symbol: str, limit: int = 3) -> Dict[str, Any]:
        try:
            ts_code = normalize_a_share_symbol(symbol)
        except ValueError:
            return _invalid_symbol_result(symbol)

        limit = clamp_news_limit(limit)

        # 股票基础信息（简称/全称）用于构造相关性关键词；仅尽力而为。
        # 即使 stock_basic 失败（如频率限制），也必须继续探测 news 接口权限，
        # 绝不能用 stock_basic 的错误掩盖 news 的真实状态。
        stock_info = None
        stock_info_error = None
        try:
            stock_info = self._client.lookup_stock_info(ts_code)
        except TushareTokenMissingError:
            return _token_missing_result(symbol)
        except (TushareNewsPermissionError, TushareAPIError) as exc:
            stock_info_error = str(exc)

        keywords = build_stock_keywords(
            ts_code=ts_code,
            short_name=(stock_info or {}).get("name"),
            full_name=(stock_info or {}).get("fullname"),
        )

        start_date, end_date = self._client.news_window(hours=24)
        try:
            rows, source_status = self._client.fetch_news_multi_source(
                start_date, end_date
            )
        except TushareTokenMissingError:
            return _token_missing_result(symbol)

        statuses = {item["status"] for item in source_status}

        if rows:
            relevant = filter_relevant_news(rows, keywords)
            unique, _removed = deduplicate_news(relevant)
            fetched_at = utc_now_iso()
            unique, timeline_issues = validate_news_timeline(unique, fetched_at)
            result = build_news_result(
                symbol=ts_code,
                news_rows=unique[:limit],
                fetched_at=fetched_at,
                data_source="Tushare",
                source_status=source_status,
            )
            if timeline_issues:
                result["timeline_issues"] = timeline_issues
            if stock_info_error:
                result["stock_info_error"] = stock_info_error
            return result

        if "permission_denied" in statuses:
            detail = (
                "所有新闻来源均无访问权限（账户积分不足），"
                "需要先在 Tushare 平台获取 news 接口权限。"
            )
            return _permission_result(symbol, ts_code, detail, source_status,
                                      stock_info_error=stock_info_error)

        if "ok" in statuses:
            result = {
                "error": "no_news",
                "symbol": symbol,
                "ts_code": ts_code,
                "detail": "新闻接口可用，但 24 小时窗口内未获取到相关新闻。",
                "source_status": source_status,
            }
            if stock_info_error:
                result["stock_info_error"] = stock_info_error
            return result

        result = {
            "error": "tushare_error",
            "symbol": symbol,
            "ts_code": ts_code,
            "detail": "所有新闻来源均调用失败。",
            "source_status": source_status,
        }
        if stock_info_error:
            result["stock_info_error"] = stock_info_error
        return result


def _permission_result(
    symbol: str,
    ts_code: str,
    detail: str,
    source_status: Optional[List[Dict[str, Any]]],
    stock_info_error: Optional[str] = None,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "error": NEWS_API_PERMISSION_REQUIRED,
        "symbol": symbol,
        "ts_code": ts_code,
        "detail": detail,
    }
    if source_status is not None:
        result["source_status"] = source_status
    if stock_info_error:
        result["stock_info_error"] = stock_info_error
    return result
