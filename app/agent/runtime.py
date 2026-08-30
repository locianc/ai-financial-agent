from __future__ import annotations

from dataclasses import dataclass, field
from time import monotonic
from typing import Any

from app.agent.observability import RunTrace

DEFAULT_MAX_TOOL_ROUNDS = 8
DEFAULT_MAX_TOOL_CALLS = 20
DEFAULT_REQUEST_TIMEOUT_SECONDS = 120.0


@dataclass
class RuntimeLimits:
    max_tool_rounds: int = DEFAULT_MAX_TOOL_ROUNDS
    max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS
    request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS


@dataclass
class RuntimeState:
    started_at: float = field(default_factory=monotonic)
    tool_rounds: int = 0
    tool_calls: int = 0
    llm_calls: int = 0
    cancelled: bool = False
    timed_out: bool = False
    limit_exceeded: bool = False
    error: str | None = None
    trace: RunTrace = field(default_factory=RunTrace)

    @property
    def elapsed_seconds(self) -> float:
        return monotonic() - self.started_at

    def check_limits(self, limits: RuntimeLimits) -> str | None:
        if self.tool_rounds >= limits.max_tool_rounds:
            self.limit_exceeded = True
            return "tool_round_limit"

        if self.tool_calls >= limits.max_tool_calls:
            self.limit_exceeded = True
            return "tool_call_limit"

        if self.elapsed_seconds >= limits.request_timeout_seconds:
            self.timed_out = True
            return "request_timeout"

        return None

    def snapshot(self) -> dict[str, Any]:
        return {
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "tool_rounds": self.tool_rounds,
            "tool_calls": self.tool_calls,
            "llm_calls": self.llm_calls,
            "cancelled": self.cancelled,
            "timed_out": self.timed_out,
            "limit_exceeded": self.limit_exceeded,
            "error": self.error,
        }

    def snapshot_with_trace(self) -> dict[str, Any]:
        return {**self.snapshot(), "trace": self.trace.snapshot(self)}
