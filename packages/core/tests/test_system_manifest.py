"""
Unit tests for the ANNEX-FIELDS SystemManifest additions (Annex IV Sections
4, 6-9 provider attestations).

Covers: the eight fields round-trip through JSON, and a legacy manifest JSON
written before these fields existed still validates, falling back to the
documented defaults (None / empty list).
"""

from __future__ import annotations

import json

from opencomplai_core.models import SystemManifest


def _manifest_kwargs(**overrides: object) -> dict:
    base: dict[str, object] = {
        "system_id": "sys-1",
        "intended_purpose": "credit scoring",
        "compliance_target": "EU_AI_ACT",
        "high_risk_presumption": True,
        "commit_ref": "abc123",
        "training_data_description": "internal loan applications 2018-2024",
        "model_architecture": "gradient boosted trees",
    }
    base.update(overrides)
    return base


def test_annex_iv_attestation_fields_round_trip_through_json():
    """The eight Annex IV Section 4/6-9 fields must survive a JSON round-trip
    unchanged, since they flow from `opencomplai init` to the doc-generator
    purely through the serialized manifest."""
    manifest = SystemManifest(
        **_manifest_kwargs(
            metrics_appropriateness_rationale="Recall matches the screening use case.",
            lifecycle_changes=["v1.1: recalibrated threshold"],
            change_log_reference="CHANGELOG.md#v1.1",
            harmonised_standards=["EN ISO/IEC 42001:2023"],
            alternative_solutions="Manual review fallback pending certification.",
            eu_declaration_of_conformity_ref="DoC-2026-001",
            post_market_monitoring_plan_ref="docs/pmm-plan.md",
            post_market_monitoring_summary="Quarterly drift review with sign-off.",
        )
    )

    round_tripped = SystemManifest.model_validate_json(manifest.model_dump_json())

    assert round_tripped.metrics_appropriateness_rationale == (
        "Recall matches the screening use case."
    )
    assert round_tripped.lifecycle_changes == ["v1.1: recalibrated threshold"]
    assert round_tripped.change_log_reference == "CHANGELOG.md#v1.1"
    assert round_tripped.harmonised_standards == ["EN ISO/IEC 42001:2023"]
    assert round_tripped.alternative_solutions == (
        "Manual review fallback pending certification."
    )
    assert round_tripped.eu_declaration_of_conformity_ref == "DoC-2026-001"
    assert round_tripped.post_market_monitoring_plan_ref == "docs/pmm-plan.md"
    assert round_tripped.post_market_monitoring_summary == (
        "Quarterly drift review with sign-off."
    )


def test_legacy_manifest_json_without_annex_iv_fields_still_validates():
    """A manifest JSON persisted before ANNEX-FIELDS existed (no Section 4/6-9
    keys at all) must still validate, defaulting the new fields to None/[]."""
    legacy_json = json.dumps(_manifest_kwargs())

    manifest = SystemManifest.model_validate_json(legacy_json)

    assert manifest.metrics_appropriateness_rationale is None
    assert manifest.lifecycle_changes == []
    assert manifest.change_log_reference is None
    assert manifest.harmonised_standards == []
    assert manifest.alternative_solutions is None
    assert manifest.eu_declaration_of_conformity_ref is None
    assert manifest.post_market_monitoring_plan_ref is None
    assert manifest.post_market_monitoring_summary is None
