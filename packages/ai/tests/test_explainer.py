"""Tests for opencomplai_ai.explainer JSON parsing and IntentExplainer.classify."""

from __future__ import annotations

import json
import threading
import time
from unittest.mock import MagicMock, patch

import pytest
from opencomplai_ai.explainer import (
    IntentExplainer,
    _parse_annotation,
    _resolve_timeout_seconds,
)
from opencomplai_ai.models import IntentAnnotation


def test_parse_annotation_credit_scoring_area_5():
    data = {
        "annex_iii_area": 5,
        "art5_prohibited": False,
        "art6_3_profiling": True,
        "decision_autonomy": "autonomous",
        "subject_type": "natural_person",
        "consequential": "yes",
        "explanation": "Credit scoring under Annex III area 5(b).",
    }
    ann = _parse_annotation(data, model_id="qwen2.5-coder", confidence=0.92)
    assert isinstance(ann, IntentAnnotation)
    assert ann.annex_iii_area == 5
    assert ann.art6_3_profiling is True
    assert ann.art5_prohibited is False
    assert ann.decision_autonomy == "autonomous"
    assert any("Art." in o for o in ann.eu_obligation)


def test_parse_annotation_rejects_invalid_area():
    data = {
        "annex_iii_area": 99,
        "art5_prohibited": False,
        "art6_3_profiling": False,
        "decision_autonomy": "display_only",
        "subject_type": "system",
        "consequential": "no",
        "explanation": "No Annex III match.",
    }
    ann = _parse_annotation(data, model_id="qwen2.5-coder", confidence=0.5)
    assert ann.annex_iii_area is None


def test_parse_annotation_art5_prohibited():
    data = {
        "annex_iii_area": None,
        "art5_prohibited": True,
        "art6_3_profiling": False,
        "decision_autonomy": "autonomous",
        "subject_type": "natural_person",
        "consequential": "yes",
        "explanation": "Social scoring prohibited under Art. 5.",
    }
    ann = _parse_annotation(data, model_id="qwen2.5-coder", confidence=0.95)
    assert ann.art5_prohibited is True


@pytest.mark.parametrize(
    "bad_area",
    [3.5, "five", None, float("nan"), float("inf"), float("-inf")],
)
def test_parse_annotation_coerces_non_integer_area(bad_area):
    # nan/inf reach here for real: json.loads accepts the bare NaN/Infinity
    # tokens, and int() on a non-finite float raises instead of coercing.
    data = {
        "annex_iii_area": bad_area,
        "art5_prohibited": False,
        "art6_3_profiling": False,
        "decision_autonomy": "unknown",
        "subject_type": "unknown",
        "consequential": "unknown",
        "explanation": "",
    }
    ann = _parse_annotation(data, model_id="test", confidence=0.1)
    assert ann.annex_iii_area is None


# --------------------------------------------------------------------------
# IntentExplainer.classify — timeout, concurrency, and failure-mode tests
# (finding 48.4). __init__ is bypassed (no ensure_model / no llama-cpp
# import) since only classify()'s own logic is under test here.
# --------------------------------------------------------------------------


def _llama_response(text: str) -> dict:
    return {"choices": [{"text": text}]}


_LIMITED_RISK_PAYLOAD = {
    "annex_iii_area": None,
    "art5_prohibited": False,
    "art6_3_profiling": False,
    "decision_autonomy": "advisory",
    "subject_type": "natural_person",
    "consequential": "no",
    "risk_tier": "limited_risk",
    "explanation": "Chatbot disclosure required.",
}


@pytest.fixture
def explainer():
    exp = IntentExplainer.__new__(IntentExplainer)
    exp._model_id = "qwen2.5-coder-1.5b"
    exp._llama = MagicMock()
    exp._lock = threading.Lock()
    exp._inference_lock = threading.Lock()
    exp.timeout_seconds = 1.0
    exp.last_failure = None
    exp.timeout_count = 0
    return exp


def test_classify_returns_annotation_from_valid_json(explainer):
    explainer._llama.return_value = _llama_response(json.dumps(_LIMITED_RISK_PAYLOAD))

    ann = explainer.classify("const x = openai.chat.completions.create(...)")

    assert isinstance(ann, IntentAnnotation)
    assert ann.risk_tier == "limited_risk"
    assert explainer.last_failure is None


def test_classify_stamps_gate_reason_onto_annotation(explainer):
    explainer._llama.return_value = _llama_response(json.dumps(_LIMITED_RISK_PAYLOAD))

    ann = explainer.classify("snippet", gate_reason="inference_verb_with_file_context")

    assert ann is not None
    assert ann.gate_reason == "inference_verb_with_file_context"


