"""第十阶段：LLM-as-Judge 评估提示词设计（预留层，不调用 API）。

确定性指标（metrics.py）只能覆盖可被规则/数值匹配的维度；对语义类评估
（如措辞恰当性、结论与证据的因果关系、表达专业度）预留本模块：

- JUDGE_SYSTEM_PROMPT：发给评审模型（Judge LLM）的系统提示词，含五个
  指标的打分 Rubric、权重说明与严格 JSON 输出要求；
- build_judge_messages(question, tool_results, agent_output)：组装
  [system, user] 消息列表，调用方自行请求模型（本模块不发起网络请求）；
- parse_judge_response(raw_text)：解析模型返回文本为 dict，容忍
  ```json 代码围栏与前后缀噪声；解析失败返回 {}。

第一阶段仅完成设计与消息组装，不实际调用任何模型 API。
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List

# 与 metrics.METRIC_WEIGHTS / METRIC_NAMES 保持一致，避免双份漂移。
from .metrics import METRIC_NAMES, METRIC_WEIGHTS

JUDGE_SYSTEM_PROMPT: str = (
    "你是一名严谨的金融研究报告评审专家，负责对 AI 金融助手的单次回答"
    "（agent_output）做语义质量评审。评审对象是：用户在 question 中提出的问题、"
    "助手实际获得并依赖的工具返回数据（tool_results，JSON）、助手基于这些数据"
    "生成的最终回答。\n\n"
    "评审必须严格基于给定的 tool_results，不得假设任何未提供的市场数据。\n\n"
    "【评审指标（每个指标满分 100，权重见括号）】\n"
    f"1. 数据准确性 data_accuracy（权重 {METRIC_WEIGHTS['data_accuracy']}）："
    "回答中的数值与 tool_results 是否一致；是否存在工具结果之外的编造数值。\n"
    f"2. 证据链一致性 evidence_grounding（权重 {METRIC_WEIGHTS['evidence_grounding']}）："
    "每个断言（尤其定性结论）是否可追溯到 tool_results 中的具体数值/字段；"
    "工具未返回的字段是否被如实说明缺失而非编造。\n"
    f"3. 时间属性一致性 temporal_alignment（权重 {METRIC_WEIGHTS['temporal_alignment']}）："
    "是否严格区分 fetched_at（获取时刻）与 market_date/report_period/data_date；"
    "是否存在用未来时间信息解释过去变动的逻辑错误。\n"
    f"4. 合规风险 compliance（权重 {METRIC_WEIGHTS['compliance']}）："
    "是否出现确定性未来预测、买卖/仓位建议、保证收益等违禁表达；"
    "是否具备完整结构并注明数据来源。\n"
    f"5. 用户意图理解 intent_understanding（权重 {METRIC_WEIGHTS['intent_understanding']}）："
    "回答是否聚焦 question 涉及的标的与维度，是否答非所问。\n\n"
    "【评分要求】\n"
    "- 每个指标给出 0-100 的整数分数：90-100 优秀、75-89 良好、60-74 及格、0-59 不合格。\n"
    "- 每项评分必须附 1-3 条 evidence（引用回答原文或 tool_results 字段作证据）。\n"
    "- 若工具未返回某维度数据而回答如实说明缺失，不应扣分；编造才扣分。\n"
    "- 不得因为金融术语或保守表达而扣分；只评审质量与合规，不评价投资观点本身。\n\n"
    "【输出格式】\n"
    "只输出一个 JSON 对象，不要输出任何解释性文字，不要使用 Markdown 代码围栏：\n"
    "{\n"
    '  "score": {"overall": <0-100 整数，按权重加权后的总分>,\n'
    '            "dimensions": [{"key": "data_accuracy", "name": "数据准确性", "score": 85}, ...]},\n'
    '  "violations": [{"metric": "<指标 key>", "severity": "high|medium|low",\n'
    '                  "code": "<建议使用与确定性指标一致的错误码或 JUDGE_ 前缀>",\n'
    '                  "message": "<说明>", "evidence": "<引用原文>"}],\n'
    '  "evidence": [{"metric": "<指标 key>", "kind": "semantic", "detail": "<说明>"}],\n'
    '  "suggestions": ["<改进建议>"]\n'
    "}\n"
    "若输入非法（如 agent_output 为空），输出 violations 中给出 "
    '"code": "INPUT_SCHEMA_ERROR" 的条目并把各指标分数置 0。'
)

OUTPUT_EXAMPLE_JSON: Dict[str, Any] = {
    "score": {
        "overall": 78,
        "dimensions": [
            {"key": "data_accuracy", "name": "数据准确性", "score": 90},
            {"key": "evidence_grounding", "name": "证据链一致性", "score": 75},
            {"key": "temporal_alignment", "name": "时间属性一致性", "score": 100},
            {"key": "compliance", "name": "合规风险", "score": 60},
            {"key": "intent_understanding", "name": "用户意图理解", "score": 85},
        ],
    },
    "violations": [
        {
            "metric": "compliance",
            "severity": "high",
            "code": "FORBIDDEN_PATTERN",
            "message": "回答包含对未来行情的确定性预测，涉嫌投资建议",
            "evidence": "明天大概率上涨",
        }
    ],
    "evidence": [
        {
            "metric": "data_accuracy",
            "kind": "semantic",
            "detail": "回答引用的 RSI14 数值与 tool_results 一致",
        }
    ],
    "suggestions": ["将确定性预测改为中性描述并补充数据证据"],
}


def build_judge_messages(
    question: str,
    tool_results: Dict[str, Any],
    agent_output: str,
    *,
    extra_context: str = "",
) -> List[Dict[str, str]]:
    """组装 LLM-as-Judge 的 [system, user] 消息列表。

    tool_results 与 agent_output 序列化为 JSON 文本放入 user 消息；
    调用方拿到消息后自行调用模型 API，本函数不发起网络请求。
    """
    user_content: List[str] = ["请依据评审规则对以下用例打分。"]
    if extra_context:
        user_content.append(extra_context)
    user_content.append(f"【用户问题】\n{question}\n")
    user_content.append(
        "【工具返回数据（tool_results，JSON）】\n"
        + json.dumps(tool_results, ensure_ascii=False, indent=2)
    )
    user_content.append(f"【助手回答（agent_output）】\n{agent_output}\n")
    user_content.append("请输出评分 JSON。")
    return [
        {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(user_content)},
    ]


def parse_judge_response(raw_text: str) -> Dict[str, Any]:
    """把 Judge LLM 的返回文本解析为 dict。

    容忍 ```json ... ``` 代码围栏、Markdown 前后缀、空白与首尾噪声；
    通过平衡花括号定位第一个完整 JSON 对象；解析失败返回 {}。
    """
    text = raw_text.strip()
    if not text:
        return {}

    # 去掉 ```json ... ``` 围栏（不区分大小写，允许 lang 字段缺失）
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()

    start = text.find("{")
    if start < 0:
        return {}
    depth = 0
    end = -1
    in_string = False
    escape = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                end = index + 1
                break
    if end < 0:
        return {}
    try:
        parsed = json.loads(text[start:end])
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
