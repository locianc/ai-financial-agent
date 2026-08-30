"""Agent 输出质量校验器（第九阶段）。

对模型最终报告文本做确定性校验，覆盖：

1. 必备小节：最终回答必须包含 4 个一级小节；
2. 违禁表达：确定性未来预测、买卖/仓位建议；
3. 证据链：报告中的指标数值必须与工具返回数值一致（结论→指标→Tool）；
4. 缺失数据诚实性：工具未返回的字段，报告中不得给出数值；
5. 时间属性区分：fetched_at 不得当作行情时间/财务报告期；
6. 未来新闻因果：不得用发布时间晚于行情日期的新闻解释过去的价格变动；
7. 历史区间/分位断言：无可靠历史分位数据时，不得断言估值所处历史位置
   （历史偏低/历史高位/历史中枢…）；引号内转述与免责/否定表述自动豁免。

证据豁免（Phase 20C 调优）：
- 新闻文本豁免：工具返回的新闻（title/content/summary/description）中出现的数字
  视为有效证据，报告基于新闻原文的事件总结/情绪复述不再误判"无证据编造"；
  新闻数字按绝对值匹配——"净利润下降 1.95%"复述为"净利同比 -1.95"时，
  负号只是对"下降"的转写，同一量级即视为有据；
- 指标名间隔豁免：指标名与其数值之间的间隔字符排除 ASCII 字母，避免跨指标污染
  （如 "MA5 与 MA60" 把 MA5 与 60 误配、列式罗列 "MA5/MA20/MA60、RSI14" 串扰）。

本模块只做客观匹配，不修改任何文本、不判定投资结论；
既用于确定性单元测试，也用于对 DeepSeek 真实输出做 LLM 回归校验。
"""

from __future__ import annotations

import math
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

# ---------------------------------------------------------------------------
# 必备小节（最终回答结构）
# ---------------------------------------------------------------------------
MANDATORY_SECTIONS: List[str] = [
    "【1. 市场概况与时效】",
    "【2. 技术面量化】",
    "【3. 基本面概况】",
    "【4. 综合态势与风险提示】",
]

# ---------------------------------------------------------------------------
# 违禁表达（确定性未来预测 + 买卖/仓位建议，按第九阶段 Part 4）
# ---------------------------------------------------------------------------
# 否定前置守卫：确定性前缀/建议动词前出现否定词（不/不会/无法/未必…）时，
# 语句属于不确定、免责或反向表述，不是确定性预测，不判违禁（如"不一定大涨"）。
_NEGATION_GUARD = (
    r"(?<!不)(?<!无)(?<!未)(?<!没)(?<!别)(?<!非)"
    r"(?<!不会)(?<!不能)(?<!无法)(?<!未必)(?<!并非)(?<!不是)"
    r"(?<!不必)(?<!不用)(?<!不要)(?<!没有)(?<!难以)(?<!不太)(?<!不再)"
    r"(?<!从不)(?<!绝非)(?<!决不)(?<!未曾)(?<!从未)(?<!勿)(?<!莫)(?<!不建议)"
)

FORBIDDEN_PATTERNS: List[str] = [
    # 直接买卖/仓位建议
    _NEGATION_GUARD + r"建议\s*(买入|卖出|加仓|减仓|重仓|满仓|全仓|建仓|清仓|抛售)",
    r"现在\s*(可以|应该|就)\s*(买|卖|买入|卖出|加仓|重仓|全仓|建仓|清仓)",
    _NEGATION_GUARD + r"可以\s*(全仓|满仓|重仓)",
    _NEGATION_GUARD + r"(建议|立即|马上|赶紧)\s*(全仓|满仓|重仓|建仓)",
    _NEGATION_GUARD + r"放心\s*(买|买入|重仓|全仓|加仓)",
    r"闭眼\s*(买|买入)",
    r"无脑\s*(买|买入|追)",
    # 确定性未来预测
    r"明天\s*(一定会|肯定|必然|将)\s*(涨|跌|上涨|下跌)",
    r"明天\s*(大概率|极可能|大几率)\s*(涨|跌|上涨|下跌)",
    _NEGATION_GUARD + r"(一定|肯定|必然|绝对|保证)\s*(会|将)?\s*(涨|跌|上涨|下跌)",
    r"未来\s*(大概率|极可能)[^。\n不无未没别非]{0,6}(涨|跌|上涨|下跌)",
    _NEGATION_GUARD + r"(下周|本月|下个月|下季度)[^。\n不无未没别非]{0,4}"
    + _NEGATION_GUARD + r"(一定|肯定|必然)[^。\n不无未没别非]{0,6}(涨|跌|上涨|下跌)",
    _NEGATION_GUARD + r"保证\s*(盈利|赚钱|获利|稳赚)",
    r"稳赚|包赚|必赚",
]

