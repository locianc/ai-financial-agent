"""AKShare 基本面 Provider（东方财富公开财务接口，无需 token）。

针对 Tushare income / fina_indicator 接口无权限（2026-08-20 实测）的现状，
用东方财富公开接口补全"盈利、成长、三大财务报表"字段：

- stock_yjbb_em(date=...)               业绩报表：每股收益/营收/净利/ROE/毛利率/同比
- stock_profit_sheet_by_report_em       利润表（按报告期）
- stock_balance_sheet_by_report_em      资产负债表（按报告期）
- stock_cash_flow_sheet_by_report_em    现金流量表（按报告期）
- get_a_share_quote                     实时估值快照（PE/PB/市值，daily_basic 替代）

四类接口并行抓取以缩短耗时。数据来自公开行情源，不保证零延迟；
严格区分 report_period（财务报表报告期）与 fetched_at（获取时刻），
缺失字段一律为 None，绝不伪造。数据仅用于研究和分析，不构成投资建议。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from typing import Any, Dict, List, Optional

# 本机网络环境适配必须在本模块发起任何网络请求前生效
from tools import network_adapter  # noqa: F401

import akshare as ak

from app.fundamentals.processing import build_fundamentals_from_sources
from app.fundamentals.providers import BaseFundamentalProvider, _invalid_symbol_result
from app.news.processing import normalize_a_share_symbol, utc_now_iso
from tools.market_data import get_a_share_quote

_YUAN_TO_YI = 1e8    # 元 -> 亿元（三大报表金额）
_YUAN_TO_WAN = 1e4   # 元 -> 万元（daily_basic 的 total_mv/circ_mv 口径）
_QUARTER_END = ("0331", "0630", "0930", "1231")
_MAX_YJBB_FALLBACK = 8  # yjbb 回溯的最大季度数

# 东方财富业绩报表列名（探测定稿）
_YJBB_CODE = "股票代码"
_YJBB_NAME = "股票简称"
_YJBB_EPS = "每股收益"
_YJBB_REVENUE_GROWTH = "营业总收入-同比增长"
_YJBB_NETPROFIT_GROWTH = "净利润-同比增长"
_YJBB_BOOK_VALUE = "每股净资产"
_YJBB_ROE = "净资产收益率"
_YJBB_OCF_PER_SHARE = "每股经营现金流量"
_YJBB_GROSS_MARGIN = "销售毛利率"
_YJBB_ANNOUNCE = "最新公告日期"


def _to_float(value: Any) -> Optional[float]:
    """安全转换为 float，转换失败或为 NaN 时返回 None，不编造数据。"""
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result:  # NaN
        return None
    return result


def _to_yi(value: Any) -> Optional[float]:
    """元 -> 亿元（保留 4 位小数）。"""
    number = _to_float(value)
    if number is None:
        return None
    return round(number / _YUAN_TO_YI, 4)


def _period_iso(value: Any) -> Optional[str]:
    """把报表日期（如 "2026-06-30 00:00:00"）统一为 YYYY-MM-DD。"""
    text = str(value).strip()[:10]
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        return text
    return None


def _period_compact(period_iso: str) -> str:
    """YYYY-MM-DD -> YYYYMMDD（报告期，与 Tushare end_date 同口径）。"""
    return period_iso.replace("-", "")


def _quarter_end(today: date) -> str:
    """今日所在季度末，格式 YYYYMMDD（如 2026-08-21 -> 20260930）。"""
    year = today.year
    month = today.month
    if month <= 3:
        return f"{year}0331"
    if month <= 6:
        return f"{year}0630"
    if month <= 9:
        return f"{year}0930"
    return f"{year}1231"


def _previous_periods(start_period: str, count: int) -> List[str]:
    """从 start_period（YYYYMMDD）起按季度回退生成候选报告期列表。"""
    year = int(start_period[:4])
    quarter = (int(start_period[4:6]) - 1) // 3  # 0..3
    periods: List[str] = []
    for i in range(count):
        total = year * 4 + quarter - i
        y, q = divmod(total, 4)
        periods.append(f"{y}{_QUARTER_END[q]}")
    return periods


def _safe_sheet(func: Any, symbol: str) -> Optional[Any]:
    """安全调用 emweb 报表接口，失败返回 None（状态由调用方记录）。"""
    try:
        df = func(symbol=symbol)
    except Exception:
        return None
    if df is None or df.empty:
        return None
    return df


def _safe_yjbb(period: str) -> Optional[Any]:
    """安全调用业绩报表接口，失败返回 None。"""
    try:
        df = ak.stock_yjbb_em(date=period)
    except Exception:
        return None
    if df is None or df.empty:
        return None
    return df


class AkshareFundamentalProvider(BaseFundamentalProvider):
    """AKShare 基本面 Provider（东方财富公开接口，无需 token）。

    估值来自实时行情快照（data_date 为快照当日）；盈利/成长来自业绩报表；
    三大财务报表金额来自 emweb 按报告期接口。所有来源状态如实写入
    data_quality.sources，缺失字段一律为 None。
    """

    def get_fundamentals(self, symbol: str) -> Dict[str, Any]:
        try:
            ts_code = normalize_a_share_symbol(symbol)
        except ValueError:
            return _invalid_symbol_result(symbol)

        code = ts_code.split(".")[0]
        em_symbol = f"SH{code}" if code.startswith("6") else f"SZ{code}"
        fetched_at = utc_now_iso()
        today = date.today()

        # 并行抓取：三张报表 + 实时估值 + 当日季度业绩报表
        first_candidate = _quarter_end(today)
        with ThreadPoolExecutor(max_workers=5) as pool:
            f_income = pool.submit(_safe_sheet, ak.stock_profit_sheet_by_report_em, em_symbol)
            f_balance = pool.submit(_safe_sheet, ak.stock_balance_sheet_by_report_em, em_symbol)
            f_cash = pool.submit(_safe_sheet, ak.stock_cash_flow_sheet_by_report_em, em_symbol)
            f_quote = pool.submit(get_a_share_quote, code)
            f_yjbb = pool.submit(_safe_yjbb, first_candidate)
            income_df = f_income.result()
            balance_df = f_balance.result()
            cash_df = f_cash.result()
            quote = f_quote.result()
            yjbb_df = f_yjbb.result()

        sources_status: List[Dict[str, Any]] = [
            {"source": "profit_sheet", "status": "ok" if income_df is not None else "error",
             "count": len(income_df) if income_df is not None else 0},
            {"source": "balance_sheet", "status": "ok" if balance_df is not None else "error",
             "count": len(balance_df) if balance_df is not None else 0},
            {"source": "cash_flow_sheet", "status": "ok" if cash_df is not None else "error",
             "count": len(cash_df) if cash_df is not None else 0},
            {"source": "quote", "status": "ok" if "error" not in quote else "error",
             "detail": "实时快照估值，data_date 为快照当日"},
        ]

        # 报告期以利润表最新一期为准；缺利润表时回退资产负债表/现金流量表
        period_iso: Optional[str] = None
        for df in (income_df, balance_df, cash_df):
            if df is not None and not df.empty:
                period_iso = _period_iso(df.iloc[0].get("REPORT_DATE"))
                if period_iso:
                    break

        # 业绩报表：优先当日季度 -> 报表报告期 -> 逐季度回溯
        yjbb_row: Optional[Dict[str, Any]] = None
        yjbb_period: Optional[str] = None
        if yjbb_df is not None and not yjbb_df.empty:
            matched = yjbb_df[yjbb_df[_YJBB_CODE].astype(str) == code]
            if not matched.empty:
                yjbb_row = matched.iloc[0].to_dict()
                yjbb_period = first_candidate
        if yjbb_row is None and period_iso:
            sheet_period = _period_compact(period_iso)
            if sheet_period != first_candidate:
                yjbb_df = _safe_yjbb(sheet_period)
                if yjbb_df is not None and not yjbb_df.empty:
                    matched = yjbb_df[yjbb_df[_YJBB_CODE].astype(str) == code]
                    if not matched.empty:
                        yjbb_row = matched.iloc[0].to_dict()
                        yjbb_period = sheet_period
        if yjbb_row is None:
            base_period = period_iso and _period_compact(period_iso) or first_candidate
            for period in _previous_periods(base_period, _MAX_YJBB_FALLBACK):
                if period == yjbb_period:
                    continue
                yjbb_df = _safe_yjbb(period)
                if yjbb_df is None or yjbb_df.empty:
                    continue
                matched = yjbb_df[yjbb_df[_YJBB_CODE].astype(str) == code]
                if not matched.empty:
                    yjbb_row = matched.iloc[0].to_dict()
                    yjbb_period = period
                    break
        sources_status.append({
            "source": "yjbb",
            "status": "ok" if yjbb_row is not None else "error",
            "detail": f"报告期 {yjbb_period}" if yjbb_period else None,
        })

        # 构建与 Tushare 同口径的原始行，复用统一组装流水线
        daily_rows: List[Dict[str, Any]] = []
        if "error" not in quote:
            total_mv = _to_float(quote.get("total_market_cap"))
            circ_mv = _to_float(quote.get("float_market_cap"))
            daily_rows.append({
                "trade_date": today.strftime("%Y%m%d"),
                "pe_ttm": _to_float(quote.get("pe")),
                "pb": _to_float(quote.get("pb")),
                # 市值单位换算：元 -> 万元（processing 再 /10000 -> 亿元）
                "total_mv": total_mv / _YUAN_TO_WAN if total_mv is not None else None,
                "circ_mv": circ_mv / _YUAN_TO_WAN if circ_mv is not None else None,
            })

        income_rows: List[Dict[str, Any]] = []
        if income_df is not None and not income_df.empty:
            row = income_df.iloc[0]
            end_date = _period_compact(_period_iso(row.get("REPORT_DATE")) or "")
            income_rows.append({
                "end_date": end_date,
                "ann_date": end_date,  # emweb 报表无公告日字段，以报告期代
                "revenue": row.get("TOTAL_OPERATE_INCOME"),
                "n_income_attr_p": row.get("PARENT_NETPROFIT"),
            })

        fina_rows: List[Dict[str, Any]] = []
        if yjbb_row is not None and yjbb_period:
            fina_rows.append({
                "end_date": yjbb_period,
                "ann_date": str(yjbb_row.get(_YJBB_ANNOUNCE))[:10].replace("-", ""),
                "eps": _to_float(yjbb_row.get(_YJBB_EPS)),
                "roe": _to_float(yjbb_row.get(_YJBB_ROE)),
                "yoy_tr": _to_float(yjbb_row.get(_YJBB_REVENUE_GROWTH)),
                "yoyprofit": _to_float(yjbb_row.get(_YJBB_NETPROFIT_GROWTH)),
            })

        name = None
        if yjbb_row is not None:
            name = yjbb_row.get(_YJBB_NAME)
        elif "error" not in quote:
            name = quote.get("name")

        any_data = any((daily_rows, income_rows, fina_rows, income_df, balance_df, cash_df))
        if not any_data:
            return {
                "error": "no_data",
                "symbol": ts_code,
                "detail": "AKShare 财务接口可访问，但未获取到该股票的数据。",
                "data_source": "AKShare",
                "sources_status": sources_status,
            }

        result = build_fundamentals_from_sources(
            symbol=ts_code,
            name=name,
            daily_basic_rows=daily_rows,
            income_rows=income_rows,
            fina_rows=fina_rows,
            fetched_at=fetched_at,
            data_source="AKShare",
            sources_status=sources_status,
        )

        # 盈利补充字段（来自业绩报表）
        if yjbb_row is not None:
            result["profitability"]["gross_margin"] = _to_float(
                yjbb_row.get(_YJBB_GROSS_MARGIN))
            result["profitability"]["book_value_per_share"] = _to_float(
                yjbb_row.get(_YJBB_BOOK_VALUE))
            result["profitability"]["operating_cash_flow_per_share"] = _to_float(
                yjbb_row.get(_YJBB_OCF_PER_SHARE))

        # 三大财务报表（金额统一为亿元）
        statements: Dict[str, Any] = {"report_period": period_iso, "units": "亿元"}
        if income_df is not None and not income_df.empty:
            row = income_df.iloc[0]
            statements["income"] = {
                "revenue": _to_yi(row.get("TOTAL_OPERATE_INCOME")),
                "operating_profit": _to_yi(row.get("OPERATE_PROFIT")),
                "total_profit": _to_yi(row.get("TOTAL_PROFIT")),
                "net_profit": _to_yi(row.get("NETPROFIT")),
                "parent_net_profit": _to_yi(row.get("PARENT_NETPROFIT")),
            }
        if balance_df is not None and not balance_df.empty:
            row = balance_df.iloc[0]
            statements["balance_sheet"] = {
                "total_assets": _to_yi(row.get("TOTAL_ASSETS")),
                "total_liabilities": _to_yi(row.get("TOTAL_LIABILITIES")),
                "total_equity": _to_yi(row.get("TOTAL_EQUITY")),
                "parent_equity": _to_yi(row.get("TOTAL_PARENT_EQUITY")),
                "monetary_funds": _to_yi(row.get("MONETARYFUNDS")),
            }
        if cash_df is not None and not cash_df.empty:
            row = cash_df.iloc[0]
            statements["cash_flow"] = {
                "operating_net_cash_flow": _to_yi(row.get("NETCASH_OPERATE")),
                "investing_net_cash_flow": _to_yi(row.get("NETCASH_INVEST")),
                "financing_net_cash_flow": _to_yi(row.get("NETCASH_FINANCE")),
            }
        if len(statements) > 2:
            result["financial_statements"] = statements

        # 补全报告期列表：汇总报表接口返回的全部报告期
        all_periods: List[str] = []
        for df in (income_df, balance_df, cash_df):
            if df is None or df.empty:
                continue
            for value in df.get("REPORT_DATE", []):
                period = _period_iso(value)
                if period:
                    all_periods.append(_period_compact(period))
        if yjbb_period:
            all_periods.append(yjbb_period)
        result["data_quality"]["report_periods"] = sorted(set(all_periods), reverse=True)
        return result
