"""Agent 执行逻辑服务化核心模块（唯一真源）。

职责：
- 配置管理：MODEL / BASE_URL / MAX_TOOL_ROUNDS 常量，AgentSettings 与 create_client；
  其中 system_prompt / tool_schemas 支持按 Agent 或按请求注入（缺省用模块常量）；
- 工具清单：TOOL_SCHEMAS（告知模型的 JSON Schema）与 TOOL_DISPATCH（名称 -> 实际执行函数）；
- 提示词：SYSTEM_PROMPT 唯一真源；
- 编排：run_agent 执行一次完整的多轮 Tool Calling 流程，返回结构化 AgentResult。

服务化约定：
- run_agent 默认静默（不 print），通过 progress 回调把 CLI 可见的进度文本交给调用方；
- main.py 仅作 CLI 壳层并从本模块再导出，保持 from main import ... 旧 import 兼容。

流程：
1. 用户输入问题，DeepSeek 识别意图；
2. 模型请求调用工具（支持一次请求并行调用多个工具）；
3. Python 实际执行工具（get_stock_price / get_technical_analysis /
   get_stock_fundamentals / get_valuation_analysis）；
4. 工具结果以 role="tool" 消息返回模型，循环直到模型不再请求工具；
5. 模型基于工具返回的真实数据生成结构化综合分析报告，禁止编造。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from typing import Any, AsyncIterator, Callable, Dict, Iterator, List, Optional, Tuple

from openai import OpenAI

from app.agent.evidence import (
    build_evidence_context,
    render_evidence_context,
)
from app.agent.observability import ToolTrace
from app.agent.runtime import RuntimeLimits, RuntimeState
from app.agent.workers import WORKER_TOOLS
from app.output_quality.validator import (
    build_degraded_answer,
    check_forbidden_patterns,
    validate_report_critical,
)
from app.tools.fundamentals_tool import (
    FUNDAMENTALS_TOOL_SCHEMA,
    get_stock_fundamentals,
)
from app.tools.technical_tool import get_technical_analysis
from app.tools.valuation_tool import (
    VALUATION_TOOL_SCHEMA,
    get_valuation_analysis,
)
from app.tools.news_tool import NEWS_TOOL_SCHEMA, get_stock_news
from tools.stock_tool import get_stock_price

MODEL = "deepseek-v4-pro"
BASE_URL = "https://api.deepseek.com"
# 防止模型陷入无休止的工具调用循环；每轮可并行执行多个工具
MAX_TOOL_ROUNDS = 8

logger = logging.getLogger("app.agent.orchestrator")

# ---------------------------------------------------------------------------
# Tool Schema：以 JSON 格式把工具名称、用途、参数"告诉"模型。
# 模型读到 Schema 才知道存在哪些工具、该传什么参数，从而决定是否调用。
# ---------------------------------------------------------------------------
TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "get_stock_price",
            "description": (
                "获取指定股票（A 股或美股）的最新实时行情数据，"
                "包括价格、涨跌幅、开盘/最高/最低、成交额、市值等。"
                "数据来自公开行情源，不保证零延迟，仅用于研究和分析。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {
                        "type": "string",
                        "description": (
                            "A 股代码（如 600519、600519.SH）或美股代码"
                            "（如 NVDA、AAPL、TSLA）"
                        ),
                    }
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_technical_analysis",
            "description": (
                "获取指定股票（A 股或美股）最近约 170 个交易日的历史行情，"
                "并计算 MA5、MA20、MA60、RSI14、MACD、ATR14 等技术指标。"
                "历史数据来自 AKShare，技术指标由 Python 本地计算。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {
                        "type": "string",
                        "description": (
                            "A 股代码（如 600519、600519.SH）或美股代码"
                            "（如 NVDA、AAPL、TSLA）"
                        ),
                    }
                },
                "required": ["symbol"],
            },
        },
    },
    FUNDAMENTALS_TOOL_SCHEMA,
    VALUATION_TOOL_SCHEMA,
    NEWS_TOOL_SCHEMA,
]

# 工具名 -> 实际执行函数（Python 本地实现）
TOOL_DISPATCH = {
    "get_stock_price": get_stock_price,
    "get_technical_analysis": get_technical_analysis,
    "get_stock_fundamentals": get_stock_fundamentals,
    "get_valuation_analysis": get_valuation_analysis,
    "get_stock_news": get_stock_news,
}

# ---------------------------------------------------------------------------
# Worker 工具域隔离（Phase 20A）：
# WORKER_TOOLS 在 app.agent.workers 中定义，这里按域从 TOOL_DISPATCH 取值
# 裁剪出各 Worker 的工具执行表；不复制工具实现，完整 TOOL_DISPATCH 保留。
# _TOOL_WORKER 为 工具名 -> Worker 域 的反查映射，供路由过滤使用。
# ---------------------------------------------------------------------------
WORKER_TOOL_DISPATCH: Dict[str, Dict[str, Any]] = {
    worker: {name: TOOL_DISPATCH[name] for name in WORKER_TOOLS[worker]}
    for worker in WORKER_TOOLS
}

_TOOL_WORKER: Dict[str, str] = {
    tool: worker
    for worker, tools in WORKER_TOOLS.items()
    for tool in tools
}

SYSTEM_PROMPT = (
    "你是金融市场研究与信息分析助手，不是自动投资顾问，也不是自动交易系统。\n"
    "你只基于工具返回的真实数据做研究和分析，不执行真实交易，不给投资建议。\n\n"

    "【工具使用规则】\n"
    "涉及当前价格、涨跌幅、成交量、成交额等实时或近期市场数据的问题，"
    "必须优先调用 get_stock_price 工具获取实时行情。\n"
    "涉及趋势、均线、RSI、MACD、波动性等技术面分析的问题，"
    "必须调用 get_technical_analysis 工具。\n"
    "涉及估值（PE/PB/市值）、财务指标（EPS、ROE、毛利率、营收、净利）、"
    "成长性或三大财务报表的问题，必须调用 get_stock_fundamentals 工具。\n"
    "涉及估值所处历史区间高低（如“历史偏低”“历史中枢附近”“历史高位”）"
    "或 PE/PB 历史分位的问题，必须调用 get_valuation_analysis 工具。\n"
    "涉及个股近期新闻、公告、事件或快讯类问题（如“最近有什么新闻”），"
    "必须调用 get_stock_news 工具获取新闻资讯。\n"
    "技术指标（MA、RSI、MACD、ATR）必须来自工具返回的数据，"
    "不允许自行推算、计算或凭记忆补充；工具未返回的指标不得给出数值。\n"
    "不得编造价格、财务数据、指标、历史数据或新闻；工具返回什么就分析什么。\n"
    "对工具返回的数据，必须说明数据来源（AKShare/东方财富/Tushare 等）。\n"
    "所有结论都必须停留在工具数据可支撑的范围内：工具未返回的数据维度"
    "不得给出数值或位置断言；涉及估值历史区间/分位（“历史偏低”“历史高位”"
    "“历史中枢附近”等）时，仅在 get_valuation_analysis 返回可靠分位数据后"
    "方可断言，否则必须声明“当前数据不足以支持对估值历史位置的判断”，"
    "不得自行推断或凭记忆补充历史分位。\n\n"

    "【最终回答结构】\n"
    "最终回答必须包含以下 4 个一级小节，按顺序输出；"
    "若某类工具未被调用，对应小节必须如实说明未获取该维度数据，不得编造数据。\n"
    "【1. 市场概况与时效】\n"
    "写明股票名称与代码、最新价格、涨跌幅、数据来源。\n"
    "必须区分并如实说明时间属性：market_date（行情对应的市场交易日）、"
    "timestamp（行情快照精确时刻）、fetched_at（获取数据时刻）。\n"
    "fetched_at 不等于行情发生时间，绝不能把 fetched_at 当作行情时间或交易时间。\n"
    "若行情数据只有市场日期而没有精确时刻，必须明确说明"
    "“该数据为某交易日的行情/快照数据，未提供精确时刻”，不得编造时间。\n"
    "若调用过 get_stock_news 工具，必须列出相关新闻事实，并注明 publish_date"
    "（发布时间）与 source（来源），引用工具返回的新闻内容而非凭记忆补充。\n"
    "【2. 技术面量化】\n"
    "仅当调用过 get_technical_analysis 工具时输出；列出来自工具返回的指标："
    "MA5/MA20/MA60、RSI14、MACD（DIF/DEA/柱）、ATR14 等，并注明对应市场日期。\n"
    "不得计算或编造工具结果中不存在的指标。\n"
    "【3. 基本面概况】\n"
    "仅当调用过 get_stock_fundamentals 工具时输出；严格区分 report_period"
    "（财务报表报告期）、data_date（估值数据日期）、fetched_at（获取时刻）。\n"
    "可分析营收、净利、ROE、毛利率、现金流、PE、PB、股息率等，"
    "但只使用工具返回的数值；缺失、未返回、无权限的字段必须如实说明，"
    "不得用模型自身知识填充。\n"
    "涉及估值所处历史区间高低时，若调用过 get_valuation_analysis，"
    "必须引用工具返回的 percentiles（PE/PB 历史分位）与 horizon"
    "（回看窗口起始/结束日期、交易日数）；percentile 缺失或 reliable 为 False 时，"
    "必须说明“当前数据不足以支持对估值历史位置的判断”，"
    "不得脱离工具数据自行断言“历史偏低”“历史中枢附近”等结论。\n"
    "【4. 综合态势与风险提示】\n"
    "基于前面小节的数据给出技术面/基本面的强、弱、中性判断、估值高低与风险水平。\n"
    "每一个定性结论都必须能追溯到具体的工具数据（结论→指标→工具），并引用数值，"
    "例如“技术面当前呈现偏弱特征：RSI14 为 42.73，MACD 柱为 -18.55，"
    "最新收盘价 1272.83”。多个指标支持同一结论时应并列列出。\n"
    "不得使用工具结果之外的数据作为证据；证据不足时必须明确说明"
    "“当前数据不足以支持该判断”。\n"
    "最后必须附上风险提示：本报告基于公开数据，仅供研究和分析，不构成投资建议。\n\n"

    "【时间属性与事件因果】\n"
    "严格区分 market_date / timestamp / report_period / data_date / publish_date / "
    "published_at / fetched_at；fetched_at 不能被解释为行情发生时间。\n"
    "结合行情、基本面、新闻等数据时，必须按事件发生时间判断先后，"
    "不得用未来发生的新闻解释过去的价格变动。\n"
    "仅凭新闻无法确认绝对因果关系，评价新闻影响时需使用"
    "“可能受到…影响”等客观表述，不得将新闻与股价变动直接等同为因果。\n\n"

    "【合规与表达限制】\n"
    "禁止出现以下表达：建议买入/卖出、现在可以买、现在应该卖、可以全仓、"
    "建议重仓、明天一定上涨、明天大概率上涨、保证盈利、稳赚、一定会跌，"
    "以及一切对未来的确定性预测。\n"
    "允许的中性表达示例：“技术面当前呈现偏弱特征”“从当前指标来看，"
    "短期趋势尚未形成明确的向上确认”“当前数据未能支持确定性的上涨判断”。\n"
    "允许描述历史事实（如“过去一个交易日上涨 2.3%”）；仅禁止对未来的确定性预测。\n"
    "用户问题中若本身包含违禁措辞（如“明天一定会涨吗”“现在可以全仓买入吗”），"
    "回答时必须改述，不得原样引用该措辞，例如改述为“该问题询问明天是否一定上涨、"
    "当前是否可以全仓买入”，并说明这类问题涉及确定性预测/投资建议，本报告无法给出结论。\n\n"

    "【豁免与放宽规则】\n"
    "在严格遵守上述“严禁无证据编造”与“严禁荐股”的前提下，以下表达属于合法豁免范围，"
    "不会被判定为违规：\n"
    "一、新闻事件豁免：基于工具返回的新闻数据（标题/正文/摘要）做事件总结与情绪复述"
    "（如“据新闻所述，公司近期发布中报”“相关报道指出市场情绪偏谨慎”），"
    "只要不脱离新闻原文内容、不凭空补充原文中不存在的事实，即视为有证据支撑，不算无证据编造。\n"
    "二、风险分析放宽：允许基于现有基本面、技术面和新闻数据做客观的风险推演与分析，"
    "例如“结合上述新闻，公司可能面临销量下滑的风险”“若营收增速持续走弱，估值可能承压”，"
    "此类推断属于风险提示，不属于确定性预测。\n"
    "三、底线重申：以下两类表达无论何种情况都必须严格规避，绝不因上述放宽而松动——"
    "（1）绝对性预测，如“明天一定会涨”“下周必然下跌”“保证盈利”“稳赚”；"
    "（2）直接操作建议，如“建议全仓买入”“现在应该卖出”“可以满仓”等买卖/仓位指令。\n\n"

    "【缺失与异常数据处理】\n"
    "数据缺失（None）、接口报错、权限不足、限流、数据质量异常等，"
    "必须在报告中如实反映。例如：“由于 Tushare 当前接口权限限制，"
    "本次无法获得 ROE 数据”，不得用“公司 ROE 表现优异”之类的表述替代。\n"
    "应注明哪些数据来源可用、哪些不可用。\n\n"

    "请输出结构化的综合分析报告，并注明数据来源。"
    "数据仅用于研究和分析，不构成投资建议。"
)

ROUTER_PROMPT = """
你是金融投研任务路由器。

