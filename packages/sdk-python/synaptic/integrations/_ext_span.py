"""Span subclass with the rich fields used by framework integrations.

The SDK's exporter calls ``.to_dict()`` on each span polymorphically;
overriding it here adds these fields without modifying the SDK core
dataclass. The API's ``SpanInput`` already accepts every field on this
subclass — see ``packages/api/models.py``.

Lives in ``synaptic/integrations/`` (not the SDK core) because it's
only relevant when an integration is actually loaded.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

from synaptic.span import Span


@dataclass
class _ExtSpan(Span):
    model: Optional[str] = None
    provider: Optional[str] = None
    tokens_input: Optional[int] = None
    tokens_output: Optional[int] = None
    cost_usd: Optional[float] = None
    tool_name: Optional[str] = None
    # Claude extended-thinking support — separates the thinking phase
    # from the response phase so the dashboard can render and price them
    # independently. span_subtype is "thinking" | "response" | None.
    span_subtype: Optional[str] = None
    thinking_tokens: Optional[int] = None

    def to_dict(self) -> dict:
        d = super().to_dict()
        d["model"] = self.model
        d["provider"] = self.provider
        d["tokens_input"] = self.tokens_input
        d["tokens_output"] = self.tokens_output
        d["cost_usd"] = self.cost_usd
        d["tool_name"] = self.tool_name
        d["span_subtype"] = self.span_subtype
        d["thinking_tokens"] = self.thinking_tokens
        return d


def _serialize(value: Any, max_size: int = 10_240) -> Optional[str]:
    if value is None:
        return None
    try:
        s = json.dumps(value, default=str, ensure_ascii=False)
    except Exception:
        s = str(value)
    return s[:max_size]


def _estimate_tokens(text: str) -> int:
    """Rough estimator: ~4 characters per token for English. Anthropic
    doesn't separately report thinking tokens in the usage object —
    output_tokens rolls up everything. We need an approximation to
    show a useful number on the thinking span; this is intentionally
    conservative and labeled as an estimate in the dashboard."""
    if not text:
        return 0
    return max(1, len(text) // 4)
