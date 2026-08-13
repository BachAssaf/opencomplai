"""
Annex IV dossier generator.

Builds a complete AnnexIVDossier from a system manifest and risk assessment
result. Computes and sets the bundle_checksum. Local signing support via
a private key file if LOCAL_SIGNING_KEY_PATH is configured.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

from opencomplai_core.dossier import (
    PROVIDER_SUPPLIED_PLACEHOLDER,
    AnnexIVDossier,
    AnnexIVSection1,
    AnnexIVSection2,
    AnnexIVSection3,
    AnnexIVSection4,
    AnnexIVSection5,
    AnnexIVSection6,
    AnnexIVSection7,
    AnnexIVSection8,
    AnnexIVSection9,
    ArticleTwelveRecordKeeping,
)
from opencomplai_core.models import CorroborationReport, RiskResult, SystemManifest
from opencomplai_core.rules import RULE_SET_VERSION


def _manifest_str(manifest: SystemManifest, field: str) -> str | None:
    """Read an optional provider-attestation field the manifest may not define."""
    value = getattr(manifest, field, None)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _manifest_list(manifest: SystemManifest, field: str) -> list[str]:
    value = getattr(manifest, field, None)
    return list(value) if isinstance(value, (list, tuple)) and value else []


def _build_section6(manifest: SystemManifest) -> AnnexIVSection6:
    """Annex IV pt.6 — relevant changes through the system's lifecycle."""
    changes = _manifest_list(manifest, "lifecycle_changes")
    ref = _manifest_str(manifest, "change_log_reference")
    supplied = bool(changes or ref)
    return AnnexIVSection6(
        changes=changes,
        change_log_reference=ref,
        note="" if supplied else PROVIDER_SUPPLIED_PLACEHOLDER,
        provider_supplied=supplied,
    )


def _build_section7(manifest: SystemManifest) -> AnnexIVSection7:
    """Annex IV pt.7 — harmonised standards applied, or alternative solutions."""
    standards = _manifest_list(manifest, "harmonised_standards")
    alternative = _manifest_str(manifest, "alternative_solutions")
    supplied = bool(standards or alternative)
    return AnnexIVSection7(
        harmonised_standards=standards,
        alternative_solutions=alternative,
        note="" if supplied else PROVIDER_SUPPLIED_PLACEHOLDER,
        provider_supplied=supplied,
    )


def _build_section8(manifest: SystemManifest) -> AnnexIVSection8:
    """Annex IV pt.8 — reference to the EU declaration of conformity (Art. 47)."""
    ref = _manifest_str(manifest, "eu_declaration_of_conformity_ref")
    return AnnexIVSection8(
        declaration_reference=ref,
        note="" if ref else PROVIDER_SUPPLIED_PLACEHOLDER,
        provider_supplied=bool(ref),
    )


def _build_section9(manifest: SystemManifest) -> AnnexIVSection9:
    """Annex IV pt.9 — post-market monitoring plan (Art. 72)."""
    ref = _manifest_str(manifest, "post_market_monitoring_plan_ref")
    summary = _manifest_str(manifest, "post_market_monitoring_summary")
    supplied = bool(ref or summary)
    return AnnexIVSection9(
        monitoring_plan_reference=ref,
        plan_summary=summary,
        note="" if supplied else PROVIDER_SUPPLIED_PLACEHOLDER,
        provider_supplied=supplied,
    )


def _performance_metrics_with_evals(
    base: dict[str, float], eval_report: object | None
) -> dict[str, float]:
    from opencomplai_core.models import EvalReport

    perf = dict(base)
    if isinstance(eval_report, EvalReport):
        for r in eval_report.results:
            perf[f"eval_{r.category.value}_score"] = r.score
    return perf


