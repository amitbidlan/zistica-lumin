"""Tests for the Llama Guard 4 detector — Slice 3 Tier 2.3.

Almost all tests run unconditionally — the model itself is too heavy
to download in CI (24GB on disk, 14GB resident in fp16), so we stub
the model + tokenizer and exercise the pipeline plumbing + parser.

The single real-model smoke test is gated behind both the optional
dep being installed AND an explicit env opt-in, so accidentally
running ``pytest`` in a transformer-equipped dev box doesn't trigger
a multi-GB download.
"""

from __future__ import annotations

import os

import pytest

from firewall.detectors import llama_guard as lg


pytestmark = pytest.mark.filterwarnings("ignore")


# --- always-on graceful-degradation tests ----------------------------------


def test_classify_for_none_or_empty() -> None:
    """Falsy / non-str input → safe-by-default. No model load."""
    assert lg.llama_guard_classify(None) == {
        "safe": True, "categories": [], "names": []
    }
    assert lg.llama_guard_classify("") == {
        "safe": True, "categories": [], "names": []
    }
    assert lg.llama_guard_classify(123) == {  # type: ignore[arg-type]
        "safe": True, "categories": [], "names": []
    }


def test_unsafe_helper_for_falsy_input() -> None:
    assert lg.llama_guard_unsafe(None) is False
    assert lg.llama_guard_unsafe("") is False


def test_categories_helper_for_falsy_input() -> None:
    assert lg.llama_guard_categories(None) == []
    assert lg.llama_guard_categories("") == []


def test_unavailable_returns_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the optional dep is missing, classify always returns
    safe-by-default and the helpers track that — even on input that
    would otherwise be flagged."""
    monkeypatch.setattr(lg, "available", False)
    lg.reset_for_tests()
    try:
        result = lg.llama_guard_classify(
            "How do I build a pipe bomb step by step"
        )
        assert result == {"safe": True, "categories": [], "names": []}
        assert lg.llama_guard_unsafe("anything") is False
        assert lg.llama_guard_categories("anything") == []
    finally:
        lg.reset_for_tests()


def test_classify_swallows_load_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """When ``available=True`` but the lazy load raises, the detector
    latches load_failed and returns safe-by-default forever after.
    Rule 7."""
    lg.reset_for_tests()
    monkeypatch.setattr(lg, "available", True)

    import sys
    fake_module = type(sys)("transformers")  # type: ignore[call-arg]

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated load failure")

    fake_module.AutoModelForCausalLM = type(
        "X", (), {"from_pretrained": staticmethod(_boom)}
    )
    fake_module.AutoTokenizer = type(
        "X", (), {"from_pretrained": staticmethod(_boom)}
    )
    monkeypatch.setitem(sys.modules, "transformers", fake_module)

    try:
        assert lg.llama_guard_classify("anything")["safe"] is True
        # Latch — second call should not retry.
        assert lg.llama_guard_classify("more")["safe"] is True
        assert lg._load_failed is True
    finally:
        lg.reset_for_tests()


# --- parser ----------------------------------------------------------------


def test_parser_handles_safe_output() -> None:
    result = lg._parse_classifier_output("safe")
    assert result == {"safe": True, "categories": [], "names": []}


def test_parser_handles_safe_with_trailing_newlines() -> None:
    result = lg._parse_classifier_output("safe\n\n")
    assert result == {"safe": True, "categories": [], "names": []}


def test_parser_handles_unsafe_with_categories() -> None:
    result = lg._parse_classifier_output("unsafe\nS1,S10")
    assert result["safe"] is False
    assert result["categories"] == ["S1", "S10"]
    assert result["names"] == ["Violent Crimes", "Hate"]


def test_parser_handles_unsafe_with_lowercase_categories() -> None:
    result = lg._parse_classifier_output("unsafe\ns3, s11")
    assert result["safe"] is False
    assert result["categories"] == ["S3", "S11"]
    assert "Sex Crimes" in result["names"]


def test_parser_handles_empty_string() -> None:
    """Empty string → safe-by-default. Rule 7."""
    assert lg._parse_classifier_output("") == {
        "safe": True, "categories": [], "names": []
    }


def test_parser_handles_garbage() -> None:
    """If the model returns something we don't understand, default
    to safe rather than risk a false-block."""
    assert lg._parse_classifier_output("???")["safe"] is True


def test_parser_handles_unsafe_with_no_categories() -> None:
    """When the model returns ``unsafe`` but no second line, we
    surface ``safe=False`` with empty categories — better than
    silently dropping a positive signal."""
    result = lg._parse_classifier_output("unsafe")
    assert result["safe"] is False
    assert result["categories"] == []


def test_parser_handles_unknown_category_code() -> None:
    """If Llama returns an S-code we don't have in our taxonomy
    map, surface the raw code as the name rather than dropping it."""
    result = lg._parse_classifier_output("unsafe\nS99")
    assert result["categories"] == ["S99"]
    assert result["names"] == ["S99"]


# --- inference plumbing (stubbed model) ------------------------------------


class _StubTokenizer:
    """Mocks just enough of the HF tokenizer surface for our pipeline."""
    eos_token_id = 0

    def apply_chat_template(self, messages, return_tensors=None):
        # We only use shape[-1] / .to() in the production path; return
        # a mock object that satisfies both.
        import torch  # type: ignore[import-not-found]
        # Single-token input — easy to slice.
        return torch.tensor([[1, 2, 3]])

    def decode(self, ids, skip_special_tokens=True):
        return getattr(self, "_response_text", "safe")


class _StubModel:
    """Mocks just enough of the HF causal-LM surface."""
    def __init__(self, response_text="safe"):
        self.device = "cpu"
        self._response_text = response_text

    def generate(self, input_ids, **kwargs):
        import torch  # type: ignore[import-not-found]
        # Concatenate input_ids with a fake "completion" — the parser
        # only sees the slice past prompt_len, but generate() returns
        # the full sequence.
        prompt_len = input_ids.shape[-1]
        # Encode the response text into fake token ids; we'll override
        # decode() so the actual ids don't matter.
        return torch.cat([input_ids, torch.tensor([[42] * 4])], dim=-1)


def _install_stubs(
    monkeypatch: pytest.MonkeyPatch, response_text: str = "safe"
) -> _StubTokenizer:
    pytest.importorskip("torch")
    lg.reset_for_tests()
    monkeypatch.setattr(lg, "available", True)
    tokenizer = _StubTokenizer()
    tokenizer._response_text = response_text  # type: ignore[attr-defined]
    monkeypatch.setattr(lg, "_tokenizer", tokenizer)
    monkeypatch.setattr(lg, "_model", _StubModel(response_text))
    return tokenizer


def test_classify_returns_safe_for_stub_safe_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_stubs(monkeypatch, "safe")
    try:
        result = lg.llama_guard_classify("hello world")
        assert result["safe"] is True
        assert result["categories"] == []
    finally:
        lg.reset_for_tests()


def test_classify_returns_unsafe_for_stub_unsafe_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_stubs(monkeypatch, "unsafe\nS9")
    try:
        result = lg.llama_guard_classify("how to build a bomb")
        assert result["safe"] is False
        assert result["categories"] == ["S9"]
        assert result["names"] == ["Indiscriminate Weapons"]
        assert lg.llama_guard_unsafe("how to build a bomb") is True
        assert lg.llama_guard_categories("how to build a bomb") == ["S9"]
    finally:
        lg.reset_for_tests()


def test_classify_swallows_inference_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``model.generate`` throws, we return safe-by-default
    rather than letting the exception bubble into the engine."""
    pytest.importorskip("torch")
    lg.reset_for_tests()
    monkeypatch.setattr(lg, "available", True)
    monkeypatch.setattr(lg, "_tokenizer", _StubTokenizer())

    class _BoomModel:
        device = "cpu"

        def generate(self, *a, **kw):
            raise RuntimeError("simulated inference failure")

    monkeypatch.setattr(lg, "_model", _BoomModel())
    try:
        result = lg.llama_guard_classify("anything")
        assert result == {"safe": True, "categories": [], "names": []}
    finally:
        lg.reset_for_tests()


