"""Tests for the core assessment engine."""

from opencomplai_core.engine import assess
from opencomplai_core.models import AssessmentInput, ModelMetadata, RiskLevel


def _make_input(use_case: str) -> AssessmentInput:
    """Helper to build a minimal AssessmentInput."""
    return AssessmentInput(
        model=ModelMetadata(
            name="test-model",
            version="1.0.0",
            modality="text",
            use_case=use_case,
            deployment_context="production",
        )
    )


def test_minimal_risk_general_use_case():
    result = assess(_make_input("customer support chatbot"))
    assert result.risk_level == RiskLevel.MINIMAL
    assert result.rules_passed == result.rules_evaluated


def test_high_risk_employment_use_case():
    result = assess(_make_input("employment screening and ranking"))
    assert result.risk_level == RiskLevel.HIGH
    assert result.rules_failed >= 1


def test_high_risk_biometric_use_case():
    result = assess(_make_input("biometric identification system"))
    assert result.risk_level == RiskLevel.HIGH


def test_result_has_evidence():
    result = assess(_make_input("customer support chatbot"))
    assert result.evidence_summary
    assert result.generated_at
    assert len(result.rule_results) > 0


def test_result_rule_counts_consistent():
    result = assess(_make_input("customer support chatbot"))
    assert result.rules_evaluated == result.rules_passed + result.rules_failed


def test_system_manifest_model():
    """SystemManifest must be importable and validatable from core models."""
    from opencomplai_core.models import SystemManifest

    m = SystemManifest(
        system_id="test-sys",
        intended_purpose="customer support chatbot",
        compliance_target="EU_AI_ACT",
        high_risk_presumption=False,
        commit_ref="abc123",
    )
    assert m.system_id == "test-sys"


def test_scan_status_artifact_model():
    """ScanStatusArtifact must be importable and validatable from core models."""
    from opencomplai_core.models import ScanResult, ScanStatusArtifact

    artifact = ScanStatusArtifact(
        install_id="uuid-1",
        system_id="test-sys",
        commit_ref="abc123",
        result=ScanResult.PASS,
        failed_controls=[],
        evidence_hashes=["sha256:abc"],
        rationale_hash="sha256:def",
        duration_ms=1200,
        pending_verifications_count=0,
    )
    assert artifact.result == ScanResult.PASS
    assert artifact.signature is None  # unsigned in OSS mode


def test_scan_status_artifact_controls_absent_from_dict_parses_as_none():
    """CTRL-ARTIFACT back-compat: an artifact dict predating the `controls`
    block (no `controls` key at all) must still validate, with `controls`
    defaulting to None — same contract as the `gap_report` donor field."""
    from opencomplai_core.models import ScanResult, ScanStatusArtifact

    raw = {
        "install_id": "uuid-1",
        "system_id": "test-sys",
        "commit_ref": "abc123",
        "result": ScanResult.PASS.value,
        "failed_controls": [],
        "evidence_hashes": ["sha256:abc"],
        "rationale_hash": "sha256:def",
        "duration_ms": 1200,
        "pending_verifications_count": 0,
    }
    assert "controls" not in raw
    artifact = ScanStatusArtifact.model_validate(raw)
    assert artifact.controls is None


def test_scan_status_artifact_controls_block_round_trips():
    """An artifact carrying a `controls` block survives model_validate(model_dump())."""
    from opencomplai_core.models import (
        ControlsSummary,
        ControlState,
        ControlSummaryRow,
        ScanResult,
        ScanStatusArtifact,
    )

    controls = ControlsSummary(
        summary={
            "satisfied": 1,
            "evidence_missing": 1,
            "evidence_stale": 0,
            "pending_review": 0,
            "waived": 0,
        },
        items=[
            ControlSummaryRow(
                control_id="c1",
                article_ref="Art. 9",
                state=ControlState.SATISFIED,
                owner="alice",
                due_at="2026-09-01T00:00:00+00:00",
            ),
            ControlSummaryRow(
                control_id="c2",
                article_ref="Art. 10",
                state=ControlState.EVIDENCE_MISSING,
                owner=None,
                due_at=None,
            ),
        ],
    )
    artifact = ScanStatusArtifact(
        install_id="uuid-1",
        system_id="test-sys",
        commit_ref="abc123",
        result=ScanResult.PASS,
        failed_controls=[],
        evidence_hashes=["sha256:abc"],
        rationale_hash="sha256:def",
        duration_ms=1200,
        pending_verifications_count=0,
        controls=controls,
    )

    round_tripped = ScanStatusArtifact.model_validate(artifact.model_dump())
    assert round_tripped == artifact
