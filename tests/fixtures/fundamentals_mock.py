"""基本面 Mock 数据（第七阶段测试专用）。

覆盖用户要求的场景：贵州茅台完整数据、正常股票、无数据、缺 PE、负 PE、
缺 ROE、多报告期、重复数据、非法代码、接口无权限。

所有 Mock 行只用于单元测试与演示，绝不冒充真实财务数据。
数据仅用于研究和分析，不构成投资建议。
"""

from __future__ import annotations

from typing import Any, Dict, List

from app.data.tushare_client import TusharePermissionError

# ---------------------------------------------------------------------------
# 贵州茅台（完整数据）
# ---------------------------------------------------------------------------
MAOTAI_TS_CODE = "600519.SH"
MAOTAI_NAME = "贵州茅台"

MAOTAI_DAILY_BASIC_ROWS: List[Dict[str, Any]] = [
    {
        "ts_code": MAOTAI_TS_CODE,
        "trade_date": "20260819",
        "pe_ttm": 24.5,
        "pb": 7.9,
        "ps_ttm": 9.2,
        "total_mv": 189680000.0,   # 万元 -> 18968.0 亿元
        "circ_mv": 189680000.0,    # 万元 -> 18968.0 亿元
        "dv_ttm": 2.05,            # %
    },
]

MAOTAI_INCOME_ROWS: List[Dict[str, Any]] = [
    {
        "ts_code": MAOTAI_TS_CODE,
        "end_date": "20260331",
        "ann_date": "20260428",
        "revenue": 4.18e10,        # 元 -> 418.0 亿元
        "n_income_attr_p": 2.08e10,  # 元 -> 208.0 亿元
    },
    {
        "ts_code": MAOTAI_TS_CODE,
        "end_date": "20251231",
        "ann_date": "20260415",
        "revenue": 1.7e12,
        "n_income_attr_p": 8.6e11,
    },
]

MAOTAI_FINA_ROWS: List[Dict[str, Any]] = [
    {
        "ts_code": MAOTAI_TS_CODE,
        "end_date": "20260331",
        "ann_date": "20260428",
        "eps": 16.6,     # 元
        "roe": 9.6,      # %
        "yoy_tr": 12.3,  # %
        "yoyprofit": 13.8,  # %
    },
    {
        "ts_code": MAOTAI_TS_CODE,
        "end_date": "20251231",
        "ann_date": "20260415",
        "eps": 64.8,
        "roe": 30.4,
        "yoy_tr": 15.5,
        "yoyprofit": 19.3,
    },
]

# ---------------------------------------------------------------------------
# 正常股票（平安银行，只有 daily_basic；验证 pe 回退与部分数据）
# ---------------------------------------------------------------------------
NORMAL_TS_CODE = "000001.SZ"
NORMAL_NAME = "平安银行"

NORMAL_DAILY_BASIC_ROWS: List[Dict[str, Any]] = [
    {
        "ts_code": NORMAL_TS_CODE,
        "trade_date": "20260819",
        "pe": 5.2,               # 无 pe_ttm，验证回退 pe
        "pb": 0.55,
        "total_mv": 2.3e7,       # 万元 -> 2300.0 亿元
        "circ_mv": 2.3e7,
        "dv_ratio": 6.1,         # 无 dv_ttm，验证回退 dv_ratio
    },
]

# ---------------------------------------------------------------------------
# 无数据
# ---------------------------------------------------------------------------
NO_DATA_DAILY_BASIC_ROWS: List[Dict[str, Any]] = []
NO_DATA_INCOME_ROWS: List[Dict[str, Any]] = []
NO_DATA_FINA_ROWS: List[Dict[str, Any]] = []

# ---------------------------------------------------------------------------
# 缺 PE（无 pe_ttm / pe 字段）
# ---------------------------------------------------------------------------
MISSING_PE_DAILY_BASIC_ROWS: List[Dict[str, Any]] = [
    {
        "ts_code": MAOTAI_TS_CODE,
        "trade_date": "20260819",
        "pb": 7.9,
        "total_mv": 189680000.0,
        "circ_mv": 189680000.0,
    },
]

