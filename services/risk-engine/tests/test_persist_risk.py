"""
PERSIST-RISK: review queue, override idempotency, and eval cache survive a
process restart (they now live in evidence-vault, not process-local dicts).

Simulated here by asserting risk-engine's module-level stores no longer
exist at all (a restart trivially preserves state that isn't held
in-process), and that persistence round-trips through the fake vault exactly
as it would through the real evidence-vault HTTP contract.
"""

from __future__ import annotations

import pytest
from opencomplai_core.models import ReviewReason
from opencomplai_risk_engine import main as risk_main
from opencomplai_risk_engine import review_queue


def test_risk_engine_no_longer_holds_in_memory_override_or_eval_state():
    """
    The whole point of PERSIST-RISK: a restart must not silently drop
    in-flight review items or break idempotency guarantees. That's only true
    if there is no process-local dict left to lose.
    """
    assert not hasattr(risk_main, "_ACCEPTED_OVERRIDES")
    assert not hasattr(risk_main, "_COMPLETED_EVALS")
    assert not hasattr(review_queue, "_REVIEW_ITEMS")
    assert not hasattr(review_queue, "_REVIEW_CONTEXTS")


def test_override_idempotency_cache_persists_via_vault(fake_vault):
    risk_main._store_accepted_override("idem-1", "fp-1", {"status": "accepted"})

    # A fresh lookup call is indistinguishable from one made by a brand-new
    # process replica reading the same durable store.
    cached = risk_main._lookup_accepted_override("idem-1")
    assert cached == ("fp-1", {"status": "accepted"})
    assert fake_vault.accepted_overrides["idem-1"]["payload_fingerprint"] == "fp-1"


def test_eval_cache_persists_via_vault(fake_vault):
    risk_main._store_completed_eval("run-1", {"overall_outcome": "pass"})

    cached = risk_main._lookup_completed_eval("run-1")
    assert cached == {"overall_outcome": "pass"}
    assert fake_vault.completed_evals["run-1"] == {"overall_outcome": "pass"}


def test_review_item_visible_via_get_after_enqueue(fake_vault):
    ctx = review_queue.build_redacted_context(
        "x", ReviewReason.EVALUATOR_FAIL, detector_ids=["EVAL_SAFETY"]
    )
    item = review_queue.enqueue_review(
        tenant_id="t1",
        system_id="sys",
        commit_ref="HEAD",
        reason=ReviewReason.EVALUATOR_FAIL,
        payload_ref="sha256:abc",
        context=ctx,
    )

    # A second process (or the same one after a restart) reading the same
    # durable store must see exactly what was written.
    fetched = review_queue.get_review_item("t1", item.review_id)
    assert fetched is not None
    assert fetched.review_id == item.review_id
    assert fake_vault.review_items[item.review_id]["system_id"] == "sys"


def test_assign_and_decide_transitions_persist(fake_vault):
    ctx = review_queue.build_redacted_context("x", ReviewReason.MANUAL)
    item = review_queue.enqueue_review(
        tenant_id="t1",
        system_id="sys",
        commit_ref="HEAD",
        reason=ReviewReason.MANUAL,
        payload_ref="sha256:abc",
        context=ctx,
    )

    assigned = review_queue.assign_review("t1", item.review_id, "reviewer-42")
    assert assigned.assigned_to == "reviewer-42"
    assert fake_vault.review_items[item.review_id]["assigned_to"] == "reviewer-42"

    decided = review_queue.mark_decided("t1", item.review_id, "ovr_sha256:xyz")
    assert decided.state.value == "decided"
    assert decided.linked_override_id == "ovr_sha256:xyz"
    assert fake_vault.review_items[item.review_id]["state"] == "decided"


def test_assign_review_missing_raises_keyerror(fake_vault):
    with pytest.raises(KeyError):
        review_queue.assign_review("t1", "does-not-exist", "reviewer-1")