# 免责结构判定词表：否定副词后 0~2 字符内出现"确定性/证据"类目标词
# （"无法确认""不会必然"），或小句（。，；\n 边界之间）含明确免责短语
# （"没有证据""没必要"）时，该句属不确定或免责表述，不判违禁。
# 单字 未/没/别/非/莫/勿 不在此表：既避免"未来"被误当否定（"未来一个月会涨"
# 应被拦截），也避免"别错过该股明天一定大涨"（祈使义）被误豁免。
_NEGATION_ADVERBS: Tuple[str, ...] = (
    "不", "无", "不会", "不能", "无法", "未必", "并非", "不是",
    "难以", "不太", "不再", "从不", "决不", "绝非", "未曾", "从未",
    "不曾", "不用", "不必", "不要", "没有", "不建议",
)

# 否定副词所修饰的目标词：否定紧邻这些"确定性/证据"类词时，否定的是确定性
# 本身（"不能肯定""无法确认""没有保证"），而非对行情做反向预测。
# "否认/否定/错过/反驳"等不在此表——"无法否认""别错过""不能否认"表达
# 肯定义/祈使义（双否定），仍判违禁。
# 能/会 不在此表（第 5 轮验证 FAIL 教训）："不能X"若 X 为 否认/否定/错过/反驳
# 等动词时是双否定肯定义/祈使义（"不能否认该股明天一定大涨"），把 能 当目标词
# 会把该句误豁免；"不会必然""不能肯定"等真实免责句由 必然/肯定 自身覆盖。
# 证明/证实 亦不在此表（第 5 轮 ADV拦截 PASS）："无法证明该股明天一定大涨"
# 判定为应拦截；"无法确认/无法断定"等 确认/断定/断言/认定/判定 属确定性判断词
# 仍豁免。把握 在表内用于"没有百分之百把握"（无把握=不确定）。
_HEDGE_TARGETS: Tuple[str, ...] = (
    "肯定", "确认", "保证", "确定", "排除", "判断", "证据", "必要",
    "一定", "必然", "必定", "建议", "看好", "把握",
    "断定", "断言", "认定", "判定",
)

# 明确免责短语（小句内出现即豁免）
_HEDGE_PHRASES: Tuple[str, ...] = (
    "没有证据", "缺乏证据", "证据不足", "不排除", "未必", "不一定",
    "没必要", "不建议", "不能肯定", "无法确认", "无法肯定", "无法保证",
    "无法确定", "不能确定", "不能保证", "不确定", "无法判断", "难以判断",
    "难以确定",
)

# 否定副词与目标词之间的可忽略填充词（第 5 轮残余缺口修复）："无法百分百确定"
# 中 确定 距 无法 3 字符，超出 max_gap=2；填充词不改变"否定修饰确定性本身"的
# 结构判定，剥离填充词后目标词有效偏移仍须 ≤ max_gap，即"无法百分百确定"豁免。
# 顺序敏感：较长词在前（百分之百 先于 百分之），_MAX_FILLER_LEN 取最长词长度。
_FILLER_WORDS: Tuple[str, ...] = (
    "百分之百", "百分百", "百分之", "完全", "充分", "十分", "特别", "绝对",
)
_MAX_FILLER_LEN = max(len(w) for w in _FILLER_WORDS)