def generate_dossier(
    manifest: SystemManifest,
    risk_result: RiskResult,
    evidence_hashes: list[str] | None = None,
    ledger_root_hash: str | None = None,
    provider_name: str = "Unknown Provider",
    eval_report: object | None = None,
    corroboration_report: CorroborationReport | None = None,
) -> AnnexIVDossier:
    """
    Generate an Annex IV dossier from a system manifest and risk result.

    Args:
        manifest: The system manifest describing the AI system.
        risk_result: The risk assessment result from the risk engine.
        evidence_hashes: SHA-256 hashes of evidence objects in the vault.
        ledger_root_hash: Current Merkle root of the evidence ledger.
        provider_name: Name of the AI system provider.

    Returns:
        AnnexIVDossier with all sections populated and bundle_checksum set.
    """
    dossier_id = str(uuid.uuid4())
    generated_at = datetime.now(UTC).isoformat()

    from opencomplai_core.evaluators.registry import EVAL_SET_VERSION
    from opencomplai_core.models import EvalReport

    failed_rule_ids = [r.rule_id for r in risk_result.rule_results if not r.passed]
    eval_evidence_hashes: list[str] = []
    eval_overall: str | None = None
    eval_set_version: str | None = None
    if isinstance(eval_report, EvalReport):
        eval_set_version = eval_report.eval_set_version
        eval_overall = eval_report.overall_outcome.value
        eval_evidence_hashes = [r.evidence_hash for r in eval_report.results]
        for r in eval_report.results:
            if r.outcome.value == "fail":
                failed_rule_ids.append(r.evaluator_id)

    rationale = json.dumps(
        [{"rule_id": r.rule_id, "passed": r.passed} for r in risk_result.rule_results],
        sort_keys=True,
    )
    rationale_hash = f"sha256:{hashlib.sha256(rationale.encode()).hexdigest()}"

    # Determine whether Section 2 is substantively complete.
    # Required meaningful fields: training_data_description AND model_architecture.
    # A field is considered "stub" when it was not supplied by the manifest and
    # falls back to the placeholder string set in the generator.
    _stub = "Not specified in this release."
    _section2_training_complete = bool(
        manifest.training_data_description
        and manifest.training_data_description != _stub
    )
    _section2_arch_complete = bool(
        manifest.model_architecture and manifest.model_architecture != _stub
    )
    _is_high_risk = risk_result.risk_level.value == "high"
    # section2_complete is False only when high-risk AND at least one required
    # field is missing/stub.  For non-high-risk systems stubs are acceptable.
    section2_complete = not _is_high_risk or (
        _section2_training_complete and _section2_arch_complete
    )

    # Annex IV points 6-9 are provider attestations. For a HIGH-risk system a
    # placeholder in any of them means the file is not a complete Annex IV
    # dossier, and must not be presented as one.
    _sections_6_9 = (
        _build_section6(manifest),
        _build_section7(manifest),
        _build_section8(manifest),
        _build_section9(manifest),
    )
    annex_iv_complete = not _is_high_risk or all(
        section.provider_supplied for section in _sections_6_9
    )

    dossier = AnnexIVDossier(
        dossier_id=dossier_id,
        system_id=manifest.system_id,
        commit_ref=manifest.commit_ref,
        generated_at=generated_at,
        compliance_target=manifest.compliance_target,
        section2_complete=section2_complete,
        rule_version=RULE_SET_VERSION,
        assessed_against="Reg. (EU) 2024/1689",
        scope_disclaimer=(
            "This assessment was generated by Opencomplai (rule-based, deterministic engine). "
            "It constitutes structured evidence, not legal advice. "
            f"Assessment against Reg. (EU) 2024/1689. "
            f"Rule set version: {RULE_SET_VERSION}. "
            "Generation timestamp is recorded in generated_at."
        ),
        section1=AnnexIVSection1(
            system_name=manifest.system_id,
            system_version=manifest.commit_ref,
            provider_name=provider_name,
            intended_purpose=manifest.intended_purpose,
            compliance_target=manifest.compliance_target,
            risk_class=risk_result.risk_level.value,
            deployment_context="production",
        ),
        section2=AnnexIVSection2(
            training_data_description=(
                manifest.training_data_description or "Not specified in this release."
            ),
            model_architecture=(
                manifest.model_architecture or "Not specified in this release."
            ),
            performance_metrics=_performance_metrics_with_evals(
                manifest.performance_metrics, eval_report
            ),
            known_limitations=list(manifest.known_limitations),
        ),
        section3=AnnexIVSection3(
            human_oversight_measures=(
                list(manifest.human_oversight_measures)
                if manifest.human_oversight_measures
                else ["HITL orchestrator enabled"]
            ),
            monitoring_approach=(
                manifest.monitoring_approach
                or "Evidence Vault + continuous CI compliance checks"
            ),
            incident_response_procedure=(
                manifest.incident_response_procedure or "See docs/incident-response.md"
            ),
        ),
        section4=AnnexIVSection4(
            metrics_reported=_performance_metrics_with_evals(
                manifest.performance_metrics, eval_report
            ),
            appropriateness_rationale=_manifest_str(
                manifest, "metrics_appropriateness_rationale"
            )
            or PROVIDER_SUPPLIED_PLACEHOLDER,
            known_metric_limitations=list(manifest.known_limitations),
            provider_supplied=bool(
                _manifest_str(manifest, "metrics_appropriateness_rationale")
            ),
        ),
        record_keeping=ArticleTwelveRecordKeeping(
            logging_enabled=True,
            log_retention_days=int(
                os.environ.get("LOG_RETENTION_DAYS", "2555")
            ),  # 7 years
            evidence_vault_enabled=True,
            ledger_root_hash=ledger_root_hash,
        ),
        section6=_build_section6(manifest),
        section7=_build_section7(manifest),
        section8=_build_section8(manifest),
        section9=_build_section9(manifest),
        annex_iv_complete=annex_iv_complete,
        section5=AnnexIVSection5(
            risk_assessment_id=f"ra_{rationale_hash[7:15]}",
            risk_level=risk_result.risk_level.value,
            rules_evaluated=risk_result.rules_evaluated,
            rules_passed=risk_result.rules_passed,
            rules_failed=risk_result.rules_failed,
            failed_rule_ids=failed_rule_ids,
            rationale_hash=rationale_hash,
            eval_set_version=eval_set_version or EVAL_SET_VERSION,
            eval_overall_outcome=eval_overall,
            eval_evidence_hashes=eval_evidence_hashes,
            scanner_version=(
                corroboration_report.scanner_version if corroboration_report else None
            ),
            corroboration_detected_categories=(
                corroboration_report.detected_categories if corroboration_report else []
            ),
            corroboration_discrepancies=(
                corroboration_report.discrepancies if corroboration_report else []
            ),
            corroboration_severity=(
                corroboration_report.severity.value if corroboration_report else None
            ),
            corroboration_review_status=None,
            corroboration_baseline_ref=(
                corroboration_report.baseline_ref if corroboration_report else None
            ),
            corroboration_report_hash=(
                corroboration_report.report_hash if corroboration_report else None
            ),
        ),
        evidence_hashes=(evidence_hashes or []) + eval_evidence_hashes,
    )

    # Compute bundle checksum over deterministic content only (excludes envelope
    # metadata and mutable/derived fields that don't represent document content).
    bundle_json = dossier.model_dump_json(
        exclude={
            "dossier_id",
            "generated_at",
            "bundle_checksum",
            "signature",
            "signature_status",
            "section2_complete",  # derived from section2 content; not part of the evidence hash
        }
    )
    bundle_checksum = f"sha256:{hashlib.sha256(bundle_json.encode()).hexdigest()}"
    dossier.bundle_checksum = bundle_checksum

    # Signing precedence: Ed25519 (Pro/Enterprise) → HMAC (OSS fallback) → unsigned.
    # Ed25519 wins when DOSSIER_SIGNING_KEY_PATH points at a PEM private key;
    # HMAC kicks in when only LOCAL_SIGNING_KEY_PATH is set. Each branch
    # updates signature_status so the dossier self-describes its trust level.
    ed25519_key_path = os.environ.get("DOSSIER_SIGNING_KEY_PATH")
    hmac_key_path = os.environ.get("LOCAL_SIGNING_KEY_PATH")

    if ed25519_key_path:
        signature = _sign_bundle_ed25519(bundle_json, ed25519_key_path)
        if signature is not None:
            dossier.signature = signature
            dossier.signature_status = "ed25519"
    elif hmac_key_path:
        signature = _sign_bundle(bundle_json, hmac_key_path)
        if signature is not None:
            dossier.signature = signature
            dossier.signature_status = "hmac-local"

    return dossier


