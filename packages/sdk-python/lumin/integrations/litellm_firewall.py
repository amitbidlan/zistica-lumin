"""LiteLLM firewall integration (§9.5).

LiteLLM has a guardrails surface — ``CustomGuardrail`` — that fires
on every completion request and response. Lumin's adapter
implements the contract directly so operators don't have to go
through LiteLLM's hosted guardrails marketplace.

Two integration paths:

  1. **Guardrail class** — register
     ``LuminFirewallGuardrail`` with LiteLLM Proxy via guardrails
     config. Best for hosted-proxy deployments.

  2. **Manual call** — for direct ``litellm.completion()`` users,
     ``check_request()`` and ``check_response()`` are sync
     functions you call before / after.

Mapping:
  pre-call  → before_proxy_call decide on the user messages
  post-call → after_proxy_call decide on the assistant response

Block / require_approval at the pre-call boundary raises
``LuminFirewallBlocked`` so LiteLLM raises 4xx to the caller.
Rewrite verbs at post-call swap the response content.

Usage (manual):

    from lumin.integrations.litellm_firewall import LuminFirewallGuardrail
    g = LuminFirewallGuardrail(project="bot")
    g.check_request(messages=[{"role": "user", "content": "..."}])
    response = litellm.completion(...)
    response = g.check_response(messages=..., response=response)
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from lumin.firewall_client import (
    DecideRequest,
    DecideResponse,
    FirewallClient,
)

logger = logging.getLogger("lumin.integrations.litellm_firewall")


class LuminFirewallBlocked(Exception):
    def __init__(self, decision: DecideResponse, *, lifecycle: str) -> None:
        self.decision = decision
        self.lifecycle = lifecycle
        super().__init__(
            f"Lumin firewall {decision.decision} at {lifecycle}: "
            f"{decision.policy_name or '_engine_'}"
        )


class LuminFirewallGuardrail:
    """LiteLLM guardrail that gates pre-call + post-call.

    Designed to be importable even when ``litellm`` itself isn't
    installed — operators using ``check_request()`` / ``check_response()``
    in standalone code don't need the full LiteLLM dependency. The
    optional CustomGuardrail subclass below activates only when
    ``litellm`` is available.
    """

    def __init__(
        self,
        *,
        host: Optional[str] = None,
        project: Optional[str] = None,
        agent: Optional[str] = None,
        timeout_ms: int = 75,
        on_error: str = "allow",
    ) -> None:
        self._client = FirewallClient(
            host=host, project=project, agent=agent,
            timeout_ms=timeout_ms, on_error=on_error,  # type: ignore[arg-type]
        )

    def check_request(
        self,
        *,
        messages: List[Dict[str, Any]],
        session_id: Optional[str] = None,
        model: Optional[str] = None,
    ) -> None:
        """Run before_proxy_call decide. Raises on block."""
        decision = self._client.decide(DecideRequest(
            lifecycle="before_proxy_call",
            messages=messages,
            session_id=session_id,
            model=model,
        ))
        if decision.is_block or decision.needs_approval:
            raise LuminFirewallBlocked(decision, lifecycle="before_proxy_call")

    def check_response(
        self,
        *,
        messages: List[Dict[str, Any]],
        response: Any,
        session_id: Optional[str] = None,
        model: Optional[str] = None,
    ) -> Any:
        """Run after_proxy_call decide. Returns the (possibly
        rewritten) response. Raises on block."""
        text = _extract_text(response)
        decision = self._client.decide(DecideRequest(
            lifecycle="after_proxy_call",
            messages=messages,
            output={"text": text},
            session_id=session_id,
            model=model,
        ))
        if decision.is_block or decision.needs_approval:
            raise LuminFirewallBlocked(decision, lifecycle="after_proxy_call")
        if decision.is_rewrite:
            new_text = decision.rewritten.get("result")
            if isinstance(new_text, str):
                response = _replace_text(response, new_text)
        return response

    def close(self) -> None:
        self._client.close()


def _extract_text(response: Any) -> str:
    """LiteLLM normalises response.choices[0].message.content."""
    try:
        return response["choices"][0]["message"]["content"] or ""
    except Exception:
        pass
    try:
        return response.choices[0].message.content or ""
    except Exception:
        return ""


def _replace_text(response: Any, new_text: str) -> Any:
    """Mutate the response in place when the firewall rewrites
    output. Both dict and dotted-attribute shapes supported."""
    try:
        response["choices"][0]["message"]["content"] = new_text
        return response
    except Exception:
        pass
    try:
        response.choices[0].message.content = new_text
    except Exception:
        pass
    return response


def make_litellm_guardrail():
    """Factory that returns a ``CustomGuardrail`` subclass when
    LiteLLM is installed. Returns None otherwise so the import
    surface is non-fatal."""
    try:
        from litellm.integrations.custom_guardrail import CustomGuardrail  # type: ignore
    except ImportError:  # pragma: no cover
        return None

    class _Wrapped(CustomGuardrail):  # type: ignore[misc]
        """LiteLLM guardrails subclass that delegates to the
        Lumin firewall. Configure via litellm proxy yaml:

            guardrails:
              - guardrail_name: lumin_firewall
                litellm_params:
                  guardrail: lumin.integrations.litellm_firewall.make_litellm_guardrail
                  mode: pre_call
        """

        def __init__(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            super().__init__(*args, **kwargs)
            self._lumin = LuminFirewallGuardrail()

        async def async_pre_call_hook(self, *, data, **kwargs):  # type: ignore[no-untyped-def]
            self._lumin.check_request(
                messages=data.get("messages") or [],
                session_id=data.get("user"),
                model=data.get("model"),
            )
            return data

        async def async_post_call_success_hook(self, *, data, response, **kwargs):  # type: ignore[no-untyped-def]
            return self._lumin.check_response(
                messages=data.get("messages") or [],
                response=response,
                session_id=data.get("user"),
                model=data.get("model"),
            )

    return _Wrapped


__all__ = [
    "LuminFirewallBlocked",
    "LuminFirewallGuardrail",
    "make_litellm_guardrail",
]
