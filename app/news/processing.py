"""新闻处理纯逻辑（第六阶段基础设施，不依赖网络，使用 Mock 数据测试）。

本模块只做"文本/数据"层面的纯函数处理，不含任何网络请求：

1. normalize_a_share_symbol(): A 股代码标准化
2. clean_news_text() / make_summary(): 文本清洗与摘要截断
3. build_stock_keywords(): 由股票信息构造相关性过滤关键词
4. filter_relevant_news(): 关键词相关性过滤（标题优先于正文）
5. deduplicate_news(): 按标题 + 发布时间去重
6. validate_news_timeline(): 严格区分 published_at 与 fetched_at
7. build_news_result(): 标准 JSON 结构输出
8. utc_now_iso(): 获取数据时刻（UTC ISO 8601）

重要约束（方案 2）：
- 本阶段不得修改现有第四、五阶段的真实行情逻辑；
- 本模块不判定任何投资结论，只做客观文本处理；
- 所有数据仅用于研究和分析，不构成投资建议。
"""

from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# A 股代码标准化规则（Tushare ts_code 规则）
# ---------------------------------------------------------------------------
_SH_PREFIXES = ("600", "601", "603", "605", "688", "689")
_SZ_PREFIXES = ("000", "001", "002", "003", "300", "301")


def normalize_a_share_symbol(symbol: str) -> str:
    """把 A 股代码标准化为 Tushare ts_code 形式（如 600519.SH）。

    支持输入形式：600519、600519.SH、600519.sh、000001.SZ 等。

    规则（优先使用 Tushare stock_basic 既有 A 股代码规则）：
    - 600/601/603/605/688/689 开头 -> .SH（沪市，含科创板 688）
    - 000/001/002/003/300/301 开头 -> .SZ（深市，含创业板 300）
    - 其余（如 4/8/9 开头北交所等）暂不支持 -> 抛出 ValueError

    Args:
        symbol: 用户输入的 A 股代码。

    Returns:
        标准化后的 ts_code，例如 "600519.SH"。

    Raises:
        ValueError: 代码为空、格式非法或属于暂不支持的板块。
    """
    if not symbol or not isinstance(symbol, str):
        raise ValueError("股票代码为空或类型非法")
    raw = symbol.strip().upper()
    if not raw:
        raise ValueError("股票代码为空")

    # 拆分 "代码.交易所" 形式
    if "." in raw:
        parts = raw.split(".")
        if len(parts) != 2:
            raise ValueError(f"股票代码格式非法: {symbol!r}")
        code, exchange = parts
        if exchange not in ("SH", "SZ"):
            raise ValueError(f"交易所代码非法: {exchange!r}")
    else:
        code, exchange = raw, None

    if not code.isdigit() or len(code) != 6:
        raise ValueError(f"股票代码必须为 6 位数字: {symbol!r}")

    expected = _exchange_of(code)
    if exchange is not None and exchange != expected:
        raise ValueError(f"股票代码 {code} 属于 {expected}，与传入的 {exchange} 不一致")

    return f"{code}.{expected}"


def _exchange_of(code: str) -> str:
    """根据 6 位数字代码前缀判断所属交易所。"""
    if code.startswith(_SH_PREFIXES):
        return "SH"
    if code.startswith(_SZ_PREFIXES):
        return "SZ"
    raise ValueError(f"暂不支持的股票代码板块: {code}")


# ---------------------------------------------------------------------------
# 文本清洗与摘要
# ---------------------------------------------------------------------------
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_MAX_SUMMARY_LEN = 150
_ELLIPSIS = "..."


def clean_news_text(text: Optional[str]) -> str:
    """清洗新闻文本：去掉 HTML 标签、合并多余空白、处理空值。

    - None / 非字符串 -> 返回空字符串（不编造内容）；
    - 先去除 <...> HTML 标签，再解码 HTML 实体（&nbsp; 等）；
    - 连续空白（含换行与非断空格）折叠为单个空格；
    - 不改变任何事实性内容（不删词、不替换）。
    """
    if text is None:
        return ""
    if not isinstance(text, str):
        return ""
    # 先剥标签再反转义：避免 "&lt;" 被解码成 "<" 后误当成标签被剥掉
    cleaned = _TAG_RE.sub("", text)
    cleaned = html.unescape(cleaned)
    cleaned = _WS_RE.sub(" ", cleaned)
    return cleaned.strip()


def make_summary(text: Optional[str], max_len: int = _MAX_SUMMARY_LEN) -> str:
    """生成新闻摘要：先清洗，再截断到 max_len 字符。

    "..." 仅在真正发生截断时追加（截断后长度 = max_len + 3），
    未截断时绝不出现在文本末尾。内容保持与原文一致，不改动事实。
    """
    cleaned = clean_news_text(text)
    if len(cleaned) <= max_len:
        return cleaned
    return cleaned[:max_len] + _ELLIPSIS