你的任务不是分析股票，也不是回答用户问题。
你只负责判断当前问题需要哪些专业数据维度。

返回严格 JSON：

{
  "needs_fundamental": true/false,
  "needs_quant": true/false,
  "needs_event": true/false
}

分类规则：

fundamental：
- 财务数据
- 营收
- 净利润
- EPS
- ROE
- 毛利率
- 现金流
- PE/PB
- 估值
- 基本面
- 历史估值分位

quant：
- 当前价格
- 涨跌幅
- 行情
- 趋势
- 均线
- MA
- RSI
- MACD
- ATR
- 技术面
- 波动率

event：
- 新闻
- 公告
- 消息
- 事件
- 最近发生了什么
- 公司近期动态

如果问题明显涉及多个维度，可以同时返回 true。

不要输出 JSON 之外的任何内容。
"""


# ---------------------------------------------------------------------------
# 服务化配置与结果结构
# ---------------------------------------------------------------------------

@dataclass
class AgentSettings:
    """Agent 运行配置；缺省值与模块常量保持一致，可用 from_env 从环境变量读取。

    system_prompt / tool_schemas 支持按 Agent 或按请求注入差异化提示词与工具清单；
    tool_schemas 默认是 TOOL_SCHEMAS 的深拷贝，不共享可变全局对象。
    """

    model: str = MODEL
    base_url: str = BASE_URL
    max_tool_rounds: int = MAX_TOOL_ROUNDS
    api_key: Optional[str] = None
    system_prompt: str = SYSTEM_PROMPT
    tool_schemas: List[Dict[str, Any]] = field(default_factory=lambda: deepcopy(TOOL_SCHEMAS))

    @classmethod
    def from_env(cls) -> "AgentSettings":
        return cls(api_key=os.getenv("DEEPSEEK_API_KEY"))


@dataclass
class ToolCallRecord:
    """一次工具调用的完整记录：第几轮、工具名、解析后的参数与执行结果。"""

    round: int
    name: str
    arguments: Dict[str, Any]
    result: Dict[str, Any]


@dataclass
class AgentResult:
    """run_agent 的结构化返回：最终回答与执行统计，供服务方直接消费。"""

    answer: str = ""
    tool_rounds: int = 0
    tool_calls: List[ToolCallRecord] = field(default_factory=list)
    max_rounds_reached: bool = False
    error: Optional[str] = None


def create_client(settings: Optional[AgentSettings] = None) -> OpenAI:
    """创建 DeepSeek API 客户端；无参调用时兼容旧接口（读取环境变量）。"""
    settings = settings or AgentSettings.from_env()
    if not settings.api_key:
        raise RuntimeError("未配置 DEEPSEEK_API_KEY，请在 .env 中配置后重试。")
    return OpenAI(api_key=settings.api_key, base_url=settings.base_url)


def _parse_arguments(arguments_str: str) -> Tuple[Dict[str, Any], Optional[Exception]]:
    """解析模型返回的工具参数 JSON；失败时返回空 dict 与异常（调用方决定告警）。"""
    try:
        return json.loads(arguments_str or "{}"), None
    except json.JSONDecodeError as exc:
        return {}, exc


def _execute_tool(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """执行工具；未知工具 / 参数错误 / 运行异常统一返回 {error, symbol} 结构。

    TOOL_DISPATCH 在调用时取模块级引用，测试可通过 mock.patch.dict 替换。
    每次调用记录耗时与状态（Phase 19A 结构化日志埋点）。
    """
    if name not in TOOL_DISPATCH:
        logger.warning("tool=%s 未知工具，已拒绝执行", name)
        return {"error": f"未知工具: {name}", "symbol": arguments.get("symbol", "")}
    start = time.perf_counter()
    try:
        result = TOOL_DISPATCH[name](**arguments)
    except TypeError as exc:
        result = {
            "error": f"工具参数错误: {exc}",
            "symbol": arguments.get("symbol", ""),
        }
    except Exception as exc:
        result = {
            "error": f"工具执行异常: {type(exc).__name__}: {exc}",
            "symbol": arguments.get("symbol", ""),
        }
    duration_ms = (time.perf_counter() - start) * 1000
    status = "ok" if "error" not in result else "error"
    logger.info(
        "tool=%s args=%s status=%s duration_ms=%.1f",
        name,
        arguments,
        status,
        duration_ms,
    )
    return result


def _route_question(
    client: OpenAI,
    user_question: str,
    settings: Optional[AgentSettings] = None,
    runtime: Optional[RuntimeState] = None,
) -> Dict[str, bool]:
    """Phase 20A 轻量级意图路由：一次 LLM 调用把问题分类到 Worker 数据维度。

    Router 不分析股票、不调用金融工具（不传 tools），仅以
    response_format=json_object 强制模型输出 JSON 分类结果。
    返回固定 3 字段布尔字典；缺失字段默认为 False。
    解析失败或 API 异常时安全回退为全 True——宁可多调用工具，
    也不能因为路由错误导致数据缺失。

    Phase 23：传入 runtime 时在 Router 调用成功后记录 usage；usage 缺失
    时保持 0，不估算 Token。
    """
    settings = settings or AgentSettings()
    try:
        response = client.chat.completions.create(
            model=settings.model,
            messages=[
                {"role": "system", "content": ROUTER_PROMPT},
                {"role": "user", "content": user_question},
            ],
            response_format={"type": "json_object"},
        )
        if runtime is not None:
            runtime.trace.add_usage(getattr(response, "usage", None))
        content = response.choices[0].message.content or ""
        data = json.loads(content)
        route = {
            "needs_fundamental": bool(data.get("needs_fundamental", False)),
            "needs_quant": bool(data.get("needs_quant", False)),
            "needs_event": bool(data.get("needs_event", False)),
        }
        logger.info("route=%s", route)
        return route
    except Exception as exc:
        logger.warning("路由分类失败，回退全维度: %s: %s", type(exc).__name__, exc)
        return {"needs_fundamental": True, "needs_quant": True, "needs_event": True}


def _select_tool_schemas(
    route: Dict[str, bool],
    tool_schemas: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """按路由裁剪工具 Schema：只保留命中 Worker 域的已知工具（Phase 20A）。

    route 中为 True 的维度对应的 Worker 工具保留；不在 _TOOL_WORKER 中的
    自定义注入 Schema（settings.tool_schemas 注入的差异化工具）始终保留，
    不因路由被丢弃，以维持工具清单按 Agent/请求注入的既有契约。
    不修改原始 TOOL_SCHEMAS / settings.tool_schemas，返回新列表。
    """
    schemas = list(tool_schemas if tool_schemas is not None else TOOL_SCHEMAS)
    active = {
        worker
        for worker, flag in (
            ("fundamental", route.get("needs_fundamental")),
            ("quant", route.get("needs_quant")),
            ("event", route.get("needs_event")),
        )
        if flag
    }
    return [
        schema
        for schema in schemas
        if schema["function"]["name"] not in _TOOL_WORKER
        or _TOOL_WORKER[schema["function"]["name"]] in active
    ]


def run_agent(
    client: OpenAI,
    user_question: str,
    settings: Optional[AgentSettings] = None,
    progress: Optional[Callable[[str], None]] = None,
) -> AgentResult:
    """执行一次完整的 Agent Tool Calling 流程（支持并行与多轮调用）。

    默认静默；传入 progress 回调可收到与 CLI 输出一致的进度文本（逐行）。
    Phase 20A：入口先经 Router 轻量路由分类，再按路由裁剪本轮可用工具
    Schema，随后进入原有单循环 Tool Calling 流程（不创建多 Agent 进程）。
    Phase 21：RuntimeState/RuntimeLimits 保护层在统一入口创建，三个检查点
    （每轮开始前 / 每个 Tool Call 前 / 每次 LLM 请求前）调用 check_limits，
    超限或超时立即停止工具调用与后续生成；Router 与 Final LLM 均计入 llm_calls。
    返回 AgentResult：answer / tool_rounds / tool_calls / max_rounds_reached / error。
    """
    settings = settings or AgentSettings()
    result = AgentResult()
    # Phase 21：运行时保护层。统一创建 RuntimeState / RuntimeLimits（默认限制
    # 8 轮 / 20 次 / 120 秒，均取自 RuntimeLimits，不写死、不读环境变量）。
    runtime = RuntimeState()
    limits = RuntimeLimits()
    reason: Optional[str] = None
    try:
        # Phase 20C 调优：查询级合规门禁（与 _stream_agent_events 一致）。用户问题
        # 本身含违禁措辞时，不进入 Router / 工具 / 生成流程，直接返回合规降级回答。
        query_violations = check_forbidden_patterns(user_question)
        if query_violations:
            logger.warning(
                "查询级合规拦截（%d 项）：%s", len(query_violations), user_question[:80]
            )
            runtime.trace.status = "degraded"
            result.answer = build_degraded_answer(query_violations)
            return result
        # Phase 20A：先轻量路由分类，再按路由裁剪本轮可用工具 Schema。
        # 最终回答仍由 SYSTEM_PROMPT（settings.system_prompt）负责，Router 不参与。
        # Phase 21：Router 是首次 LLM 请求，请求前检查并计入 llm_calls。
        reason = runtime.check_limits(limits)
        if reason is not None:
            result.error = _runtime_abort_error(reason, limits)
            return result
        runtime.llm_calls += 1
        route = _route_question(client, user_question, settings, runtime)
        schemas = _select_tool_schemas(route, settings.tool_schemas)
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": settings.system_prompt},
            {"role": "user", "content": user_question},
        ]
        # Phase 20B：结构化证据只在首次 Tool Calling 完成后注入一次
        evidence_injected = False

        for round_index in range(1, settings.max_tool_rounds + 1):
            # Phase 21：每轮开始前检查，通过后计入 tool_rounds。
            reason = runtime.check_limits(limits)
            if reason is not None:
                break
            runtime.tool_rounds += 1
            # 第一步：调用 DeepSeek，期望模型请求调用工具。
            # Phase 21：每次 LLM 请求前检查，通过后计入 llm_calls。
            reason = runtime.check_limits(limits)
            if reason is not None:
                break
            runtime.llm_calls += 1
            try:
                response = client.chat.completions.create(
                    model=settings.model,
                    # 快照拷贝：调用方/记录方不会观察到后续轮次对 messages 的追加
                    messages=list(messages),
                    tools=schemas,
                )
            except Exception as exc:
                result.error = f"DeepSeek API 调用失败。\n{type(exc).__name__}: {exc}"
                return result
            # Phase 23：Final LLM 调用成功后才记录 usage；缺失时保持 0，不估算。
            runtime.trace.add_usage(getattr(response, "usage", None))

            message = response.choices[0].message

            # 模型不再请求工具：本轮输出即为最终回答。
            if not message.tool_calls:
                result.answer = message.content or ""
                return result

            # 把 assistant 的 tool_call 消息加入对话历史（并行调用为多条）。
            messages.append(
                {
                    "role": "assistant",
                    "content": message.content,
                    "tool_calls": [
                        {
                            "id": tool_call.id,
                            "type": "function",
                            "function": {
                                "name": tool_call.function.name,
                                "arguments": tool_call.function.arguments,
                            },
                        }
                        for tool_call in message.tool_calls
                    ],
                }
            )
            result.tool_rounds = round_index
            if progress:
                progress(f"[第 {round_index} 轮] 模型请求调用 {len(message.tool_calls)} 个工具")

            # 第二步：Python 实际执行所有请求的工具。
            for tool_call in message.tool_calls:
                # Phase 21：每个 Tool Call 前检查，通过后计入 tool_calls。
                reason = runtime.check_limits(limits)
                if reason is not None:
                    break
                runtime.tool_calls += 1
                name = tool_call.function.name
                arguments, parse_error = _parse_arguments(tool_call.function.arguments)
                if parse_error is not None and progress:
                    progress(f"  警告：{name} 参数 JSON 解析失败：{parse_error}")
                if progress:
                    progress(f"  调用 {name}{arguments}")
                # Phase 23：工具耗时埋点。只记录 name/elapsed_seconds/success，
                # 不记录参数、结果与任何隐私数据。
                start = time.monotonic()
                try:
                    tool_result = _execute_tool(name, arguments)
                    tool_success = True
                except Exception:
                    tool_success = False
                    raise
                finally:
                    runtime.trace.tools.append(
                        ToolTrace(
                            name=name,
                            elapsed_seconds=time.monotonic() - start,
                            success=tool_success,
                        )
                    )
                # Phase 21：工具执行异常不崩溃 Runtime，保留 tool_result/error 机制，
                # 记录到 runtime.error（错误已随 tool 消息回传模型，无新增事件）。
                if "error" in tool_result:
                    runtime.error = tool_result["error"]

                result.tool_calls.append(
                    ToolCallRecord(round=round_index, name=name, arguments=arguments, result=tool_result)
                )

                # 第三步：把工具结果以 role="tool" 消息返回给模型（按 tool_call_id 关联）。
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": json.dumps(tool_result, ensure_ascii=False),
                    }
                )

            # Phase 21：工具级检查触发时终止本轮并跳出外层循环（不再请求 Final LLM）。
            if reason is not None:
                break

            # Phase 20B：本轮 Tool Calling 完成后，把结构化证据作为辅助 user 上下文
            # 注入 messages（仅一次）。原始 tool 消息保留，不重复复制完整 Tool Result。
            if not evidence_injected and result.tool_calls:
                evidence_injected = True
                messages.append(_evidence_user_message(result.tool_calls))

        # Phase 21：超时/超限终止时设置错误信息；正常耗尽轮次才标记 max_rounds_reached。
        if runtime.timed_out or runtime.limit_exceeded:
            result.error = _runtime_abort_error(reason, limits)
        else:
            result.max_rounds_reached = True
        return result
    finally:
        _set_run_status(runtime, result)
        _log_runtime_end(runtime)


def collect_tool_results(tool_calls: List[ToolCallRecord]) -> Dict[str, Any]:
    """{工具名: 结果 dict} 容器，满足 Validator 的 tool_results 契约。"""
    return {record.name: record.result for record in tool_calls}


# ---------------------------------------------------------------------------
# Phase 20B：Evidence Context（纯 Python，LLM-free）
# 把 ToolCallRecord 列表转换为 build_evidence_context 所需的 {tool_name, result}
# 序列，渲染为注入最终 synthesis LLM 的辅助 user 消息。
# 不调用 LLM / 不访问金融 API / 不重新计算指标；原始 tool result 原样保留。
# ---------------------------------------------------------------------------

def _collect_evidence_records(tool_calls: List[ToolCallRecord]) -> List[Dict[str, Any]]:
    return [
        {"tool_name": record.name, "result": record.result}
        for record in tool_calls
    ]


def _evidence_user_message(tool_calls: List[ToolCallRecord]) -> Dict[str, Any]:
    """渲染结构化证据并包装为一条 user 消息；作为辅助上下文而非完整结果副本。"""
    evidence_text = render_evidence_context(
        build_evidence_context(_collect_evidence_records(tool_calls))
    )
    return {
        "role": "user",
        "content": (
            "以下是本轮工具调用得到的结构化证据。\n"
            "只能基于这些证据以及已有工具结果进行最终分析。\n"
            "不得补充工具未提供的金融事实。\n\n"
            f"{evidence_text}"
        ),
    }


def _result_dict(result: AgentResult) -> Dict[str, Any]:
    """AgentResult -> dict；tool_calls 保持 round/name/arguments/result 原始结构。"""
    return {
        "answer": result.answer,
        "tool_rounds": result.tool_rounds,
        "tool_calls": [asdict(record) for record in result.tool_calls],
        "max_rounds_reached": result.max_rounds_reached,
        "error": result.error,
    }


def _runtime_abort_error(reason: str, limits: RuntimeLimits) -> str:
    """生成运行时超时/超限终止的错误文案（用于结果 error 与流式 error 事件）。"""
    if reason == "request_timeout":
        return (
            f"请求超过 {limits.request_timeout_seconds:g} 秒超时上限，"
            "已停止工具调用并终止生成。"
        )
    if reason == "tool_round_limit":
        return f"工具调用轮数超过上限（{limits.max_tool_rounds} 轮），已终止生成。"
    if reason == "tool_call_limit":
        return f"工具调用次数超过上限（{limits.max_tool_calls} 次），已终止生成。"
    return "运行时资源限制触发，已终止生成。"


def _set_run_status(runtime: RuntimeState, result: AgentResult) -> None:
    """Phase 23：统一设置 Run 状态。已有降级/错误语义优先保留，其余按优先级
    cancelled > timeout > error > success。仅在仍为 running 时覆盖。"""
    if runtime.trace.status != "running":
        return
    if runtime.cancelled:
        runtime.trace.status = "cancelled"
    elif runtime.timed_out:
        runtime.trace.status = "timeout"
    elif runtime.limit_exceeded or result.error is not None:
        runtime.trace.status = "error"
    else:
        runtime.trace.status = "success"


def _log_runtime_end(runtime: RuntimeState) -> None:
    """Agent 结束结构化日志：记录状态、耗时与 Token/工具统计。

    不记录 Prompt/Response/密钥；Token usage 缺失时保持 0，不估算。
    """
    usage = runtime.trace.usage
    logger.info(
        "agent_end status=%s elapsed_seconds=%.3f llm_calls=%d "
        "prompt_tokens=%d completion_tokens=%d total_tokens=%d "
        "tool_calls=%d tool_rounds=%d",
        runtime.trace.status,
        runtime.elapsed_seconds,
        runtime.llm_calls,
        usage.prompt_tokens,
        usage.completion_tokens,
        usage.total_tokens,
        runtime.tool_calls,
        runtime.tool_rounds,
    )


def _stream_agent_events(
    client: OpenAI,
    user_question: str,
    settings: Optional[AgentSettings] = None,
    stop_event: Optional[threading.Event] = None,
) -> Iterator[Tuple[str, Dict[str, Any]]]:
    """同步生成器：产出 (event_type, payload) 事件元组，供异步包装/测试直接消费。

    事件协议：
    - ("tool_call", {"tool": name, "args": {...}})：即将执行某工具。先于执行发出，
      前端据此展示"执行中"状态（AKShare 调用可能较慢）；
    - ("tool_result", {"tool": name, "status": "ok"|"error"})：工具执行完成；
    - ("token", {"content": str})：最终回答的文本增量（通过合规校验后逐片下发）；
    - ("degraded", {"message": str, "violations": [...]})：最终回答未通过高危
      合规校验（无证据编造/违规荐股），原始结论被拦截，message 为受限降级回答；
    - ("error", {"message": str})：DeepSeek API 调用失败（不抛出，直接产出）；
    - ("__result__", {answer/tool_rounds/tool_calls/max_rounds_reached/error})：
      内部事件，携带完整 AgentResult 快照供流式端点持久化，不下发前端。

    最终回答轮先整体缓冲、再调用 Validator 校验：只有通过才下发 token，命中
    高危违规则下发 degraded 降级回答（真正的拦截发生在内容到达前端之前）。
    stop_event 为客户端断连/主动停止的取消标记：在各轮开头、chunk 循环内、
    工具执行前检查，一旦置位立即 return（不产出 __result__，避免持久化半成品）。
    与 run_agent 共用 _parse_arguments / _execute_tool / SYSTEM_PROMPT，行为一致；
    区别仅在于 create(stream=True) 逐片累积 delta.content / delta.tool_calls。

    Phase 20A：进入主循环前先做一次轻量路由分类（内部非流式 LLM 调用，不传
    tools，不产出任何 SSE 事件），再按路由裁剪工具 Schema。SSE 事件协议保持不变。

    Phase 21：与 run_agent 同源创建 RuntimeState/RuntimeLimits 运行时保护层，
    三个检查点（每轮开始前 / 每个 Tool Call 前 / 每次 LLM 请求前）调用
    check_limits，超时/超限停止工具调用与生成，产出现有 error 语义（不新增
    SSE event type）；客户端停止置位 cancelled；工具异常记录到 runtime.error
    不中断生成；最终回答仍经 Validator 校验。
    """
    settings = settings or AgentSettings()
    result = AgentResult()
    # Phase 21：运行时保护层（与 run_agent 同源创建；默认限制见 RuntimeLimits）。
    runtime = RuntimeState()
    limits = RuntimeLimits()
    reason: Optional[str] = None
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": settings.system_prompt},
        {"role": "user", "content": user_question},
    ]
    # Phase 20B：结构化证据只在首次 Tool Calling 完成后注入一次（不在 token 循环内执行）
    evidence_injected = False

    def _stopped() -> bool:
        stopped = stop_event is not None and stop_event.is_set()
        if stopped:
            # Phase 21：客户端断连/主动停止时记录 cancelled
            runtime.cancelled = True
        return stopped

    try:
        # Phase 20A：断连停止检查优先（stop 前置时零 LLM 调用），随后轻量路由分类、
        # 按路由裁剪工具 Schema。Router 为一次独立非流式 LLM 调用，不传 tools。
        if _stopped():
            return
        # Phase 20C 调优：查询级合规门禁。用户问题本身含违禁措辞（确定性未来预测 /
        # 买卖与仓位建议）时，不进入 Router / 工具 / 生成流程，直接下发合规降级回答，
        # 使合规防线对诱导性提问形成确定性拦截，而非依赖最终回答是否恰好编造数值。
        query_violations = check_forbidden_patterns(user_question)
        if query_violations:
            logger.warning(
                "查询级合规拦截（%d 项）：%s", len(query_violations), user_question[:80]
            )
            runtime.trace.status = "degraded"
            degraded = build_degraded_answer(query_violations)
            result.answer = degraded
            yield ("degraded", {"message": degraded, "violations": query_violations})
            yield ("__result__", _result_dict(result))
            return
        # Phase 21：Router（LLM 请求）前检查，通过后计入 llm_calls。
        reason = runtime.check_limits(limits)
        if reason is not None:
            result.error = _runtime_abort_error(reason, limits)
            yield ("error", {"message": result.error})
            yield ("__result__", _result_dict(result))
            return
        runtime.llm_calls += 1
        route = _route_question(client, user_question, settings, runtime)
        schemas = _select_tool_schemas(route, settings.tool_schemas)

        for round_index in range(1, settings.max_tool_rounds + 1):
            # Phase 21：每轮开始前检查，通过后计入 tool_rounds。
            reason = runtime.check_limits(limits)
            if reason is not None:
                break
            runtime.tool_rounds += 1
            if _stopped():
                return
            # Phase 21：每次 LLM 请求前检查，通过后计入 llm_calls。
            reason = runtime.check_limits(limits)
            if reason is not None:
                break
            runtime.llm_calls += 1
            try:
                stream = client.chat.completions.create(
                    model=settings.model,
                    messages=list(messages),
                    tools=schemas,
                    stream=True,
                )
            except Exception as exc:
                result.error = f"DeepSeek API 调用失败。\n{type(exc).__name__}: {exc}"
                yield ("error", {"message": result.error})
                yield ("__result__", _result_dict(result))
                return

            content_parts: List[str] = []
            # 流式 tool_calls 按 index 分片累积：arguments 为增量拼片，需逐片拼接
            tool_calls_delta: Dict[int, Dict[str, Any]] = {}
            # Phase 23：流式 usage 通常由末片 chunk 携带；缺失时保持 0，不估算。
            stream_usage: Any = None

            for chunk in stream:
                if _stopped():
                    return
                if getattr(chunk, "usage", None):
                    stream_usage = chunk.usage
                if not getattr(chunk, "choices", None):
                    continue
                delta = chunk.choices[0].delta
                if getattr(delta, "content", None):
                    content_parts.append(delta.content)
                if getattr(delta, "tool_calls", None):
                    for tc in delta.tool_calls:
                        slot = tool_calls_delta.setdefault(
                            tc.index, {"id": None, "name": None, "arguments": ""}
                        )
                        if tc.id:
                            slot["id"] = tc.id
                        if tc.function and tc.function.name:
                            slot["name"] = tc.function.name
                        if tc.function and tc.function.arguments:
                            slot["arguments"] += tc.function.arguments

            # Phase 23：Final LLM 流式请求成功完成后记录 usage（成功后才记录）。
            runtime.trace.add_usage(stream_usage)

            # 本轮模型请求的工具调用（与 run_agent 的 assistant message 结构一致）
            assistant_tool_calls = [
                {
                    "id": tool_calls_delta[idx]["id"] or f"call_{idx}",
                    "type": "function",
                    "function": {
                        "name": tool_calls_delta[idx]["name"],
                        "arguments": tool_calls_delta[idx]["arguments"],
                    },
                }
                for idx in sorted(tool_calls_delta)
            ]

            messages.append(
                {
                    "role": "assistant",
                    "content": "".join(content_parts),
                    "tool_calls": assistant_tool_calls,
                }
            )

            # 模型不再请求工具：本轮累积的文本即为最终回答。
            # 先缓冲后校验——通过才逐片下发，命中高危违规则降级拦截。
            if not assistant_tool_calls:
                if _stopped():
                    return
                answer = "".join(content_parts)
                violations = validate_report_critical(
                    answer, collect_tool_results(result.tool_calls)
                )
                if violations:
                    logger.warning(
                        "输出合规校验未通过（%d 项），降级拦截最终回答", len(violations)
                    )
                    runtime.trace.status = "degraded"
                    degraded = build_degraded_answer(violations)
                    result.answer = degraded
                    yield ("degraded", {"message": degraded, "violations": violations})
                else:
                    result.answer = answer
                    for part in content_parts:
                        if _stopped():
                            return
                        yield ("token", {"content": part})
                yield ("__result__", _result_dict(result))
                return

            # 仅当本轮实际调用工具时才计入 tool_rounds（与 run_agent 语义一致）。
            result.tool_rounds = round_index

            # 工具轮前言文本在工具执行前下发；然后执行本轮全部工具：
            # 先发 tool_call（前端展示执行中），执行完发 tool_result。
            for part in content_parts:
                if _stopped():
                    return
                yield ("token", {"content": part})
            for call in assistant_tool_calls:
                # Phase 21：每个 Tool Call 前检查，通过后计入 tool_calls。
                reason = runtime.check_limits(limits)
                if reason is not None:
                    break
                runtime.tool_calls += 1
                if _stopped():
                    return
                name = call["function"]["name"]
                arguments, _ = _parse_arguments(call["function"]["arguments"])
                yield ("tool_call", {"tool": name, "args": arguments})
                # Phase 23：工具耗时埋点。只记录 name/elapsed_seconds/success，
                # 不记录参数、结果与任何隐私数据。
                start = time.monotonic()
                try:
                    tool_result = _execute_tool(name, arguments)
                    tool_success = True
                except Exception:
                    tool_success = False
                    raise
                finally:
                    runtime.trace.tools.append(
                        ToolTrace(
                            name=name,
                            elapsed_seconds=time.monotonic() - start,
                            success=tool_success,
                        )
                    )
                # Phase 21：工具执行异常不中断生成，记录到 runtime.error
                #（错误已随 tool 消息回传模型，无新增 SSE 事件）。
                if "error" in tool_result:
                    runtime.error = tool_result["error"]
                status = "ok" if "error" not in tool_result else "error"
                yield ("tool_result", {"tool": name, "status": status})
                result.tool_calls.append(
                    ToolCallRecord(round=round_index, name=name, arguments=arguments, result=tool_result)
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": json.dumps(tool_result, ensure_ascii=False),
                    }
                )
            # Phase 21：工具级检查触发时终止本轮并跳出外层循环（不再请求 Final LLM）。
            if reason is not None:
                break

            # Phase 20B：本轮 Tool Calling 完成后，把结构化证据作为辅助 user 上下文
            # 注入 messages（仅一次，与 run_agent 一致）。原始 tool 消息保留。
            if not evidence_injected and result.tool_calls:
                evidence_injected = True
                messages.append(_evidence_user_message(result.tool_calls))

        if _stopped():
            return
        # Phase 21：超时/超限 → 现有 error 语义 + __result__，不产出未验证内容。
        if runtime.timed_out or runtime.limit_exceeded:
            result.error = _runtime_abort_error(reason, limits)
            yield ("error", {"message": result.error})
            yield ("__result__", _result_dict(result))
            return
        result.max_rounds_reached = True
        yield ("__result__", _result_dict(result))
    finally:
        _set_run_status(runtime, result)
        _log_runtime_end(runtime)


async def run_agent_streaming(
    client: OpenAI,
    user_question: str,
    settings: Optional[AgentSettings] = None,
    stop_event: Optional[threading.Event] = None,
) -> AsyncIterator[Tuple[str, Dict[str, Any]]]:
    """异步流式包装：同步生成器在工作线程运行，事件经 asyncio.Queue 中继。

    DeepSeek API 与 AKShare 工具均为阻塞调用，直接 await 会阻塞事件循环；
    这里把 _stream_agent_events 放到守护线程，产出的每个事件用
    loop.call_soon_threadsafe 投递回事件循环，异步消费者逐条拿到。
    结束时投递 ("__end__", None) 哨兵，由本生成器自行收尾。

    stop_event：客户端断连（CancelledError/GeneratorExit）或主动停止的取消标记。
    消费者无论正常结束还是被取消，finally 都会置位 stop，通知工作线程在最近
    检查点终止后续 LLM 推理与工具调用，避免后台继续消耗 API token 与系统资源。
    """
    loop = asyncio.get_running_loop()
    queue: "asyncio.Queue[Tuple[str, Dict[str, Any]]]" = asyncio.Queue()
    stop = stop_event or threading.Event()

    def _push(event_type: str, payload: Dict[str, Any]) -> None:
        try:
            loop.call_soon_threadsafe(queue.put_nowait, (event_type, payload))
        except RuntimeError:
            # 事件循环已关闭（客户端断连后任务被取消/销毁）：丢弃剩余事件
            pass

    def _produce() -> None:
        try:
            for event_type, payload in _stream_agent_events(client, user_question, settings, stop):
                _push(event_type, payload)
        except Exception as exc:  # 生成器内部异常兜底，避免线程静默挂掉
            _push("error", {"message": f"{type(exc).__name__}: {exc}"})
        finally:
            _push("__end__", None)

    thread = threading.Thread(target=_produce, daemon=True)
    thread.start()
    try:
        while True:
            event_type, payload = await queue.get()
            if event_type == "__end__":
                return
            yield event_type, payload
    finally:
        # 正常结束 / CancelledError / GeneratorExit（客户端断连）一律置位停止标记
        stop.set()
