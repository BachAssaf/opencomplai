"""Unit tests for EvidenceObject provenance/freshness metadata (EVID-PROV).

Covers: round-trip with and without the optional provenance fields, and
back-compat loading of pre-existing JSON that predates these fields.
"""

from __future__ import annotations

from opencomplai_core.models import EvidenceObject


def test_round_trip_with_provenance():
    obj = EvidenceObject(
        evidence_id="ev-1",
        content_hash="sha256:abc",
        storage_uri="file:///tmp/abc",
        source="risk-engine",
        source_version="1.2.3",
        collected_at="2026-01-01T00:00:00+00:00",
        valid_until="2026-06-01T00:00:00+00:00",
    )
    dumped = obj.model_dump_json()
    restored = EvidenceObject.model_validate_json(dumped)
    assert restored == obj
    assert restored.source == "risk-engine"
    assert restored.source_version == "1.2.3"
    assert restored.collected_at == "2026-01-01T00:00:00+00:00"
    assert restored.valid_until == "2026-06-01T00:00:00+00:00"


def test_round_trip_without_provenance():
    obj = EvidenceObject(
        evidence_id="ev-2",
        content_hash="sha256:def",
        storage_uri="file:///tmp/def",
    )
    dumped = obj.model_dump_json()
    restored = EvidenceObject.model_validate_json(dumped)
    assert restored == obj
    assert restored.source is None
    assert restored.source_version is None
    assert restored.collected_at is None
    assert restored.valid_until is None


def test_old_format_json_without_new_fields_still_loads():
    # Simulates a pre-existing CAS object serialized before provenance fields
    # existed — only the original three fields are present.
    old_json = (
        '{"evidence_id": "ev-3", "content_hash": "sha256:ghi", '
        '"storage_uri": "file:///tmp/ghi"}'
    )
    restored = EvidenceObject.model_validate_json(old_json)
    assert restored.evidence_id == "ev-3"
    assert restored.content_hash == "sha256:ghi"
    assert restored.storage_uri == "file:///tmp/ghi"
    assert restored.source is None
    assert restored.source_version is None
    assert restored.collected_at is None
    assert restored.valid_until is None