def _sign_bundle(bundle_json: str, key_path: str) -> str | None:
    """
    Sign the dossier bundle JSON using a local private key file (HMAC-SHA256).

    OSS fallback when no Ed25519 key is configured. Verifiable only by holders
    of the same symmetric key — adequate for in-org integrity, not for an
    auditor who needs third-party verifiability.
    Returns base64-encoded signature, or None if signing fails.
    """
    try:
        key = Path(key_path).read_bytes()
        sig = hmac.new(key, bundle_json.encode("utf-8"), hashlib.sha256).digest()
        return base64.b64encode(sig).decode("utf-8")
    except Exception:
        return None


def _sign_bundle_ed25519(bundle_json: str, key_path: str) -> str | None:
    """
    Sign the dossier bundle JSON with an Ed25519 PEM private key.

    Pro/Enterprise path: produces an asymmetric signature an auditor can
    verify with the published public key without needing the private key.
    Returns base64-encoded signature, or None on any failure (missing key,
    wrong format, cryptography lib not installed).
    """
    try:
        from opencomplai_core.signing import SigningDomain, sign_bundle_bytes

        return sign_bundle_bytes(
            bundle_json.encode("utf-8"), Path(key_path), SigningDomain.DOSSIER_BUNDLE
        )
    except Exception:
        return None


def validate_dossier_schema(dossier: AnnexIVDossier) -> bool:
    """
    Validate that a dossier contains all required Annex IV sections and fields.

    Returns True if the schema is complete (REQ-DOC-001).
    This is the validator used in the CI release gate.

    For a HIGH-risk system this requires all nine Annex IV points, including
    the provider attestations in Sections 6-9. It previously inspected only
    six Section 1 fields plus two hashes, so a dossier carrying 5 of 9 sections
    passed the release gate as complete.
    """
    required_section1_fields = [
        "system_name",
        "system_version",
        "provider_name",
        "intended_purpose",
        "compliance_target",
        "risk_class",
    ]
    for field in required_section1_fields:
        if not getattr(dossier.section1, field, None):
            return False

    if not dossier.section5.rationale_hash:
        return False

    if not dossier.bundle_checksum:
        return False

    # Article 12 record-keeping must be present and enabled.
    if dossier.record_keeping is None:
        return False

    if dossier.section1.risk_class == "high":
        # Every Annex IV point must be attested, not merely instantiated.
        for section in (
            dossier.section6,
            dossier.section7,
            dossier.section8,
            dossier.section9,
        ):
            if not section.provider_supplied:
                return False
        if not dossier.annex_iv_complete:
            return False
        if not dossier.section4.provider_supplied:
            return False

    return True
