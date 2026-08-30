"""A 股新闻处理基础设施单元测试（第六阶段）。

测试分为两部分，严格分离（方案 2 第十二条）：
- MOCK TEST：使用 tests/fixtures/news_mock.py 的模拟数据测试纯逻辑，
  不访问网络、不需要 TUSHARE_TOKEN；
- LIVE API TEST：真实访问 Tushare news 接口，验证其当前无权限状态
  （NEWS_API_PERMISSION_REQUIRED），默认跳过，加 --live 或 NEWS_LIVE_TEST=1 才运行。

运行方式（项目根目录执行）：
    .venv\\Scripts\\python.exe tests/test_news.py
    .venv\\Scripts\\python.exe tests/test_news.py --live   # 额外跑真实接口检测

重要：Mock 测试通过（PASS）绝不代表 Tushare news API 可用。
"""

import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

# 确保能导入项目根目录下的 app 包与 tests/fixtures 包
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_TESTS_DIR = _PROJECT_ROOT / "tests"
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_TESTS_DIR))

# Windows Git Bash 控制台中文输出需要显式使用 UTF-8
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.news.processing import (
    build_news_result,
    build_stock_keywords,
    clean_news_text,
    deduplicate_news,
    filter_relevant_news,
    make_summary,
    normalize_a_share_symbol,
    utc_now_iso,
    validate_news_timeline,
)
from app.news.providers import (
    NEWS_API_PERMISSION_REQUIRED,
    AkshareNewsProvider,
    MockNewsProvider,
    TushareNewsProvider,
    clamp_news_limit,
)
from app.tools.news_tool import NEWS_TOOL_SCHEMA, get_stock_news
from fixtures import news_mock as fx

