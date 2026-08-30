"""基本面 Provider 抽象（第七阶段）。

设计（对齐第六阶段方案 2 第十条）：MockFundamentalProvider 与
TushareFundamentalProvider 遵循同一个接口 get_fundamentals(symbol)，
返回相同结构的标准 JSON，未来数据源切换无需修改 Tool Schema。

契约：
- 任何 Provider 都不得在真实调用失败时返回模拟数据冒充成功；
- TushareFundamentalProvider 对每个接口独立探测并如实记录
  ok / permission_denied / rate_limited / error，绝不假定成功；
- 财务数据不是实时数据：严格区分 data_date / report_period / fetched_at；
- 数据仅用于研究和分析，不构成投资建议。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from app.data.tushare_client import (
    TushareAPIError,
    TushareClient,
    TusharePermissionError,
    TushareRateLimitError,
    TushareTokenMissingError,
)
from app.fundamentals.processing import build_fundamentals_from_sources
from app.news.processing import normalize_a_share_symbol, utc_now_iso

# ---------------------------------------------------------------------------
# 错误契约
# ---------------------------------------------------------------------------
FUNDAMENTALS_API_PERMISSION_REQUIRED = "fundamentals_api_permission_required"
"""基本面接口无权限时的统一错误码。任何 Provider 都不得绕过它。"""


def _invalid_symbol_result(symbol: str) -> Dict[str, Any]:
    return {"error": "invalid_symbol", "symbol": symbol}


def _token_missing_result(symbol: str) -> Dict[str, Any]:
    return {"error": "token_missing", "symbol": symbol}


def _permission_result(symbol: str, detail: str) -> Dict[str, Any]:
    return {
        "error": FUNDAMENTALS_API_PERMISSION_REQUIRED,
        "symbol": symbol,
        "detail": detail,
    }


# ---------------------------------------------------------------------------
# Provider 接口
# ---------------------------------------------------------------------------
class BaseFundamentalProvider(ABC):
    """基本面 Provider 统一接口。"""

    @abstractmethod
    def get_fundamentals(self, symbol: str) -> Dict[str, Any]:
        """获取指定 A 股公司的公开基本面、估值及财务指标。

        Args:
            symbol: A 股代码（支持 600519 / 600519.SH / 600519.sh 等形式）。

        Returns:
            统一结构 JSON dict；失败时返回包含 "error" 字段的 dict。
        """


# ---------------------------------------------------------------------------
# Mock Fundamental Provider（模拟数据，仅用于测试与演示）
# ---------------------------------------------------------------------------
class MockFundamentalProvider(BaseFundamentalProvider):
    """模拟基本面 Provider。

    只用于单元测试与演示，通过注入三类原始行（daily_basic / income /
    fina_indicator）走与 Tushare 相同的处理流水线，data_source="Mock"，
    并附 notice 说明，绝不冒充真实财务数据。
    """

    def __init__(
        self,
        daily_basic_rows: List[Dict[str, Any]],
        income_rows: List[Dict[str, Any]],
        fina_rows: List[Dict[str, Any]],
        name: Optional[str] = None,
    ) -> None:
        self._daily_basic_rows = daily_basic_rows
        self._income_rows = income_rows
        self._fina_rows = fina_rows
        self._name = name

    def get_fundamentals(self, symbol: str) -> Dict[str, Any]:
        try:
            ts_code = normalize_a_share_symbol(symbol)
        except ValueError:
            return _invalid_symbol_result(symbol)

        sources_status = [
            {"source": "daily_basic", "status": "ok", "count": len(self._daily_basic_rows)},
            {"source": "income", "status": "ok", "count": len(self._income_rows)},
            {"source": "fina_indicator", "status": "ok", "count": len(self._fina_rows)},
        ]
        if not (self._daily_basic_rows or self._income_rows or self._fina_rows):
            return {
                "error": "no_data",
                "symbol": ts_code,
                "detail": "基本面接口可访问，但未获取到该股票的数据。",
                "data_source": "Mock",
                "sources_status": sources_status,
            }
        result = build_fundamentals_from_sources(
            symbol=ts_code,
            name=self._name,
            daily_basic_rows=self._daily_basic_rows,
            income_rows=self._income_rows,
            fina_rows=self._fina_rows,
            fetched_at=utc_now_iso(),
            data_source="Mock",
            sources_status=sources_status,
        )
        result["notice"] = "模拟数据，仅用于测试与演示，不代表真实财务数据。"
        return result


# ---------------------------------------------------------------------------
# Tushare Fundamental Provider（真实数据源；逐接口如实探测）
# ---------------------------------------------------------------------------
class TushareFundamentalProvider(BaseFundamentalProvider):
    """Tushare 基本面 Provider。

    对 daily_basic / income / fina_indicator 三个接口分别调用并如实记录状态。
    当前账户（2026-08-20 实测）daily_basic 有权限，income / fina_indicator
    无权限——因此结果可能是"估值有真实数据、盈利/成长字段为 None"的部分数据，
    并以 data_quality.sources 如实呈现各接口状态。绝不补算缺失字段。
    """

    def __init__(self, client: Optional[TushareClient] = None) -> None:
        self._client = client or TushareClient()

    def get_fundamentals(self, symbol: str) -> Dict[str, Any]:
        try:
            ts_code = normalize_a_share_symbol(symbol)
        except ValueError:
            return _invalid_symbol_result(symbol)

        # 股票名称（简称）尽力而为：失败只记录，绝不影响核心接口探测。
        name: Optional[str] = None
        stock_info_error: Optional[str] = None
        try:
            info = self._client.lookup_stock_info(ts_code)
            name = (info or {}).get("name")
        except TushareTokenMissingError:
            return _token_missing_result(symbol)
        except (TusharePermissionError, TushareRateLimitError, TushareAPIError) as exc:
            stock_info_error = str(exc)

        today = date.today()
        daily_start = (today - timedelta(days=30)).strftime("%Y%m%d")
        daily_end = today.strftime("%Y%m%d")
        fin_start = f"{today.year - 2}0101"
        fin_end = daily_end

        rows: Dict[str, List[Dict[str, Any]]] = {
            "daily_basic": [],
            "income": [],
            "fina_indicator": [],
        }
        sources_status: List[Dict[str, Any]] = []

        probes = (
            ("daily_basic", self._client.fetch_daily_basic,
             {"ts_code": ts_code, "start_date": daily_start, "end_date": daily_end}),
            ("income", self._client.fetch_income,
             {"ts_code": ts_code, "start_date": fin_start, "end_date": fin_end}),
            ("fina_indicator", self._client.fetch_fina_indicator,
             {"ts_code": ts_code, "start_date": fin_start, "end_date": fin_end}),
        )

        for source_name, fetch_fn, kwargs in probes:
            try:
                batch = fetch_fn(**kwargs)
            except TushareTokenMissingError:
                return _token_missing_result(symbol)
            except TusharePermissionError as exc:
                sources_status.append(
                    {"source": source_name, "status": "permission_denied", "detail": str(exc)}
                )
                continue
            except TushareRateLimitError as exc:
                sources_status.append(
                    {"source": source_name, "status": "rate_limited", "detail": str(exc)}
                )
                continue
            except TushareAPIError as exc:
                sources_status.append(
                    {"source": source_name, "status": "error", "detail": str(exc)}
                )
                continue
            rows[source_name] = batch
            sources_status.append(
                {"source": source_name, "status": "ok", "count": len(batch)}
            )

        any_data = any(rows.values())
        denied_all = all(
            item["status"] == "permission_denied" for item in sources_status
        )
        all_failed = all(
            item["status"] in ("permission_denied", "rate_limited", "error")
            for item in sources_status
        )

        if denied_all:
            return _permission_result(
                symbol,
                "所有基本面接口均无访问权限（账户积分不足），"
                "需要先在 Tushare 平台获取对应接口权限。",
            )
        if not any_data and all_failed:
            result = {
                "error": "tushare_error",
                "symbol": symbol,
                "ts_code": ts_code,
                "detail": "所有基本面接口均调用失败。",
                "sources_status": sources_status,
            }
            if stock_info_error:
                result["stock_info_error"] = stock_info_error
            return result
        if not any_data:
            result = {
                "error": "no_data",
                "symbol": symbol,
                "ts_code": ts_code,
                "detail": "基本面接口可访问，但未获取到该股票的数据。",
                "sources_status": sources_status,
            }
            if stock_info_error:
                result["stock_info_error"] = stock_info_error
            return result

        result = build_fundamentals_from_sources(
            symbol=ts_code,
            name=name,
            daily_basic_rows=rows["daily_basic"],
            income_rows=rows["income"],
            fina_rows=rows["fina_indicator"],
            fetched_at=utc_now_iso(),
            data_source="Tushare",
            sources_status=sources_status,
        )
        if stock_info_error:
            result["stock_info_error"] = stock_info_error
        return result


# ---------------------------------------------------------------------------
# 组合 Provider：Tushare 估值 + AKShare 财务补全
# ---------------------------------------------------------------------------
def _merge_fundamentals(
    symbol: str,
    tushare_result: Dict[str, Any],
    akshare_result: Dict[str, Any],
) -> Dict[str, Any]:
    """合并 Tushare 与 AKShare 结果，字段不重叠时各自保留。

    规则：
    - 估值/股息/名称/data_date：Tushare 优先（真实 trade_date 与官方简称）；
    - 盈利/成长/三大报表：AKShare 优先（完整财务报表补全）；
    - 任何 Provider 失败都如实并入 data_quality.sources，绝不掩盖；
    - data_source 反映实际参与合并的数据源。
    """
    t_ok = "error" not in tushare_result
    a_ok = "error" not in akshare_result

    if not t_ok and not a_ok:
        detail = (
            akshare_result.get("detail")
            or tushare_result.get("detail")
            or f"{tushare_result.get('error')} / {akshare_result.get('error')}"
        )
        return {
            "error": tushare_result.get("error") or akshare_result.get("error"),
            "symbol": symbol,
            "detail": detail,
            "tushare": _status_summary(tushare_result),
            "akshare": _status_summary(akshare_result),
        }

    base = dict(akshare_result if a_ok else tushare_result)
    base["data_source"] = "+".join(
        name for name, ok in (("Tushare", t_ok), ("AKShare", a_ok)) if ok
    )

    if t_ok:
        base["valuation"] = tushare_result.get("valuation") or base.get("valuation")
        base["dividend"] = tushare_result.get("dividend") or base.get("dividend")
        base["data_date"] = tushare_result.get("data_date") or base.get("data_date")
        base["name"] = tushare_result.get("name") or base.get("name")
    if t_ok and a_ok:
        # 盈利/成长：AKShare 有完整财务补全时优先；否则保留 Tushare 部分数据
        akshare_prof = akshare_result.get("profitability")
        if akshare_prof and any(v is not None for v in akshare_prof.values()):
            base["profitability"] = akshare_prof
        akshare_growth = akshare_result.get("growth")
        if akshare_growth and any(v is not None for v in akshare_growth.values()):
            base["growth"] = akshare_growth
        if akshare_result.get("financial_statements"):
            base["financial_statements"] = akshare_result["financial_statements"]

    merged_sources: List[Dict[str, Any]] = []
    for name, result in (("tushare", tushare_result), ("akshare", akshare_result)):
        if "error" not in result:
            merged_sources.extend(result.get("data_quality", {}).get("sources", []))
        else:
            merged_sources.append(
                {"source": name, "status": result.get("error"),
                 "detail": result.get("detail")}
            )
    base.setdefault("data_quality", {})
    base["data_quality"]["sources"] = merged_sources
    return base


def _status_summary(result: Dict[str, Any]) -> Dict[str, Any]:
    """提取失败 Provider 的可读状态摘要（不含完整堆栈）。"""
    return {
        "error": result.get("error"),
        "detail": result.get("detail"),
        "sources_status": result.get("sources_status"),
    }


class CompositeFundamentalProvider(BaseFundamentalProvider):
    """组合 Provider：Tushare 估值/股息 + AKShare 财务补全。

    当前账户（2026-08-20 实测）Tushare daily_basic 有权限、income /
    fina_indicator 无权限，因此组合后估值字段真实、盈利/成长/三大报表由
    AKShare 东方财富公开接口补全。两个数据源的状态如实体现在
    data_quality.sources 中。
    """

    def __init__(
        self,
        tushare_provider: Optional[BaseFundamentalProvider] = None,
        akshare_provider: Optional[BaseFundamentalProvider] = None,
    ) -> None:
        # 惰性导入避免模块级循环依赖
        from app.fundamentals.akshare_provider import AkshareFundamentalProvider

        self._tushare_provider = tushare_provider
        self._akshare_provider = akshare_provider or AkshareFundamentalProvider()

    def _resolve_tushare(self, symbol: str) -> Optional[BaseFundamentalProvider]:
        if self._tushare_provider is not None:
            return self._tushare_provider
        try:
            return TushareFundamentalProvider()
        except TushareTokenMissingError:
            # 未配置 token：Tushare 侧以 token_missing 如实记录，不阻断 AKShare
            return None

    def get_fundamentals(self, symbol: str) -> Dict[str, Any]:
        tushare = self._resolve_tushare(symbol)
        tushare_result = (
            tushare.get_fundamentals(symbol)
            if tushare is not None
            else _token_missing_result(symbol)
        )
        akshare_result = self._akshare_provider.get_fundamentals(symbol)
        return _merge_fundamentals(symbol, tushare_result, akshare_result)