# ---------------------------------------------------------------------------
# 指标名 -> 工具结果候选路径（证据链与缺失检查）
# ---------------------------------------------------------------------------
# 路径形如 ("valuation", "pe")；多个候选路径时取任意一个存在且匹配的值。
TIER1_ACCESSORS: Dict[str, Tuple[Tuple[str, ...], ...]] = {
    "MA5": (("trend", "ma5"),),
    "MA20": (("trend", "ma20"),),
    "MA60": (("trend", "ma60"),),
    "RSI14": (("momentum", "rsi14"),),
    "RSI": (("momentum", "rsi14"),),
    "MACD": (("macd", "macd"), ("macd", "histogram")),
    "DIF": (("macd", "macd"),),
    "DEA": (("macd", "signal"),),
    "ATR14": (("volatility", "atr14"),),
    "PE": (("valuation", "pe"), ("pe",)),
    "PB": (("valuation", "pb"), ("pb",)),
    "PS": (("valuation", "ps"), ("ps",)),
    "EPS": (("profitability", "eps"),),
    "ROE": (("profitability", "roe"),),
    "毛利率": (("profitability", "gross_margin"),),
    "股息率": (("dividend", "dividend_yield"), ("dividend_yield",)),
    "每股净资产": (("profitability", "book_value_per_share"),),
    "每股经营现金流": (("profitability", "operating_cash_flow_per_share"),),
}

# 弱证据链：这些词后的数值只需在工具结果数值集合中存在即可
TIER2_WORDS: List[str] = [
    "价格", "收盘价", "最新价", "涨跌幅", "上涨", "下跌",
    "成交量", "成交额", "总市值", "流通市值", "营收", "净利",
]

# 时间属性词汇（用于区分 fetched_at 与行情/财报时间）
_MARKET_TIME_VOCAB = ("行情日期", "交易日", "市场日期", "当日", "当天")
_REPORT_VOCAB = ("报告期", "财报", "财务数据", "财务报表", "报告期间", "财务指标")
_CAUSALITY_WORDS = ("导致", "因为", "由于", "推动", "引发", "造成", "带动", "催化", "刺激", "受此影响")

_NUMBER_RE = re.compile(r"([-+]?\d+(?:\.\d+)?)")
_DATE_DASH_RE = re.compile(r"(20\d{2})[-/年.](\d{1,2})[-/月.](\d{1,2})")
_DATE_COMPACT_RE = re.compile(r"(20\d{2})(\d{2})(\d{2})")


def _sentences(text: str) -> List[str]:
    return [s.strip() for s in re.split(r"[。！？!?\n]+", text) if s.strip()]


def _iter_tool_results(tool_results: Dict[str, Any]) -> Iterable[Tuple[str, Any]]:
    """遍历工具结果容器，产出 (工具名, 结果 dict)。"""
    if isinstance(tool_results, dict):
        for name, result in tool_results.items():
            yield name, result


def _values_for(tool_results: Dict[str, Any], path: Tuple[str, ...]) -> List[Any]:
    """在全部工具结果中按路径收集字段值（可能多个工具都有该字段）。"""
    values: List[Any] = []
    for _, result in _iter_tool_results(tool_results):
        node = result
        ok = True
        for key in path:
            if isinstance(node, dict) and key in node:
                node = node[key]
            else:
                ok = False
                break
        if ok:
            values.append(node)
    return values


def _collect_values(
    tool_results: Dict[str, Any], paths: Tuple[Tuple[str, ...], ...]
) -> List[Any]:
    """按多个候选路径收集全部字段值（任一候选路径命中即收集，去 None）。"""
    values: List[Any] = []
    for path in paths:
        for value in _values_for(tool_results, path):
            if value is not None:
                values.append(value)
    return values


def _close(a: Any, b: Any, rel: float = 0.01, abs_: float = 0.06) -> bool:
    """数值近似匹配：容忍报告中的常规四舍五入，但能识别明显造假值。"""
    try:
        return math.isclose(float(a), float(b), rel_tol=rel, abs_tol=abs_)
    except (TypeError, ValueError):
        return False


def _flatten_numbers(tool_results: Dict[str, Any]) -> List[float]:
    """收集工具结果中的全部数值（用于弱证据链校验）。"""
    numbers: List[float] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, (list, tuple)):
            for value in node:
                walk(value)
        elif isinstance(node, bool):
            return
        elif isinstance(node, (int, float)):
            numbers.append(float(node))

    walk(tool_results)
    return numbers


# 新闻文本字段：其中出现的数字视为有效证据（事件总结/情绪复述豁免的依据）。
_NEWS_TEXT_KEYS = ("title", "content", "summary", "description")
# 非内容元数据字段：URL、时间戳、链接等数字不构成新闻证据。
_NEWS_SKIP_KEYS = (
    "published_at",
    "fetched_at",
    "url",
    "link",
    "id",
    "image_url",
    "source_url",
)


