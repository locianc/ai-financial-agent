"""Tushare 基本面接口权限探测脚本（第七阶段第一步）。

对当前 TUSHARE_TOKEN 探测以下 6 个接口的可用性并如实报告：
stock_basic / daily_basic / income / balancesheet / cashflow / fina_indicator

只报告接口是否可用，绝不打印 token，绝不用其他接口数据冒充本接口结果。
数据仅用于研究和分析，不构成投资建议。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv  # noqa: E402

from app.data.tushare_client import (  # noqa: E402
    TushareClient,
    TushareTokenMissingError,
)

load_dotenv()

# 各接口的探测参数：用最小数据量探测，避免浪费积分额度。
# stock_basic 有频率限制（约 1 次/小时），探测到 rate_limited 说明接口本身有权限。
PROBE_ARGS = {
    "stock_basic": {"exchange": "", "list_status": "L", "fields": "ts_code,symbol,name"},
    "daily_basic": {"ts_code": "600519.SH", "start_date": "20260801", "end_date": "20260820"},
    "income": {"ts_code": "600519.SH", "start_date": "20250101", "end_date": "20260630"},
    "balancesheet": {"ts_code": "600519.SH", "start_date": "20250101", "end_date": "20260630"},
    "cashflow": {"ts_code": "600519.SH", "start_date": "20250101", "end_date": "20260630"},
    "fina_indicator": {"ts_code": "600519.SH", "start_date": "20250101", "end_date": "20260630"},
}

_STATUS_LABEL = {
    "ok": "OK（有权限）",
    "permission_denied": "NO_PERMISSION（无权限，不得绕过）",
    "rate_limited": "RATE_LIMITED（有权限，频率超限）",
    "error": "ERROR（其他错误）",
}


def main() -> int:
    token = os.getenv("TUSHARE_TOKEN")
    if not token:
        print("未配置 TUSHARE_TOKEN，请在项目根目录 .env 中配置后重试。")
        return 2

    try:
        client = TushareClient()
    except TushareTokenMissingError as exc:
        print(f"TushareClient 初始化失败：{exc}")
        return 2

    print("Tushare 基本面接口权限探测（token 已配置，绝不打印内容）\n")
    print(f"{'接口':<14} {'状态':<42} 数据条数")
    print("-" * 70)

    summary = {"ok": [], "permission_denied": [], "rate_limited": [], "error": []}
    for api_name, kwargs in PROBE_ARGS.items():
        result = client.check_interface_permission(api_name, **kwargs)
        status = result["status"]
        summary[status].append(api_name)
        label = _STATUS_LABEL[status]
        count = result["count"]
        print(f"{api_name:<14} {label:<42} {count}")
        if status in ("permission_denied", "rate_limited", "error"):
            print(f"{'':<14} detail: {result['detail']}")

    print("-" * 70)
    print("\n汇总：")
    print(f"  可用（ok）          : {', '.join(summary['ok']) or '无'}")
    print(f"  无权限（denied）    : {', '.join(summary['permission_denied']) or '无'}")
    print(f"  频率超限（limited） : {', '.join(summary['rate_limited']) or '无'}")
    print(f"  其他错误（error）   : {', '.join(summary['error']) or '无'}")

    has_permission = bool(summary["ok"]) or bool(summary["rate_limited"])
    if has_permission:
        print("\n结论：存在可用的基本面接口，后续 LIVE 验证可用真实接口。")
    else:
        print("\n结论：所有基本面接口均无权限，LIVE 验证只能如实报告 NO_PERMISSION。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
