"""Live end-to-end demo: a real LlamaIndex query routed through the
Synaptic callback handler. Uses a stub LLM + stub embeddings so the
demo runs without API keys; the trace shape is identical to a real
agent run.

Prereqs:
  1. Synaptic running on localhost:8000
  2. pip install synaptic[llama_index]

Run:
  python examples/test_llama_index.py

Then open http://localhost:3000/traces — you should see:
  - QUERY (root)
    - RETRIEVE (with retrieved nodes + similarity scores)
    - SYNTHESIZE
      - LLM call (with model, tokens, cost)
"""

from __future__ import annotations

import os
import time
from typing import Any, List, Sequence

import synaptic
from synaptic.integrations.llama_index import SynapticCallbackHandler


def main() -> None:
    synaptic.configure(
        host=os.environ.get("SYNAPTIC_HOST", "http://localhost:8000"),
        flush_interval=0.5,
    )

    from llama_index.core import Document, Settings, VectorStoreIndex
    from llama_index.core.callbacks import CallbackManager
    from llama_index.core.base.embeddings.base import BaseEmbedding
    from llama_index.core.llms.mock import MockLLM

    handler = SynapticCallbackHandler()
    Settings.callback_manager = CallbackManager([handler])

    # Stub embedder — deterministic 8-dim vectors.
    class StubEmbedding(BaseEmbedding):
        def _hash(self, text: str) -> List[float]:
            import hashlib

            h = hashlib.sha256(text.encode("utf-8")).digest()
            return [b / 255.0 for b in h[:8]]

        def _get_query_embedding(self, query: str) -> List[float]:
            return self._hash(query)

        def _get_text_embedding(self, text: str) -> List[float]:
            return self._hash(text)

        async def _aget_query_embedding(self, query: str) -> List[float]:
            return self._hash(query)

        async def _aget_text_embedding(self, text: str) -> List[float]:
            return self._hash(text)

    # MockLLM ships with llama-index-core and triggers proper LLM
    # callback events — unlike a hand-rolled CustomLLM whose chat()
    # bypasses the instrumentation wrapper.
    Settings.llm = MockLLM(max_tokens=64)
    Settings.embed_model = StubEmbedding()

    # Tiny corpus, build the index, ask one question
    docs = [
        Document(text="Paris is the capital of France."),
        Document(text="Tokyo is the capital of Japan."),
        Document(text="The Eiffel Tower is in Paris."),
    ]
    index = VectorStoreIndex.from_documents(docs)
    query_engine = index.as_query_engine()
    response = query_engine.query("What is the capital of France?")
    print(f"LlamaIndex answered: {response}")

    time.sleep(2)
    print("✓ Spans submitted. Open http://localhost:3000/traces to see the trace.")


if __name__ == "__main__":
    main()