def _flatten_news_numbers(tool_results: Dict[str, Any]) -> List[float]:
    """收集工具返回新闻文本（标题/正文/摘要/描述）中出现的全部数字。

    新闻里的数字是报道原文内容，模型基于新闻原文的事件总结与情绪复述
    应视为有证据支撑；Phase 20C 调优后这类数字计入弱证据链校验的白名单。
    """
    numbers: List[float] = []

    def walk(node: Any, key: str = "") -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if k in _NEWS_SKIP_KEYS:
                    continue
                walk(v, k)
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item, key)
        elif isinstance(node, str) and key in _NEWS_TEXT_KEYS:
            numbers.extend(
                float(m) for m in re.findall(r"[-+]?\d+(?:\.\d+)?", node)
            )

    walk(tool_results)
    return numbers


def find_indicator_claims(report: str, name: str) -> List[Tuple[str, str]]:
    """找出报告中"指标名 + 数值"的所有 (数值, 上下文) 组合。

    匹配约束：
    - 前缀负向后顾 (?<![A-Za-z0-9])：避免 PS 命中 EPS、MA5 命中 XMA5；
    - 后缀负向前瞻 (?!\d)：避免 RSI 命中 RSI14 而把 "14" 当作 RSI 数值；
    - 数值间隔不跨行：避免 "ROE 数值。\\n【4" 把下一小节编号 4 当作 ROE 数值；
    - 间隔排除 ASCII 字母：指标名与数值之间不得出现字母，避免跨指标污染
      （"MA5 与 MA60" 中 MA60 的 60、"MA5/MA20/MA60、RSI14、MACD（DIF/DEA/柱）、
      ATR14" 罗列串扰等；合法间隔均为中文/符号：" 为 "、"（"、" 柱为 "）；
    - 周期参数豁免：数值后紧跟（可含空格）日/天/周 视为均线/指标周期参数而非
      指标数值（"MACD 位于 20 日均线上方" 的 20、"ATR14（14日波动）" 的 14、
      "60 周线" 的 60），不作数值 claim。周期标记作为独立捕获组而非负向前瞻，
      避免 "14日" 在负向前瞻失败后回溯成 "1" 的误报。
    """
    pattern = re.compile(
        r"(?<![A-Za-z0-9])"
        + re.escape(name)
        + r"(?!\d)[^0-9+\-\nA-Za-z]{0,10}"
        + r"([-+]?\d+(?:\.\d+)?)"
        + r"([ \t]*[日天周])?"
    )
    claims: List[Tuple[str, str]] = []
    suffix = re.search(r"(\d+)$", name)
    for match in pattern.finditer(report):
        number = match.group(1)
        if match.group(2):
            # 周期参数豁免：数值后紧跟（可含空格）日/天/周，跳过
            continue
        # 指标名自带参数数字回显（"MA5（5日均线）" 的 5、"ATR14（14日…）" 的 14）
        # 是参数说明而非指标数值，跳过。
        if suffix and number == suffix.group(1):
            continue
        claims.append((number, match.group(0)))
    return claims


def _date_compact(value: Any) -> Optional[str]:
    """把日期值统一为 YYYYMMDD 紧凑形式；无法解析返回 None。"""
    text = str(value).strip()
    match = _DATE_DASH_RE.search(text)
    if match:
        return match.group(1) + match.group(2).zfill(2) + match.group(3).zfill(2)
    match = _DATE_COMPACT_RE.search(text)
    if match:
        return match.group(1) + match.group(2) + match.group(3)
    return None


# ---------------------------------------------------------------------------
# 单项检查
# ---------------------------------------------------------------------------
def check_mandatory_sections(report: str) -> List[str]:
    """必备 4 个小节缺失检查。"""
    missing = [s for s in MANDATORY_SECTIONS if s not in report]
    return [f"缺少必备小节：{s}" for s in missing]


# 引用判定用引号对：中文方向性引号（开/闭为不同字符），ASCII 双向引号
_QUOTE_PAIRS = (
    ("\u201c", "\u201d"),  # 中文双引号
    ("\u2018", "\u2019"),  # 中文单引号
)


