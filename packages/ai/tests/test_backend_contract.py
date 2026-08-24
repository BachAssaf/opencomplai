"""Contract test: every backend the registry can hand back must accept
whatever keyword set IntentDetector.detect() actually passes to classify().

Finding 1 (issue #45): detector.py always builds a classify_kwargs dict that
includes "gate_reason" whenever a usage match exists (the default,
non-legacy path), then calls ``backend.classify(snippet, **classify_kwargs)``.
Only IntentClassifier.classify declared gate_reason; IntentExplainer.classify
and SaaSIntentClient.classify raised TypeError on the very first gated call,
which scan_engine.py silently downgrades to a warning — a default install's
``scan --ai-intent`` reported zero AI findings without ever surfacing an
error.

This test derives the expected kwarg set from detector.py's own source (via
AST, not a hardcoded literal) so that if detector.py starts passing a new
kwarg, this test fails for every backend that doesn't accept it — the same
class of bug, caught before it ships, without needing a model download
(signature inspection only, no instantiation).
"""

from __future__ import annotations

import ast
import inspect
import textwrap

import pytest
from opencomplai_ai._saas_client import SaaSIntentClient
from opencomplai_ai.classifier import IntentClassifier
from opencomplai_ai.detector import IntentDetector
from opencomplai_ai.explainer import IntentExplainer

# Every backend class opencomplai_ai.registry.ModelRegistry.resolve() can
# return for a MODEL_CATALOG entry (codebert-onnx, saas, and every GGUF id).
BACKEND_CLASSES = (IntentClassifier, IntentExplainer, SaaSIntentClient)


def _extract_classify_kwargs() -> set[str]:
    """Statically find every key detector.py's classify_kwargs dict can hold.

    Covers both the dict-literal assignment and the conditional
    ``classify_kwargs["gate_reason"] = ...`` subscript assignment, regardless
    of nesting (``ast.walk`` visits the whole tree, including the body of the
    ``if usage:`` block).
    """
    source = textwrap.dedent(inspect.getsource(IntentDetector.detect))
    tree = ast.parse(source)
    keys: set[str] = set()

    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if (
                isinstance(target, ast.Name)
                and target.id == "classify_kwargs"
                and isinstance(node.value, ast.Dict)
            ):
                for k in node.value.keys:
                    if isinstance(k, ast.Constant) and isinstance(k.value, str):
                        keys.add(k.value)
            elif (
                isinstance(target, ast.Subscript)
                and isinstance(target.value, ast.Name)
                and target.value.id == "classify_kwargs"
            ):
                idx = target.slice
                if isinstance(idx, ast.Constant) and isinstance(idx.value, str):
                    keys.add(idx.value)

    return keys


def test_extraction_finds_gate_reason():
    """Sanity check on the AST extraction itself: if this ever comes back
    empty (e.g. detector.py's source shape changed so the walk no longer
    matches), the contract test below would trivially pass on zero keys —
    catch that here instead of silently losing coverage."""
    kwargs = _extract_classify_kwargs()
    assert kwargs, "AST extraction found no classify_kwargs keys in detector.py"
    assert "gate_reason" in kwargs, (
        "detector.py no longer conditionally sets gate_reason — if that's "
        "intentional, this assertion (and the finding-1 regression it "
        "guards) can be revisited."
    )


@pytest.mark.parametrize("backend_cls", BACKEND_CLASSES)
def test_classify_signature_accepts_detector_kwargs(backend_cls):
    expected = _extract_classify_kwargs()
    sig = inspect.signature(backend_cls.classify)
    params = sig.parameters

    accepts_var_keyword = any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
    )
    if accepts_var_keyword:
        return

    missing = sorted(k for k in expected if k not in params)
    assert not missing, (
        f"{backend_cls.__module__}.{backend_cls.__qualname__}.classify is "
        f"missing keyword(s) {missing} that IntentDetector.detect() passes "
        f"via classify_kwargs — this is exactly finding 1 (issue #45): a "
        f"gated callsite hits this backend and classify() raises TypeError."
    )
