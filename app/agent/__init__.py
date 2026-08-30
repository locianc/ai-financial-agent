"""Agent 执行逻辑服务化包。

唯一真源：app/agent/orchestrator.py 持有 SYSTEM_PROMPT / TOOL_SCHEMAS /
TOOL_DISPATCH / MODEL / MAX_TOOL_ROUNDS 以及 run_agent 编排逻辑与
AgentSettings / AgentResult 结构。main.py 从本包再导出，保持旧 import 兼容。
"""

from app.agent.orchestrator import (
    BASE_URL,
    MAX_TOOL_ROUNDS,
    MODEL,
    SYSTEM_PROMPT,
    TOOL_DISPATCH,
    TOOL_SCHEMAS,
    AgentResult,
    AgentSettings,
    ToolCallRecord,
    collect_tool_results,
    create_client,
    run_agent,
    run_agent_streaming,
)

__all__ = [
    "BASE_URL",
    "MAX_TOOL_ROUNDS",
    "MODEL",
    "SYSTEM_PROMPT",
    "TOOL_DISPATCH",
    "TOOL_SCHEMAS",
    "AgentResult",
    "AgentSettings",
    "ToolCallRecord",
    "collect_tool_results",
    "create_client",
    "run_agent",
    "run_agent_streaming",
]
