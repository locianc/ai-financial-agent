"""Evidence context aggregation for the multi-agent architecture.

This module is deliberately LLM-free.
It only organizes existing tool results into a structured,
traceable context for the final synthesis model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class EvidenceItem:
    """One piece of tool-derived evidence."""

    tool_name: str
    domain: str
    data: Any
    source: str | None = None
    data_time: str | None = None
    fetched_at: str | None = None


@dataclass
class EvidenceContext:
    """Structured evidence collected from tool results."""

    fundamental: list[EvidenceItem] = field(default_factory=list)
    quant: list[EvidenceItem] = field(default_factory=list)
    event: list[EvidenceItem] = field(default_factory=list)
    other: list[EvidenceItem] = field(default_factory=list)

    def all_items(self) -> list[EvidenceItem]:
        return [
            *self.fundamental,
            *self.quant,
            *self.event,
            *self.other,
        ]


_TOOL_DOMAIN = {
    "get_stock_fundamentals": "fundamental",
    "get_valuation_analysis": "fundamental",
    "get_stock_price": "quant",
    "get_technical_analysis": "quant",
    "get_stock_news": "event",
}


def _first_value(
    data: Mapping[str, Any],
    *keys: str,
) -> str | None:
    for key in keys:
        value = data.get(key)
        if value is not None and value != "":
            return str(value)
    return None


def build_evidence_context(
    tool_results: Sequence[Mapping[str, Any]],
) -> EvidenceContext:
    """Convert existing tool results into domain-grouped evidence.

    No network calls, LLM calls, or recalculation occur here.
    """

    context = EvidenceContext()

    for result in tool_results:
        tool_name = str(result.get("tool_name", "unknown"))
        data = result.get("result")

        if data is None:
            data = result.get("data")

        if data is None:
            data = result

        domain = _TOOL_DOMAIN.get(tool_name, "other")

        source = None
        data_time = None
        fetched_at = None

        if isinstance(data, Mapping):
            source = _first_value(
                data,
                "source",
                "provider",
                "news_source",
            )
            data_time = _first_value(
                data,
                "data_time",
                "data_date",
                "trade_date",
                "date",
                "report_period",
                "published_at",
                "publish_time",
            )
            fetched_at = _first_value(
                data,
                "fetched_at",
                "fetch_time",
                "retrieved_at",
            )

        item = EvidenceItem(
            tool_name=tool_name,
            domain=domain,
            data=data,
            source=source,
            data_time=data_time,
            fetched_at=fetched_at,
        )

        if domain == "fundamental":
            context.fundamental.append(item)
        elif domain == "quant":
            context.quant.append(item)
        elif domain == "event":
            context.event.append(item)
        else:
            context.other.append(item)

    return context


def render_evidence_context(context: EvidenceContext) -> str:
    """Render structured evidence for the final synthesis model."""

    sections: list[str] = []

    def render_section(
        title: str,
        items: list[EvidenceItem],
    ) -> None:
        if not items:
            return

        lines = [f"【{title}】"]

        for index, item in enumerate(items, start=1):
            lines.append(
                f"[Evidence {index}] "
                f"tool={item.tool_name}"
            )

            if item.source:
                lines.append(f"source={item.source}")

            if item.data_time:
                lines.append(f"data_time={item.data_time}")

            if item.fetched_at:
                lines.append(f"fetched_at={item.fetched_at}")

            lines.append(f"data={item.data!r}")

        sections.append("\n".join(lines))

    render_section("基本面证据", context.fundamental)
    render_section("量化证据", context.quant)
    render_section("事件证据", context.event)
    render_section("其他证据", context.other)

    if not sections:
        return "【证据上下文】\n暂无工具证据。"

    return "\n\n".join(sections)