# ---------------------------------------------------------------------------
# 负 PE（保留原值并在 issues 中标记）
# ---------------------------------------------------------------------------
NEGATIVE_PE_DAILY_BASIC_ROWS: List[Dict[str, Any]] = [
    {
        "ts_code": MAOTAI_TS_CODE,
        "trade_date": "20260819",
        "pe_ttm": -8.6,
        "pb": 7.9,
        "total_mv": 189680000.0,
        "circ_mv": 189680000.0,
    },
]

# ---------------------------------------------------------------------------
# 缺 ROE（无 roe 字段）
# ---------------------------------------------------------------------------
MISSING_ROE_FINA_ROWS: List[Dict[str, Any]] = [
    {
        "ts_code": MAOTAI_TS_CODE,
        "end_date": "20260331",
        "ann_date": "20260428",
        "eps": 16.6,
        "yoy_tr": 12.3,
        "yoyprofit": 13.8,
    },
]

# ---------------------------------------------------------------------------
# 多报告期（3 个报告期，最新为 20260331）
# ---------------------------------------------------------------------------
MULTI_PERIOD_FINA_ROWS: List[Dict[str, Any]] = [
    {
        "ts_code": MAOTAI_TS_CODE,
        "end_date": "20250331",
        "ann_date": "20250428",
        "eps": 15.1,
        "roe": 8.8,
        "yoy_tr": 11.2,
        "yoyprofit": 12.5,
    },
    {
        "ts_code": MAOTAI_TS_CODE,
        "end_date": "20250630",
        "ann_date": "20250828",
        "eps": 31.2,
        "roe": 18.4,
        "yoy_tr": 11.6,
        "yoyprofit": 13.0,
    },
    {
        "ts_code": MAOTAI_TS_CODE,
        "end_date": "20260331",
        "ann_date": "20260428",
        "eps": 16.6,
        "roe": 9.6,
        "yoy_tr": 12.3,
        "yoyprofit": 13.8,
    },
]

# ---------------------------------------------------------------------------
# 重复数据（同一报告期 20260331 两条，保留 ann_date 最新 20260515）
# ---------------------------------------------------------------------------
DUPLICATE_FINA_ROWS: List[Dict[str, Any]] = [
    {
        "ts_code": MAOTAI_TS_CODE,
        "end_date": "20260331",
        "ann_date": "20260429",
        "eps": 16.6,
        "roe": 9.6,
    },
    {
        "ts_code": MAOTAI_TS_CODE,
        "end_date": "20260331",
        "ann_date": "20260515",
        "eps": 16.7,
        "roe": 9.9,
    },
]

# ---------------------------------------------------------------------------
# 非法代码
# ---------------------------------------------------------------------------
INVALID_SYMBOLS: List[Any] = [
    "",
    "  ",
    None,
    "12345",          # 5 位
    "ABCDEF",         # 非数字
    "600519.SH.SZ",   # 多余后缀
    "830799",         # 非沪深主板/创业/科创前缀
    "000001.SH",      # 000 前缀应为 SZ，后缀矛盾
]

# ---------------------------------------------------------------------------
# 无权限假客户端（模拟 income / fina_indicator 无权限）
# ---------------------------------------------------------------------------
class FakePermissionClient:
    """所有基本面接口都抛 TusharePermissionError 的假客户端。"""

    def lookup_stock_info(self, ts_code: str) -> Dict[str, Any]:
        return {"ts_code": ts_code, "name": "测试公司"}

    def fetch_daily_basic(self, **kwargs: Any) -> List[Dict[str, Any]]:
        raise TusharePermissionError("无权限")

    def fetch_income(self, **kwargs: Any) -> List[Dict[str, Any]]:
        raise TusharePermissionError("无权限")

    def fetch_fina_indicator(self, **kwargs: Any) -> List[Dict[str, Any]]:
        raise TusharePermissionError("无权限")
