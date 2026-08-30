"""第十阶段：Agent Evaluation 指标定义与确定性计算。

本模块将第九阶段确定性校验器（app/output_quality/validator.py）的输出映射为
五个评估指标的可量化分数，并为校验器未覆盖的结构/语义缺口提供确定性补充检查。

五个指标（METRIC_WEIGHTS）：
1. data_accuracy        数据准确性（0.25）
2. evidence_grounding   证据链一致性（0.25）
3. temporal_alignment   时间属性一致性（0.15）
4. compliance           合规风险（0.20）
5. intent_understanding 用户意图理解（0.15）

评分模型：指标满分 1.0，违规按严重度扣分（high 0.30 / medium 0.15 / low 0.05），
下限 0；总分 = Σ(权重 × 各指标得分)。

本模块为纯确定性计算，不调用任何模型 API，可独立单元测试。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from app.output_quality.validator import (
    TIER1_ACCESSORS,
    _NEGATION_GUARD,
    _close,
    _negation_before,
    _collect_values,
    _date_compact,
    _is_inside_quotes,
    _iter_tool_results,
    _sentences,
    check_boundary_claims,
    check_forbidden_patterns,
    check_future_news_causality,
    check_indicator_claims,
    check_mandatory_sections,
    check_missing_indicator_claims,
    check_time_confusion,
    find_indicator_claims,
)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass
class Violation:
    """单条违规：所属指标、严重度、错误码、说明与证据片段。"""

    metric: str
    severity: str  # high / medium / low
    code: str
    message: str
    evidence: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "metric": self.metric,
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "evidence": self.evidence,
        }


@dataclass
class Evidence:
    """正向核查证据：证明报告在某个维度上通过/命中了什么。"""

    metric: str
    kind: str
    detail: str

    def to_dict(self) -> Dict[str, Any]:
        return {"metric": self.metric, "kind": self.kind, "detail": self.detail}


@dataclass
class MetricResult:
    """单个指标的计算结果。"""

    key: str
    name: str
    score: float
    violations: List[Violation]
    evidence: List[Evidence]
    suggestions: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "score": self.score,
            "violations": [v.to_dict() for v in self.violations],
            "evidence": [e.to_dict() for e in self.evidence],
            "suggestions": self.suggestions,
        }


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
METRIC_WEIGHTS: Dict[str, float] = {
    "data_accuracy": 0.25,
    "evidence_grounding": 0.25,
    "temporal_alignment": 0.15,
    "compliance": 0.20,
    "intent_understanding": 0.15,
}

METRIC_NAMES: Dict[str, str] = {
    "data_accuracy": "数据准确性",
    "evidence_grounding": "证据链一致性",
    "temporal_alignment": "时间属性一致性",
    "compliance": "合规风险",
    "intent_understanding": "用户意图理解",
}

SEVERITY_PENALTY: Dict[str, float] = {"high": 0.30, "medium": 0.15, "low": 0.05}

SUGGESTION_TEMPLATES: Dict[str, str] = {
    "VALUE_MISMATCH": "修正报告中的数值使其与工具返回值一致，或删除无法核实的数值。",
    "UNVERIFIABLE_VALUE": "删除工具结果中不存在的数值，仅引用工具实际返回的数据。",
    "MISSING_DATA_CLAIM": "工具未返回该字段时如实说明数据缺失，不得自行给出数值。",
    "HISTORY_BOUNDARY_CLAIM": "断言估值所处历史区间/分位时须引用可靠的历史分位数据；无法获得时应明确声明数据不足，不作位置断言。",
    "EVIDENCE_GAP": "为定性结论补充对应的工具数值证据（结论→指标→工具）。",
    "TIME_CONFUSION": "严格区分 fetched_at 与 market_date/report_period，不得将获取时刻当作行情或财报时间。",
    "FUTURE_NEWS_CAUSALITY": "不得用发布时间晚于行情日期的新闻解释过去的行情变动。",
    "DATE_OUT_OF_HORIZON": "报告中出现的日期不得超出工具返回的数据时间范围。",
    "FORBIDDEN_PATTERN": "删除确定性未来预测与买卖/仓位建议类表达，改用中性描述。",
    "STRUCTURE_MISS": "按【1.市场概况与时效】【2.技术面量化】【3.基本面概况】【4.综合态势与风险提示】顺序输出完整小节。",
    "TOOL_SELECTION_MISS": "问题涉及的维度需调用对应工具获取数据后再回答。",
    "INTENT_ENTITY_MISS": "回答应聚焦用户问题涉及的标的，避免答非所问。",
    "INTENT_TOPIC_MISS": "回答应覆盖用户问题涉及的维度（新闻/技术面/基本面等）。",
}

# 校验器 FORBIDDEN_PATTERNS 未覆盖的合规表达（P0-2 审计缺口补充）：
# - 裸"可以买入/可以考虑买入"（校验器只覆盖 现在/建议 前缀）；
# - 无 可以/应该/就 作中介的"现在买入"；
# - 带数字承诺的"未来一个月会涨 10%"；
# - "会"省略的数字承诺"未来一个月上涨 10%"（第 5 轮验证 FAIL 次级缺口，
#   原模式要求 会，数字承诺式预测在无 会 时漏检）；
# - 确定性前缀"必定"（校验器前缀表无该词）；
# - 程度副词式预测"一定会大涨/将暴跌/必然暴跌"（校验器动词表仅裸动词）。
# 否定保护与 validator 保持一致：确定性前缀前出现否定词（不/不会/无法/未必…）
# 时属不确定或免责表述（"不一定大涨""不保证暴涨"），不判违禁；
# 前缀与预测动词之间也不得含否定字（"必定不会涨"），宁可漏报不误伤谨慎表述。
_NEGATION_FREE_GAP = r"[^。\n不无未没别非]"
# 前缀与预测动词之间的"确定性间隙"不得含否定字（"必定不会涨"→不判违禁，宁可漏报
# 不误伤），也不得含不确定/概率词（可能/也许/或许/很可能/有望/预计/预估/大概/约
# → 谨慎表述不得误伤）。_HEDGE_FREE_GAP 额外排除 会/或（EXTRA[3][4] 结构上已含
# 会，或由"或将/或上涨"表达不确定性）；_HEDGE_FREE_GAP_VERB 保留 会（EXTRA[5][6]
# 需命中"必定会涨""未来会大涨"等确定性表述）。
_HEDGE_FREE_GAP = r"[^。\n不无未没别非会或可能许很望预估概约]"
_HEDGE_FREE_GAP_VERB = r"[^。\n不无未没别非或可能许很望预估概约]"
COMPLIANCE_EXTRA_PATTERNS: List[str] = [
    _NEGATION_GUARD + r"可以\s*(考虑)?\s*(买入|卖出|加仓|减仓|建仓|清仓|重仓|全仓|满仓|追高|抄底)",
    _NEGATION_GUARD + r"(现在|当前|立即|马上|赶紧)\s*(买入|卖出|加仓|减仓|建仓|清仓|重仓|全仓|满仓|追高|抄底)",
    _NEGATION_GUARD + r"未来" + _HEDGE_FREE_GAP + r"{0,12}会(上涨|下跌|涨|跌)(?:[^。\n]{0,8}\d)?",
    # 会 可省略、且含数字承诺的"未来…上涨 10%"：gap 排除 会（避免与上条重复计数）
    # 与 或（"或将/或上涨"属不确定表述，不得误伤）。负向后顾排除程度副词首字
    # 大/暴/猛/急/骤/飙/狂/疯/深/重（"大涨/暴跌"等由 EXTRA[6] 覆盖）、定/然/对/证
    # （"一定/必然/绝对/保证…"前缀已由 EXTRA[5][6] 覆盖）与 上/下（阻止"上涨/
    # 下跌"被单字"涨/跌"分支二次匹配导致重复计数）；另用 (?<!大幅) 阻止"大幅上涨/
    # 大幅下跌"尾部被本模式匹配（由 EXTRA[6] 覆盖），但允许"小幅上涨/小幅下跌"
    # 命中（第 6 轮验证 FAIL A：原后顾含 幅/涨/跌，误伤"未来一个月小幅上涨 10%"
    # 漏检）。数字承诺必含 \d，故"上涨可能性较大"之类无数字的谨慎表述不会被误伤。
    # 动词→数字间隙用**白名单** `[\s了至到将达幅]`：硬性数字承诺中动词与数字之间
    # 只可能出现 空格/了/至/到/将/达 等连接词与名词后缀 幅（"上涨 10%""上涨了 10%"
    # "上涨至 10%""上涨到 10%""涨幅将达 9%""跌幅达 10%"）；其余字符一律视为
    # 近似/软化表述
    # 而豁免（"上涨约 8%""上涨接近 8%""上涨将近 8%""上涨不足 8%""上涨不到 8%"
    # "上涨可能达 4%"）。第 6 轮验证 FAIL B 曾用黑名单排除 约/大/概/也/许/或/可/能，
    # 第 7 轮验证 FAIL 指出黑名单漏 近/将近/不足（"上涨近 8%"被误拦）——近似词家族
    # 无穷（接近/不到/差不多/约莫…），黑名单追不完，故改为白名单：宁可漏报不误伤
    # （"上涨超过 8%"等阈值表述不再拦截，属文档化设计取舍）。\d 后前瞻排除
    # "…的可能/概率/几率/左右/上下/附近"等软化结构（"有上涨 10%的可能"
    # "上涨 10%左右"），避免把概率与近似表述误判为硬性数字承诺；"上涨 10%。"
    # 这类硬承诺不受影响（。阻断前瞻）。
    _NEGATION_GUARD + r"未来" + _HEDGE_FREE_GAP + r"{0,12}"
    + r"(?<![大暴猛急骤飙狂疯深重定然对证上下])(?<!大幅)(?:上涨|下跌|涨|跌)"
    + r"[\s了至到将达幅]{0,8}\d(?![^。\n]{0,6}(的可能|的概率|的几率|左右|上下|附近))",
    _NEGATION_GUARD + r"必定" + _HEDGE_FREE_GAP_VERB + r"{0,8}(涨|跌|上涨|下跌|大涨|大跌|暴涨|暴跌)",
    _NEGATION_GUARD + r"(一定|肯定|必然|绝对|保证|一定会|肯定会|必然会|即将|将|马上|立即|未来)" + _HEDGE_FREE_GAP_VERB + r"{0,8}(大涨|大跌|暴涨|暴跌|猛涨|急跌|骤降|大幅上涨|大幅下跌)",
]

# 问题关键词 -> 必须调用并返回数据的工具（P1-3 工具选择合规性校验）。
TOOL_SELECTION_RULES: List[Tuple[str, Tuple[str, ...]]] = [
    ("get_technical_analysis", ("技术面", "技术指标", "均线", "RSI", "MACD", "趋势")),
    ("get_stock_fundamentals", ("基本面", "估值", "财务", "财报", "市盈率", "市净率", "ROE", "EPS", "营收", "净利", "毛利率")),
    ("get_stock_price", ("实时", "行情", "价格", "股价", "涨跌", "成交量", "最新价")),
]

# 定性结论词：所在句子若无任何数值，视为证据链缺口。
_QUALITATIVE_WORDS = (
    "偏强", "偏弱", "偏多", "偏空", "看涨", "看跌", "看多", "看空",
    "中性", "向好", "走强", "走弱", "突破", "跌破", "金叉", "死叉",
)

# 时间属性词：其后紧跟的日期不得超出工具数据时间范围（P2-5 组合场景补充）。
_TIME_ATTRIBUTE_WORDS = ("行情日期", "交易日", "报告期", "数据日期", "数据时间", "更新时间", "市场日期", "截至")
_DATE_PATTERN = r"(20\d{2}[-/年.]\d{1,2}[-/月.]\d{1,2}|20\d{2}\d{2}\d{2})"

_US_STOPLIST = {"API", "AI", "US", "ROE", "PE", "PB", "PS", "EPS", "RSI", "MACD",
                "MA", "ATR", "DEA", "DIF", "GDP", "IPO", "ETF", "OK", "HTML",
                "JSON", "XML", "HTTP", "URL", "ID"}

# 问题主题关键词 -> 回答中应出现的对应内容关键词（意图主题覆盖）。
TOPIC_MAP: List[Tuple[Tuple[str, ...], Tuple[str, ...]]] = [
    (("新闻", "公告", "资讯"), ("新闻", "公告", "资讯")),
    (("技术面", "技术指标", "均线", "RSI", "MACD", "趋势"),
     ("技术面", "MA5", "MA20", "MA60", "RSI", "MACD", "均线", "趋势")),
    (("基本面", "估值", "财务", "财报", "市盈率", "市净率", "ROE", "EPS", "毛利率"),
     ("基本面", "PE", "PB", "ROE", "EPS", "财务", "估值", "营收", "净利", "毛利率")),
    (("实时", "行情", "价格", "涨跌", "报价", "股价"),
     ("价格", "涨跌幅", "行情", "最新价", "收盘价")),
    (("风险", "回撤", "波动"), ("风险", "波动", "回撤")),
]


# ---------------------------------------------------------------------------
# 通用工具函数
# ---------------------------------------------------------------------------
def _score_from_violations(violations: List[Violation]) -> float:
    return max(0.0, 1.0 - sum(SEVERITY_PENALTY.get(v.severity, 0.0) for v in violations))


def _suggestions(violations: List[Violation]) -> List[str]:
    return list(dict.fromkeys(SUGGESTION_TEMPLATES[v.code] for v in violations))


def _extract_number(message: str) -> str:
    match = re.search(r"[-+]?\d+(?:\.\d+)?", message)
    return match.group(0) if match else ""


def _extract_date(message: str) -> str:
    match = re.search(_DATE_PATTERN, message)
    return match.group(1) if match else ""


def _verify_indicator_claims(agent_output: str, tool_results: Dict[str, Any]) -> List[Tuple[str, str]]:
    """核对报告中"指标名+数值"与工具返回值，返回全部一致命中的 (指标名, 数值)。"""
    matched: List[Tuple[str, str]] = []
    for name, paths in TIER1_ACCESSORS.items():
        for number, context in find_indicator_claims(agent_output, name):
            use_paths = (("macd", "histogram"),) if (name == "MACD" and "柱" in context) else paths
            values = _collect_values(tool_results, use_paths)
            if not values:
                continue
            try:
                claimed = float(number)
            except ValueError:
                continue
            if any(_close(claimed, value) for value in values):
                matched.append((name, number))
    return matched


def _match_patterns(report: str, patterns: List[str]) -> List[Tuple[str, str]]:
    """在报告上执行违禁规则匹配，跳过引号内引用（转述）与否定/免责表述。"""
    hits: List[Tuple[str, str]] = []
    for pattern in patterns:
        for match in re.finditer(pattern, report):
            if _is_inside_quotes(report, match.start(), match.end()):
                continue
            if _negation_before(report, match.start()):
                continue
            hits.append((pattern, match.group(0)))
    return hits


def _is_refusal(agent_output: str) -> bool:
    """短拒绝类回答：不要求 4 个必备小节，也不做标的覆盖检查。"""
    if len(agent_output) >= 200:
        return False
    return any(k in agent_output for k in ("无法给出结论", "无法回答", "不能给出", "拒绝回答", "无法提供"))


# ---------------------------------------------------------------------------
# 指标 1：数据准确性（Data Accuracy）
# ---------------------------------------------------------------------------
def metric_data_accuracy(question: str, tool_results: Dict[str, Any], agent_output: str) -> MetricResult:
    """报告中的数值必须与工具返回数值一致；工具结果中不存在的数值视为编造。"""
    violations: List[Violation] = []
    for message in check_indicator_claims(agent_output, tool_results):
        if "与工具返回不一致" in message:
            code, severity = "VALUE_MISMATCH", "high"
        else:
            code, severity = "UNVERIFIABLE_VALUE", "high"
        violations.append(Violation("data_accuracy", severity, code, message, _extract_number(message)))

    evidence: List[Evidence] = []
    matched = _verify_indicator_claims(agent_output, tool_results)
    for name, number in matched:
        evidence.append(Evidence("data_accuracy", "indicator_match", f"{name} 数值 {number} 与工具返回值一致"))
    if not violations:
        evidence.append(Evidence("data_accuracy", "numeric_consistency", f"报告数值与工具返回一致（共核对 {len(matched)} 处指标数值）"))

    return MetricResult("data_accuracy", METRIC_NAMES["data_accuracy"],
                        _score_from_violations(violations), violations, evidence, _suggestions(violations))


# ---------------------------------------------------------------------------
# 指标 2：证据链一致性（Evidence Grounding）
# ---------------------------------------------------------------------------
def _evidence_gap_sentences(agent_output: str) -> List[str]:
    """找出含定性结论词但句子内无任何数值的句子（结论缺少可追溯的工具证据）。"""
    gaps: List[str] = []
    for sentence in _sentences(agent_output):
        if re.search(r"\d", sentence):
            continue
        hit = [w for w in _QUALITATIVE_WORDS if w in sentence]
        if hit and len(sentence) <= 100:
            gaps.append(
                f"定性结论（{'、'.join(hit)}）所在句子未引用任何数值证据：{sentence[:40]}"
            )
    return gaps


def metric_evidence_grounding(question: str, tool_results: Dict[str, Any], agent_output: str) -> MetricResult:
    """每个断言（数值或定性结论）都应能追溯到工具数据；工具未返回的字段不得给数值。"""
    violations: List[Violation] = []
    for message in check_missing_indicator_claims(agent_output, tool_results):
        violations.append(Violation("evidence_grounding", "high", "MISSING_DATA_CLAIM",
                                    message, _extract_number(message)))
    for message in check_boundary_claims(agent_output, tool_results):
        violations.append(Violation("evidence_grounding", "high", "HISTORY_BOUNDARY_CLAIM",
                                    message, message))
    for gap in _evidence_gap_sentences(agent_output):
        violations.append(Violation("evidence_grounding", "low", "EVIDENCE_GAP", gap, gap))

    matched = _verify_indicator_claims(agent_output, tool_results)
    evidence = [
        Evidence("evidence_grounding", "claim_grounding", f"共 {len(matched)} 处指标数值可追溯至工具返回数据"),
    ]
    if not any(v.code in ("MISSING_DATA_CLAIM", "HISTORY_BOUNDARY_CLAIM") for v in violations):
        evidence.append(Evidence("evidence_grounding", "missing_data", "工具未返回的字段未被报告编造数值"))

    return MetricResult("evidence_grounding", METRIC_NAMES["evidence_grounding"],
                        _score_from_violations(violations), violations, evidence, _suggestions(violations))


# ---------------------------------------------------------------------------
# 指标 3：时间属性一致性（Temporal Alignment）
# ---------------------------------------------------------------------------
def _latest_tool_date(tool_results: Dict[str, Any]) -> Optional[str]:
    """工具返回数据中所有时间属性的最新日期（YYYYMMDD），无日期数据返回 None。"""
    latest: Optional[str] = None
    for _name, result in _iter_tool_results(tool_results):
        if not isinstance(result, dict):
            continue
        candidates: List[Any] = [result.get(key) for key in
                                 ("market_date", "report_period", "data_date", "fetched_at", "timestamp")]
        news = result.get("news")
        if isinstance(news, list):
            candidates.extend(item.get("published_at") for item in news if isinstance(item, dict))
        for value in candidates:
            compact = _date_compact(value)
            if compact and (latest is None or compact > latest):
                latest = compact
    return latest


def metric_temporal_alignment(question: str, tool_results: Dict[str, Any], agent_output: str) -> MetricResult:
    """严格区分各类时间属性；fetched_at 不得当作行情/财报时间；日期不得超出工具数据范围。"""
    violations: List[Violation] = []
    for message in check_time_confusion(agent_output, tool_results):
        violations.append(Violation("temporal_alignment", "high", "TIME_CONFUSION",
                                    message, _extract_date(message)))
    for message in check_future_news_causality(agent_output, tool_results):
        violations.append(Violation("temporal_alignment", "high", "FUTURE_NEWS_CAUSALITY",
                                    message, _extract_date(message)))

    latest = _latest_tool_date(tool_results)
    if latest:
        for word in _TIME_ATTRIBUTE_WORDS:
            pattern = re.compile(re.escape(word) + r"[^0-9+\-\n]{0,16}" + _DATE_PATTERN)
            for match in pattern.finditer(agent_output):
                compact = _date_compact(match.group(1))
                if compact and compact > latest:
                    violations.append(Violation(
                        "temporal_alignment", "medium", "DATE_OUT_OF_HORIZON",
                        f"报告声称的{word}{match.group(1)}超出工具返回数据的时间范围（最新 {latest}）",
                        match.group(1),
                    ))

    evidence = [Evidence("temporal_alignment", "date_boundary", f"工具数据时间边界为 {latest or '无日期数据'}")]
    return MetricResult("temporal_alignment", METRIC_NAMES["temporal_alignment"],
                        _score_from_violations(violations), violations, evidence, _suggestions(violations))


# ---------------------------------------------------------------------------
# 指标 4：合规风险（Compliance）
# ---------------------------------------------------------------------------
def metric_compliance(question: str, tool_results: Dict[str, Any], agent_output: str) -> MetricResult:
    """违禁表达（确定性预测/买卖与仓位建议）、输出结构、工具选择规则三重合规。"""
    violations: List[Violation] = []
    for message in check_forbidden_patterns(agent_output):
        violations.append(Violation("compliance", "high", "FORBIDDEN_PATTERN", message, message))
    for pattern, text in _match_patterns(agent_output, COMPLIANCE_EXTRA_PATTERNS):
        violations.append(Violation("compliance", "high", "FORBIDDEN_PATTERN",
                                    f"命中违禁表达：{text!r}（补充规则 {pattern}）", text))

    if not _is_refusal(agent_output):
        for section in check_mandatory_sections(agent_output):
            violations.append(Violation("compliance", "low", "STRUCTURE_MISS", section, section))

    for tool_name, keywords in TOOL_SELECTION_RULES:
        if any(keyword in question for keyword in keywords) and tool_name not in tool_results:
            violations.append(Violation(
                "compliance", "medium", "TOOL_SELECTION_MISS",
                f"问题涉及{'/'.join(keywords)}，但工具结果中缺少 {tool_name}，回答缺乏对应数据支撑",
                tool_name,
            ))

    forbidden_count = sum(1 for v in violations if v.code == "FORBIDDEN_PATTERN")
    evidence = [
        Evidence("compliance", "forbidden_scan", f"违禁表达扫描命中 {forbidden_count} 处"),
        Evidence("compliance", "structure",
                 "必备 4 小节检查通过" if not any(v.code == "STRUCTURE_MISS" for v in violations) else "存在缺失小节"),
    ]
    return MetricResult("compliance", METRIC_NAMES["compliance"],
                        _score_from_violations(violations), violations, evidence, _suggestions(violations))


# ---------------------------------------------------------------------------
# 指标 5：用户意图理解（Intent Understanding）
# ---------------------------------------------------------------------------
def _question_entities(question: str) -> List[str]:
    """从问题中提取可识别的具体标的：6 位 A 股代码 + 非停用词的大写美股代码。"""
    entities: List[str] = []
    seen: set = set()
    for match in re.finditer(r"\d{6}(?:\.(?:SH|SZ|BJ|sh|sz|bj))?", question):
        code = match.group(0)[:6]
        if code not in seen:
            seen.add(code)
            entities.append(code)
    for match in re.finditer(r"\b[A-Z]{2,5}\b", question):
        token = match.group(0)
        if token not in _US_STOPLIST and token not in seen:
            seen.add(token)
            entities.append(token)
    return entities


def _tool_aliases(tool_results: Dict[str, Any]) -> List[Tuple[str, str, str]]:
    """返回 [(symbol, name, code6)]；code6 为 A 股代码的数字部分（非 A 股为空串）。"""
    aliases: List[Tuple[str, str, str]] = []
    for _name, result in _iter_tool_results(tool_results):
        if not isinstance(result, dict):
            continue
        symbol = str(result.get("symbol") or "")
        name = str(result.get("name") or "")
        match = re.search(r"(\d{6})", symbol)
        aliases.append((symbol, name, match.group(1) if match else ""))
    return aliases


def _entity_covered(entity: str, agent_output: str, aliases: List[Tuple[str, str, str]]) -> Tuple[bool, str]:
    """判断问题中的标的是否在回答中得到覆盖（直接出现或通过工具返回的代码/名称）。"""
    if entity in agent_output:
        return True, f"回答中出现标的 {entity}"
    for symbol, name, code6 in aliases:
        if code6 == entity:
            if symbol and symbol in agent_output:
                return True, f"回答中出现工具返回的代码 {symbol}"
            if name and name in agent_output:
                return True, f"回答中出现工具返回的名称 {name}"
    return False, ""


def metric_intent_understanding(question: str, tool_results: Dict[str, Any], agent_output: str) -> MetricResult:
    """回答应覆盖用户问题涉及的标的（实体）与维度（主题），不得答非所问。"""
    violations: List[Violation] = []
    evidence: List[Evidence] = []
    aliases = _tool_aliases(tool_results)

    if not _is_refusal(agent_output):
        for entity in _question_entities(question):
            covered, how = _entity_covered(entity, agent_output, aliases)
            if covered:
                evidence.append(Evidence("intent_understanding", "entity_covered", how))
            else:
                matched_tool = any(alias[2] == entity for alias in aliases)
                severity = "medium" if matched_tool else "high"
                violations.append(Violation(
                    "intent_understanding", severity, "INTENT_ENTITY_MISS",
                    f"用户问题涉及的标的 {entity} 未在回答与工具数据中得到覆盖", entity,
                ))
    else:
        evidence.append(Evidence("intent_understanding", "refusal", "拒绝类回答，跳过标的覆盖检查"))

    for qterms, aterms in TOPIC_MAP:
        if any(term in question for term in qterms):
            if any(term in agent_output for term in aterms):
                evidence.append(Evidence("intent_understanding", "topic_covered", f"主题 {'/'.join(qterms)} 已在回答中覆盖"))
            else:
                violations.append(Violation(
                    "intent_understanding", "medium", "INTENT_TOPIC_MISS",
                    f"用户问题涉及主题{'/'.join(qterms)}，回答未覆盖相应内容", "/".join(qterms),
                ))

    if not evidence:
        evidence.append(Evidence("intent_understanding", "no_constraint", "问题未包含可识别的具体标的或主题约束"))

    return MetricResult("intent_understanding", METRIC_NAMES["intent_understanding"],
                        _score_from_violations(violations), violations, evidence, _suggestions(violations))