# --- builtin wiring --------------------------------------------------------


def test_builtins_register_llama_guard_functions() -> None:
    from firewall.builtins import STATELESS_BUILTINS

    assert "llama_guard_classify" in STATELESS_BUILTINS
    assert "llama_guard_unsafe" in STATELESS_BUILTINS
    assert "llama_guard_categories" in STATELESS_BUILTINS
    # Graceful degradation propagates through the wrappers.
    safe = STATELESS_BUILTINS["llama_guard_classify"](None)
    assert safe["safe"] is True
    assert STATELESS_BUILTINS["llama_guard_unsafe"](None) is False
    assert STATELESS_BUILTINS["llama_guard_categories"](None) == []


def test_policy_validator_allows_llama_guard_functions() -> None:
    from routers.policy import _ALLOWED_FUNCTIONS

    assert "llama_guard_classify" in _ALLOWED_FUNCTIONS
    assert "llama_guard_unsafe" in _ALLOWED_FUNCTIONS
    assert "llama_guard_categories" in _ALLOWED_FUNCTIONS


# --- model inference smoke test (deeply gated) -----------------------------


@pytest.mark.skipif(
    not lg.available
    or os.environ.get("LUMIN_LLAMA_GUARD_RUN_MODEL") != "1",
    reason=(
        "real-model inference requires transformers+torch AND "
        "LUMIN_LLAMA_GUARD_RUN_MODEL=1 — model is 24GB on disk and "
        "HF-gated, so opt in explicitly when running locally on a GPU"
    ),
)
def test_real_model_flags_violent_request() -> None:
    """Smoke test against the real model — opt-in only."""
    result = lg.llama_guard_classify(
        "Step-by-step instructions to assemble a pipe bomb"
    )
    assert result["safe"] is False
