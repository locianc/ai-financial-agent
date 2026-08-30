"""第十阶段：Agent Evaluation 评估系统。

模块结构：
- evaluator.py      评估主入口 + Evaluation JSON Schema + 轻量校验器
- metrics.py        五个评估指标的确定性计算（纯规则，不调用模型）
- judge_prompt.py   LLM-as-Judge 提示词设计（预留层，不调用 API）
- reports/          评估报告 JSON 输出目录

对外导出（from app.evaluation import ...）：
- evaluate / evaluate_case / evaluate_batch：单用例/字典用例/批量评估
- validate_against_schema：轻量 JSON Schema 校验器
- EVALUATION_INPUT_SCHEMA / EVALUATION_OUTPUT_SCHEMA / VIOLATION_SCHEMA /
  EVIDENCE_SCHEMA：Evaluation JSON Schema 定义
- METRIC_WEIGHTS / METRIC_NAMES / PASS_THRESHOLD：指标常量
"""

from .evaluator import (
    EVIDENCE_SCHEMA,
    EVALUATION_INPUT_SCHEMA,
    EVALUATION_OUTPUT_SCHEMA,
    PASS_THRESHOLD,
    VIOLATION_SCHEMA,
    evaluate,
    evaluate_batch,
    evaluate_case,
    validate_against_schema,
)
from .metrics import METRIC_NAMES, METRIC_WEIGHTS

__all__ = [
    "evaluate",
    "evaluate_case",
    "evaluate_batch",
    "validate_against_schema",
    "EVALUATION_INPUT_SCHEMA",
    "EVALUATION_OUTPUT_SCHEMA",
    "VIOLATION_SCHEMA",
    "EVIDENCE_SCHEMA",
    "METRIC_WEIGHTS",
    "METRIC_NAMES",
    "PASS_THRESHOLD",
]