def test_classify_does_not_overwrite_existing_gate_reason(explainer):
    payload = dict(_LIMITED_RISK_PAYLOAD)
    explainer._llama.return_value = _llama_response(json.dumps(payload))

    # _parse_annotation never sets gate_reason from the LLM's JSON, so this
    # documents that classify()'s own stamping only fills a genuinely empty
    # field rather than blindly overwriting.
    ann = explainer.classify("snippet", gate_reason="ai_callsite")
    assert ann.gate_reason == "ai_callsite"


def test_classify_minimal_risk_returns_none_when_not_legacy(explainer):
    payload = {
        "annex_iii_area": None,
        "art5_prohibited": False,
        "art6_3_profiling": False,
        "decision_autonomy": "unknown",
        "subject_type": "unknown",
        "consequential": "unknown",
        "risk_tier": "minimal",
        "explanation": "",
    }
    explainer._llama.return_value = _llama_response(json.dumps(payload))

    assert explainer.classify("snippet") is None


def test_classify_timeout_never_fabricates_even_in_legacy_mode(explainer):
    """Finding 48.4's central assertion: a timeout must return None, never a
    fabricated 'minimal' IntentAnnotation, even with legacy=True."""
    explainer.timeout_seconds = 0.05

    def _slow(*_args, **_kwargs):
        time.sleep(0.3)
        return _llama_response(json.dumps(_LIMITED_RISK_PAYLOAD))

    explainer._llama.side_effect = _slow

    result = explainer.classify("snippet", legacy=True)

    assert result is None
    assert explainer.last_failure == "timeout"
    assert explainer.timeout_count == 1

    # Let the zombie worker finish and release the inference lock so it
    # doesn't bleed into another test via a shared fixture instance.
    time.sleep(0.4)


def test_classify_refuses_concurrent_call_while_previous_worker_running(explainer):
    """A still-running (e.g. timed-out) worker holds _inference_lock; a new
    classify() call must fail fast rather than invoke self._llama again."""
    explainer._inference_lock.acquire()
    try:
        result = explainer.classify("snippet")
    finally:
        explainer._inference_lock.release()

    assert result is None
    assert explainer.last_failure == "backend_busy"
    explainer._llama.assert_not_called()


def test_classify_crash_sets_last_failure_and_legacy_fallback(explainer):
    explainer._llama.side_effect = RuntimeError("boom")

    result = explainer.classify("snippet")
    assert result is None
    assert explainer.last_failure == "crash"

    explainer._llama.side_effect = RuntimeError("boom")
    legacy_result = explainer.classify("snippet", legacy=True)
    assert isinstance(legacy_result, IntentAnnotation)
    assert legacy_result.risk_tier == "minimal"
    assert explainer.last_failure == "crash"


def test_classify_no_json_object_sets_malformed_json_failure(explainer):
    explainer._llama.return_value = _llama_response("the model said something else")

    result = explainer.classify("snippet")

    assert result is None
    assert explainer.last_failure == "malformed_json"


def test_classify_invalid_json_syntax_sets_malformed_json_failure(explainer):
    explainer._llama.return_value = _llama_response("{not actually: valid json}")

    result = explainer.classify("snippet")

    assert result is None
    assert explainer.last_failure == "malformed_json"


def test_classify_nan_area_does_not_crash(explainer):
    # json.loads accepts the bare NaN token, so the model can hand
    # _parse_annotation a float("nan") area; int(nan) raises ValueError,
    # which is not a ValidationError and used to escape classify() entirely
    # (scan_engine's phase-level except then discarded every AI finding).
    raw = json.dumps(_LIMITED_RISK_PAYLOAD).replace(
        '"annex_iii_area": null', '"annex_iii_area": NaN'
    )
    assert "NaN" in raw
    explainer._llama.return_value = _llama_response(raw)

    result = explainer.classify("snippet")

    assert explainer.last_failure is None
    assert isinstance(result, IntentAnnotation)
    assert result.annex_iii_area is None


_SCHEMA_INVALID_PAYLOAD = {
    "annex_iii_area": None,
    "art5_prohibited": False,
    "art6_3_profiling": False,
    "decision_autonomy": "semi_automated",  # not in the DecisionAutonomy Literal
    "subject_type": "natural_person",
    "consequential": "no",
    "risk_tier": "limited_risk",
    "explanation": "test",
}


def test_classify_schema_invalid_value_sets_invalid_schema_failure(explainer):
    """Syntactically-valid JSON with an out-of-Literal field value (e.g. a
    decision_autonomy the model hallucinated) must not raise out of
    classify() — it's a distinct failure mode from malformed_json, which
    never even produced a parseable dict."""
    explainer._llama.return_value = _llama_response(json.dumps(_SCHEMA_INVALID_PAYLOAD))

    result = explainer.classify("snippet")

    assert result is None
    assert explainer.last_failure == "invalid_schema"


