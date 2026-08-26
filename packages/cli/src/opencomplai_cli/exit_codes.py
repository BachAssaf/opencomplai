"""Shared process exit-code mapping for the ScanResult contract.

One source of truth for main.py's _exit_code and every CI connector
(connectors/github_actions.py, connectors/gitlab_ci.py), which each
previously carried an identical hand-copied dict — a third copy that could
silently drift and fall through to exit 0 for any ScanResult value added
later, exactly the "CI passes when opencomplai check would have failed"
class of bug FINDING 48.8 closed for policy_block/trap_detected.
"""

from __future__ import annotations

from opencomplai_core.models import ScanResult

#: Exit codes for the ScanResult states that always mean "fail the build",
#: regardless of caller. PASS and DEGRADED_COMPLETE are deliberately absent:
#: PASS is always 0, and DEGRADED_COMPLETE's code depends on scan_mode (see
#: main._exit_code), so each caller handles both via its own fallback.
#: ScanResult is a StrEnum, so lookups with the plain artifact strings
#: ("policy_block", ...) hit these keys too.
HARD_FAIL_EXIT_CODES: dict[str, int] = {
    ScanResult.CONTROL_FAIL: 1,
    ScanResult.VALIDATION_FAIL: 2,
    ScanResult.POLICY_BLOCK: 3,
    ScanResult.TRAP_DETECTED: 4,
}
