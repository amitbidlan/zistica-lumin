"""Detector modules — see §6 of AGENT_FIREWALL_SPEC.md.

Each detector implements an opt-in capability the firewall engine can
draw on:

  - regex_pack: pure-Python pattern library, always available
  - simpleeval (in firewall.builtins): functions exposed inside
    policy expressions
  - presidio (later slice): semantic PII detection
  - prompt_guard_2 (later slice): injection / jailbreak classifier
  - llama_guard (later slice): content-safety classifier
  - embedding (later slice): faiss similarity vs known-bad corpus
  - llm_judge (later slice): LLM-as-judge with cheap models
  - local_classifier (later slice): operator-trained classifier

Detectors that require optional ML deps must degrade gracefully when
those deps aren't installed — set ``available = False`` at module
load and have policy builtins return safe defaults (typically 0.0
or empty dict). Rule 7: a missing classifier never blocks an agent.
"""