_FAILURES: List[str] = []
_LIVE_FAILURES: List[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    """记录一条断言结果（MOCK TEST）。"""
    status = "PASS" if condition else "FAIL"
    suffix = f"  [{detail}]" if detail and not condition else ""
    print(f"  [{status}] {name}{suffix}")
    if not condition:
        _FAILURES.append(name)


def check_live(name: str, condition: bool, detail: str = "") -> None:
    """记录一条断言结果（LIVE API TEST）。"""
    status = "PASS" if condition else "FAIL"
    suffix = f"  [{detail}]" if detail and not condition else ""
    print(f"  [{status}] {name}{suffix}")
    if not condition:
        _LIVE_FAILURES.append(name)


# ---------------------------------------------------------------------------
# 1. A 股代码标准化
# ---------------------------------------------------------------------------
def test_normalize_symbol() -> None:
    print("测试 1：normalize_a_share_symbol 代码标准化")
    cases = [
        ("600519", "600519.SH"),
        ("600519.SH", "600519.SH"),
        ("600519.sh", "600519.SH"),
        ("601318", "601318.SH"),
        ("603259", "603259.SH"),
        ("605117", "605117.SH"),
        ("688981", "688981.SH"),
        ("689009", "689009.SH"),
        ("000001", "000001.SZ"),
        ("000001.SZ", "000001.SZ"),
        ("001979", "001979.SZ"),
        ("002594", "002594.SZ"),
        ("003816", "003816.SZ"),
        ("300750", "300750.SZ"),
        ("301236", "301236.SZ"),
    ]
    for raw, expected in cases:
        check(f"normalize({raw!r}) == {expected}", normalize_a_share_symbol(raw) == expected)

    invalid = ["", "  ", None, 600519, "12345", "ABCDEF", "600519.SH.SZ",
               "830799", "920002", "000001.SH", "688981.SZ"]
    for bad in invalid:
        try:
            normalize_a_share_symbol(bad)  # type: ignore[arg-type]
            check(f"非法代码 {bad!r} 应抛 ValueError", False)
        except ValueError:
            check(f"非法代码 {bad!r} 应抛 ValueError", True)


# ---------------------------------------------------------------------------
# 2. 非法 symbol 在工具层返回 invalid_symbol
# ---------------------------------------------------------------------------
def test_invalid_symbol_tool() -> None:
    print("测试 2：非法 symbol 返回 invalid_symbol")
    provider = MockNewsProvider()
    for bad in ["", "12345", "830799", "abc"]:
        result = get_stock_news(bad, provider=provider)
        check(
            f"get_stock_news({bad!r}) error == invalid_symbol",
            result.get("error") == "invalid_symbol",
            str(result),
        )


# ---------------------------------------------------------------------------
# 3. 文本清洗
# ---------------------------------------------------------------------------
def test_clean_news_text() -> None:
    print("测试 3：clean_news_text 文本清洗")
    html_text = "<p>据交易所数据，<b>贵州茅台</b>获净买入。</p>"
    cleaned = clean_news_text(html_text)
    check("HTML 标签被移除", "<" not in cleaned and ">" not in cleaned)
    check("文本内容保留", "贵州茅台" in cleaned and "获净买入" in cleaned)

    ws = "贵州茅台\n\n发布公告\t\t内容   详情"
    check("多余空白折叠为单空格", clean_news_text(ws) == "贵州茅台 发布公告 内容 详情")

    entity = "当日成交&nbsp;额显著放大"
    cleaned_entity = clean_news_text(entity)
    check("&nbsp; 实体被清除", "&nbsp;" not in cleaned_entity)
    check("实体文本合并", "当日成交额显著放大" in cleaned_entity.replace(" ", ""))

    check("None -> 空串", clean_news_text(None) == "")
    check("空串 -> 空串", clean_news_text("") == "")
    check("非字符串 -> 空串", clean_news_text(123) == "")


# ---------------------------------------------------------------------------
# 4. 摘要截断
# ---------------------------------------------------------------------------
def test_make_summary() -> None:
    print("测试 4：make_summary 摘要截断")
    long_text = fx.LONG_CONTENT_ROWS[0]["content"]
    summary = make_summary(long_text)
    check("超长正文截断到 150 字符", len(summary) == 153, f"len={len(summary)}")
    check("截断时以 ... 结尾", summary.endswith("..."))
    check("截断保留原文前 150 字符", summary[:-3] == long_text[:150])

    short = "贵州茅台发布公告"
    check("短文本不截断", make_summary(short) == short)
    check("短文本无省略号", not make_summary(short).endswith("..."))

    check("None 摘要为空", make_summary(None) == "")
    check("空摘要为空", make_summary("") == "")

    exactly_150 = "文" * 150
    check("恰好 150 字符不截断", make_summary(exactly_150) == exactly_150)
    check("恰好 150 无省略号", not make_summary(exactly_150).endswith("..."))


# ---------------------------------------------------------------------------
# 5. 空值处理
# ---------------------------------------------------------------------------
def test_empty_values() -> None:
    print("测试 5：空值处理（空标题）")
    result = filter_relevant_news(fx.EMPTY_TITLE_ROWS, fx.maotai_keywords())
    # 空标题行：标题无命中，正文命中 -> medium（保留，标题清洗为空）
    check("空标题正文命中 -> 保留", len(result) == 2, f"len={len(result)}")
    check("空标题行 relevance == medium", all(r["relevance"] == "medium" for r in result))
    check("空标题清洗为空串", all(clean_news_text(r.get("title")) == "" for r in result))


# ---------------------------------------------------------------------------
# 6. 相关性过滤（标题优先、无关丢弃）
# ---------------------------------------------------------------------------
def test_filter_relevant() -> None:
    print("测试 6：filter_relevant_news 相关性过滤")
    keywords = fx.maotai_keywords()
    check("关键词含全称代码简称", "600519.SH" in keywords and "600519" in keywords
          and "贵州茅台" in keywords and "贵州茅台酒股份有限公司" in keywords)

    rows = [*fx.HIGH_RELEVANCE_ROWS, *fx.WEAK_RELEVANCE_ROWS, *fx.IRRELEVANT_ROWS]
    result = filter_relevant_news(rows, keywords)
    kept = {r["title"] for r in result}
    check("无关新闻被丢弃", "苹果公司发布新款产品" not in kept and "宏观经济数据公布" not in kept)
    check("保留 3 条", len(result) == 3, f"len={len(result)}")
    check("保留项相关性均为 high/medium", all(
        r["relevance"] in ("high", "medium") for r in result))
    by_title = {r["title"]: r["relevance"] for r in result}
    check("标题命中 -> high", by_title.get("贵州茅台发布2026年半年度报告 营收保持增长") == "high")
    check("标题数字代码命中 -> high", by_title.get("600519 获多家机构上调目标价") == "high")
    check("仅正文命中 -> medium", by_title.get("白酒板块午后走高") == "medium")


# ---------------------------------------------------------------------------
# 7. 标题命中优先于正文
# ---------------------------------------------------------------------------
def test_title_priority() -> None:
    print("测试 7：标题命中优先于正文")
    rows = [
        {
            "title": "贵州茅台股价异动",
            "content": "贵州茅台、五粮液等集体走强。",  # 标题与正文都命中
            "published_at": "2026-08-18 10:00:00",
            "source": "mock",
        }
    ]
    result = filter_relevant_news(rows, fx.maotai_keywords())
    check("标题命中优先 -> high", len(result) == 1 and result[0]["relevance"] == "high")


# ---------------------------------------------------------------------------
# 8. 去重
# ---------------------------------------------------------------------------
def test_deduplicate() -> None:
    print("测试 8：deduplicate_news 去重")
    unique, removed = deduplicate_news(fx.DUPLICATE_ROWS)
    check("完全重复删除 1 条", removed == 1 and len(unique) == 1, f"removed={removed}")
    check("保留首条内容", unique[0]["content"] == "内容 A。")

    unique2, removed2 = deduplicate_news(fx.DIFFERENT_TIME_ROWS)
    check("同标题不同时间不去重", removed2 == 0 and len(unique2) == 2, f"removed={removed2}")

    unique3, removed3 = deduplicate_news([])
    check("空列表去重", len(unique3) == 0 and removed3 == 0)


# ---------------------------------------------------------------------------
# 9. published_at 校验
# ---------------------------------------------------------------------------
def test_validate_timeline() -> None:
    print("测试 9：validate_news_timeline 时间校验")
    fetched_at = "2026-08-20T08:00:00.000000+00:00"
    rows, issues = validate_news_timeline(fx.MISSING_PUBLISHED_AT_ROWS, fetched_at)
    check("缺发布时间被如实报告", len(issues) == 2, f"issues={issues}")
    check("缺发布时间保留为 None", all(r["published_at"] is None for r in rows))
    check("绝不使用 fetched_at 冒充", all(r.get("published_at") != fetched_at for r in rows))

    kept = fx.HIGH_RELEVANCE_ROWS
    rows2, issues2 = validate_news_timeline(kept, fetched_at)
    check("正常时间原样保留", rows2[0]["published_at"] == "2026-08-19 09:30:00")
    check("正常时间无 issue", len(issues2) == 0)


# ---------------------------------------------------------------------------
# 10. fetched_at 生成
# ---------------------------------------------------------------------------
def test_fetched_at_generation() -> None:
    print("测试 10：utc_now_iso fetched_at 生成")
    fetched = utc_now_iso()
    check("fetched_at 为 ISO 8601 且 UTC", fetched.endswith("+00:00"), fetched)
    try:
        datetime.fromisoformat(fetched)
        check("fetched_at 可被解析", True)
    except ValueError:
        check("fetched_at 可被解析", False)


# ---------------------------------------------------------------------------
# 11. 空新闻
# ---------------------------------------------------------------------------
def test_empty_news() -> None:
    print("测试 11：空新闻")
    check("过滤空列表", filter_relevant_news([], fx.maotai_keywords()) == [])
    result = build_news_result("600519.SH", [], utc_now_iso())
    check("空新闻 news_count == 0", result["news_count"] == 0)
    check("空新闻 news == []", result["news"] == [])


# ---------------------------------------------------------------------------
# 12. 异常数据不崩溃
# ---------------------------------------------------------------------------
def test_abnormal_rows() -> None:
    print("测试 12：异常数据（None 字段、垃圾发布时间）")
    rows = [
        {"title": None, "content": None, "published_at": None, "source": None},
        {"title": "贵州茅台公告", "content": "内容。", "published_at": "这不是时间", "source": "mock"},
        {"title": 123, "content": 456, "published_at": 999, "source": "mock"},
    ]
    cleaned = [clean_news_text(r.get("title")) for r in rows]
    check("异常标题清洗不崩溃", all(isinstance(c, str) for c in cleaned))
    keywords = fx.maotai_keywords()
    relevant = filter_relevant_news(rows, keywords)
    check("异常数据过滤不崩溃", isinstance(relevant, list))
    unique, removed = deduplicate_news(rows)
    check("异常数据去重不崩溃", isinstance(unique, list))
    valid, issues = validate_news_timeline(rows, utc_now_iso())
    check("异常数据时间校验不崩溃", len(issues) >= 0)
    result = build_news_result("600519.SH", valid, utc_now_iso())
    check("异常数据组装不崩溃", "news" in result)


# ---------------------------------------------------------------------------
# 13. 标准 JSON 结构
# ---------------------------------------------------------------------------
def test_build_news_result() -> None:
    print("测试 13：build_news_result 标准 JSON 结构")
    keywords = fx.maotai_keywords()
    rows = filter_relevant_news(
        [*fx.HIGH_RELEVANCE_ROWS, *fx.LONG_CONTENT_ROWS, *fx.HTML_CONTENT_ROWS],
        keywords,
    )
    unique, _ = deduplicate_news(rows)
    fetched_at = utc_now_iso()
    valid, issues = validate_news_timeline(unique, fetched_at)
    result = build_news_result("600519.SH", valid, fetched_at, source_status=[
        {"source": "sina", "status": "permission_denied"},
    ])

    check("顶层 symbol", result["symbol"] == "600519.SH")
    check("asset_type == stock", result["asset_type"] == "stock")
    check("market == A-share", result["market"] == "A-share")
    check("data_source == Tushare", result["data_source"] == "Tushare")
    check("顶层 fetched_at", result["fetched_at"] == fetched_at)
    check("source_status 保留", isinstance(result.get("source_status"), list))
    check("news 条数正确", result["news_count"] == len(result["news"]))

    first = result["news"][0]
    for field in ("title", "summary", "published_at", "source", "relevance"):
        check(f"新闻条目含 {field}", field in first)
    long_item = next(n for n in result["news"] if n["title"].startswith("贵州茅台2026年半年度报告摘要"))
    check("超长正文摘要被截断", len(long_item["summary"]) == 153 and long_item["summary"].endswith("..."))
    html_item = next(n for n in result["news"] if "净买入" in n["title"])
    check("HTML 正文清洗后无标签", "<" not in html_item["summary"] and "&nbsp;" not in html_item["summary"])
    check("相关性来自过滤结果", first["relevance"] in ("high", "medium"))


# ---------------------------------------------------------------------------
# 14. MockNewsProvider 全流程
# ---------------------------------------------------------------------------
def test_mock_provider() -> None:
    print("测试 14：MockNewsProvider 全流程（明确标注模拟）")
    provider = MockNewsProvider()
    result = provider.get_news("600519", limit=3)
    check("Mock 顶层 data_source == Mock", result["data_source"] == "Mock")
    check("Mock 带 notice 说明", "notice" in result and "模拟数据" in result["notice"])
    check("Mock symbol 标准化", result["symbol"] == "600519.SH")
    check("Mock 新闻数在 1-5 内", 1 <= result["news_count"] <= 5)
    check("Mock 无 error 字段", "error" not in result)
    check("Mock 每条 source == mock", all(n["source"] == "mock" for n in result["news"]))
    check("Mock 相关性非空", all(n.get("relevance") in ("high", "medium") for n in result["news"]))
    check("Mock 时间字段齐全", all(n.get("published_at") for n in result["news"]))

    for bad in ["", "12345"]:
        check(f"Mock 非法代码返回 invalid_symbol: {bad!r}",
              provider.get_news(bad).get("error") == "invalid_symbol")


# ---------------------------------------------------------------------------
# 15. limit 钳制
# ---------------------------------------------------------------------------
def test_limit_clamp() -> None:
    print("测试 15：limit 钳制到 1-5")
    check("limit=10 -> 5", clamp_news_limit(10) == 5)
    check("limit=0 -> 1", clamp_news_limit(0) == 1)
    check("limit=-3 -> 1", clamp_news_limit(-3) == 1)
    check("limit=3 -> 3", clamp_news_limit(3) == 3)
    check("limit=abc -> 默认 3", clamp_news_limit("abc") == 3)
    check("limit=None -> 默认 3", clamp_news_limit(None) == 3)


# ---------------------------------------------------------------------------
# 16. NEWS_TOOL_SCHEMA（设计稿结构）
# ---------------------------------------------------------------------------
def test_news_tool_schema() -> None:
    print("测试 16：NEWS_TOOL_SCHEMA 结构（已接入 orchestrator）")
    check("schema 类型 function", NEWS_TOOL_SCHEMA["type"] == "function")
    fn = NEWS_TOOL_SCHEMA["function"]
    check("工具名 get_stock_news", fn["name"] == "get_stock_news")
    check("schema 含描述", isinstance(fn.get("description"), str) and len(fn["description"]) > 0)
    props = fn["parameters"]["properties"]
    check("必填参数含 symbol", fn["parameters"]["required"] == ["symbol"])
    check("symbol 参数存在", props["symbol"]["type"] == "string")
    check("limit 参数范围 1-5", props["limit"]["minimum"] == 1 and props["limit"]["maximum"] == 5)
    check("limit 默认 3", props["limit"]["default"] == 3)


# ---------------------------------------------------------------------------
# 17. AkshareNewsProvider（桩 _fetch_raw_rows，无网络）
# ---------------------------------------------------------------------------
def test_akshare_provider_stubbed() -> None:
    print("测试 17：AkshareNewsProvider 全流程（桩 _fetch_raw_rows，无网络）")
    provider = AkshareNewsProvider()
    raw_rows = [
        {"title": "贵州茅台发布2026年半年度报告", "content": "公司实现营收保持增长。",
         "published_at": "2026-08-20 09:30:00", "source": "东方财富"},
        {"title": "600519 获多家机构上调目标价", "content": "多家机构看好公司长期价值。",
         "published_at": "2026-08-19 15:00:00", "source": "财联社"},
        {"title": "贵州茅台发布2026年半年度报告", "content": "公司实现营收保持增长。",
         "published_at": "2026-08-20 09:30:00", "source": "东方财富"},
    ]
    with mock.patch.object(provider, "_fetch_raw_rows", return_value=raw_rows):
        result = provider.get_news("600519", limit=3)
    check("Akshare 顶层 data_source == Akshare", result["data_source"] == "Akshare")
    check("Akshare symbol 标准化", result["symbol"] == "600519.SH")
    check("Akshare 无 error 字段", "error" not in result, str(result))
    check("Akshare 去重后 2 条", result["news_count"] == 2, f"count={result['news_count']}")
    check("Akshare 每条字段齐全",
          all("title" in n and "summary" in n and n.get("published_at") and n.get("source")
              for n in result["news"]))

    many = [{"title": f"新闻{i}", "content": "内容", "published_at": "2026-08-20 09:30:00",
             "source": "东方财富"} for i in range(5)]
    with mock.patch.object(provider, "_fetch_raw_rows", return_value=many):
        result2 = provider.get_news("600519", limit=2)
    check("Akshare limit=2 截断", result2["news_count"] == 2, f"count={result2['news_count']}")

    with mock.patch.object(provider, "_fetch_raw_rows",
                           side_effect=RuntimeError("timeout")):
        result3 = provider.get_news("600519")
    check("Akshare 网络异常返回 akshare_news_failed",
          result3.get("error") == "akshare_news_failed", str(result3))
    check("Akshare 错误 JSON 含 detail", bool(result3.get("detail")), str(result3))

    with mock.patch.object(provider, "_fetch_raw_rows", return_value=[]):
        result4 = provider.get_news("600519")
    check("Akshare 空数据返回 akshare_news_failed",
          result4.get("error") == "akshare_news_failed", str(result4))

    check("Akshare 非法代码返回 invalid_symbol",
          provider.get_news("12345").get("error") == "invalid_symbol")


# ---------------------------------------------------------------------------
# 18. get_stock_news 标准字段契约（title/summary/publish_date/source）
# ---------------------------------------------------------------------------
def test_news_tool_standard_format() -> None:
    print("测试 18：get_stock_news 标准字段契约（title/summary/publish_date/source）")
    provider = AkshareNewsProvider()
    raw_rows = [
        {"title": "<b>贵州茅台</b>发布2026年半年度报告",
         "content": "公司上半年实现营业收入" + "额" * 200 + "亿元，同比增长。",
         "published_at": "2026-08-20 09:30:00", "source": "东方财富"},
        {"title": "600519 获多家机构上调目标价", "content": "多家机构上调目标价。",
         "published_at": "2026-08-19 15:00:00", "source": "财联社"},
    ]
    with mock.patch.object(provider, "_fetch_raw_rows", return_value=raw_rows):
        result = get_stock_news("600519", limit=3, provider=provider)
    check("工具无 error", "error" not in result, str(result))
    check("news_count == 2", result["news_count"] == 2)
    check("顶层字段完整",
          result["symbol"] == "600519.SH" and result["data_source"] == "Akshare"
          and result.get("asset_type") == "stock" and result.get("market") == "A-share"
          and bool(result.get("fetched_at")))
    for item in result["news"]:
        check("条目含 title（清洗后非空）",
              isinstance(item.get("title"), str) and len(item["title"]) > 0)
        check("条目含 summary 且 ≤100 字符",
              isinstance(item.get("summary"), str) and len(item["summary"]) <= 100,
              f"len={len(item.get('summary', ''))}")
        check("条目含 publish_date", bool(item.get("publish_date")), str(item))
        check("条目含 source", bool(item.get("source")), str(item))
        check("条目无 published_at 键", "published_at" not in item)
    first = result["news"][0]
    check("HTML 标题被清洗", "<" not in first["title"] and ">" not in first["title"])
    check("超长摘要截断到 100 内且带省略号",
          len(first["summary"]) <= 100 and first["summary"].endswith("..."),
          f"len={len(first['summary'])}")


# ---------------------------------------------------------------------------
# 19. get_stock_news 错误路径（失败透传 Error JSON，供 Agent 优雅降级）
# ---------------------------------------------------------------------------
def test_news_tool_error_paths() -> None:
    print("测试 19：get_stock_news 错误路径（失败透传 Error JSON）")
    # 非法代码：默认 Provider 也不触碰网络即返回
    result = get_stock_news("830799")
    check("非法代码返回 invalid_symbol（默认 Provider，无网络）",
          result.get("error") == "invalid_symbol", str(result))

    provider = AkshareNewsProvider()
    with mock.patch.object(provider, "_fetch_raw_rows",
                           side_effect=RuntimeError("rate limited")):
        result2 = get_stock_news("600519", provider=provider)
    check("接口异常透传 akshare_news_failed",
          result2.get("error") == "akshare_news_failed", str(result2))
    check("错误 JSON 含 symbol 与 detail", bool(result2.get("symbol")) and bool(result2.get("detail")))


# ---------------------------------------------------------------------------
# LIVE API TEST（真实访问 Tushare，验证当前无权限状态）
# ---------------------------------------------------------------------------
def run_live_tests() -> None:
    print("=" * 50)
    print("LIVE API TEST（真实访问 Tushare news 接口）")
    print("=" * 50)
    print()
    from dotenv import load_dotenv

    load_dotenv(_PROJECT_ROOT / ".env")

    from app.data.tushare_client import (
        TushareClient,
        TushareNewsPermissionError,
        TushareTokenMissingError,
    )

    token = os.getenv("TUSHARE_TOKEN")
    check_live("LIVE 找到 TUSHARE_TOKEN 配置", bool(token))
    if not token:
        print()
        return

    try:
        client = TushareClient()
        check_live("LIVE 创建 TushareClient 成功", True)
    except TushareTokenMissingError as exc:
        check_live("LIVE 创建 TushareClient 成功", False, str(exc))
        print()
        return
    except Exception as exc:
        check_live("LIVE 创建 TushareClient 成功", False, str(exc))
        print()
        return

    start, end = client.news_window(hours=24)
    try:
        client.fetch_news(start, end, "sina")
        check_live("LIVE news 接口无权限（应抛权限错误）", False, "接口竟然返回成功？")
    except TushareNewsPermissionError:
        check_live("LIVE news 接口无权限（应抛权限错误）", True)
    except Exception as exc:
        check_live("LIVE news 接口无权限（应抛权限错误）", False, str(exc))

    provider = TushareNewsProvider(client=client)
    result = provider.get_news("600519", limit=3)
    check_live(
        "LIVE TushareNewsProvider 返回 NEWS_API_PERMISSION_REQUIRED",
        result.get("error") == NEWS_API_PERMISSION_REQUIRED,
        str(result)[:300],
    )

    result2 = get_stock_news("600519", 3, provider=provider)
    check_live(
        "LIVE get_stock_news 返回 NEWS_API_PERMISSION_REQUIRED",
        result2.get("error") == NEWS_API_PERMISSION_REQUIRED,
        str(result2)[:300],
    )
    print()


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> None:
    print("=" * 50)
    print("A-Share News Processing Unit Tests (Phase 6)")
    print("=" * 50)
    print()
    print("[MOCK TEST] 使用 tests/fixtures/news_mock.py 的模拟数据，不访问网络")
    print()

    test_normalize_symbol()
    print()
    test_invalid_symbol_tool()
    print()
    test_clean_news_text()
    print()
    test_make_summary()
    print()
    test_empty_values()
    print()
    test_filter_relevant()
    print()
    test_title_priority()
    print()
    test_deduplicate()
    print()
    test_validate_timeline()
    print()
    test_fetched_at_generation()
    print()
    test_empty_news()
    print()
    test_abnormal_rows()
    print()
    test_build_news_result()
    print()
    test_mock_provider()
    print()
    test_limit_clamp()
    print()
    test_news_tool_schema()
    print()
    test_akshare_provider_stubbed()
    print()
    test_news_tool_standard_format()
    print()
    test_news_tool_error_paths()
    print()

    live_requested = "--live" in sys.argv or os.getenv("NEWS_LIVE_TEST") == "1"
    if live_requested:
        run_live_tests()
    else:
        print("[LIVE API TEST] 已跳过（加 --live 或 NEWS_LIVE_TEST=1 运行真实接口检测）")
        print()

    print("-" * 50)
    print(f"Mock 测试结果：{len(_FAILURES)} 项失败")
    if _FAILURES:
        print("失败项 ->", _FAILURES)
        sys.exit(1)
    print("Mock 新闻处理：PASS")
    print()
    if live_requested:
        print(f"LIVE API TEST 结果：{len(_LIVE_FAILURES)} 项失败")
        if not _LIVE_FAILURES:
            print("Tushare news LIVE API：FAIL / NO_PERMISSION（已按预期确认无权限）")
            print("说明：确认结果为【无权限】= 新闻接口当前不可用，这不是成功。")
        else:
            print("Tushare news LIVE API：检查异常 ->", _LIVE_FAILURES)
            sys.exit(1)
    else:
        print("Tushare news LIVE API：未运行（默认跳过，需 --live）")
    print()
    print("注意：Mock 新闻处理 PASS 绝不代表 Tushare news API 可用。")
    print("数据仅用于研究和分析，不构成投资建议。")
    sys.exit(0)


if __name__ == "__main__":
    main()