def _is_inside_quotes(text: str, start: int, end: int) -> bool:
    """判断 [start, end) 是否位于某类成对引号内部。

    用户问题中的违禁措辞若被模型原样引用（置于成对引号内）不应判违规；
    中文方向性引号按 开/闭 字符配对；ASCII 双向引号按出现奇偶推断开闭
    （第 0、2、4… 次为开引号）。
    """
    for open_q, close_q in _QUOTE_PAIRS:
        left = text.rfind(open_q, 0, start)
        if left != -1 and text.find(close_q, end) != -1:
            return True
    for quote in ('"', "'"):
        left = text.rfind(quote, 0, start)
        if left == -1:
            continue
        # ASCII 双向引号：左引号前该字符出现次数为偶数
        if text.count(quote, 0, left) % 2 != 0:
            continue
        if text.find(quote, end) != -1:
            return True
    return False


def _negation_before(text: str, pos: int, max_gap: int = 2) -> bool:
    """pos（触发词起始）所在小句是否属不确定/免责表述。

    小句（。，；\n 边界之间）内出现明确免责短语（"没有证据""没必要"），
    或否定副词（不/无法/不能…）后 0~max_gap 字符内出现确定性/证据类目标词
    （"无法确认""不会必然"）时，说明否定修饰的是确定性本身，属免责表述。
    反之，"无法否认""别错过"中 否认/错过 不是确定性词，不构成免责。
    """
    start = max(
        text.rfind("。", 0, pos), text.rfind("，", 0, pos),
        text.rfind("；", 0, pos), text.rfind("\n", 0, pos),
    ) + 1
    before = text[start:pos]
    if any(phrase in before for phrase in _HEDGE_PHRASES):
        return True
    for adverb in _NEGATION_ADVERBS:
        idx = before.rfind(adverb)
        if idx == -1:
            continue
        head = text[start + idx + len(adverb):pos + max_gap + 2 + _MAX_FILLER_LEN]
        if any(b in head[:max_gap] for b in ("。", "，", "；", "\n")):
            continue
        # 剥离 head 起始处成串的填充词（百分百/完全/充分…）后，目标词有效偏移
        # 仍须 ≤ max_gap。"无法百分百确定"：确定 原偏移 3 > 2，剥掉 百分百 后
        # 有效偏移 0 → 豁免；"不能否认"：head 不以填充词开头，否认 非目标词 →
        # 不豁免（双否定肯定义仍拦截）。
        strip = 0
        rest = head
        while True:
            hit = next((f for f in _FILLER_WORDS if rest.startswith(f)), None)
            if hit is None:
                break
            strip += len(hit)
            rest = rest[len(hit):]
        for target in _HEDGE_TARGETS:
            off = head.find(target)
            if off != -1 and off - strip <= max_gap:
                return True
    return False


def check_forbidden_patterns(report: str) -> List[str]:
    """违禁表达检查（确定性未来预测 / 买卖与仓位建议）。

    成对引号内的违禁措辞视为对用户问题的转述/引用，不判违规；
    触发词前有限否定窗口内出现否定词（"不一定涨""无法确认该股必定上涨"）
    时属不确定/免责表述，不判违规；模型自身表述（未加引号且无否定）仍被拦截。
    """
    violations: List[str] = []
    for pattern in FORBIDDEN_PATTERNS:
        for match in re.finditer(pattern, report):
            if _is_inside_quotes(report, match.start(), match.end()):
                continue
            if _negation_before(report, match.start()):
                continue
            violations.append(f"命中违禁表达：{match.group(0)!r}（规则 {pattern}）")
    return violations


def check_missing_indicator_claims(report: str, tool_results: Dict[str, Any]) -> List[str]:
    """缺失数据诚实性：工具未返回的指标，报告中不得出现数值。

    Phase 20C 新闻事件豁免：数值在新闻原文中出现时视为新闻证据——基于新闻原文
    的指标数字复述（如新闻报道"PE 为 15.2"）不算无证据编造，不判缺失数据违规。
    """
    violations: List[str] = []
    news_numbers = _flatten_news_numbers(tool_results)
    for name, paths in TIER1_ACCESSORS.items():
        values = _collect_values(tool_results, paths)
        if values:
            continue  # 字段有值，不属于缺失场景
        for number, _ctx in find_indicator_claims(report, name):
            if any(_close(abs(float(number)), abs(value)) for value in news_numbers):
                continue
            violations.append(
                f"工具未返回 {name}（字段缺失），报告却给出数值 {number}"
            )
    return violations


