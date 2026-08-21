"""Unit tests for CTRL-FRESH's read-time staleness detector.

Covers: TTL expiry via last_evidence_at + catalog TTL, via evidence
valid_until (earliest wins), via instance ttl_days override beating the
catalog; None TTL never goes stale; non-satisfied states are skipped;
deterministic dedup keys; detect_manifest_change's stored-None/equal/changed
paths and the freshly-evidenced-control exemption; apply_stale flipping only
the named controls.
"""

from __future__ import annotations

from opencomplai_core.control_catalog import ControlCatalogEntry
from opencomplai_core.control_freshness import (
    FreshnessConfig,
    StaleReason,
    apply_stale,
    control_expiry,
    detect_manifest_change,
    detect_stale,
    effective_ttl_days,
    stale_dedup_key,
)
from opencomplai_core.models import ControlInstance, ControlState, EvidenceObject

NOW = "2026-06-01T00:00:00+00:00"

CATALOG: dict[str, ControlCatalogEntry] = {
    "Art. 9": ControlCatalogEntry(title="Risk management system", default_ttl_days=90),
    "Art. 43": ControlCatalogEntry(title="Conformity assessment", default_ttl_days=365),
    "Art. 4": ControlCatalogEntry(title="AI literacy measures", default_ttl_days=None),
}


def _control(**overrides: object) -> ControlInstance:
    base: dict[str, object] = {
        "control_id": "c1" * 16,
        "tenant_id": "tenant-a",
        "system_id": "sys-1",
        "obligation_id": "Art. 9",
        "article_ref": "Art. 9",
        "owner": "alice",
        "state": ControlState.SATISFIED,
        "evidence_refs": [],
        "ttl_days": None,
        "last_assessed_at": NOW,
        "last_evidence_at": "2026-01-01T00:00:00+00:00",
        "due_at": None,
        "waiver_rationale": None,
    }
    base.update(overrides)
    return ControlInstance(**base)


class TestEffectiveTtlDays:
    def test_instance_override_beats_catalog(self):
        control = _control(ttl_days=10)
        assert effective_ttl_days(control, CATALOG, FreshnessConfig()) == 10

    def test_catalog_default_used_when_no_instance_override(self):
        control = _control(ttl_days=None)
        assert effective_ttl_days(control, CATALOG, FreshnessConfig()) == 90

    def test_config_default_used_when_no_instance_or_catalog_ttl(self):
        control = _control(
            ttl_days=None, article_ref="Art. 999", obligation_id="Art. 999"
        )
        assert (
            effective_ttl_days(control, CATALOG, FreshnessConfig(default_ttl_days=30))
            == 30
        )

    def test_none_when_nothing_set(self):
        control = _control(ttl_days=None, article_ref="Art. 4", obligation_id="Art. 4")
        assert effective_ttl_days(control, CATALOG, FreshnessConfig()) is None


class TestDetectStaleTtlExpiry:
    def test_expired_via_last_evidence_at_plus_catalog_ttl(self):
        # last_evidence_at 2026-01-01 + 90d catalog TTL = 2026-04-01, before NOW.
        control = _control(last_evidence_at="2026-01-01T00:00:00+00:00")
        rows = detect_stale([control], CATALOG, NOW)
        assert len(rows) == 1
        assert rows[0].stale_reason == StaleReason.TTL_EXPIRED
        assert rows[0].control_id == control.control_id

    def test_not_expired_when_within_ttl_window(self):
        control = _control(last_evidence_at="2026-05-25T00:00:00+00:00")
        rows = detect_stale([control], CATALOG, NOW)
        assert rows == []

    def test_expired_via_evidence_valid_until_earliest_wins(self):
        control = _control(
            evidence_refs=["ev-1", "ev-2"],
            last_evidence_at="2026-05-25T00:00:00+00:00",  # fresh under TTL alone
        )
        evidence = {
            "ev-1": EvidenceObject(
                evidence_id="ev-1",
                content_hash="h1",
                storage_uri="uri1",
                valid_until="2026-07-01T00:00:00+00:00",
            ),
            "ev-2": EvidenceObject(
                evidence_id="ev-2",
                content_hash="h2",
                storage_uri="uri2",
                valid_until="2026-05-01T00:00:00+00:00",  # earliest -> already expired
            ),
        }
        rows = detect_stale([control], CATALOG, NOW, evidence=evidence)
        assert len(rows) == 1
        assert rows[0].expired_at == "2026-05-01T00:00:00+00:00"

    def test_instance_ttl_override_beats_catalog_default(self):
        # catalog TTL (90d) would keep this fresh; a short instance override
        # (5d) pushes the same last_evidence_at into staleness.
        control = _control(ttl_days=5, last_evidence_at="2026-05-01T00:00:00+00:00")
        rows = detect_stale([control], CATALOG, NOW)
        assert len(rows) == 1
        assert rows[0].stale_reason == StaleReason.TTL_EXPIRED

    def test_none_ttl_never_stale(self):
        control = _control(
            article_ref="Art. 4",
            obligation_id="Art. 4",
            ttl_days=None,
            last_evidence_at="2020-01-01T00:00:00+00:00",
            due_at=None,
        )
        rows = detect_stale([control], CATALOG, NOW)
        assert rows == []

    def test_non_satisfied_states_skipped(self):
        for state in (
            ControlState.WAIVED,
            ControlState.EVIDENCE_MISSING,
            ControlState.PENDING_REVIEW,
            ControlState.EVIDENCE_STALE,
        ):
            control = _control(
                state=state, last_evidence_at="2020-01-01T00:00:00+00:00"
            )
            assert detect_stale([control], CATALOG, NOW) == []

    def test_dedup_key_deterministic(self):
        control = _control(last_evidence_at="2026-01-01T00:00:00+00:00")
        rows_a = detect_stale([control], CATALOG, NOW)
        rows_b = detect_stale([control], CATALOG, NOW)
        assert rows_a[0].dedup_key == rows_b[0].dedup_key
        assert rows_a[0].dedup_key == stale_dedup_key(
            "tenant-a", control.control_id, control.last_evidence_at
        )

    def test_deterministic_input_order(self):
        stale_1 = _control(
            control_id="a" * 32, last_evidence_at="2026-01-01T00:00:00+00:00"
        )
        stale_2 = _control(
            control_id="b" * 32, last_evidence_at="2026-01-02T00:00:00+00:00"
        )
        rows = detect_stale([stale_2, stale_1], CATALOG, NOW)
        assert [r.control_id for r in rows] == [stale_2.control_id, stale_1.control_id]


