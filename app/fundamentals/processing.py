"""基本面数据纯逻辑处理（第七阶段）。

本模块只包含纯函数：字段提取、单位换算、去重、数据质量检查、结果组装。
Mock 与 Tushare Provider 共用同一套处理逻辑，保证两条路径产出相同结构。

单位约定（Tushare 原始单位）：
- 市值 total_mv/circ_mv：万元 → 亿元（除以 10000）
- 利润表 revenue/n_income_attr_p/n_income：元 → 亿元（除以 1e8）
- roe/yoy_tr/yoyprofit/dv_ttm/dv_ratio：百分数（按原值保留，单位记为 %）
- eps：元（按原值保留）

严格区分三个时间概念：
- data_date    : daily_basic 的 trade_date（估值数据的日期）
- report_period: 财务报表报告期（income/fina_indicator 的 end_date，API 格式 YYYYMMDD）
- fetched_at   : 数据抓取时刻（UTC ISO）
绝不可把 fetched_at 当作财务数据日期。

数据仅用于研究和分析，不构成投资建议。
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

# 单位换算常量
_WAN_TO_YI = 10000.0  # 万元 -> 亿元
_YUAN_TO_YI = 1e8     # 元   -> 亿元

# 统一单位说明（放在 data_quality.units 中）
_UNITS: Dict[str, str] = {
    "total_market_cap": "亿元",
    "float_market_cap": "亿元",
    "revenue": "亿元",
    "net_profit": "亿元",
    "roe": "%",
    "eps": "元",
    "revenue_growth": "%",
    "net_profit_growth": "%",
    "dividend_yield": "%",
}


def _to_float(value: Any) -> Optional[float]:
    """把任意值安全转为 float；None/NaN/非法文本返回 None。"""
    if value is None:
        return None
    if isinstance(value, float) and value != value:  # NaN
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(result) or math.isinf(result):
        return None
    return result


def _pick_first(row: Dict[str, Any], *keys: str) -> Optional[float]:
    """按顺序取第一个非空数值字段。"""
    for key in keys:
        value = _to_float(row.get(key))
        if value is not None:
            return value
    return None


def _normalize_key_rows(
    rows: List[Dict[str, Any]], period_key: str, announce_key: str
) -> Dict[str, Any]:
    """通用去重：同一 (period) 只保留 ann_date 最新的一条，按 period 降序排序。

    Returns:
        {"latest": 最新报告期记录(可能为 None), "all_desc": 按报告期降序的全列表,
         "removed": 被去重的条数}
    """
    if not rows:
        return {"latest": None, "all_desc": [], "removed": 0}

    keep: Dict[str, Dict[str, Any]] = {}
    removed = 0
    for row in rows:
        period = row.get(period_key)
        if period is None:
            removed += 1
            continue
        ann = row.get(announce_key) or ""
        existing = keep.get(period)
        if existing is None:
            keep[period] = row
        elif str(ann) > str(existing.get(announce_key) or ""):
            keep[period] = row
            removed += 1
        else:
            removed += 1

    all_desc = sorted(keep.values(), key=lambda r: str(r.get(period_key)), reverse=True)
    return {"latest": all_desc[0] if all_desc else None, "all_desc": all_desc, "removed": removed}


# ---------------------------------------------------------------------------
# 估值（daily_basic）
# ---------------------------------------------------------------------------
def normalize_daily_basic_rows(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """归一化每日指标：按 trade_date 降序取最新一天，其余记录计入 removed。"""
    if not rows:
        return {"latest": None, "removed": 0}
    kept: Dict[str, Dict[str, Any]] = {}
    removed = 0
    for row in rows:
        trade_date = row.get("trade_date")
        if trade_date is None:
            removed += 1
            continue
        existing = kept.get(trade_date)
        if existing is None:
            kept[trade_date] = row
        else:
            removed += 1
    latest = max(kept.values(), key=lambda r: str(r.get("trade_date")))
    return {"latest": latest, "removed": removed}


def extract_valuation(row: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """从 daily_basic 单条记录提取估值字段（单位：亿元）。

    - pe: 优先 pe_ttm，回退 pe
    - ps: 优先 ps_ttm，回退 ps
    - total_market_cap: total_mv(万元)/10000
    - float_market_cap: circ_mv(万元)/10000
    """
    if not row:
        return {
            "pe": None, "pb": None, "ps": None,
            "total_market_cap": None, "float_market_cap": None,
        }
    pe = _pick_first(row, "pe_ttm", "pe")
    ps = _pick_first(row, "ps_ttm", "ps")
    pb = _pick_first(row, "pb")
    total_mv = _to_float(row.get("total_mv"))
    circ_mv = _to_float(row.get("circ_mv"))
    return {
        "pe": pe,
        "pb": pb,
        "ps": ps,
        "total_market_cap": round(total_mv / _WAN_TO_YI, 4) if total_mv is not None else None,
        "float_market_cap": round(circ_mv / _WAN_TO_YI, 4) if circ_mv is not None else None,
    }


def extract_dividend(row: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """股息率：优先 dv_ttm，回退 dv_ratio（均为 %，按原值保留）。"""
    if not row:
        return {"dividend_yield": None}
    return {"dividend_yield": _pick_first(row, "dv_ttm", "dv_ratio")}


# ---------------------------------------------------------------------------
# 财务指标（fina_indicator）
# ---------------------------------------------------------------------------
def normalize_fina_rows(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """归一化财务指标：同一 end_date 保留 ann_date 最新一条，按 end_date 降序。"""
    return _normalize_key_rows(rows, "end_date", "ann_date")


def extract_profitability(row: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """roe(%) / eps(元)，缺失字段为 None。"""
    if not row:
        return {"roe": None, "eps": None}
    return {"roe": _to_float(row.get("roe")), "eps": _to_float(row.get("eps"))}


def extract_growth_from_fina(row: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """同比增速：yoy_tr(营收同比%) / yoyprofit(净利润同比%)，缺失为 None。"""
    if not row:
        return {"revenue_growth": None, "net_profit_growth": None}
    return {
        "revenue_growth": _to_float(row.get("yoy_tr")),
        "net_profit_growth": _to_float(row.get("yoyprofit")),
    }


# ---------------------------------------------------------------------------
# 利润表（income）
# ---------------------------------------------------------------------------
def normalize_income_rows(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """归一化利润表：同一 end_date 保留 ann_date 最新一条，按 end_date 降序。"""
    return _normalize_key_rows(rows, "end_date", "ann_date")


def extract_income(row: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """营业总收入 / 归母净利润（元 -> 亿元）。

    revenue 优先 revenue，回退 total_revenue；
    net_profit 优先 n_income_attr_p（归母），回退 n_income。
    """
    if not row:
        return {"revenue": None, "net_profit": None}
    revenue = _pick_first(row, "revenue", "total_revenue")
    net_profit = _pick_first(row, "n_income_attr_p", "n_income")
    return {
        "revenue": round(revenue / _YUAN_TO_YI, 4) if revenue is not None else None,
        "net_profit": round(net_profit / _YUAN_TO_YI, 4) if net_profit is not None else None,
    }


# ---------------------------------------------------------------------------
# 数据质量检查
# ---------------------------------------------------------------------------
def check_valuation_issues(valuation: Dict[str, Any]) -> List[Dict[str, Any]]:
    """检查估值字段异常：非正 PE/PB/PS 记录为 issue（保留原值，不篡改）。"""
    issues: List[Dict[str, Any]] = []
    for field in ("pe", "pb", "ps"):
        value = valuation.get(field)
        if value is not None and value <= 0:
            issues.append(
                {"field": field, "issue": "non_positive", "value": value}
            )
    return issues


# ---------------------------------------------------------------------------
# 结果组装
# ---------------------------------------------------------------------------
def build_fundamentals_result(
    symbol: str,
    name: Optional[str],
    valuation: Dict[str, Any],
    profitability: Dict[str, Any],
    growth: Dict[str, Any],
    dividend: Dict[str, Any],
    data_date: Optional[str],
    report_period: Optional[str],
    report_periods: List[str],
    fetched_at: str,
    data_source: str,
    data_quality: Dict[str, Any],
) -> Dict[str, Any]:
    """组装统一结构的 JSON 结果。缺失字段一律为 None，绝不伪造。"""
    return {
        "symbol": symbol,
        "name": name,
        "market": "A-share",
        "asset_type": "stock",
        "valuation": valuation,
        "profitability": profitability,
        "growth": growth,
        "dividend": dividend,
        "data_date": data_date,
        "report_period": report_period,
        "fetched_at": fetched_at,
        "data_source": data_source,
        "data_quality": data_quality,
    }


def build_fundamentals_from_sources(
    symbol: str,
    name: Optional[str],
    daily_basic_rows: List[Dict[str, Any]],
    income_rows: List[Dict[str, Any]],
    fina_rows: List[Dict[str, Any]],
    fetched_at: str,
    data_source: str,
    sources_status: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Mock 与 Tushare Provider 共用的组装流水线。

    从三类原始行中各自归一化、提取字段，并生成 data_quality：
    - units: 各字段单位说明
    - report_periods: 全部财务报告期（降序）
    - dedupe: 各类记录去重条数
    - sources: 各数据源状态
    - issues: 估值字段异常列表
    """
    daily_norm = normalize_daily_basic_rows(daily_basic_rows)
    income_norm = normalize_income_rows(income_rows)
    fina_norm = normalize_fina_rows(fina_rows)

    daily_latest = daily_norm["latest"]
    income_latest = income_norm["latest"]
    fina_latest = fina_norm["latest"]

    valuation = extract_valuation(daily_latest)
    dividend = extract_dividend(daily_latest)
    profitability = extract_profitability(fina_latest)
    growth = extract_growth_from_fina(fina_latest)
    income_vals = extract_income(income_latest)
    # 用户 schema 中 growth 同时承载利润表金额与同比增速；金额来自 income，
    # 增速来自 fina_indicator。任何来源缺失对应字段即为 None，绝不补算。
    growth = {
        "revenue": income_vals["revenue"],
        "revenue_growth": growth["revenue_growth"],
        "net_profit": income_vals["net_profit"],
        "net_profit_growth": growth["net_profit_growth"],
    }

    data_date = (
        str(daily_latest.get("trade_date")) if daily_latest is not None else None
    )

    all_financial_periods = [
        r.get("end_date")
        for r in income_norm["all_desc"] + fina_norm["all_desc"]
        if r.get("end_date") is not None
    ]
    report_periods = sorted(set(all_financial_periods), reverse=True)
    report_period = report_periods[0] if report_periods else None

    issues = check_valuation_issues(valuation)

    data_quality: Dict[str, Any] = {
        "units": dict(_UNITS),
        "report_periods": report_periods,
        "dedupe": {
            "daily_basic_removed": daily_norm["removed"],
            "income_removed": income_norm["removed"],
            "fina_indicator_removed": fina_norm["removed"],
        },
        "sources": sources_status,
        "issues": issues,
    }

    return build_fundamentals_result(
        symbol=symbol,
        name=name,
        valuation=valuation,
        profitability=profitability,
        growth=growth,
        dividend=dividend,
        data_date=data_date,
        report_period=report_period,
        report_periods=report_periods,
        fetched_at=fetched_at,
        data_source=data_source,
        data_quality=data_quality,
    )