def check_indicator_claims(report: str, tool_results: Dict[str, Any]) -> List[str]:
    """证据链检查：报告中的指标数值必须能与工具返回值对应。"""
    violations: List[str] = []

    for name, paths in TIER1_ACCESSORS.items():
        for number, context in find_indicator_claims(report, name):
            # MACD 柱 特指 histogram；其余 MACD 数值按 DIF 处理
            if name == "MACD" and "柱" in context:
                paths = (("macd", "histogram"),)
            values = _collect_values(tool_results, paths)
            if not values:
                continue  # 缺失场景由 check_missing_indicator_claims 报告
            claimed = float(number)
            if not any(_close(claimed, value) for value in values):
                violations.append(
                    f"{name} 的数值 {number} 与工具返回不一致（工具值 {values}）"
                )

    tool_numbers = _flatten_numbers(tool_results)
    news_numbers = _flatten_news_numbers(tool_results)
    # 中文财报/行情常用单位：数字后可跟 万亿/亿/万（如"总市值约 1.90 万亿元"）。
    # 先把声称值换算回原始单位再与工具值对比，避免单位换算造成误报。
    # 数值支持千分位写法（"33,472"）；另跳过三类非独立数值：
    # - 数字前紧跟 ASCII 字母（"略高于 MA60" 中 MA60 的 60 是指标参数）；
    # - 数字后紧跟日期续写（"价格为 2026-08-21" 中的 2026 是日期年）；
    # - 数字后紧跟 日/天/周 周期标记（"20 日均线" 的 20 是均线周期参数）。
    # Phase 20C 新闻事件豁免：与工具数值严格按符号匹配（DIF=-8.2 不能写成 8.2）；
    # 与新闻原文数字按绝对值匹配——"净利润下降 1.95%"复述为"净利同比 -1.95"时，
    # 负号只是对"下降"的转写，同一量级即视为有新闻证据支撑，不判无证据编造。
    number_re = re.compile(
        r"[-+]?\d{1,3}(?:,\d{3})+(?:\.\d+)?|[-+]?\d+(?:\.\d+)?"
    )
    for word in TIER2_WORDS:
        pattern = re.compile(
            re.escape(word)
            + r"[^0-9+\-\n]{0,8}"
            + r"(" + number_re.pattern + r")"
            + r"\s*(万亿|亿|万)?"
        )
        for match in pattern.finditer(report):
            start = match.start(1)
            if start > 0 and report[start - 1].isascii() and report[start - 1].isalpha():
                continue
            if re.match(r"^\s*[-/年.]\s*\d", report[match.end(1):]):
                continue
            if re.match(r"^\s*[日天周]", report[match.end(1):]):
                continue
            claimed = float(match.group(1).replace(",", ""))
            unit = match.group(2) or ""
            scale = {"万": 1e4, "亿": 1e8, "万亿": 1e12}.get(unit, 1.0)
            if not any(
                _close(claimed, value) or _close(claimed * scale, value)
                for value in tool_numbers
            ) and not any(
                _close(abs(claimed), abs(value))
                or _close(abs(claimed * scale), abs(value))
                for value in news_numbers
            ):
                violations.append(
                    f"{word} 相关数值 {match.group(1)}{unit} 在工具结果中不存在"
                )
    return violations


def check_time_confusion(report: str, tool_results: Dict[str, Any]) -> List[str]:
    """时间属性区分：fetched_at 不得当作行情时间或财务报告期。

    采用"属性词 + 紧邻日期"的定中结构匹配（如"行情日期 2026-08-21"、
    "报告期为 2026-08-21"），避免属性词仅因同句出现而误报；
    仅当 fetched_at 与市场日期/报告期不同日时才校验。
    """
    fetched_at = market_date = report_period = None
    for _name, result in _iter_tool_results(tool_results):
        if not isinstance(result, dict):
            continue
        if fetched_at is None:
            fetched_at = result.get("fetched_at")
        if market_date is None and result.get("market_date"):
            market_date = result.get("market_date")
        if report_period is None and result.get("report_period"):
            report_period = result.get("report_period")

    if not fetched_at:
        return []

    fetched_c = _date_compact(fetched_at)
    if not fetched_c:
        return []

    # fetched_at 的紧凑（20260821）与带横线（2026-08-21）两种写法都要能匹配
    fetched_alt = r"(?:" + fetched_c + r"|" + f"{fetched_c[:4]}-{fetched_c[4:6]}-{fetched_c[6:]}" + r")"
    # 属性词与日期之间至多 8 个非数字/连字符/换行/句号字符，构成定中结构
    gap = r"[^0-9+\-\n。；]{0,8}"

    violations: List[str] = []

    if market_date:
        market_c = _date_compact(market_date)
        if market_c and fetched_c != market_c:
            for word in _MARKET_TIME_VOCAB:
                if re.compile(re.escape(word) + gap + fetched_alt).search(report):
                    violations.append(
                        f"把获取时刻（fetched_at {str(fetched_at)[:10]}）当作行情/交易时间"
                    )

    if report_period:
        period_c = _date_compact(report_period)
        if period_c and fetched_c != period_c:
            for word in _REPORT_VOCAB:
                if re.compile(re.escape(word) + gap + fetched_alt).search(report):
                    violations.append(
                        f"把获取时刻（fetched_at {str(fetched_at)[:10]}）当作财务数据日期"
                    )

    return list(dict.fromkeys(violations))