# ---------------------------------------------------------------------------
# 关键词与相关性过滤
# ---------------------------------------------------------------------------
def build_stock_keywords(
    ts_code: Optional[str] = None,
    short_name: Optional[str] = None,
    full_name: Optional[str] = None,
) -> List[str]:
    """由股票信息构造相关性过滤关键词列表。

    关键词来源：ts_code（如 600519.SH）、纯数字代码（600519）、
    股票简称（贵州茅台）、股票全称（贵州茅台酒股份有限公司）。
    空值自动忽略。返回列表中的关键词互不重复。
    """
    keywords: List[str] = []
    for kw in (ts_code, short_name, full_name):
        if kw and isinstance(kw, str) and kw.strip():
            kw = kw.strip()
            if kw not in keywords:
                keywords.append(kw)
    # 纯数字代码：由 ts_code 派生
    if ts_code and isinstance(ts_code, str) and "." in ts_code:
        pure = ts_code.split(".")[0]
        if pure not in keywords:
            keywords.append(pure)
    return keywords


def filter_relevant_news(
    rows: List[Dict[str, Any]], keywords: List[str]
) -> List[Dict[str, Any]]:
    """按关键词过滤新闻。

    规则（标题优先）：
    - 标题包含任一关键词 -> relevance = "high"，保留；
    - 标题未命中但正文包含任一关键词 -> relevance = "medium"，保留；
    - 标题与正文均未命中 -> 丢弃。

    本函数只做客观匹配，不输出任何投资判断。匹配前对标题/正文做清洗
    （清洗只用于匹配，返回行中的原始字段保持不变，仅附加 relevance）。
    """
    if not keywords:
        return []
    results: List[Dict[str, Any]] = []
    for row in rows:
        title = clean_news_text(row.get("title"))
        content = clean_news_text(row.get("content"))
        title_hit = any(kw in title for kw in keywords if kw)
        content_hit = any(kw in content for kw in keywords if kw)
        if title_hit:
            results.append({**row, "relevance": "high"})
        elif content_hit:
            results.append({**row, "relevance": "medium"})
    return results


def deduplicate_news(
    rows: List[Dict[str, Any]]
) -> Tuple[List[Dict[str, Any]], int]:
    """按 (标题, 发布时间) 去重，保留首次出现。

    标题先清洗后作为去重键。返回 (去重后列表, 被删除条数)。
    """
    seen = set()
    unique: List[Dict[str, Any]] = []
    removed = 0
    for row in rows:
        title = clean_news_text(row.get("title"))
        published_at = row.get("published_at")
        key = (title, published_at)
        if key in seen:
            removed += 1
            continue
        seen.add(key)
        unique.append(row)
    return unique, removed


# ---------------------------------------------------------------------------
# 时间线校验
# ---------------------------------------------------------------------------
def validate_news_timeline(
    rows: List[Dict[str, Any]], fetched_at: str
) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    """校验新闻时间线，严格区分 published_at 与 fetched_at。

    - published_at 是新闻原文发布时间，原样保留（缺失则保留 None，绝不补写）；
    - fetched_at 是 Python 获取数据时刻，只出现在顶层，绝不写入新闻条目；
    - 发现的异常以 issues 形式如实报告，不静默修复。

    Returns:
        (校验后的行列表, issues 列表)。
    """
    issues: List[Dict[str, str]] = []
    validated: List[Dict[str, Any]] = []
    for row in rows:
        published_at = row.get("published_at")
        title = clean_news_text(row.get("title"))
        if not published_at:
            issues.append(
                {"title": title, "issue": "missing_published_at",
                 "detail": "保留为空，未用 fetched_at 冒充"}
            )
            validated.append({**row, "published_at": None})
        else:
            if published_at == fetched_at:
                issues.append(
                    {"title": title, "issue": "published_at_equals_fetched_at",
                     "detail": "发布时间与获取时刻相同，需要人工核实"}
                )
            validated.append(dict(row))
    return validated, issues


# ---------------------------------------------------------------------------
# 标准 JSON 结构与工具函数
# ---------------------------------------------------------------------------
def utc_now_iso() -> str:
    """当前 UTC 时刻，ISO 8601 格式（如 2026-08-20T08:00:00.123456+00:00）。"""
    return datetime.now(timezone.utc).isoformat()


def build_news_result(
    symbol: str,
    news_rows: List[Dict[str, Any]],
    fetched_at: str,
    data_source: str = "Tushare",
    source_status: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """把处理后的新闻行组装成标准 JSON 结构。

    结构：
        {
          "symbol": "600519.SH",
          "asset_type": "stock",
          "market": "A-share",
          "data_source": "Tushare",
          "fetched_at": "2026-08-20T...",
          "news_count": 3,
          "news": [
            {"title": "...", "summary": "...", "published_at": "...",
             "source": "sina", "relevance": "high"},
            ...
          ]
        }

    - summary 由 content 清洗并截断生成（不超过 150 字符）；
    - published_at 原样保留（缺失为 None），绝不使用 fetched_at；
    - relevance 仅当行内已有（来自 filter_relevant_news）时写入，不编造。
    """
    news_items: List[Dict[str, Any]] = []
    for row in news_rows:
        item: Dict[str, Any] = {
            "title": clean_news_text(row.get("title")),
            "summary": make_summary(row.get("content")),
            "published_at": row.get("published_at"),
            "source": row.get("source"),
        }
        if "relevance" in row:
            item["relevance"] = row["relevance"]
        news_items.append(item)

    result: Dict[str, Any] = {
        "symbol": symbol,
        "asset_type": "stock",
        "market": "A-share",
        "data_source": data_source,
        "fetched_at": fetched_at,
        "news_count": len(news_items),
        "news": news_items,
    }
    if source_status is not None:
        result["source_status"] = source_status
    return result
