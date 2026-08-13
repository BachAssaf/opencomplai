"""
Tests for GO-LIVE CORE-4's ``opencomplai push`` command
(``opencomplai_cli.commands.push``).

``run_push`` is exercised directly (arg/env handling, exit codes) with
``publish_scan_status`` mocked so nothing here touches the network --
mirrors the existing connector tests' own convention of mocking
``urllib.request.urlopen``, one layer higher since push's own transport
call is already covered by ``test_publish.py``. The real, unmocked,
end-to-end HTTP proof (a real ingest-api TestClient, a real issued
``ock_`` key) lives in
``dashboard-saas/services/ingest-api/tests/test_core_loop_e2e.py``.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from opencomplai_cli.commands.push import EXIT_OK, EXIT_PUBLISH_FAILED, run_push
from typer.testing import CliRunner

runner = CliRunner()

_VALID_ENV = {
    "OPENCOMPLAI_API_KEY": "ock_testkey",
    "OPENCOMPLAI_DASHBOARD_URL": "http://dash.test/api/ingest",
}

_ARTIFACT = {
    "install_id": "install-1",
    "system_id": "sys-1",
    "commit_ref": "a" * 40,
    "result": "pass",
    "failed_controls": [],
    "evidence_hashes": ["sha256:aaa"],
    "rationale_hash": "sha256:bbb",
    "duration_ms": 100,
    "pending_verifications_count": 0,
    "signature": None,
    "eval_summary": None,
    "scan_summary": None,
    "gap_report": None,
}


class TestRunPushArgAndEnvHandling:
    def test_missing_file_returns_publish_failed(self, tmp_path):
        code = run_push(tmp_path / "nope.json", env=dict(_VALID_ENV))
        assert code == EXIT_PUBLISH_FAILED

    def test_invalid_json_returns_publish_failed(self, tmp_path):
        bad = tmp_path / "compliance-artifact.json"
        bad.write_text("{not json", encoding="utf-8")
        code = run_push(bad, env=dict(_VALID_ENV))
        assert code == EXIT_PUBLISH_FAILED

    def test_directory_path_returns_publish_failed_not_a_traceback(self, tmp_path):
        directory = tmp_path / "compliance-artifact.json"
        directory.mkdir()
        code = run_push(directory, env=dict(_VALID_ENV))
        assert code == EXIT_PUBLISH_FAILED

    def test_non_utf8_bytes_returns_publish_failed_not_a_traceback(self, tmp_path):
        artifact_file = tmp_path / "compliance-artifact.json"
        artifact_file.write_bytes(b"\xff\xfe\x00\x01not utf-8")
        code = run_push(artifact_file, env=dict(_VALID_ENV))
        assert code == EXIT_PUBLISH_FAILED

    def test_json_array_not_object_returns_publish_failed(self, tmp_path):
        artifact_file = tmp_path / "compliance-artifact.json"
        artifact_file.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        code = run_push(artifact_file, env=dict(_VALID_ENV))
        assert code == EXIT_PUBLISH_FAILED

    def test_json_scalar_not_object_returns_publish_failed(self, tmp_path):
        artifact_file = tmp_path / "compliance-artifact.json"
        artifact_file.write_text(json.dumps("just a string"), encoding="utf-8")
        code = run_push(artifact_file, env=dict(_VALID_ENV))
        assert code == EXIT_PUBLISH_FAILED

    def test_missing_api_key_returns_publish_failed(self, tmp_path):
        artifact_file = tmp_path / "compliance-artifact.json"
        artifact_file.write_text(json.dumps(_ARTIFACT), encoding="utf-8")
        env = {"OPENCOMPLAI_DASHBOARD_URL": "http://dash.test"}
        code = run_push(artifact_file, env=env)
        assert code == EXIT_PUBLISH_FAILED

    def test_missing_dashboard_url_returns_publish_failed(self, tmp_path):
        artifact_file = tmp_path / "compliance-artifact.json"
        artifact_file.write_text(json.dumps(_ARTIFACT), encoding="utf-8")
        env = {"OPENCOMPLAI_API_KEY": "ock_testkey"}
        code = run_push(artifact_file, env=env)
        assert code == EXIT_PUBLISH_FAILED

    def test_default_path_is_compliance_artifact_json_in_cwd(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "compliance-artifact.json").write_text(
            json.dumps(_ARTIFACT), encoding="utf-8"
        )
        from pathlib import Path

        with patch(
            "opencomplai_cli.commands.push.publish_scan_status",
            return_value=(201, {"outcome": "accepted", "content_hash": "abc"}),
        ):
            code = run_push(Path("compliance-artifact.json"), env=dict(_VALID_ENV))
        assert code == EXIT_OK


class TestRunPushExitCodes:
    def test_201_returns_ok_and_prints_outcome(self, tmp_path, capsys):
        artifact_file = tmp_path / "compliance-artifact.json"
        artifact_file.write_text(json.dumps(_ARTIFACT), encoding="utf-8")
        with patch(
            "opencomplai_cli.commands.push.publish_scan_status",
            return_value=(201, {"outcome": "accepted", "content_hash": "deadbeef"}),
        ):
            code = run_push(artifact_file, env=dict(_VALID_ENV))
        assert code == EXIT_OK
        out = capsys.readouterr().out
        assert "accepted" in out
        assert "deadbeef" in out

    def test_200_replay_returns_ok(self, tmp_path):
        artifact_file = tmp_path / "compliance-artifact.json"
        artifact_file.write_text(json.dumps(_ARTIFACT), encoding="utf-8")
        with patch(
            "opencomplai_cli.commands.push.publish_scan_status",
            return_value=(200, {"outcome": "replayed", "content_hash": "abc"}),
        ):
            code = run_push(artifact_file, env=dict(_VALID_ENV))
        assert code == EXIT_OK

    def test_422_returns_publish_failed(self, tmp_path, capsys):
        artifact_file = tmp_path / "compliance-artifact.json"
        artifact_file.write_text(json.dumps(_ARTIFACT), encoding="utf-8")
        with patch(
            "opencomplai_cli.commands.push.publish_scan_status",
            return_value=(422, {"error_code": "SCHEMA_VIOLATION"}),
        ):
            code = run_push(artifact_file, env=dict(_VALID_ENV))
        assert code == EXIT_PUBLISH_FAILED
        assert "SCHEMA_VIOLATION" in capsys.readouterr().err

    def test_401_returns_publish_failed(self, tmp_path):
        artifact_file = tmp_path / "compliance-artifact.json"
        artifact_file.write_text(json.dumps(_ARTIFACT), encoding="utf-8")
        with patch(
            "opencomplai_cli.commands.push.publish_scan_status",
            return_value=(401, {"detail": "invalid or expired token"}),
        ):
            code = run_push(artifact_file, env=dict(_VALID_ENV))
        assert code == EXIT_PUBLISH_FAILED

    def test_network_failure_returns_publish_failed(self, tmp_path):
        artifact_file = tmp_path / "compliance-artifact.json"
        artifact_file.write_text(json.dumps(_ARTIFACT), encoding="utf-8")
        with patch(
            "opencomplai_cli.commands.push.publish_scan_status",
            return_value=(0, {"error": "connection refused"}),
        ):
            code = run_push(artifact_file, env=dict(_VALID_ENV))
        assert code == EXIT_PUBLISH_FAILED


class TestEnvelopeShape:
    def test_no_install_id_and_prepared_artifact_and_signature_default(self, tmp_path):
        """The push envelope must never carry install_id (server derives it
        from the API key's project) and must default signature to "" rather
        than None/absent."""
        artifact_file = tmp_path / "compliance-artifact.json"
        no_sig_artifact = {**_ARTIFACT, "signature": None}
        artifact_file.write_text(json.dumps(no_sig_artifact), encoding="utf-8")

        with patch(
            "opencomplai_cli.commands.push.publish_scan_status",
            return_value=(201, {"outcome": "accepted", "content_hash": "abc"}),
        ) as mock_publish:
            run_push(artifact_file, env=dict(_VALID_ENV))

        _base_url, _token, envelope = mock_publish.call_args[0]
        assert "install_id" not in envelope
        assert envelope["signature"] == ""
        assert envelope["system_id"] == "sys-1"
        # The artifact went through the mapper: schema-required fields present.
        assert envelope["artifact"]["policy_bundle_version"].startswith("cli-")
        assert envelope["artifact"]["timestamp"]
        # And the OSS evidentiary fields are still there, untouched.
        assert envelope["artifact"]["evidence_hashes"] == ["sha256:aaa"]

    def test_signed_but_mapped_artifact_signature_is_not_forwarded(self, tmp_path):
        """G-5: _ARTIFACT has no policy_bundle_version/timestamp, so the
        mapper mutates it (adds both) -- under publish.envelope_signature
        that mutation means the embedded "base64sig==" (signed over the
        pre-mapping bytes) must NOT be forwarded; sending it anyway would
        deterministically fail SIGNATURE_INVALID server-side. Renamed from
        test_real_signature_is_forwarded_not_dropped, which asserted the
        pre-G-5 (incorrect) behaviour."""
        artifact_file = tmp_path / "compliance-artifact.json"
        signed_artifact = {**_ARTIFACT, "signature": "base64sig=="}
        artifact_file.write_text(json.dumps(signed_artifact), encoding="utf-8")

        with patch(
            "opencomplai_cli.commands.push.publish_scan_status",
            return_value=(201, {"outcome": "accepted", "content_hash": "abc"}),
        ) as mock_publish:
            run_push(artifact_file, env=dict(_VALID_ENV))

        _base_url, _token, envelope = mock_publish.call_args[0]
        assert envelope["signature"] == ""

    def test_identity_mapped_signed_artifact_signature_is_forwarded(self, tmp_path):
        """The other half of the G-5 rule: when the artifact already
        carries everything the mapper would otherwise synthesize (a usable
        commit_ref, policy_bundle_version, timestamp), prepare_scan_status
        _artifact changes nothing -- so the embedded signature covers
        exactly what's being sent, and IS forwarded."""
        artifact_file = tmp_path / "compliance-artifact.json"
        identity_artifact = {
            **_ARTIFACT,
            "commit_ref": "a" * 40,
            "policy_bundle_version": "cli-0.0.0-test",
            "timestamp": "2026-01-01T00:00:00Z",
            "signature": "base64sig==",
        }
        artifact_file.write_text(json.dumps(identity_artifact), encoding="utf-8")

        with patch(
            "opencomplai_cli.commands.push.publish_scan_status",
            return_value=(201, {"outcome": "accepted", "content_hash": "abc"}),
        ) as mock_publish:
            run_push(artifact_file, env=dict(_VALID_ENV))

        _base_url, _token, envelope = mock_publish.call_args[0]
        assert envelope["signature"] == "base64sig=="

    def test_base_url_and_token_forwarded(self, tmp_path):
        artifact_file = tmp_path / "compliance-artifact.json"
        artifact_file.write_text(json.dumps(_ARTIFACT), encoding="utf-8")

        with patch(
            "opencomplai_cli.commands.push.publish_scan_status",
            return_value=(201, {"outcome": "accepted", "content_hash": "abc"}),
        ) as mock_publish:
            run_push(artifact_file, env=dict(_VALID_ENV))

        base_url, token, _envelope = mock_publish.call_args[0]
        assert base_url == "http://dash.test/api/ingest"
        assert token == "ock_testkey"


class TestPushCliCommand:
    def test_cli_exits_0_on_success(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "compliance-artifact.json").write_text(
            json.dumps(_ARTIFACT), encoding="utf-8"
        )
        from opencomplai_cli.main import app

        with patch(
            "opencomplai_cli.commands.push.publish_scan_status",
            return_value=(201, {"outcome": "accepted", "content_hash": "abc"}),
        ):
            result = runner.invoke(app, ["push"], env=dict(_VALID_ENV))
        assert result.exit_code == 0

    def test_cli_exits_3_on_publish_failure(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "compliance-artifact.json").write_text(
            json.dumps(_ARTIFACT), encoding="utf-8"
        )
        from opencomplai_cli.main import app

        with patch(
            "opencomplai_cli.commands.push.publish_scan_status",
            return_value=(422, {"error_code": "SCHEMA_VIOLATION"}),
        ):
            result = runner.invoke(app, ["push"], env=dict(_VALID_ENV))
        assert result.exit_code == 3

    def test_cli_accepts_explicit_path_argument(self, tmp_path):
        custom = tmp_path / "custom-artifact.json"
        custom.write_text(json.dumps(_ARTIFACT), encoding="utf-8")
        from opencomplai_cli.main import app

        with patch(
            "opencomplai_cli.commands.push.publish_scan_status",
            return_value=(201, {"outcome": "accepted", "content_hash": "abc"}),
        ) as mock_publish:
            result = runner.invoke(app, ["push", str(custom)], env=dict(_VALID_ENV))
        assert result.exit_code == 0
        mock_publish.assert_called_once()


class TestOutputSafetyAndWarnings:
    """F4(b)/(c)/(d): server-controlled response text must never inject
    rich markup, a dropped signature is explained, and a plain-http
    non-local dashboard URL gets a visible warning."""

    def test_server_response_markup_is_escaped_on_success(self, tmp_path, capsys):
        artifact_file = tmp_path / "compliance-artifact.json"
        artifact_file.write_text(json.dumps(_ARTIFACT), encoding="utf-8")

        malicious = "[bold red]INJECTED[/bold red]"
        with patch(
            "opencomplai_cli.commands.push.publish_scan_status",
            return_value=(201, {"outcome": malicious, "content_hash": malicious}),
        ):
            code = run_push(artifact_file, env=dict(_VALID_ENV))

        assert code == EXIT_OK
        out = capsys.readouterr().out
        # The literal markup text must appear verbatim (escaped, not
        # interpreted) -- proving it was neither dropped nor turned into
        # rich styling/a crash.
        assert malicious in out

    def test_server_response_markup_is_escaped_on_failure(self, tmp_path, capsys):
        artifact_file = tmp_path / "compliance-artifact.json"
        artifact_file.write_text(json.dumps(_ARTIFACT), encoding="utf-8")

        malicious = {"error_code": "[red on white]HACKED[/red on white]"}
        with patch(
            "opencomplai_cli.commands.push.publish_scan_status",
            return_value=(422, malicious),
        ):
            code = run_push(artifact_file, env=dict(_VALID_ENV))

        assert code == EXIT_PUBLISH_FAILED
        err = capsys.readouterr().err
        assert "[red on white]HACKED[/red on white]" in err

    def test_dropped_signature_prints_informational_note(self, tmp_path, capsys):
        """The artifact carries a real signature, but _ARTIFACT lacks
        policy_bundle_version/timestamp so the mapper mutates it -- under
        G-5 the signature is dropped, and the CLI must say so."""
        artifact_file = tmp_path / "compliance-artifact.json"
        signed = {**_ARTIFACT, "signature": "base64sig=="}
        artifact_file.write_text(json.dumps(signed), encoding="utf-8")

        with patch(
            "opencomplai_cli.commands.push.publish_scan_status",
            return_value=(201, {"outcome": "accepted", "content_hash": "abc"}),
        ):
            run_push(artifact_file, env=dict(_VALID_ENV))

        err = capsys.readouterr().err
        assert "note:" in err
        assert "not forwarded" in err

    def test_no_note_when_artifact_was_never_signed(self, tmp_path, capsys):
        artifact_file = tmp_path / "compliance-artifact.json"
        artifact_file.write_text(json.dumps(_ARTIFACT), encoding="utf-8")

        with patch(
            "opencomplai_cli.commands.push.publish_scan_status",
            return_value=(201, {"outcome": "accepted", "content_hash": "abc"}),
        ):
            run_push(artifact_file, env=dict(_VALID_ENV))

        assert "not forwarded" not in capsys.readouterr().err

    def test_plain_http_non_local_dashboard_warns(self, tmp_path, capsys):
        artifact_file = tmp_path / "compliance-artifact.json"
        artifact_file.write_text(json.dumps(_ARTIFACT), encoding="utf-8")
        env = {
            "OPENCOMPLAI_API_KEY": "ock_testkey",
            "OPENCOMPLAI_DASHBOARD_URL": "http://dash.example.com/api/ingest",
        }

        with patch(
            "opencomplai_cli.commands.push.publish_scan_status",
            return_value=(201, {"outcome": "accepted", "content_hash": "abc"}),
        ):
            run_push(artifact_file, env=env)

        err = capsys.readouterr().err
        assert "http" in err.lower()

    def test_https_dashboard_does_not_warn(self, tmp_path, capsys):
        artifact_file = tmp_path / "compliance-artifact.json"
        artifact_file.write_text(json.dumps(_ARTIFACT), encoding="utf-8")
        env = {
            "OPENCOMPLAI_API_KEY": "ock_testkey",
            "OPENCOMPLAI_DASHBOARD_URL": "https://dash.example.com/api/ingest",
        }

        with patch(
            "opencomplai_cli.commands.push.publish_scan_status",
            return_value=(201, {"outcome": "accepted", "content_hash": "abc"}),
        ):
            run_push(artifact_file, env=env)

        assert "plain http" not in capsys.readouterr().err

    def test_localhost_http_dashboard_does_not_warn(self, tmp_path, capsys):
        artifact_file = tmp_path / "compliance-artifact.json"
        artifact_file.write_text(json.dumps(_ARTIFACT), encoding="utf-8")
        env = {
            "OPENCOMPLAI_API_KEY": "ock_testkey",
            "OPENCOMPLAI_DASHBOARD_URL": "http://localhost:8080/api/ingest",
        }

        with patch(
            "opencomplai_cli.commands.push.publish_scan_status",
            return_value=(201, {"outcome": "accepted", "content_hash": "abc"}),
        ):
            run_push(artifact_file, env=env)

        assert "plain http" not in capsys.readouterr().err
