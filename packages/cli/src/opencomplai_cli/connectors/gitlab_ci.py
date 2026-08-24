"""
GitLab CI connector (C1.5).

Wraps ``opencomplai check`` with GitLab CI platform conventions:

* Emits a GitLab-compatible JUnit XML artifact so the scan result appears
  in the test report panel (``--report-junit``).
* Creates a GitLab section for collapsible log output.
* Sets GitLab environment variables via ``dotenv`` artifact when the
  ``GL_ENV_FILE`` env var is set.
* Propagates exit code: preserves ``opencomplai check``'s own contract
  (control_fail→1, validation_fail→2, policy_block→3, trap_detected→4) so a
  blocked or frozen deployment fails the pipeline rather than passing silently.
* Publishes the signed status artifact to the dashboard via the standard
  ingest path when configured.

Environment variables consumed
-------------------------------
``GITLAB_CI``                 — set to ``true`` by GitLab; connector detects.
``CI_COMMIT_SHA``             — used as ``commit_ref`` in annotations.
``CI_JOB_NAME``               — job name surfaced in summary.
``GL_ENV_FILE``               — path for dotenv artifact (optional).
``OPENCOMPLAI_DASHBOARD_URL`` — dashboard ingest base URL.
``OPENCOMPLAI_TENANT_ID``     — tenant ID.
``OPENCOMPLAI_API_KEY``       — an ``ock_...`` API key (GO-LIVE CORE-3/-4).
                                Takes precedence over everything below: when
                                set, the connector sends the key-authed
                                envelope (no ``install_id``) and never
                                touches ``OPENCOMPLAI_AUTH_TOKEN`` or OIDC.
``OPENCOMPLAI_AUTH_TOKEN``    — bearer token (legacy JWT path, only
                                consulted when OPENCOMPLAI_API_KEY is
                                unset). Takes precedence over the three
                                vars below when set.
``OPENCOMPLAI_CLIENT_ID``       — OIDC client_id from /enroll, used only
                                   when OPENCOMPLAI_AUTH_TOKEN is unset.
``OPENCOMPLAI_CLIENT_SECRET``   — OIDC client_secret from /enroll (same gate).
``OPENCOMPLAI_TOKEN_ENDPOINT``  — the IdP's OAuth2 token endpoint (same gate).

Exit codes
----------
0  — scan passed (result=pass, or result=degraded_complete in local scan mode).
1  — result=control_fail, or an unexpected non-zero ``check`` exit with no
     parseable artifact result at all.
2  — result=validation_fail, or a connector-side configuration error (missing
     required env var, bad token, etc.).
3  — result=policy_block (prohibited/Article 5 system).
4  — result=trap_detected (Article 25 substantial-modification freeze).

Usage in .gitlab-ci.yml
------------------------

```yaml
opencomplai-scan:
  image: python:3.11
  before_script:
    - pip install opencomplai
  script:
    - opencomplai-gitlab-connector
  artifacts:
    reports:
      junit: opencomplai-report.xml
    dotenv: opencomplai.env
  variables:
    OPENCOMPLAI_DASHBOARD_URL: $OPENCOMPLAI_DASHBOARD_URL
    OPENCOMPLAI_TENANT_ID: $OPENCOMPLAI_TENANT_ID
    OPENCOMPLAI_AUTH_TOKEN: $OPENCOMPLAI_AUTH_TOKEN
    GL_ENV_FILE: opencomplai.env
```
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

RUNNING_IN_GITLAB = os.environ.get("GITLAB_CI") == "true"

# `check --sign` (main.py's check_cmd) always writes its artifact here,
# relative to whatever cwd the subprocess inherited -- this connector never
# passes cwd= to subprocess.run, so that is this process's own cwd too.
_ARTIFACT_FILENAME = "compliance-artifact.json"

# FINDING 48.8: mirrors `opencomplai check`'s own exit-code contract
# (main.py's `_exit_code` / `ScanResult`) -- a `pass` or `degraded_complete`
# result and any result absent from this table fall through to the
# generic 0/`returncode` handling below.
_EXIT_CODE_BY_RESULT: dict[str, int] = {
    "control_fail": 1,
    "validation_fail": 2,
    "policy_block": 3,
    "trap_detected": 4,
}


# ---------------------------------------------------------------------------
# GitLab log section helpers
# ---------------------------------------------------------------------------

_SECTION_START = "\x1b[0Ksection_start:{ts}:{name}\r\x1b[0K{title}"
_SECTION_END = "\x1b[0Ksection_end:{ts}:{name}\r\x1b[0K"


def _section_start(name: str, title: str) -> None:
    import time

    ts = int(time.time())
    print(_SECTION_START.format(ts=ts, name=name, title=title), flush=True)


def _section_end(name: str) -> None:
    import time

    ts = int(time.time())
    print(_SECTION_END.format(ts=ts, name=name), flush=True)


# ---------------------------------------------------------------------------
# JUnit XML generation
# ---------------------------------------------------------------------------


def _build_junit_xml(artifact: dict | None, stdout: str) -> str:
    suite = ET.Element("testsuite", name="opencomplai", tests="1")
    case = ET.SubElement(
        suite, "testcase", name="compliance-scan", classname="opencomplai"
    )

    if artifact:
        result = artifact.get("result", "unknown")
        if result == "control_fail":
            failed = artifact.get("failed_controls", [])
            failure = ET.SubElement(
                case,
                "failure",
                message=f"control_fail: {', '.join(str(f) for f in failed)}",
            )
            failure.text = stdout
        elif result == "trap_detected":
            # FINDING 48.8: trap_detected now fails the build (exit 4) --
            # a <system-out> here would leave the JUnit case green while the
            # pipeline itself goes red, which is the exact silent mismatch
            # the finding called out. Report it as a failure like the other
            # CI-failing results below.
            failure = ET.SubElement(
                case,
                "failure",
                message="trap_detected — Article 25 deployment freeze, HITL review required",
            )
            failure.text = stdout
        elif result == "policy_block":
            failure = ET.SubElement(
                case,
                "failure",
                message="policy_block — prohibited system (EU AI Act Article 5)",
            )
            failure.text = stdout
        elif result == "validation_fail":
            failure = ET.SubElement(
                case,
                "failure",
                message="validation_fail — manifest or input validation error",
            )
            failure.text = stdout
    else:
        ET.SubElement(case, "error", message="No artifact result parsed")

    return ET.tostring(suite, encoding="unicode", xml_declaration=False)


# ---------------------------------------------------------------------------
# Core connector
# ---------------------------------------------------------------------------


def run_connector(
    check_args: list[str] | None = None,
    env: dict[str, str] | None = None,
    junit_path: str = "opencomplai-report.xml",
) -> int:
    """
    Run ``opencomplai check --sign`` and handle GitLab CI conventions.

    Returns 0 (pass) or 1 (fail).
    """
    _env = {**os.environ, **(env or {})}

    _section_start("opencomplai_scan", "Opencomplai compliance scan")

    cmd = ["opencomplai", "check", "--sign"] + (check_args or [])
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, env=_env)
    except FileNotFoundError:
        print(
            "ERROR: opencomplai CLI not found. Install with: pip install opencomplai",
            file=sys.stderr,
        )
        _section_end("opencomplai_scan")
        return 2

    stdout = result.stdout
    if stdout:
        print(stdout, end="", flush=True)

    _section_end("opencomplai_scan")

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
    scan_result = artifact_result.get("result", "") if artifact_result else ""

    # JUnit XML artifact.
    xml_content = _build_junit_xml(artifact_result, stdout)
    try:
        with open(junit_path, "w") as fh:
            fh.write(xml_content)
    except OSError:
        pass

    # dotenv artifact for downstream jobs.
    gl_env_file = _env.get("GL_ENV_FILE", "")
    if gl_env_file and artifact_result:
        _write_dotenv(gl_env_file, artifact_result)

    # Dashboard publish.
    _publish_to_dashboard(artifact_result, _env)

    # Exit code propagation. FINDING 48.8: policy_block and validation_fail
    # are pipeline failures exactly like control_fail and trap_detected --
    # preserve `opencomplai check`'s own exit-code contract instead of
    # collapsing everything but control_fail to a passing pipeline.
    if scan_result == "control_fail":
        print(
            f"FAIL: control_fail — {', '.join(str(c) for c in (artifact_result or {}).get('failed_controls', []))}"
        )
    elif scan_result == "trap_detected":
        print(
            "FAIL: trap_detected — Article 25 deployment freeze, HITL review required",
            file=sys.stderr,
        )
    elif scan_result == "policy_block":
        print(
            "FAIL: policy_block — prohibited system (EU AI Act Article 5)",
            file=sys.stderr,
        )
    elif scan_result == "validation_fail":
        print(
            "FAIL: validation_fail — manifest or input validation error",
            file=sys.stderr,
        )

    exit_code = _EXIT_CODE_BY_RESULT.get(scan_result)
    if exit_code is not None:
        return exit_code
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


def _write_dotenv(path: str, artifact: dict) -> None:
    lines = [
        f"OPENCOMPLAI_RESULT={artifact.get('result', '')}",
        f"OPENCOMPLAI_SYSTEM_ID={artifact.get('system_id', '')}",
        f"OPENCOMPLAI_CONTENT_HASH={artifact.get('content_hash', '')}",
    ]
    try:
        with open(path, "w") as fh:
            fh.write("\n".join(lines) + "\n")
    except OSError:
        pass


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
    # commit_ref="HEAD" failed minLength: 7) -- every payload this connector
    # ever sent was rejected with a 422. prepare_scan_status_artifact fills
    # in only what the OSS artifact genuinely lacks (policy_bundle_version,
    # timestamp, a real commit_ref); everything else passes through
    # untouched onto the now-widened schema.

    api_key = env.get("OPENCOMPLAI_API_KEY", "")
    if api_key:
        # ock_ key-authed path (GO-LIVE CORE-3/CORE-4) -- the same auth
        # `opencomplai push` uses. Takes priority over OPENCOMPLAI_AUTH_TOKEN
        # and the OIDC client-credentials grant below: a caller who sets
        # OPENCOMPLAI_API_KEY has opted into the key-authed contract, which
        # is a different envelope shape (no install_id at all -- the server
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
            print(
                f"WARNING: dashboard publish failed: HTTP {status} {data}",
                file=sys.stderr,
            )
        return

    # Legacy JWT/OIDC path (pre-CORE-3 envelope contract). An operator-set
    # OPENCOMPLAI_AUTH_TOKEN always wins. If unset, acquire one via the
    # OIDC client-credentials grant using the pair /enroll issued
    # (AUTH-SAAS) -- same trio of env vars, no manual token pasting.
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
                print(f"WARNING: OIDC token acquisition failed: {exc}", file=sys.stderr)

    if not token:
        return

    prepared = prepare_scan_status_artifact(artifact, commit_env=env)

    install_id = env.get("OPENCOMPLAI_INSTALL_ID", "unknown")
    envelope = {
        "install_id": install_id,
        "system_id": prepared.get("system_id", "unknown"),
        "artifact": prepared,
        # envelope_signature (not prepared.get("signature", "")) -- otherwise
        # an unsigned OSS artifact dict carrying an explicit
        # `"signature": None` key would send JSON `null`: .get()'s default
        # only applies when the key is absent, not when it's None.
        "signature": envelope_signature(artifact, prepared),
    }
    status, data = publish_scan_status(base_url, token, envelope)
    if status not in (200, 201):
        print(
            f"WARNING: dashboard publish failed: HTTP {status} {data}",
            file=sys.stderr,
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    sys.exit(run_connector())


__all__ = ["RUNNING_IN_GITLAB", "main", "run_connector"]
