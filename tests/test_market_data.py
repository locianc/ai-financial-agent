"""AKShare 真实市场数据验证脚本。

单独验证：
1. 美股实时行情接口 stock_us_spot_em
2. 国际期货实时行情接口 futures_global_spot_em
3. 统一 dict 转换（tools.market_data）

运行方式（项目根目录执行）：
    .venv\\Scripts\\python.exe tests/test_market_data.py
"""

import json
import sys
from pathlib import Path

# 确保能导入项目根目录下的 tools 包（本脚本位于 tests/ 子目录）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Windows Git Bash 控制台中文输出需要显式使用 UTF-8
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 本机网络适配必须在直接调用 AKShare 接口前生效（见 tools/network_adapter.py）
from tools import network_adapter  # noqa: F401

import akshare as ak


def test_us_stock_spot() -> None:
    """验证美股实时行情接口，并查找 NVDA。"""
    print("=" * 50)
    print("测试 1：美股实时行情 stock_us_spot_em")
    print("=" * 50)
    df = ak.stock_us_spot_em()
    print(f"返回行数：{len(df)}")
    print(f"列名：{list(df.columns)}")
    print()

    # 代码列形如 "105.NVDA"，按代码后缀精确匹配
    code_col = df["代码"].astype(str).str.upper()
    matched = df[code_col.str.endswith("NVDA")]
    if matched.empty:
        print("未找到 NVDA，前 20 个代码：")
        print(list(df["代码"].head(20)))
        return

    row = matched.iloc[0]
    print("NVDA 行情：")
    for field in ["代码", "名称", "最新价", "涨跌额", "涨跌幅", "开盘价", "最高价", "最低价", "昨收价", "成交量", "成交额"]:
        print(f"  {field}: {row[field]}")


def test_global_futures_spot() -> None:
    """验证国际期货实时行情接口。"""
    print()
    print("=" * 50)
    print("测试 2：国际期货实时行情 futures_global_spot_em")
    print("=" * 50)
    df = ak.futures_global_spot_em()
    print(f"返回行数：{len(df)}")
    print(f"列名：{list(df.columns)}")
    print("前 10 条：")
    print(df.head(10).to_string(index=False))


def test_dict_conversion() -> None:
    """验证 tools.market_data 的 dict 统一结构转换。"""
    print()
    print("=" * 50)
    print("测试 3：统一 dict 转换（tools.market_data）")
    print("=" * 50)
    from tools.market_data import get_global_futures_quote, get_us_stock_quote

    quote = get_us_stock_quote("NVDA")
    print("美股 NVDA：")
    print(json.dumps(quote, ensure_ascii=False, indent=2))

    futures = get_global_futures_quote("黄金")
    print("国际期货（黄金）：")
    print(json.dumps(futures, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    test_us_stock_spot()
    test_global_futures_spot()
    test_dict_conversion()
