"""Tests for the HALT-WIRE system-state persistence store."""

from __future__ import annotations

from pathlib import Path

from opencomplai_core.models import SystemState
from opencomplai_core.system_state_store import load_state, save_state, state_record


def test_load_state_defaults_to_running_when_no_record(tmp_path: Path) -> None:
    assert load_state(tmp_path, "sys-a") == SystemState.RUNNING
    assert state_record(tmp_path, "sys-a") is None


def test_save_then_load_round_trips(tmp_path: Path) -> None:
    save_state(
        tmp_path,
        "sys-a",
        SystemState.HALTED_PENDING_REVIEW,
        reason="trap_detected",
        commit_ref="abc123",
    )

    assert load_state(tmp_path, "sys-a") == SystemState.HALTED_PENDING_REVIEW

    record = state_record(tmp_path, "sys-a")
    assert record is not None
    assert record["state"] == SystemState.HALTED_PENDING_REVIEW.value
    assert record["reason"] == "trap_detected"
    assert record["commit_ref"] == "abc123"
    assert record["changed_at"]  # ISO timestamp, non-empty


def test_save_state_overwrites_prior_record_for_same_system(tmp_path: Path) -> None:
    save_state(
        tmp_path,
        "sys-a",
        SystemState.HALTED_PENDING_REVIEW,
        reason="trap_detected",
        commit_ref="abc123",
    )
    save_state(
        tmp_path,
        "sys-a",
        SystemState.RUNNING,
        reason="approved by qa@example.com",
        commit_ref="abc123",
    )

    assert load_state(tmp_path, "sys-a") == SystemState.RUNNING
    record = state_record(tmp_path, "sys-a")
    assert record["reason"] == "approved by qa@example.com"


def test_per_system_isolation(tmp_path: Path) -> None:
    save_state(
        tmp_path,
        "sys-a",
        SystemState.HALTED_PENDING_REVIEW,
        reason="trap_detected",
        commit_ref="abc123",
    )

    # A different system_id in the same state directory stays untouched —
    # one halted system must not halt every other system tracked by the
    # same Opencomplai install.
    assert load_state(tmp_path, "sys-b") == SystemState.RUNNING
    assert state_record(tmp_path, "sys-b") is None

    save_state(
        tmp_path,
        "sys-b",
        SystemState.HALTED_PENDING_REVIEW,
        reason="high_risk_corroboration_failed",
        commit_ref="def456",
    )
    assert load_state(tmp_path, "sys-a") == SystemState.HALTED_PENDING_REVIEW
    assert load_state(tmp_path, "sys-b") == SystemState.HALTED_PENDING_REVIEW
    assert state_record(tmp_path, "sys-a")["reason"] == "trap_detected"
    assert state_record(tmp_path, "sys-b")["reason"] == "high_risk_corroboration_failed"


def test_corrupt_state_file_treated_as_no_record(tmp_path: Path) -> None:
    (tmp_path / "system-state.json").write_text("not valid json")
    assert load_state(tmp_path, "sys-a") == SystemState.RUNNING
    assert state_record(tmp_path, "sys-a") is None
