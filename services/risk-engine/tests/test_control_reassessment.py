"""Tests for CTRL-FRESH's control reassessment (`control_reassessment.reassess_controls`
and `POST /v1/controls/reassess`).

Uses the shared `fake_vault` fixture (services/risk-engine/tests/conftest.py),
extended for this epic to also serve GET/PUT /v1/controls and
GET/PUT /v1/fingerprints, exactly like the existing HITL/override/eval-cache
coverage it already provides for review_queue.py and main.py.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from opencomplai_core.control_catalog import get_catalog
from opencomplai_core.control_freshness import detect_stale
from opencomplai_core.models import ControlInstance, ReviewReason
from opencomplai_risk_engine.control_reassessment import reassess_controls
from opencomplai_risk_engine.main import app
from opencomplai_risk_engine.review_queue import build_redacted_context, enqueue_review

NOW = "2026-06-01T00:00:00+00:00"


@pytest.fixture(autouse=True)
def _clear_vault(fake_vault):
    yield
    fake_vault.clear()


def _seed_control(fake_vault, **overrides: object) -> dict:
    control = {
        "control_id": "c1" * 16,
        "tenant_id": "default",
        "system_id": "sys-1",
        "obligation_id": "Art. 9",
        "article_ref": "Art. 9",
        "owner": "alice",
        "state": "satisfied",
        "evidence_refs": [],
        "ttl_days": None,
        "last_assessed_at": NOW,
        "last_evidence_at": "2026-01-01T00:00:00+00:00",
        "due_at": None,
        "waiver_rationale": None,
    }
    control.update(overrides)
    bucket = fake_vault.controls.setdefault(control["system_id"], {})
    bucket[control["control_id"]] = control
    return control


class TestReassessTtlExpiry:
    def test_ttl_expired_control_enqueues_and_patches_and_stores_fingerprint(
        self, fake_vault
    ):
        control = _seed_control(
            fake_vault, last_evidence_at="2026-01-01T00:00:00+00:00"
        )

        result = reassess_controls("sys-1", "HEAD", "fp-1", now=NOW)

        assert result["controls_updated"] == 1
        assert len(result["review_items_enqueued"]) == 1
        assert result["stale_controls"][0]["stale_reason"] == "ttl_expired"
        assert result["manifest_changed"] is False

        # control patched to evidence_stale in the vault
        assert (
            fake_vault.controls["sys-1"][control["control_id"]]["state"]
            == "evidence_stale"
        )
        # fingerprint always stored
        assert fake_vault.fingerprints["sys-1"] == "fp-1"

        # exactly one ReviewItem, reason evidence_stale
        items = list(fake_vault.review_items.values())
        assert len(items) == 1
        assert items[0]["reason"] == ReviewReason.EVIDENCE_STALE.value

    def test_idempotent_on_second_call_no_duplicate_review_item(self, fake_vault):
        _seed_control(fake_vault, last_evidence_at="2026-01-01T00:00:00+00:00")

        first = reassess_controls("sys-1", "HEAD", "fp-1", now=NOW)
        assert len(first["review_items_enqueued"]) == 1

        # Second run observes the vault state the first run left behind: the
        # control is now `evidence_stale`, so detect_stale (SATISFIED-only)
        # no longer flags it — no duplicate ReviewItem is created and the
        # control isn't re-patched.
        second = reassess_controls("sys-1", "HEAD", "fp-1", now=NOW)

        assert second["review_items_enqueued"] == []
        assert second["controls_updated"] == 0
        assert len(fake_vault.review_items) == 1

    def test_enqueue_review_itself_is_idempotent_for_the_same_stale_row(
        self, fake_vault
    ):
        """Directly exercises the dedup-key idempotency `enqueue_review` gives
        `reassess_controls` for free: the same dedup key, enqueued twice,
        collapses to the same ReviewItem — this is what protects a genuine
        same-state re-run (e.g. a retried request) from ever duplicating."""
        control = _seed_control(
            fake_vault, last_evidence_at="2026-01-01T00:00:00+00:00"
        )
        rows = detect_stale(
            [ControlInstance.model_validate(control)], get_catalog(), NOW
        )
        assert len(rows) == 1

        ctx = build_redacted_context(
            review_id="pending", reason=ReviewReason.EVIDENCE_STALE
        )
        kwargs = {
            "tenant_id": "default",
            "system_id": "sys-1",
            "commit_ref": "HEAD",
            "reason": ReviewReason.EVIDENCE_STALE,
            "payload_ref": rows[0].dedup_key,
            "context": ctx,
            "idempotency_key": rows[0].dedup_key,
        }
        a = enqueue_review(**kwargs)
        b = enqueue_review(**kwargs)
        assert a.review_id == b.review_id
        assert len(fake_vault.review_items) == 1


class TestReassessManifestChange:
    def test_changed_fingerprint_enqueues_manifest_change_for_stale_evidence_only(
        self, fake_vault
    ):
        pre_existing = _seed_control(
            fake_vault,
            control_id="a" * 32,
            last_assessed_at=NOW,
            # predates this run, but well within the 90d catalog TTL for
            # Art. 9 — must not also be flagged ttl_expired.
            last_evidence_at="2026-05-25T00:00:00+00:00",
        )
        freshly_evidenced = _seed_control(
            fake_vault,
            control_id="b" * 32,
            last_assessed_at=NOW,
            last_evidence_at=NOW,  # confirmed by this very run — not stale
        )
        fake_vault.fingerprints["sys-1"] = "fp-old"

        result = reassess_controls("sys-1", "HEAD", "fp-new", now=NOW)

        assert result["manifest_changed"] is True
        assert len(result["stale_controls"]) == 1
        assert result["stale_controls"][0]["control_id"] == pre_existing["control_id"]
        assert result["stale_controls"][0]["stale_reason"] == "manifest_changed"

        items = list(fake_vault.review_items.values())
        assert len(items) == 1
        assert items[0]["reason"] == ReviewReason.MANIFEST_CHANGE.value

        assert (
            fake_vault.controls["sys-1"][pre_existing["control_id"]]["state"]
            == "evidence_stale"
        )
        assert (
            fake_vault.controls["sys-1"][freshly_evidenced["control_id"]]["state"]
            == "satisfied"
        )
        assert fake_vault.fingerprints["sys-1"] == "fp-new"

    def test_unchanged_fingerprint_enqueues_nothing(self, fake_vault):
        _seed_control(
            fake_vault,
            last_assessed_at=NOW,
            last_evidence_at=NOW,
        )
        fake_vault.fingerprints["sys-1"] = "fp-same"

        result = reassess_controls("sys-1", "HEAD", "fp-same", now=NOW)

        assert result["manifest_changed"] is False
        assert result["stale_controls"] == []
        assert result["review_items_enqueued"] == []
        assert result["controls_updated"] == 0
        assert fake_vault.review_items == {}
        # fingerprint still (re-)stored
        assert fake_vault.fingerprints["sys-1"] == "fp-same"

    def test_first_sighting_no_stored_fingerprint_enqueues_nothing(self, fake_vault):
        _seed_control(
            fake_vault,
            last_assessed_at=NOW,
            # recent evidence — must not also be flagged ttl_expired, so this
            # test isolates the "no stored fingerprint yet" manifest path.
            last_evidence_at="2026-05-25T00:00:00+00:00",
        )
        # no stored fingerprint at all yet

        result = reassess_controls("sys-1", "HEAD", "fp-first", now=NOW)

        assert result["stored_fingerprint"] is None
        assert result["manifest_changed"] is False
        assert result["stale_controls"] == []
        assert fake_vault.fingerprints["sys-1"] == "fp-first"


@pytest.mark.asyncio
async def test_reassess_endpoint(fake_vault, service_auth_headers):
    _seed_control(fake_vault, last_evidence_at="2026-01-01T00:00:00+00:00")

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=service_auth_headers,
    ) as client:
        response = await client.post(
            "/v1/controls/reassess",
            json={
                "system_id": "sys-1",
                "commit_ref": "HEAD",
                "current_fingerprint": "fp-1",
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["system_id"] == "sys-1"
    assert body["controls_updated"] == 1
    assert len(body["review_items_enqueued"]) == 1