class TestControlExpiry:
    def test_falls_back_to_due_at_when_no_ttl_no_evidence_timestamps(self):
        control = _control(
            article_ref="Art. 4",
            obligation_id="Art. 4",
            ttl_days=None,
            last_evidence_at=None,
            due_at="2026-03-01T00:00:00+00:00",
        )
        assert (
            control_expiry(control, CATALOG, FreshnessConfig())
            == "2026-03-01T00:00:00+00:00"
        )

    def test_none_when_no_ttl_and_no_timestamps(self):
        control = _control(
            article_ref="Art. 4",
            obligation_id="Art. 4",
            ttl_days=None,
            last_evidence_at=None,
            due_at=None,
        )
        assert control_expiry(control, CATALOG, FreshnessConfig()) is None


class TestDetectManifestChange:
    def test_stored_none_returns_empty(self):
        control = _control()
        assert detect_manifest_change([control], None, "fp-new", now=NOW) == []

    def test_equal_fingerprints_returns_empty(self):
        control = _control()
        assert detect_manifest_change([control], "fp-same", "fp-same", now=NOW) == []

    def test_changed_flags_only_satisfied_with_stale_evidence(self):
        pre_existing = _control(
            control_id="a" * 32,
            state=ControlState.SATISFIED,
            last_assessed_at=NOW,
            last_evidence_at="2026-01-01T00:00:00+00:00",  # predates this run
        )
        freshly_evidenced = _control(
            control_id="b" * 32,
            state=ControlState.SATISFIED,
            last_assessed_at=NOW,
            last_evidence_at=NOW,  # confirmed by this very run
        )
        not_satisfied = _control(
            control_id="c" * 32,
            state=ControlState.EVIDENCE_MISSING,
            last_assessed_at=NOW,
            last_evidence_at="2026-01-01T00:00:00+00:00",
        )
        rows = detect_manifest_change(
            [pre_existing, freshly_evidenced, not_satisfied],
            "fp-old",
            "fp-new",
            now=NOW,
        )
        assert [r.control_id for r in rows] == [pre_existing.control_id]
        assert rows[0].stale_reason == StaleReason.MANIFEST_CHANGED

    def test_control_with_no_evidence_at_all_is_flagged(self):
        control = _control(
            control_id="d" * 32,
            state=ControlState.SATISFIED,
            last_assessed_at=NOW,
            last_evidence_at=None,
        )
        rows = detect_manifest_change([control], "fp-old", "fp-new", now=NOW)
        assert [r.control_id for r in rows] == [control.control_id]


class TestApplyStale:
    def test_flips_only_named_controls(self):
        stale = _control(control_id="a" * 32)
        fresh = _control(control_id="b" * 32)
        rows = detect_stale([stale], CATALOG, "2099-01-01T00:00:00+00:00")
        assert rows  # sanity: our fixture actually produced a row

        updated = apply_stale([stale, fresh], rows)
        by_id = {c.control_id: c for c in updated}
        assert by_id[stale.control_id].state == ControlState.EVIDENCE_STALE
        assert by_id[fresh.control_id].state == fresh.state
