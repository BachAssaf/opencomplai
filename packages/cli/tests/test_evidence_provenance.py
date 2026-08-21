"""CLI evidence-emission provenance tests (EVID-PROV).

Confirms scanner-emitted ledger evidence carries a `provenance` block
identifying opencomplai-cli as the source, with the scanner/detector version.
"""

from __future__ import annotations

from opencomplai_cli import __version__
from opencomplai_cli.main import _redacted_report_payload
from opencomplai_core.models import CorroborationReport, DiscrepancySeverity


def _report(**overrides: object) -> CorroborationReport:
    base: dict[str, object] = {
        "scan_id": "scan_test",
        "system_id": "test",
        "commit_ref": "HEAD",
        "scanner_version": "0.1.0",
        "input_digest": "abc",
        "config_hash": "def",
        "detector_versions": {},
        "declared_purpose": "essential services scoring",
        "declared_categories": ["essential_services"],
        "evidence": [],
        "findings": [],
        "detected_categories": ["essential_services"],
        "discrepancies": [],
        "score_breakdown": {},
        "severity": DiscrepancySeverity.NONE,
        "feature_summary": {},
        "cache_summary": {},
        "skipped_paths": [],
        "limits_hit": [],
        "warnings": [],
        "detector_errors": [],
        "baseline_ref": None,
        "generated_at": "2026-01-01T00:00:00Z",
        "report_hash": "hash",
    }
    base.update(overrides)
    return CorroborationReport(**base)


def test_redacted_report_payload_includes_cli_provenance():
    report = _report()
    payload = _redacted_report_payload(report)

    assert payload["provenance"]["source"] == "opencomplai-cli"
    assert payload["provenance"]["source_version"] == __version__
    assert payload["provenance"]["detector_version"] == report.scanner_version
    assert "collected_at" in payload["provenance"]
