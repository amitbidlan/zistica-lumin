"""Live demo: Claude extended thinking → Lumin dashboard.

Prereqs:
  1. Lumin running on localhost:8000 (docker run lumin, or
     `uvicorn lumin_api.main:app` from packages/api)
  2. ``ANTHROPIC_API_KEY`` exported
  3. ``pip install anthropic lumin[anthropic]``

Run:
  python examples/test_thinking.py

Then open http://localhost:3000/traces and click the most recent
trace — you should see:
  - claude_call (parent, type=llm)
    - thinking (span_subtype=thinking, brain emoji 🧠, ~N tok)
    - response (span_subtype=response)
  - A thinking-vs-response cost breakdown at the top of the trace
"""

from __future__ import annotations

import os
import time

import lumin
from lumin.integrations.anthropic import instrument_anthropic


def main() -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("Set ANTHROPIC_API_KEY before running this demo.")

    lumin.init(api_url=os.environ.get("LUMIN_API_URL", "http://localhost:8000"))
    instrument_anthropic()

    from anthropic import Anthropic

    client = Anthropic()

    @lumin.trace
    def reason_about(question: str) -> str:
        response = client.messages.create(
            model="claude-opus-4-20250514",
            max_tokens=4000,
            thinking={"type": "enabled", "budget_tokens": 3000},
            messages=[{"role": "user", "content": question}],
        )
        for block in response.content:
            if getattr(block, "type", None) == "text":
                return block.text
        return ""

    answer = reason_about(
        "I have a 3-liter and a 5-liter jug. How can I measure exactly 4 liters?"
    )
    print("Claude answered:")
    print(answer)
    print()

    # Give the SDK a moment to flush spans before the script exits.
    time.sleep(2)
    print("✓ Spans submitted. Check http://localhost:3000/traces")


if __name__ == "__main__":
    main()
