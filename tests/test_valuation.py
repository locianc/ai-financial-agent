"""第十一阶段估值历史分位分析：deterministic 单元测试（不调用任何真实 API）。

覆盖：
- compute_valuation_percentiles 纯函数：分位语义、空序列、缺失/NaN/非正数剔除、
  pe_ttm 优先于 pe、current 为 None/非正数、min_samples 边界、horizon 统计；
- ValuationAnalysisProvider：正常结构、invalid_symbol / token_missing /
  permission_denied / rate_limited / tushare_error / no_data 错误分支、
  股票简称尽力而为（失败只记录）、样本不足 reliable=False；
- get_valuation_analysis 工具函数：默认 Provider 注入与 token_missing 兜底。

运行：cd E:/github/ai-financial-agent && .venv/Scripts/python.exe tests/test_valuation.py
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

# 确保能导入项目根目录下的 app / tools 包
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.data.tushare_client import (
    TushareAPIError,
    TushareClient,
    TusharePermissionError,
    TushareRateLimitError,
    TushareTokenMissingError,
)
from app.fundamentals.providers import FUNDAMENTALS_API_PERMISSION_REQUIRED
from app.fundamentals.valuation import (
    MIN_SAMPLES,
    compute_valuation_percentiles,
    ValuationAnalysisProvider,
)
from app.tools.valuation_tool import VALUATION_TOOL_SCHEMA, get_valuation_analysis


# ---------------------------------------------------------------------------
# 测试辅助
# ---------------------------------------------------------------------------

def _make_rows(n: int = 100, start: str = "20220104") -> List[Dict[str, Any]]:
    """构造 n 条 daily_basic 行：pe/pb 单调递增，trade_date 逐日递增。"""
    rows = []
    base = date.fromisoformat(f"{start[:4]}-{start[4:6]}-{start[6:8]}")
    for i in range(n):
        rows.append(
            {
                "trade_date": (base + timedelta(days=i)).strftime("%Y%m%d"),
                "pe_ttm": round(10 + i * 0.5, 4),
                "pe": round(10 + i * 0.5, 4),
                "pb": round(1 + i * 0.1, 4),
            }
        )
    return rows


class _FakeClient(TushareClient):
    """不调用真实 Tushare API 的测试替身；仅覆写 fetch_daily_basic /
    lookup_stock_info，__init__ 不调用父类（避免真实 token/网络）。"""

    def __init__(
        self,
        rows: Optional[List[Dict[str, Any]]] = None,
        name: str = "贵州茅台",
        raise_on: Optional[Dict[str, Exception]] = None,
    ) -> None:
        self._rows = rows or []
        self._name = name
        self._raise_on = raise_on or {}

    def lookup_stock_info(self, ts_code: str) -> Optional[Dict[str, Any]]:
        if "lookup_stock_info" in self._raise_on:
            raise self._raise_on["lookup_stock_info"]
        return {"name": self._name}

    def fetch_daily_basic(
        self, ts_code: str, start_date: str, end_date: str
    ) -> List[Dict[str, Any]]:
        if "fetch_daily_basic" in self._raise_on:
            raise self._raise_on["fetch_daily_basic"]
        return self._rows


def _run(name: str, fn) -> None:
    try:
        fn()
        print(f"  PASS  {name}")
    except AssertionError as exc:
        print(f"  FAIL  {name}: {exc}")
        _FAILURES.append(f"{name}: {exc}")
    except Exception as exc:  # noqa: BLE001 - 测试脚本捕获所有异常并计数
        print(f"  ERROR {name}: {type(exc).__name__}: {exc}")
        _FAILURES.append(f"{name}: {type(exc).__name__}: {exc}")


_FAILURES: List[str] = []


# ---------------------------------------------------------------------------
# A. compute_valuation_percentiles 纯函数
# ---------------------------------------------------------------------------

def test_percentile_semantics() -> None:
    rows = [
        {"trade_date": "20220104", "pe_ttm": 10, "pb": 1.0},
        {"trade_date": "20220105", "pe_ttm": 20, "pb": 2.0},
        {"trade_date": "20220106", "pe_ttm": 30, "pb": 3.0},
        {"trade_date": "20220107", "pe_ttm": 40, "pb": 4.0},
        {"trade_date": "20220110", "pe_ttm": 50, "pb": 5.0},
    ]
    res = compute_valuation_percentiles(rows, current_pe=30, current_pb=2)
    assert res["pe"]["percentile"] == 0.6, res["pe"]
    assert res["pe"]["sample_count"] == 5
    assert res["pe"]["excluded_count"] == 0
    assert res["pe"]["reliable"] is False  # 5 < MIN_SAMPLES(60)
    assert res["pb"]["percentile"] == 0.4, res["pb"]
    assert res["pe"]["min"] == 10 and res["pe"]["max"] == 50
    assert res["pe"]["median"] == 30 and res["pe"]["mean"] == 30
    assert res["horizon"]["start"] == "20220104"
    assert res["horizon"]["end"] == "20220110"
    assert res["horizon"]["trading_days"] == 5
    assert res["reliable"] is False

    # current 为序列最大值/最小值时的分位边界
    res_max = compute_valuation_percentiles(rows, current_pe=50)
    assert res_max["pe"]["percentile"] == 1.0
    res_min = compute_valuation_percentiles(rows, current_pe=5)
    assert res_min["pe"]["percentile"] == 0.0


def test_empty_rows() -> None:
    # current 存在但历史为空：返回含 percentile=None 的不可靠 dict，而非 None
    res = compute_valuation_percentiles([], current_pe=30, current_pb=1)
    for item in (res["pe"], res["pb"]):
        assert item["percentile"] is None
        assert item["reliable"] is False
        assert item["sample_count"] == 0 and item["excluded_count"] == 0
    assert res["horizon"] == {"start": None, "end": None, "trading_days": 0}
    assert res["reliable"] is False


def test_exclusion_of_missing_nan_nonpositive() -> None:
    rows = [
        {"trade_date": "20220104", "pe_ttm": 10},
        {"trade_date": "20220105", "pe_ttm": None},
        {"trade_date": "20220106", "pe_ttm": "N/A"},
        {"trade_date": "20220107", "pe_ttm": 0},
        {"trade_date": "20220110", "pe_ttm": -5},
        {"trade_date": "20220111", "pe_ttm": float("nan")},
        {"trade_date": "20220112", "pe_ttm": 20},
        {"trade_date": "20220113", "pe_ttm": 30},
        {"trade_date": "20220114", "pe_ttm": 40},
        {"trade_date": "20220115", "pe_ttm": 50},
    ]
    res = compute_valuation_percentiles(rows, current_pe=40, current_pb=None)
    assert res["pe"]["sample_count"] == 5, res["pe"]  # 有效：10,20,30,40,50
    assert res["pe"]["excluded_count"] == 5  # 剔除：None、"N/A"、0、-5、NaN
    assert res["pe"]["percentile"] == 0.8, res["pe"]  # 4/5
    assert res["pb"] is None


def test_pe_ttm_preferred_over_pe() -> None:
    rows = [
        {"trade_date": "20220104", "pe_ttm": 15, "pe": 999},
        {"trade_date": "20220105", "pe_ttm": 25, "pe": 999},
        {"trade_date": "20220106", "pe_ttm": 35, "pe": 999},
    ]
    res = compute_valuation_percentiles(rows, current_pe=25)
    assert res["pe"]["sample_count"] == 3
    assert res["pe"]["percentile"] == round(2 / 3, 4)  # 用 pe_ttm，而非 pe=999


def test_current_none() -> None:
    rows = _make_rows(100)
    res = compute_valuation_percentiles(rows, current_pe=None, current_pb=None)
    assert res["pe"] is None and res["pb"] is None
    assert res["reliable"] is False


def test_current_nonpositive() -> None:
    rows = _make_rows(100)
    res = compute_valuation_percentiles(rows, current_pe=-5, current_pb=0)
    pe = res["pe"]
    assert pe["percentile"] is None
    assert pe["reliable"] is False
    assert "无法计算" in pe["note"]
    assert pe["sample_count"] == 100
    pb = res["pb"]
    assert pb["percentile"] is None and "无法计算" in pb["note"]


def test_min_samples_boundary() -> None:
    rows = [{"trade_date": f"2022010{i}", "pe_ttm": i + 1} for i in range(3)]
    # 恰好满足 min_samples
    res_ok = compute_valuation_percentiles(rows, current_pe=2, min_samples=3)
    assert res_ok["pe"]["reliable"] is True
    # 差一个样本
    res_bad = compute_valuation_percentiles(rows, current_pe=2, min_samples=4)
    assert res_bad["pe"]["reliable"] is False
    assert res_bad["reliable"] is False


# ---------------------------------------------------------------------------
# B. ValuationAnalysisProvider（FakeClient 注入，不调真实 API）
# ---------------------------------------------------------------------------

def test_provider_normal_structure() -> None:
    rows = _make_rows(100)
    provider = ValuationAnalysisProvider(client=_FakeClient(rows, name="贵州茅台"))
    res = provider.get_fundamentals("600519")
    assert res["symbol"] == "600519"
    assert res["ts_code"] == "600519.SH"
    assert res["name"] == "贵州茅台"
    assert res["data_date"] == rows[-1]["trade_date"]
    assert res["data_source"] == "Tushare"
    assert res["fetched_at"]
    assert res["current_valuation"]["pe"] == rows[-1]["pe_ttm"]
    assert res["current_valuation"]["pb"] == rows[-1]["pb"]
    assert res["percentiles"]["pe"]["percentile"] == 1.0  # 最新值为序列最大
    assert res["percentiles"]["pe"]["reliable"] is True
    assert res["percentiles"]["pb"]["reliable"] is True
    assert res["percentiles"]["horizon"]["trading_days"] == 100
    assert res["percentiles"]["horizon"]["start"] == rows[0]["trade_date"]
    assert res["percentiles"]["reliable"] is True
    assert res["lookback_years"] == 5
    assert res["data_quality"]["sources"] == [
        {"source": "daily_basic", "status": "ok", "count": 100,
         "detail": "回看 5 年，共 100 个交易日"}
    ]
    assert "不构成投资建议" in res["notice"]
    assert "error" not in res


def test_provider_symbol_normalization() -> None:
    provider = ValuationAnalysisProvider(client=_FakeClient(_make_rows(100)))
    res = provider.get_fundamentals("600519.SH")
    assert res["ts_code"] == "600519.SH"
    res2 = provider.get_fundamentals("000001")
    assert res2["ts_code"] == "000001.SZ"


def test_provider_invalid_symbol() -> None:
    provider = ValuationAnalysisProvider(client=_FakeClient())
    res = provider.get_fundamentals("123456")  # 非 A 股合法前缀
    assert res["error"] == "invalid_symbol"
    assert res["symbol"] == "123456"


def test_provider_token_missing() -> None:
    # lookup_stock_info 抛 token_missing
    provider = ValuationAnalysisProvider(
        client=_FakeClient(
            _make_rows(100),
            raise_on={"lookup_stock_info": TushareTokenMissingError("no token")},
        )
    )
    res = provider.get_fundamentals("600519")
    assert res["error"] == "token_missing"
    # fetch_daily_basic 抛 token_missing
    provider2 = ValuationAnalysisProvider(
        client=_FakeClient(
            _make_rows(100),
            raise_on={"fetch_daily_basic": TushareTokenMissingError("no token")},
        )
    )
    res2 = provider2.get_fundamentals("600519")
    assert res2["error"] == "token_missing"


def test_provider_permission_denied() -> None:
    provider = ValuationAnalysisProvider(
        client=_FakeClient(
            _make_rows(100),
            raise_on={"fetch_daily_basic": TusharePermissionError("无权限")},
        )
    )
    res = provider.get_fundamentals("600519")
    assert res["error"] == FUNDAMENTALS_API_PERMISSION_REQUIRED
    assert res["symbol"] == "600519"


def test_provider_rate_limited() -> None:
    provider = ValuationAnalysisProvider(
        client=_FakeClient(
            _make_rows(100),
            raise_on={"fetch_daily_basic": TushareRateLimitError("频率超限")},
        )
    )
    res = provider.get_fundamentals("600519")
    assert res["error"] == "rate_limited"


def test_provider_tushare_error() -> None:
    provider = ValuationAnalysisProvider(
        client=_FakeClient(
            _make_rows(100),
            raise_on={"fetch_daily_basic": TushareAPIError("接口内部错误")},
        )
    )
    res = provider.get_fundamentals("600519")
    assert res["error"] == "tushare_error"


def test_provider_no_data() -> None:
    provider = ValuationAnalysisProvider(client=_FakeClient(rows=[]))
    res = provider.get_fundamentals("600519")
    assert res["error"] == "no_data"
    assert "未获取到该股票的历史估值数据" in res["detail"]


def test_provider_stock_info_error_recorded_only() -> None:
    # 股票简称失败只记录，不影响核心分位结果
    provider = ValuationAnalysisProvider(
        client=_FakeClient(
            _make_rows(100),
            raise_on={"lookup_stock_info": TushareAPIError("stock_basic 失败")},
        )
    )
    res = provider.get_fundamentals("600519")
    assert "error" not in res
    assert res["name"] is None
    assert "stock_info_error" in res
    assert res["percentiles"]["pe"]["reliable"] is True


def test_provider_low_samples_not_reliable() -> None:
    provider = ValuationAnalysisProvider(client=_FakeClient(_make_rows(30)))
    res = provider.get_fundamentals("600519")
    assert res["percentiles"]["pe"]["reliable"] is False
    assert res["percentiles"]["reliable"] is False
    assert res["percentiles"]["pe"]["sample_count"] == 30


def test_provider_current_nonpositive_reported() -> None:
    # 最新交易日 PE 非正（亏损）时如实返回，不编造分位
    rows = _make_rows(100)
    rows[-1]["pe_ttm"] = -5
    rows[-1]["pe"] = -5
    provider = ValuationAnalysisProvider(client=_FakeClient(rows))
    res = provider.get_fundamentals("600519")
    assert res["current_valuation"]["pe"] == -5
    pe = res["percentiles"]["pe"]
    assert pe["percentile"] is None
    assert pe["reliable"] is False
    assert "无法计算" in pe["note"]


# ---------------------------------------------------------------------------
# C. get_valuation_analysis 工具函数
# ---------------------------------------------------------------------------

def test_tool_schema_shape() -> None:
    assert VALUATION_TOOL_SCHEMA["type"] == "function"
    fn = VALUATION_TOOL_SCHEMA["function"]
    assert fn["name"] == "get_valuation_analysis"
    assert "symbol" in fn["parameters"]["properties"]
    assert fn["parameters"]["required"] == ["symbol"]
    assert "历史分位" in fn["description"]


def test_tool_function_with_provider() -> None:
    res = get_valuation_analysis("600519", provider=ValuationAnalysisProvider(
        client=_FakeClient(_make_rows(100))
    ))
    assert res["ts_code"] == "600519.SH"
    assert res["percentiles"]["pe"]["reliable"] is True


def test_tool_function_token_missing_fallback() -> None:
    res = get_valuation_analysis("600519", provider=ValuationAnalysisProvider(
        client=_FakeClient(
            _make_rows(100),
            raise_on={"fetch_daily_basic": TushareTokenMissingError("no token")},
        )
    ))
    assert res["error"] == "token_missing"
    assert res["symbol"] == "600519"


def main() -> None:
    print("=== tests/test_valuation.py 估值历史分位分析 deterministic 测试 ===")
    tests = [
        ("A.1 分位语义/边界/horizon", test_percentile_semantics),
        ("A.2 空序列", test_empty_rows),
        ("A.3 缺失/NaN/非正数剔除", test_exclusion_of_missing_nan_nonpositive),
        ("A.4 pe_ttm 优先于 pe", test_pe_ttm_preferred_over_pe),
        ("A.5 current 为 None", test_current_none),
        ("A.6 current 非正数", test_current_nonpositive),
        ("A.7 min_samples 边界", test_min_samples_boundary),
        ("B.1 Provider 正常结构", test_provider_normal_structure),
        ("B.2 代码标准化", test_provider_symbol_normalization),
        ("B.3 invalid_symbol", test_provider_invalid_symbol),
        ("B.4 token_missing", test_provider_token_missing),
        ("B.5 permission_denied", test_provider_permission_denied),
        ("B.6 rate_limited", test_provider_rate_limited),
        ("B.7 tushare_error", test_provider_tushare_error),
        ("B.8 no_data", test_provider_no_data),
        ("B.9 股票简称失败只记录", test_provider_stock_info_error_recorded_only),
        ("B.10 样本不足不可靠", test_provider_low_samples_not_reliable),
        ("B.11 当前估值非正如实报告", test_provider_current_nonpositive_reported),
        ("C.1 Tool Schema 结构", test_tool_schema_shape),
        ("C.2 工具函数正常路径", test_tool_function_with_provider),
        ("C.3 工具函数 token_missing 兜底", test_tool_function_token_missing_fallback),
    ]
    for name, fn in tests:
        _run(name, fn)
    total = len(tests)
    passed = total - len(_FAILURES)
    print(f"\n结果：{passed}/{total} 通过")
    if _FAILURES:
        print("失败明细：")
        for item in _FAILURES:
            print(f"  - {item}")
        sys.exit(1)
    print("全部通过。")


if __name__ == "__main__":
    main()
