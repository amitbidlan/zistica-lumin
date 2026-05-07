"""Llama Guard 4 — Slice 3 PR H / spec §6.5.

Meta's Llama-Guard-4 (12B) is a content-safety classifier that
labels text against the MLCommons hazard taxonomy: 14 harm
categories like violent crimes, sex crimes, child sexual exploitation,
hate, self-harm, weapons of mass destruction, code-interpreter abuse,
etc. Use it on the **response side** to catch model output that the
upstream LLM should have refused.

Unlike Prompt Guard 2 (22M, CPU, <5ms), Llama Guard is large enough
that GPU is recommended for production traffic. CPU inference works
but takes ~500-1500ms per call, which is too slow for a synchronous
``before_proxy_call`` rule. Operators wire it into ``after_proxy_call``
or ``after_tool_call`` (the verb engine treats slow detectors as a
budget concern — see §10.2).

Optional dep — install with::

    pip install transformers torch accelerate

Not in the default install:
  - torch is large (~700MB), accelerate adds another ~50MB
  - The 12B model is ~24GB on disk, ~14GB resident in fp16
  - Most operators will run it as a hosted endpoint instead
    (separate ``llama_guard_endpoint`` builtin lands in PR H+1
    once we have a stable endpoint contract)

Graceful degradation (Rule 7):
  - dep missing → ``available = False``, classify returns ``{}``
  - model load fails → latched, returns ``{}`` forever after
  - inference fails → returns ``{}`` for that one call

Configuration:

  LUMIN_LLAMA_GUARD_MODEL — HF model id, defaults to
                             ``meta-llama/Llama-Guard-4-12B``
  LUMIN_LLAMA_GUARD_DEVICE — ``cuda`` (default when CUDA available)
                             or ``cpu``. Auto-detected.
  LUMIN_LLAMA_GUARD_DTYPE — ``float16`` (default), ``bfloat16``, or
                             ``float32``
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Dict, List, Optional

logger = logging.getLogger("lumin.api.firewall.detectors.llama_guard")


# MLCommons hazard category codes (Llama Guard 4 taxonomy). Surfaced
# so operators can write rules like
# ``"S2" in llama_guard_classify(Output.text).get("categories", [])``
# without grepping through Meta's docs. Keeping these as constants
# means a future taxonomy update is one place to change.
HAZARD_CATEGORIES: Dict[str, str] = {
    "S1": "Violent Crimes",
    "S2": "Non-Violent Crimes",
    "S3": "Sex Crimes",
    "S4": "Child Sexual Exploitation",
    "S5": "Defamation",
    "S6": "Specialized Advice",  # legal / medical / financial
    "S7": "Privacy",
    "S8": "Intellectual Property",
    "S9": "Indiscriminate Weapons",
    "S10": "Hate",
    "S11": "Suicide & Self-Harm",
    "S12": "Sexual Content",
    "S13": "Elections",
    "S14": "Code Interpreter Abuse",
}


# Detect availability without importing the heavy modules.
try:
    import importlib.util as _ispec
    available = (
        _ispec.find_spec("transformers") is not None
        and _ispec.find_spec("torch") is not None
    )
except Exception:
    available = False


_MODEL_NAME = os.environ.get(
    "LUMIN_LLAMA_GUARD_MODEL", "meta-llama/Llama-Guard-4-12B"
)
_DEVICE_OVERRIDE = os.environ.get("LUMIN_LLAMA_GUARD_DEVICE")  # auto if None
_DTYPE = os.environ.get("LUMIN_LLAMA_GUARD_DTYPE", "float16").lower()

_model = None
_tokenizer = None
_model_lock = threading.Lock()
_load_failed = False


def _resolve_device() -> str:
    """Pick the right device. Honor explicit override if set, else
    use CUDA when available, else CPU. Logged on first load so
    operators can see what they got."""
    if _DEVICE_OVERRIDE:
        return _DEVICE_OVERRIDE
    try:
        import torch  # type: ignore[import-not-found]
        if torch.cuda.is_available():
            return "cuda"
    except Exception:
        pass
    return "cpu"


def _resolve_dtype():
    """Resolve the dtype string to a torch dtype. Default float16 —
    halves memory vs float32, matches Meta's recommendation. Falls
    back to float32 on parse error."""
    try:
        import torch  # type: ignore[import-not-found]
        return {
            "float16": torch.float16,
            "fp16": torch.float16,
            "bfloat16": torch.bfloat16,
            "bf16": torch.bfloat16,
            "float32": torch.float32,
            "fp32": torch.float32,
        }.get(_DTYPE, torch.float16)
    except Exception:
        return None  # Fallback to default — will be inferred by from_pretrained


def _ensure_loaded() -> bool:
    """Lazy load. Idempotent + lock-guarded."""
    global _model, _tokenizer, _load_failed
    if _model is not None and _tokenizer is not None:
        return True
    if _load_failed:
        return False
    if not available:
        return False
    with _model_lock:
        if _model is not None and _tokenizer is not None:
            return True
        if _load_failed:
            return False
        try:
            from transformers import (  # type: ignore[import-not-found]
                AutoModelForCausalLM,
                AutoTokenizer,
            )
            device = _resolve_device()
            dtype = _resolve_dtype()
            _tokenizer = AutoTokenizer.from_pretrained(_MODEL_NAME)
            kwargs: Dict[str, Any] = {"device_map": device}
            if dtype is not None:
                kwargs["torch_dtype"] = dtype
            _model = AutoModelForCausalLM.from_pretrained(_MODEL_NAME, **kwargs)
            logger.info(
                "llama_guard: loaded %s on %s (%s)",
                _MODEL_NAME, device, _DTYPE,
            )
            return True
        except Exception:
            logger.exception(
                "llama_guard: failed to load model %s — detector disabled "
                "for this process. Operators wanting Llama Guard typically "
                "run it as a hosted endpoint; CPU inference of the 12B "
                "model is too slow for synchronous policy evaluation. "
                "Set LUMIN_LLAMA_GUARD_MODEL to a smaller variant, or "
                "wait for the hosted-endpoint builtin landing in a "
                "follow-up.",
                _MODEL_NAME,
            )
            _load_failed = True
            return False


def _parse_classifier_output(text: str) -> Dict[str, Any]:
    """Llama Guard's expected output is::

        safe

    or::

        unsafe
        S1,S10

    First token is ``safe`` / ``unsafe``; second line (when unsafe)
    is a comma-separated list of hazard category codes. We surface
    a structured dict so policies can inspect either field:

        {"safe": False, "categories": ["S1", "S10"], "names": ["Violent Crimes", "Hate"]}

    Returns the safe-by-default ``{"safe": True, "categories": [], "names": []}``
    on parse failure — Rule 7.
    """
    safe_default: Dict[str, Any] = {"safe": True, "categories": [], "names": []}
    if not text:
        return safe_default
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    if not lines:
        return safe_default
    verdict = lines[0].lower()
    if verdict.startswith("safe"):
        return {"safe": True, "categories": [], "names": []}
    if not verdict.startswith("unsafe"):
        return safe_default
    cats: List[str] = []
    if len(lines) > 1:
        cats = [c.strip().upper() for c in lines[1].split(",") if c.strip()]
    names = [HAZARD_CATEGORIES.get(c, c) for c in cats]
    return {"safe": False, "categories": cats, "names": names}


# ---- public API -----------------------------------------------------------


def llama_guard_classify(text: Optional[str]) -> Dict[str, Any]:
    """Classify ``text`` against the MLCommons hazard taxonomy.

    Returns a dict::

        {
          "safe": bool,
          "categories": ["S1", "S10", ...],
          "names": ["Violent Crimes", "Hate", ...]
        }

    Safe defaults when:
      - text is None / empty: ``{"safe": True, "categories": [], "names": []}``
      - dep not installed: same
      - model load failed: same
      - inference threw: same

    The "always safe by default" return is critical — Rule 7 says a
    policy referencing this builtin must never be able to false-fire
    on operators who didn't opt in to the heavy ML deps.
    """
    safe_default: Dict[str, Any] = {"safe": True, "categories": [], "names": []}
    if not text or not isinstance(text, str):
        return safe_default
    if not _ensure_loaded():
        return safe_default
    try:
        # Prompt template — Llama Guard 4 uses chat-format with a
        # system role describing the taxonomy and an assistant role
        # for the verdict. The tokenizer's chat template handles the
        # specifics; we just pass role+content.
        snippet = text[:8000]  # 8K cap; tokenizer truncates further
        messages = [{"role": "user", "content": snippet}]
        input_ids = _tokenizer.apply_chat_template(  # type: ignore[union-attr]
            messages, return_tensors="pt"
        )
        try:
            input_ids = input_ids.to(_model.device)  # type: ignore[union-attr]
        except Exception:
            pass  # CPU / device_map="cpu" — already on the right device
        output = _model.generate(  # type: ignore[union-attr]
            input_ids=input_ids,
            max_new_tokens=64,
            pad_token_id=getattr(_tokenizer, "eos_token_id", None),  # type: ignore[union-attr]
            do_sample=False,
        )
        prompt_len = input_ids.shape[-1]
        response_ids = output[0][prompt_len:]
        response_text = _tokenizer.decode(  # type: ignore[union-attr]
            response_ids, skip_special_tokens=True
        )
        return _parse_classifier_output(response_text)
    except Exception:
        logger.exception("llama_guard: inference failed")
        return safe_default


def llama_guard_unsafe(text: Optional[str]) -> bool:
    """Convenience boolean — True iff the classifier says ``text``
    is unsafe in any category. Suitable for direct rule conditions::

        action: block
        condition: llama_guard_unsafe(Output.text)
    """
    result = llama_guard_classify(text)
    return not result.get("safe", True)


def llama_guard_categories(text: Optional[str]) -> List[str]:
    """Return the list of hazard category codes (e.g. ``["S1", "S10"]``)
    Llama Guard flagged for ``text``. Empty list when safe / unavailable.
    """
    return list(llama_guard_classify(text).get("categories", []))


def reset_for_tests() -> None:
    """Test helper — clear the loaded model + the load-failed latch."""
    global _model, _tokenizer, _load_failed
    _model = None
    _tokenizer = None
    _load_failed = False