def check_future_news_causality(report: str, tool_results: Dict[str, Any]) -> List[str]:
    """未来新闻因果：不得用发布时间晚于行情日期的新闻解释过去的价格变动。"""
    market_c: Optional[str] = None
    news_items: List[Dict[str, Any]] = []
    for _name, result in _iter_tool_results(tool_results):
        if not isinstance(result, dict):
            continue
        if market_c is None and result.get("market_date"):
            market_c = _date_compact(result.get("market_date"))
        items = result.get("news")
        if isinstance(items, list):
            news_items.extend(items)

    if not market_c or not news_items:
        return []

    violations: List[str] = []
    sentences = _sentences(report)
    for item in news_items:
        published = item.get("published_at")
        if not published:
            continue
        published_c = _date_compact(published)
        if not published_c or published_c <= market_c:
            continue  # 新闻不晚于行情日期，不是未来新闻
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        for sentence in sentences:
            if title in sentence and any(w in sentence for w in _CAUSALITY_WORDS):
                violations.append(
                    f"用发布时间晚于行情日期（{published}）的新闻解释了过去的行情变动"
                )
    return violations


def _sentence_around(text: str, pos: int) -> str:
    """返回 pos（触发词起始）所在的句子（。！？!?\n 为边界）。"""
    starts = [text.rfind(sep, 0, pos) for sep in ("。", "！", "？", "!", "?", "\n")]
    ends = [text.find(sep, pos) for sep in ("。", "！", "？", "!", "?", "\n")]
    start = max(starts) + 1
    end = min((e for e in ends if e != -1), default=len(text))
    return text[start:end]


# 历史区间/分位断言触发词：固定短语而非单个形容词，降低误报率。
BOUNDARY_CLAIM_WORDS: Tuple[str, ...] = (
    "历史分位", "历史偏低", "历史低位", "历史高位", "历史中枢",
    "历史底部", "历史顶部", "估值历史", "历史估值",
)
# 左起最长匹配：同一位置优先命中短语，避免"估值历史分位"被计为两次违规
# （全部为 4 字词，按长度降序排序不改变元组顺序）。
_BOUNDARY_CLAIM_RE = re.compile(
    "|".join(re.escape(w) for w in sorted(BOUNDARY_CLAIM_WORDS, key=len, reverse=True))
)

# 免责/否定句豁免词：仅收录"否定获取数据或作出判断/断言"类动词（"未能获得"
# "未对…作出结论""不会对…作出断言"）。不含 没有/缺乏/不足 等宽泛否定——
# "处于历史低位，但没有业绩支撑"中的历史低位断言仍应被拦截；"证据不足/
# 无法判断"等明确免责短语已由 _HEDGE_PHRASES 覆盖。
_BOUNDARY_NEGATORS: Tuple[str, ...] = (
    "未能", "未获得", "未获取", "未返回", "未成功", "未给出", "未提供",
    "未取得", "未对", "未作", "未做", "未出现", "无法", "不能", "不会",
    "不足以", "不具备", "不构成", "不作", "不做", "不认为", "难以",
    "未必", "并非", "不应",
)


def _boundary_hedged(sentence: str) -> bool:
    """句子是否属免责/否定表述（无法获得数据、无法判断、不会断言…）。"""
    if any(phrase in sentence for phrase in _HEDGE_PHRASES):
        return True
    return any(negator in sentence for negator in _BOUNDARY_NEGATORS)


