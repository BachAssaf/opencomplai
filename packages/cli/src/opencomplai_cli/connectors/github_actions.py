"""
GitHub Actions CI connector (C1.5).

Wraps the ``opencomplai check`` command with GitHub Actions platform
conventions:

* Sets a step output ``result`` to the artifact result value so downstream
  steps can consume it.
* Annotates the run summary via ``::notice::`` / ``::warning::`` / ``::error::``
  workflow commands.
* Propagates exit code: ``result=control_fail`` → non-zero exit (fails the build).
* Publishes the signed status artifact to the dashboard via the standard ingest
  path when ``OPENCOMPLAI_DASHBOARD_URL`` and a bootstrap token are configured.

Environment variables consumed
-------------------------------
``GITHUB_ACTIONS``           — set to ``true`` by GitHub; connector activates.
``GITHUB_OUTPUT``            — path to the step-output file.
``GITHUB_STEP_SUMMARY``      — path to the job summary markdown file.
``OPENCOMPLAI_DASHBOARD_URL`` — dashboard ingest base URL.
``OPENCOMPLAI_TENANT_ID``    — tenant ID for the current org.
``OPENCOMPLAI_API_KEY``      — an ``ock_...`` API key (GO-LIVE CORE-3/-4).
                               Takes precedence over everything below: when
                               set, the connector sends the key-authed
                               envelope (no ``install_id``) and never
                               touches ``OPENCOMPLAI_AUTH_TOKEN`` or OIDC.
``OPENCOMPLAI_AUTH_TOKEN``   — bearer token for dashboard ingest (legacy
                               JWT path, only consulted when
                               OPENCOMPLAI_API_KEY is unset). Takes
                               precedence over the three vars below when set.
``OPENCOMPLAI_CLIENT_ID``       — OIDC client_id from /enroll, used only when
                                   OPENCOMPLAI_AUTH_TOKEN is unset.
``OPENCOMPLAI_CLIENT_SECRET``   — OIDC client_secret from /enroll (same gate).
``OPENCOMPLAI_TOKEN_ENDPOINT``  — the IdP's OAuth2 token endpoint (same gate).

Exit codes
----------
0  — scan passed (result in {pass, trap_detected}).
1  — scan failed (result=control_fail) or unexpected error.
2  — configuration error (missing required env var, bad token, etc.).

Usage in a workflow step
------------------------

```yaml
- name: Opencomplai compliance scan
  uses: actions/setup-python@v5
  with: { python-version: "3.11" }
- run: pip install opencomplai
- run: opencomplai-gha-connector
  env:
    OPENCOMPLAI_DASHBOARD_URL: ${{ secrets.OPENCOMPLAI_DASHBOARD_URL }}
    OPENCOMPLAI_TENANT_ID: ${{ secrets.OPENCOMPLAI_TENANT_ID }}
    OPENCOMPLAI_AUTH_TOKEN: ${{ secrets.OPENCOMPLAI_AUTH_TOKEN }}
```
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

# `check --sign` (main.py's check_cmd) always writes its artifact here,
# relative to whatever cwd the subprocess inherited -- this connector never
# passes cwd= to subprocess.run, so that is this process's own cwd too.
_ARTIFACT_FILENAME = "compliance-artifact.json"

# ---------------------------------------------------------------------------
# Platform detection helpers
# ---------------------------------------------------------------------------

RUNNING_IN_GHA = os.environ.get("GITHUB_ACTIONS") == "true"


def _gha_cmd(cmd: str, value: str) -> None:
    """Write a GitHub Actions workflow command to stdout."""
    print(f"::{cmd}::{value}", flush=True)


def _set_output(name: str, value: str) -> None:
    output_file = os.environ.get("GITHUB_OUTPUT", "")
    if output_file:
        with open(output_file, "a") as fh:
            fh.write(f"{name}={value}\n")
    else:
        _gha_cmd("set-output", f"name={name}::{value}")


def _append_summary(markdown: str) -> None:
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY", "")
    if summary_file:
        with open(summary_file, "a") as fh:
            fh.write(markdown + "\n")


def _annotate(level: str, message: str) -> None:
    """level: notice | warning | error"""
    _gha_cmd(level, message)


# ---------------------------------------------------------------------------
# Core connector
# ---------------------------------------------------------------------------


def run_connector(
    check_args: list[str] | None = None,
    env: dict[str, str] | None = None,
) -> int:
    """
    Run ``opencomplai check --sign`` and handle GHA platform conventions.

    ``check_args`` is appended to the base command (for testing).
    ``env`` overrides environment variables (for testing).

    Returns the process exit code (0 = pass, 1 = fail).
    """
    _env = {**os.environ, **(env or {})}

    cmd = ["opencomplai", "check", "--sign"] + (check_args or [])
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=_env,
        )
    except FileNotFoundError:
        msg = "opencomplai CLI not found. Install with: pip install opencomplai"
        _annotate("error", msg)
        return 2

    stdout = result.stdout
    stderr = result.stderr

    # Prefer the artifact `check --sign` wrote to disk. The `cmd` list
    # above never passes `-o json`, so real stdout is Rich's indent=2
    # human/JSON output (`console.print_json`) -- never the single-line
    # JSON-object-per-line shape `_parse_artifact_result` scans for, so
    # that parse essentially never matches a real CI run and this
    # connector silently never published anything. The stdout parse is
    # kept as a fallback for a caller who passes `-o json` explicitly via
    # `check_args`, or if the artifact file is missing for some other
    # reason (e.g. `check` crashed before writing it).
    artifact_result = _read_disk_artifact(
        Path(_ARTIFACT_FILENAME)
    ) or _parse_artifact_result(stdout)

    # Set step output.
    if artifact_result:
        _set_output("result", artifact_result.get("result", "unknown"))
        _set_output("content_hash", artifact_result.get("content_hash", ""))

    # Annotate.
    scan_result = artifact_result.get("result", "") if artifact_result else ""
    if scan_result == "control_fail":
        _annotate(
            "error",
            f"Opencomplai: control_fail — {_failed_controls_summary(artifact_result)}",
        )
    elif scan_result == "trap_detected":
        _annotate("warning", "Opencomplai: trap_detected — review required")
    elif scan_result:
        _annotate("notice", f"Opencomplai: {scan_result}")

    # Job summary.
    _append_summary(_build_summary(artifact_result, stdout, stderr))

    # Publish to dashboard.
    _publish_to_dashboard(artifact_result, _env)

    # Exit code propagation: control_fail is a CI failure.
    if scan_result == "control_fail":
        return 1
    if result.returncode != 0 and not scan_result:
        return result.returncode
    return 0


def _read_disk_artifact(path: Path) -> dict[str, Any] | None:
    """Read a ``ScanStatusArtifact`` dict written to disk by ``check --sign``.

    Returns ``None`` (never raises) on anything short of a real artifact --
    missing file, unreadable/non-UTF-8, invalid JSON, JSON that isn't an
    object, or an object missing the ``result`` field -- so callers can
    fall back to the stdout parse in every one of those cases exactly as
    if the disk read had never been attempted.
    """
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or "result" not in data:
        return None
    return data


def _parse_artifact_result(stdout: str) -> dict[str, Any] | None:
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                obj = json.loads(line)
                if "result" in obj:
                    return obj
            except json.JSONDecodeError:
                pass
    return None


def _failed_controls_summary(artifact: dict | None) -> str:
    if not artifact:
        return "unknown"
    controls = artifact.get("failed_controls", [])
    if not controls:
        return "see output"
    return ", ".join(str(c) for c in controls[:5])


def _build_summary(artifact: dict | None, stdout: str, stderr: str) -> str:
    result = artifact.get("result", "unknown") if artifact else "unknown"
    system_id = artifact.get("system_id", "unknown") if artifact else "unknown"
    commit_ref = artifact.get("commit_ref", "") if artifact else ""
    lines = [
        "## Opencomplai Compliance Scan",
        "",
        "| Field | Value |",
        "|-------|-------|",
        f"| Result | `{result}` |",
        f"| System | `{system_id}` |",
        f"| Commit | `{commit_ref}` |",
    ]
    if artifact and artifact.get("failed_controls"):
        lines.append(f"| Failed controls | `{_failed_controls_summary(artifact)}` |")
    eval_summary = artifact.get("eval_summary") if artifact else None
    if isinstance(eval_summary, dict):
        lines.append(
            f"| Eval outcome | `{eval_summary.get('overall_outcome', 'n/a')}` |"
        )
    elif artifact and artifact.get("eval_overall_outcome"):
        lines.append(f"| Eval outcome | `{artifact['eval_overall_outcome']}` |")
    return "\n".join(lines)


def _publish_to_dashboard(artifact: dict | None, env: dict[str, str]) -> None:
    if not artifact:
        return
    base_url = env.get("OPENCOMPLAI_DASHBOARD_URL", "").rstrip("/")
    if not base_url:
        return

    from opencomplai_cli.publish import (
        envelope_signature,
        prepare_scan_status_artifact,
        publish_scan_status,
    )

    # G-4: the raw artifact never validated against ingest's schema
    # (additionalProperties: false rejected evidence_hashes/rationale_hash/
    # scan_summary/gap_report/eval_summary/install_id outright, and
    # commit_ref="HEAD" failed minLength: 7) — every payload this connector
    # ever sent was rejected with a 422. prepare_scan_status_artifact fills
    # in only what the OSS artifact genuinely lacks (policy_bundle_version,
    # timestamp, a real commit_ref); everything else passes through
    # untouched onto the now-widened schema.

    api_key = env.get("OPENCOMPLAI_API_KEY", "")
    if api_key:
        # ock_ key-authed path (GO-LIVE CORE-3/CORE-4) — the same auth
        # `opencomplai push` uses. Takes priority over OPENCOMPLAI_AUTH_TOKEN
        # and the OIDC client-credentials grant below: a caller who sets
        # OPENCOMPLAI_API_KEY has opted into the key-authed contract, which
        # is a different envelope shape (no install_id at all — the server
        # derives it from the key's own project), not just a different
        # bearer value on the same envelope.
        prepared = prepare_scan_status_artifact(artifact, commit_env=env)
        envelope = {
            "system_id": prepared.get("system_id", "unknown"),
            "artifact": prepared,
            "signature": envelope_signature(artifact, prepared),
        }
        status, data = publish_scan_status(base_url, api_key, envelope)
        if status not in (200, 201):
            _annotate(
                "warning",
                f"Opencomplai dashboard publish failed: HTTP {status} {data}",
            )
        return

    # Legacy JWT/OIDC path (pre-CORE-3 envelope contract). An operator-set
    # OPENCOMPLAI_AUTH_TOKEN always wins. If unset, acquire one via the
    # OIDC client-credentials grant using the pair /enroll issued
    # (AUTH-SAAS) — same trio of env vars, no manual token pasting.
    token = env.get("OPENCOMPLAI_AUTH_TOKEN", "")
    if not token:
        client_id = env.get("OPENCOMPLAI_CLIENT_ID", "")
        client_secret = env.get("OPENCOMPLAI_CLIENT_SECRET", "")
        token_endpoint = env.get("OPENCOMPLAI_TOKEN_ENDPOINT", "")
        if client_id and client_secret and token_endpoint:
            from opencomplai_cli.oidc_client import OidcTokenError, acquire_token

            try:
                token, _ = acquire_token(token_endpoint, client_id, client_secret)
            except OidcTokenError as exc:
                _annotate(
                    "warning", f"Opencomplai OIDC token acquisition failed: {exc}"
                )

    if not token:
        return

    prepared = prepare_scan_status_artifact(artifact, commit_env=env)

    install_id = env.get("OPENCOMPLAI_INSTALL_ID", "unknown")
    envelope = {
        "install_id": install_id,
        "system_id": prepared.get("system_id", "unknown"),
        "artifact": prepared,
        # envelope_signature (not prepared.get("signature", "")) — otherwise
        # an unsigned OSS artifact dict carrying an explicit
        # `"signature": None` key would send JSON `null`: .get()'s default
        # only applies when the key is absent, not when it's None.
        "signature": envelope_signature(artifact, prepared),
    }
    status, data = publish_scan_status(base_url, token, envelope)
    if status not in (200, 201):
        _annotate(
            "warning",
            f"Opencomplai dashboard publish failed: HTTP {status} {data}",
        )


# ---------------------------------------------------------------------------
# Entry point (pip-installed script)
# ---------------------------------------------------------------------------


def main() -> None:
    sys.exit(run_connector())


__all__ = ["RUNNING_IN_GHA", "main", "run_connector"]
