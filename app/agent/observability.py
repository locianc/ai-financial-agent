from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class LLMUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def add(self, usage: Any) -> None:
        if usage is None:
            return

        def value(name: str) -> int:
            value = getattr(usage, name, None)
            if value is None and isinstance(usage, dict):
                value = usage.get(name)
            try:
                return int(value or 0)
            except (TypeError, ValueError):
                return 0

        self.prompt_tokens += value("prompt_tokens")
        self.completion_tokens += value("completion_tokens")
        self.total_tokens += value("total_tokens")

    def snapshot(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass
class ToolTrace:
    name: str
    elapsed_seconds: float = 0.0
    success: bool = True

    def snapshot(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "success": self.success,
        }


@dataclass
class RunTrace:
    usage: LLMUsage = field(default_factory=LLMUsage)
    tools: list[ToolTrace] = field(default_factory=list)
    status: str = "running"

    def add_usage(self, usage: Any) -> None:
        self.usage.add(usage)

    def snapshot(self, runtime: Any) -> dict[str, Any]:
        return {
            "runtime": runtime.snapshot(),
            "llm_usage": self.usage.snapshot(),
            "tools": [tool.snapshot() for tool in self.tools],
            "status": self.status,
        }