def _is_heading_line(text: str, pos: int) -> bool:
    """pos 是否落在 Markdown 标题行（行首 # 序列）。

    标题描述分析主题而非断言估值位置（如"估值历史位置分析报告"），
    不作为边界断言触发。
    """
    line_start = text.rfind("\n", 0, pos) + 1
    return bool(re.match(r"^[ \t]*#{1,6}[ \t]", text[line_start:]))


def _has_reliable_history_percentile(tool_results: Dict[str, Any]) -> bool:
    """工具结果是否提供了可靠的历史分位数据（percentiles.reliable=True）。

    新结构在 percentiles 顶层带 reliable；旧结构兼容：顶层缺失该键时，
    pe/pb 任一子项 reliable=True 也视为可靠。
    """
    for _name, result in _iter_tool_results(tool_results):
        if not isinstance(result, dict):
            continue
        percentiles = result.get("percentiles")
        if not isinstance(percentiles, dict):
            continue
        if percentiles.get("reliable") is True:
            return True
        for item in ("pe", "pb"):
            sub = percentiles.get(item)
            if isinstance(sub, dict) and sub.get("reliable") is True:
                return True
    return False


def check_boundary_claims(report: str, tool_results: Dict[str, Any]) -> List[str]:
    """历史区间/分位断言检查：无可靠分位数据时，不得断言估值所处历史位置。

    引号内转述（对用户措辞的引用）与免责/否定句子（"证据不足""无法判断"、
    "未能获得""不会对…作出断言"）自动豁免；Markdown 标题行（描述分析主题
    而非断言位置）同样豁免；percentiles.reliable=True 时放行。
    """
    if _has_reliable_history_percentile(tool_results):
        return []
    violations: List[str] = []
    for match in _BOUNDARY_CLAIM_RE.finditer(report):
        word = match.group(0)
        if _is_inside_quotes(report, match.start(), match.end()):
            continue
        if _is_heading_line(report, match.start()):
            continue
        if _boundary_hedged(_sentence_around(report, match.start())):
            continue
        violations.append(
            f"断言估值所处历史区间/分位（{word}），但工具未提供可靠的历史分位数据"
        )
    return violations


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------
def validate_report(
    report: str, tool_results: Dict[str, Any], require_sections: bool = True
) -> List[str]:
    """对最终报告做全部确定性校验，返回违规项列表。

    Args:
        report: 模型最终回答文本。
        tool_results: {工具名: 结果 dict} 容器（news 结果放在键 "news" 下）。
        require_sections: 是否要求 4 个必备小节（简短拒绝类回答可不要求）。

    Returns:
        违规项列表；为空表示未发现违规。
    """
    if not report or not report.strip():
        return ["报告为空"]

    violations: List[str] = []
    if require_sections:
        violations.extend(check_mandatory_sections(report))
    violations.extend(check_forbidden_patterns(report))
    violations.extend(check_missing_indicator_claims(report, tool_results))
    violations.extend(check_indicator_claims(report, tool_results))
    violations.extend(check_time_confusion(report, tool_results))
    violations.extend(check_future_news_causality(report, tool_results))
    violations.extend(check_boundary_claims(report, tool_results))
    return violations


def validate_report_critical(report: str, tool_results: Dict[str, Any]) -> List[str]:
    """高危违规校验：仅拦截"无证据编造"与"违规荐股"两类。

    聚合违禁表达（买卖/仓位建议、确定性未来预测）、指标缺失（工具未返回却给出
    数值）与证据链不一致（指标数值与工具返回不符）。小节缺失等低危结构性检查
    不在此列——它们不构成对用户的高风险误导，接入真实请求链时只记日志不降级。
    """
    if not report or not report.strip():
        return []
    violations: List[str] = []
    violations.extend(check_forbidden_patterns(report))
    violations.extend(check_missing_indicator_claims(report, tool_results))
    violations.extend(check_indicator_claims(report, tool_results))
    return violations


def build_degraded_answer(violations: List[str]) -> str:
    """构造受限降级回答：明确风险提示 + 拦截原因（不泄露完整原始结论）。"""
    reasons = "\n".join(f"- {v}" for v in violations)
    return (
        "【回答受限：风险提示】\n"
        "本次生成的内容未通过系统输出合规校验，原始结论已被拦截，不再展示。\n\n"
        "拦截原因：\n"
        f"{reasons}\n\n"
        "请知悉：本系统输出仅基于公开数据，仅供研究和分析，不构成投资建议；"
        "对个股未来涨跌、买卖时点或仓位配置，系统不予给出确定性结论。"
    )