def test_classify_schema_invalid_value_legacy_fallback(explainer):
    """legacy=True still gets the historical fallback annotation, exactly
    like the other non-timeout failure modes (crash, malformed_json,
    model_load_failed)."""
    explainer._llama.return_value = _llama_response(json.dumps(_SCHEMA_INVALID_PAYLOAD))

    legacy_result = explainer.classify("snippet", legacy=True)

    assert isinstance(legacy_result, IntentAnnotation)
    assert legacy_result.risk_tier == "minimal"
    assert explainer.last_failure == "invalid_schema"


def test_classify_distinguishes_timeout_crash_and_malformed_json(explainer):
    """The three failure modes the finding calls out as indistinguishable
    must land on different last_failure values."""
    explainer._llama.side_effect = RuntimeError("boom")
    explainer.classify("snippet")
    crash_failure = explainer.last_failure

    explainer._llama.side_effect = None
    explainer._llama.return_value = _llama_response("no braces here")
    explainer.classify("snippet")
    malformed_failure = explainer.last_failure

    explainer.timeout_seconds = 0.05
    explainer._llama.side_effect = lambda *_a, **_kw: (
        time.sleep(0.3) or _llama_response(json.dumps(_LIMITED_RISK_PAYLOAD))
    )
    explainer.classify("snippet")
    timeout_failure = explainer.last_failure
    time.sleep(0.4)  # let the zombie worker finish before the test ends

    assert len({crash_failure, malformed_failure, timeout_failure}) == 3
    assert timeout_failure == "timeout"
    assert crash_failure == "crash"
    assert malformed_failure == "malformed_json"


def test_classify_model_load_failure_sets_last_failure(explainer, monkeypatch):
    explainer._llama = None  # forces _load() to actually run

    def _raise_load(self) -> None:
        raise ImportError("llama-cpp-python is required for GGUF models.")

    monkeypatch.setattr(IntentExplainer, "_load", _raise_load)

    result = explainer.classify("snippet")
    assert result is None
    assert explainer.last_failure == "model_load_failed"

    legacy_result = explainer.classify("snippet", legacy=True)
    assert isinstance(legacy_result, IntentAnnotation)
    assert legacy_result.risk_tier == "minimal"


# --------------------------------------------------------------------------
# OPENCOMPLAI_AI_TIMEOUT_SECONDS resolution
# --------------------------------------------------------------------------


def test_resolve_timeout_seconds_default(monkeypatch):
    monkeypatch.delenv("OPENCOMPLAI_AI_TIMEOUT_SECONDS", raising=False)
    assert _resolve_timeout_seconds() == 10.0


def test_resolve_timeout_seconds_reads_env_var(monkeypatch):
    monkeypatch.setenv("OPENCOMPLAI_AI_TIMEOUT_SECONDS", "5")
    assert _resolve_timeout_seconds() == 5.0


def test_resolve_timeout_seconds_invalid_value_falls_back(monkeypatch):
    monkeypatch.setenv("OPENCOMPLAI_AI_TIMEOUT_SECONDS", "not-a-number")
    with pytest.warns(UserWarning, match="not a number"):
        assert _resolve_timeout_seconds() == 10.0


def test_resolve_timeout_seconds_non_positive_falls_back(monkeypatch):
    monkeypatch.setenv("OPENCOMPLAI_AI_TIMEOUT_SECONDS", "0")
    with pytest.warns(UserWarning, match="positive, finite"):
        assert _resolve_timeout_seconds() == 10.0


@pytest.mark.parametrize("raw", ["inf", "Infinity", "-inf", "nan"])
def test_resolve_timeout_seconds_non_finite_falls_back(monkeypatch, raw):
    # float("inf") passes a bare `<= 0` check but crashes Event.wait() with
    # OverflowError later — inside classify(), where nothing catches it.
    monkeypatch.setenv("OPENCOMPLAI_AI_TIMEOUT_SECONDS", raw)
    with pytest.warns(UserWarning, match="positive, finite"):
        assert _resolve_timeout_seconds() == 10.0


def test_init_reads_timeout_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCOMPLAI_AI_TIMEOUT_SECONDS", "3")
    fake_path = tmp_path / "fake.gguf"
    fake_path.write_bytes(b"x")

    with patch("opencomplai_ai.downloader.ensure_model", return_value=fake_path):
        exp = IntentExplainer("qwen2.5-coder-1.5b")

    assert exp.timeout_seconds == 3.0
    assert exp.last_failure is None
    assert exp.timeout_count == 0
