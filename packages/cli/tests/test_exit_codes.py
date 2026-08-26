"""One shared exit-code mapping, every ScanResult member accounted for, and
the connector console scripts actually installed.

The result-to-exit-code contract used to live in three hand-copied tables
(main._exit_code plus one per CI connector); a ScanResult value added to
one but not the others fell through the connectors' .get() to `return 0` —
CI passing on a result `opencomplai check` would have failed.
"""

from __future__ import annotations

import importlib.metadata

from opencomplai_cli.exit_codes import HARD_FAIL_EXIT_CODES
from opencomplai_core.models import ScanResult

# Documented as intentionally absent from HARD_FAIL_EXIT_CODES — see the
# comment in exit_codes.py.
_INTENTIONALLY_EXCLUDED = {
    ScanResult.PASS,
    ScanResult.DEGRADED_COMPLETE,
}


def test_hard_fail_codes_cover_every_scan_result_except_documented():
    covered = set(HARD_FAIL_EXIT_CODES) | _INTENTIONALLY_EXCLUDED
    missing = set(ScanResult) - covered
    assert not missing, (
        f"ScanResult member(s) {missing} have no exit-code mapping and are "
        "not documented as intentionally excluded in exit_codes.py — the "
        "connectors would silently exit 0 for them"
    )


def test_connectors_share_the_single_mapping_object():
    from opencomplai_cli.connectors import github_actions, gitlab_ci

    assert github_actions._EXIT_CODE_BY_RESULT is HARD_FAIL_EXIT_CODES
    assert gitlab_ci._EXIT_CODE_BY_RESULT is HARD_FAIL_EXIT_CODES


def test_plain_artifact_strings_hit_the_enum_keyed_mapping():
    # Connectors look up the raw string parsed from the JSON artifact;
    # StrEnum keys must remain reachable through it.
    assert HARD_FAIL_EXIT_CODES.get("policy_block") == 3
    assert HARD_FAIL_EXIT_CODES.get("trap_detected") == 4


def test_main_exit_code_agrees_with_the_shared_mapping():
    from opencomplai_cli.main import _exit_code

    for result, code in HARD_FAIL_EXIT_CODES.items():
        assert _exit_code(ScanResult(result), scan_mode="ci") == code
    assert _exit_code(ScanResult.PASS, scan_mode="ci") == 0
    assert _exit_code(ScanResult.DEGRADED_COMPLETE, scan_mode="ci") == 1
    assert _exit_code(ScanResult.DEGRADED_COMPLETE, scan_mode="local") == 0


def _load_console_script(name: str):
    eps = list(importlib.metadata.entry_points(group="console_scripts", name=name))
    assert eps, f"no console_scripts entry point named {name!r} is registered"
    return eps[0].load()


def test_gha_connector_entry_point_resolves_to_a_callable():
    assert callable(_load_console_script("opencomplai-gha-connector"))


def test_gitlab_connector_entry_point_resolves_to_a_callable():
    assert callable(_load_console_script("opencomplai-gitlab-connector"))
