"""第十一阶段：估值历史分位分析（Valuation Percentile）。

背景：第十阶段 Evaluation 发现 Agent 常断言"估值处于历史偏低/历史中枢附近"，
但既有工具只返回当前时点 PE/PB，无历史分位数据支撑。本模块复用 Tushare
daily_basic 的历史序列，计算当前 PE/PB 在指定回看窗口内的历史分位，使估值
高低结论可追溯到真实数据。

设计（对齐第七阶段 Provider 契约）：
- compute_valuation_percentiles 为纯函数，可直接确定性测试；
- ValuationAnalysisProvider 复用 app.data.tushare_client.TushareClient 的
  fetch_daily_basic（当前账户有权限），失败时如实返回 error，绝不编造；
- 区分 fetched_at（获取时刻）与 data_date（最新估值数据对应交易日）；
- 数据仅用于研究和分析，不构成投资建议。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from app.data.tushare_client import (
    TushareAPIError,
    TushareClient,
    TusharePermissionError,
    TushareRateLimitError,
    TushareTokenMissingError,
)
from app.fundamentals.providers import (
    FUNDAMENTALS_API_PERMISSION_REQUIRED,
    BaseFundamentalProvider,
    _invalid_symbol_result,
    _token_missing_result,
)
from app.news.processing import normalize_a_share_symbol, utc_now_iso

# 默认历史分位回看窗口（自然年）
LOOKBACK_YEARS = 5
# 分位计算最小有效样本数（交易日）；低于该数视为分位不可靠
MIN_SAMPLES = 60


def _to_float(value: Any) -> Optional[float]:
    """安全的数值转换：None/非数字/NaN 一律返回 None。"""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return f


def _percentile_of(current: float, history: List[float]) -> Optional[float]:
    """current 在 history 中的经验分位（0~1）：history 中小于等于 current 的比例。

    None（空序列）时返回 None。
    """
    if not history:
        return None
    count = sum(1 for v in history if v <= current)
    return count / len(history)


def _sequence_stats(history: List[float]) -> Dict[str, Any]:
    """历史序列统计：min / max / median / mean，保留 4 位小数；空序列返回 None 值。"""
    if not history:
        return {"min": None, "max": None, "median": None, "mean": None}
    ordered = sorted(history)
    n = len(ordered)
    mid = n // 2
    if n % 2 == 1:
        median = ordered[mid]
    else:
        median = (ordered[mid - 1] + ordered[mid]) / 2.0
    return {
        "min": round(ordered[0], 4),
        "max": round(ordered[-1], 4),
        "median": round(median, 4),
        "mean": round(sum(ordered) / n, 4),
    }


def _filter_history(
    rows: List[Dict[str, Any]], field_candidates: List[str]
) -> List[float]:
    """从 daily_basic 行中提取有效历史序列。

    过滤规则：字段缺失、非数字、NaN、非正数（亏损/异常股 PE/PB 无统计意义）剔除。
    返回过滤后的数值列表（保持原始顺序，供分位计算使用）。
    """
    history: List[float] = []
    for row in rows:
        value = None
        for field in field_candidates:
            if field in row:
                value = _to_float(row.get(field))
                if value is not None:
                    break
        if value is None or value <= 0:
            continue
        history.append(value)
    return history


def compute_valuation_percentiles(
    rows: List[Dict[str, Any]],
    current_pe: Optional[float] = None,
    current_pb: Optional[float] = None,
    *,
    min_samples: int = MIN_SAMPLES,
) -> Dict[str, Any]:
    """从 daily_basic 历史行计算当前 PE/PB 的历史分位（纯函数，可确定性测试）。

    Args:
        rows: daily_basic 历史记录列表（含 trade_date / pe_ttm|pe / pb），顺序不限。
        current_pe: 当前 PE 值（通常取序列最新交易日）；None 表示无法计算分位。
        current_pb: 当前 PB 值；None 表示无法计算分位。
        min_samples: 有效样本数下限，低于此值分位标记为不可靠（reliable=False）。

    Returns:
        {
          "pe": {"percentile", "min", "max", "median", "mean",
                 "sample_count", "excluded_count", "reliable"} 或 None（current 缺失），
          "pb": 同上，
          "horizon": {"start", "end", "trading_days"},
          "reliable": 全局可靠性（pe/pb 至少一项可算且样本充足）
        }
    """
    pe_history = _filter_history(rows, ["pe_ttm", "pe"])
    pb_history = _filter_history(rows, ["pb"])

    start = end = None
    for row in rows:
        trade_date = row.get("trade_date")
        if trade_date is None:
            continue
        text = str(trade_date)
        if start is None or text < start:
            start = text
        if end is None or text > end:
            end = text

    def _build(
        current: Optional[float], history: List[float], total_rows: int
    ) -> Optional[Dict[str, Any]]:
        if current is None:
            return None
        current_f = _to_float(current)
        if current_f is None or current_f <= 0:
            # 当前估值异常（亏损/缺失）时如实返回 None，不编造分位
            return {
                "percentile": None,
                "sample_count": len(history),
                "excluded_count": total_rows - len(history),
                "reliable": False,
                "note": "当前估值缺失或非正数，无法计算历史分位。",
                **_sequence_stats(history),
            }
        stats = _sequence_stats(history)
        reliable = len(history) >= min_samples
        return {
            "percentile": round(_percentile_of(current_f, history), 4)
            if history else None,
            "sample_count": len(history),
            "excluded_count": total_rows - len(history),
            "reliable": reliable,
            **stats,
        }

    total_rows = len(rows)
    pe_result = _build(current_pe, pe_history, total_rows)
    pb_result = _build(current_pb, pb_history, total_rows)

    reliable = any(
        item is not None and item.get("reliable") for item in (pe_result, pb_result)
    )
    return {
        "pe": pe_result,
        "pb": pb_result,
        "horizon": {
            "start": start,
            "end": end,
            "trading_days": total_rows,
        },
        "reliable": reliable,
    }


def _status_error(
    result: Dict[str, Any], symbol: str, ts_code: str, error: str, detail: str
) -> Dict[str, Any]:
    return {
        **result,
        "symbol": symbol,
        "ts_code": ts_code,
        "error": error,
        "detail": detail,
    }


class ValuationAnalysisProvider(BaseFundamentalProvider):
    """基于 Tushare daily_basic 历史序列的估值分位 Provider。

    复用 TushareClient.fetch_daily_basic（当前账户有权限）拉取回看窗口内的
    全部每日指标，最新一条为当前估值，其余为历史序列。失败路径与第七阶段
    Provider 一致：token_missing / invalid_symbol / permission_denied /
    rate_limited / no_data，绝不静默成功。
    """

    def __init__(
        self,
        client: Optional[TushareClient] = None,
        lookback_years: int = LOOKBACK_YEARS,
        min_samples: int = MIN_SAMPLES,
    ) -> None:
        self._client = client or TushareClient()
        self._lookback_years = lookback_years
        self._min_samples = min_samples

    def get_fundamentals(self, symbol: str) -> Dict[str, Any]:
        try:
            ts_code = normalize_a_share_symbol(symbol)
        except ValueError:
            return _invalid_symbol_result(symbol)

        # 股票简称尽力而为：失败只记录，不影响核心分位计算
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
        start_date = (today - timedelta(days=365 * self._lookback_years)).strftime(
            "%Y%m%d"
        )
        end_date = today.strftime("%Y%m%d")

        try:
            rows = self._client.fetch_daily_basic(
                ts_code, start_date=start_date, end_date=end_date
            )
        except TushareTokenMissingError:
            return _token_missing_result(symbol)
        except TusharePermissionError as exc:
            return {
                "error": FUNDAMENTALS_API_PERMISSION_REQUIRED,
                "symbol": symbol,
                "ts_code": ts_code,
                "detail": str(exc),
            }
        except TushareRateLimitError as exc:
            return {
                "error": "rate_limited",
                "symbol": symbol,
                "ts_code": ts_code,
                "detail": str(exc),
            }
        except TushareAPIError as exc:
            return {
                "error": "tushare_error",
                "symbol": symbol,
                "ts_code": ts_code,
                "detail": str(exc),
            }

        if not rows:
            return {
                "error": "no_data",
                "symbol": symbol,
                "ts_code": ts_code,
                "detail": "daily_basic 接口可访问，但回看窗口内未获取到该股票的历史估值数据。",
            }

        # 最新交易日为当前估值（trade_date 降序取最大）
        latest = max(rows, key=lambda r: str(r.get("trade_date") or ""))
        current_pe = _to_float(latest.get("pe_ttm")) or _to_float(latest.get("pe"))
        current_pb = _to_float(latest.get("pb"))

        percentiles = compute_valuation_percentiles(
            rows, current_pe, current_pb, min_samples=self._min_samples
        )

        result: Dict[str, Any] = {
            "symbol": symbol,
            "ts_code": ts_code,
            "name": name,
            "data_date": latest.get("trade_date"),
            "fetched_at": utc_now_iso(),
            "current_valuation": {
                "pe": current_pe,
                "pb": current_pb,
                "note": "来自 daily_basic 最新交易日（data_date）。",
            },
            "percentiles": percentiles,
            "lookback_years": self._lookback_years,
            "data_source": "Tushare",
            "data_quality": {
                "sources": [
                    {
                        "source": "daily_basic",
                        "status": "ok",
                        "count": len(rows),
                        "detail": f"回看 {self._lookback_years} 年，共 {len(rows)} 个交易日",
                    }
                ]
            },
            "notice": "历史分位为基于过去数据的统计描述，不代表未来走势，不构成投资建议。",
        }
        if stock_info_error:
            result["stock_info_error"] = stock_info_error
        return result
