"""
Tests for AI-EGRESS: snippet redaction, offline mode, consent, and model
artifact integrity.

The property that matters most is negative — that certain byte sequences do
*not* appear in what leaves the process — so most assertions here are
`not in`, checked against the payload actually handed to urllib rather than
against an intermediate value.
"""

from __future__ import annotations

import json

import pytest
from opencomplai_ai import egress, redaction
from opencomplai_ai._saas_client import SaaSIntentClient
from opencomplai_ai.integrity import (
    ModelIntegrityError,
    UnpinnedModelError,
    sha256_file,
    verify_artifact,
)

# --------------------------------------------------------------------------
# Redaction
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "sample"),
    [
        ("aws_access_key_id", "AKIAIOSFODNN7EXAMPLE"),
        ("github_token", "ghp_" + "a" * 36),
        ("github_token", "github_pat_" + "B" * 30),
        ("slack_token", "xoxb-123456789012-abcdefghijkl"),  # gitleaks:allow
        ("stripe_key", "sk_live_abcdefghij1234567890"),  # gitleaks:allow
        ("google_api_key", "AIza" + "a" * 35),
        (
            "jwt",
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
            ".eyJzdWIiOiIxMjM0NTY3ODkwIn0"
            ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
        ),
        ("email", "alice.smith@example.com"),
        ("us_ssn", "123-45-6789"),
    ],
)
def test_secret_shapes_are_removed(kind: str, sample: str) -> None:
    result = redaction.redact(f"value = {sample}")

    assert sample not in result.text
    assert kind in result.counts
    assert result.redacted


def test_private_key_body_is_removed_not_just_the_header() -> None:
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEAsecretkeymaterialhere\n"  # gitleaks:allow
        "-----END RSA PRIVATE KEY-----"
    )

    result = redaction.redact(f"KEY = '''{pem}'''")

    # Redacting only the header would leave the actual key material behind.
    assert "MIIEowIBAAKCAQEAsecretkeymaterialhere" not in result.text
    assert "private_key" in result.counts


def test_assignment_keeps_the_variable_name_but_drops_the_value() -> None:
    result = redaction.redact('api_key = "super-secret-value-1234"')

    assert "super-secret-value-1234" not in result.text
    # The name is the signal the classifier needs; only the value is a secret.
    assert "api_key" in result.text


def test_connection_string_keeps_scheme_and_host() -> None:
    result = redaction.redact("postgresql://admin:hunter2@db.internal:5432/app")

    assert "hunter2" not in result.text
    assert "admin:hunter2" not in result.text
    assert "postgresql://" in result.text
    assert "db.internal" in result.text


def test_luhn_valid_card_is_redacted_but_ordinary_digit_runs_are_not() -> None:
    card = redaction.redact("card = 4242424242424242")
    ordinary = redaction.redact("timestamp_ns = 1234567890123456")

    assert "4242424242424242" not in card.text
    assert "credit_card" in card.counts
    # A long digit run that fails the checksum is far more likely to be an id
    # or a timestamp than a card number.
    assert "1234567890123456" in ordinary.text


def test_clean_code_is_returned_unchanged() -> None:
    snippet = "def score(applicant):\n    return model.predict(applicant.features)\n"

    result = redaction.redact(snippet)

    assert result.text == snippet
    assert not result.redacted
    assert result.summary() == "nothing redacted"


def test_summary_counts_every_match() -> None:
    result = redaction.redact("a@b.com and c@d.org and AKIAIOSFODNN7EXAMPLE")

    assert result.counts["email"] == 2
    assert result.counts["aws_access_key_id"] == 1
    assert "2 email" in result.summary()


# --------------------------------------------------------------------------
# Offline mode
# --------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_offline_is_recognised_from_common_truthy_values(value: str) -> None:
    assert egress.is_offline({"OPENCOMPLAI_OFFLINE": value})


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off"])
def test_offline_is_off_for_everything_else(value: str) -> None:
    assert not egress.is_offline({"OPENCOMPLAI_OFFLINE": value})


def test_require_online_raises_under_offline_mode() -> None:
    with pytest.raises(egress.OfflineModeError, match="OPENCOMPLAI_OFFLINE"):
        egress.require_online("Downloading a model", {"OPENCOMPLAI_OFFLINE": "1"})


def test_require_online_is_a_noop_when_not_offline() -> None:
    egress.require_online("Downloading a model", {})


# --------------------------------------------------------------------------
# Consent
# --------------------------------------------------------------------------


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """Point the AI config at a temp dir so tests never touch the real one."""
    cfg_dir = tmp_path / ".opencomplai"
    cfg_file = cfg_dir / "ai-config.yaml"
    monkeypatch.setattr(egress, "_CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(egress, "_AI_CONFIG_FILE", cfg_file)
    return cfg_file


def test_consent_is_absent_by_default(isolated_config) -> None:
    assert egress.get_consent() is None
    assert not egress.has_consent()


def test_recorded_consent_round_trips(isolated_config) -> None:
    record = egress.record_consent()

    assert egress.has_consent()
    assert egress.get_consent() == record
    assert record.version == egress.EGRESS_CONSENT_VERSION


def test_consent_at_a_superseded_version_does_not_count(
    isolated_config, monkeypatch
) -> None:
    egress.record_consent()
    # What the user agreed to only means something for the terms they saw.
    monkeypatch.setattr(egress, "EGRESS_CONSENT_VERSION", 99)

    assert egress.get_consent() is not None
    assert not egress.has_consent()


def test_revoke_removes_consent(isolated_config) -> None:
    egress.record_consent()
    egress.revoke_consent()

    assert not egress.has_consent()


def test_malformed_config_is_not_read_as_consent(isolated_config) -> None:
    isolated_config.parent.mkdir(parents=True, exist_ok=True)
    isolated_config.write_text("{{{ not yaml", encoding="utf-8")

    assert not egress.has_consent()


# --------------------------------------------------------------------------
# The saas client end to end
# --------------------------------------------------------------------------


def _capture_request(monkeypatch):
    """Record what would be sent, and fail the test if it is ever called."""
    sent: dict = {}

    def _urlopen(req, timeout=None):  # pragma: no cover — asserted via `sent`
        sent["body"] = req.data.decode("utf-8")
        raise AssertionError("network call should not complete in tests")

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)
    return sent


def test_offline_mode_sends_nothing(monkeypatch) -> None:
    sent = _capture_request(monkeypatch)
    monkeypatch.setenv("OPENCOMPLAI_API_KEY", "key")
    monkeypatch.setenv("OPENCOMPLAI_OFFLINE", "1")
    monkeypatch.setattr(egress, "has_consent", lambda: True)

    result = SaaSIntentClient().classify("secret code", legacy=True)

    assert sent == {}
    assert "OPENCOMPLAI_OFFLINE" in (result.explanation or "")


def test_missing_consent_sends_nothing(monkeypatch) -> None:
    sent = _capture_request(monkeypatch)
    monkeypatch.setenv("OPENCOMPLAI_API_KEY", "key")
    monkeypatch.delenv("OPENCOMPLAI_OFFLINE", raising=False)
    monkeypatch.setattr("opencomplai_ai._saas_client.has_consent", lambda: False)

    result = SaaSIntentClient().classify("secret code", legacy=True)

    assert sent == {}
    assert "consent" in (result.explanation or "").lower()


def test_offline_is_checked_before_the_api_key(monkeypatch) -> None:
    """Operator policy must not depend on whether credentials happen to be set."""
    sent = _capture_request(monkeypatch)
    monkeypatch.delenv("OPENCOMPLAI_API_KEY", raising=False)
    monkeypatch.setenv("OPENCOMPLAI_OFFLINE", "1")

    result = SaaSIntentClient().classify("code", legacy=True)

    assert sent == {}
    assert "OPENCOMPLAI_OFFLINE" in (result.explanation or "")


def test_snippet_is_redacted_before_it_reaches_the_request_body(monkeypatch) -> None:
    sent = _capture_request(monkeypatch)
    monkeypatch.setenv("OPENCOMPLAI_API_KEY", "key")
    monkeypatch.delenv("OPENCOMPLAI_OFFLINE", raising=False)
    monkeypatch.setattr("opencomplai_ai._saas_client.has_consent", lambda: True)

    SaaSIntentClient().classify(
        'conn = "postgresql://root:hunter2@db:5432/x"\nAWS = "AKIAIOSFODNN7EXAMPLE"',
        declared_purpose="contact alice@example.com for access",
        legacy=True,
    )

    body = json.loads(sent["body"])
    assert "hunter2" not in body["snippet"]
    assert "AKIAIOSFODNN7EXAMPLE" not in body["snippet"]
    # declared_purpose is user-supplied free text and leaves the process too.
    assert "alice@example.com" not in body["declared_purpose"]


class _FakeResponse:
    """Minimal stand-in for urllib.request.urlopen's context-manager result."""

    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


def test_art6_3_profiling_preserved_when_area_is_null(monkeypatch) -> None:
    """art6_3 must only be cleared by the subject-gated-conflict backstop
    (area resolved AND subject legal_entity/system AND that area is
    subject_gated) — exactly like explainer._parse_annotation — never merely
    because area came back null. Art. 6(3) profiling applies regardless of
    whether a specific Annex III area was also resolved."""
    monkeypatch.setenv("OPENCOMPLAI_API_KEY", "key")
    monkeypatch.delenv("OPENCOMPLAI_OFFLINE", raising=False)
    monkeypatch.setattr("opencomplai_ai._saas_client.has_consent", lambda: True)
    payload = {
        "annex_iii_area": None,
        "art5_prohibited": False,
        "art6_3_profiling": True,
        "decision_autonomy": "autonomous",
        "subject_type": "natural_person",
        "consequential": "yes",
        "risk_tier": "limited_risk",
        "explanation": "profiles natural persons; no single Annex III area resolved",
    }
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda req, timeout=None: _FakeResponse(payload)
    )

    result = SaaSIntentClient().classify("code")

    assert result is not None
    assert result.art6_3_profiling is True


# --------------------------------------------------------------------------
# Model artifact integrity
# --------------------------------------------------------------------------


def test_verify_artifact_accepts_a_matching_checksum(tmp_path) -> None:
    artifact = tmp_path / "model.gguf"
    artifact.write_bytes(b"weights")

    verify_artifact(artifact, sha256_file(artifact), context="test")

    assert artifact.exists()


def test_verify_artifact_deletes_and_raises_on_mismatch(tmp_path) -> None:
    artifact = tmp_path / "model.gguf"
    artifact.write_bytes(b"tampered weights")

    with pytest.raises(ModelIntegrityError, match="Checksum mismatch"):
        verify_artifact(artifact, "0" * 64, context="test")

    # Leaving it on disk would let the next run reuse it as a cache hit.
    assert not artifact.exists()


def test_verify_artifact_is_a_noop_without_an_expected_checksum(tmp_path) -> None:
    artifact = tmp_path / "model.gguf"
    artifact.write_bytes(b"weights")

    verify_artifact(artifact, "", context="test")

    assert artifact.exists()


def test_cached_artifact_is_reverified_not_trusted(tmp_path, monkeypatch) -> None:
    """
    A cache hit must re-check the checksum. Verifying only at download time
    leaves a window where the cached file is modified afterwards and reused
    forever.
    """
    from opencomplai_ai import downloader
    from opencomplai_ai.models import ModelSpec

    cache = tmp_path / "models"
    cache.mkdir()
    (cache / "m.gguf").write_bytes(b"tampered")

    spec = ModelSpec(
        model_id="pinned",
        display_name="Pinned",
        size_mb=1,
        license="MIT",
        runtime="llama-cpp",
        hf_repo="org/repo",
        filename="m.gguf",
        requires_deep=False,
        revision="abc123",
        sha256="0" * 64,
    )
    monkeypatch.setattr(downloader, "MODEL_CATALOG", {"pinned": spec})
    monkeypatch.setattr(downloader, "get_cache_dir", lambda: cache)

    with pytest.raises(ModelIntegrityError):
        downloader.ensure_model("pinned")


def test_unpinned_download_is_refused_when_non_interactive(
    tmp_path, monkeypatch
) -> None:
    """CI is where an upstream swap is least likely to be noticed."""
    from opencomplai_ai import downloader
    from opencomplai_ai.models import ModelSpec

    cache = tmp_path / "models"
    spec = ModelSpec(
        model_id="unpinned",
        display_name="Unpinned",
        size_mb=1,
        license="MIT",
        runtime="llama-cpp",
        hf_repo="org/repo",
        filename="m.gguf",
        requires_deep=False,
    )
    monkeypatch.setattr(downloader, "MODEL_CATALOG", {"unpinned": spec})
    monkeypatch.setattr(downloader, "get_cache_dir", lambda: cache)
    monkeypatch.setattr(downloader, "stdin_is_interactive", lambda: False)
    monkeypatch.delenv("OPENCOMPLAI_OFFLINE", raising=False)

    with pytest.raises(UnpinnedModelError, match="unattended"):
        downloader.ensure_model("unpinned")


def test_download_under_offline_mode_raises_before_prompting(
    tmp_path, monkeypatch
) -> None:
    from opencomplai_ai import downloader
    from opencomplai_ai.models import ModelSpec

    spec = ModelSpec(
        model_id="pinned",
        display_name="Pinned",
        size_mb=1,
        license="MIT",
        runtime="llama-cpp",
        hf_repo="org/repo",
        filename="m.gguf",
        requires_deep=False,
        revision="abc123",
    )
    monkeypatch.setattr(downloader, "MODEL_CATALOG", {"pinned": spec})
    monkeypatch.setattr(downloader, "get_cache_dir", lambda: tmp_path / "models")
    monkeypatch.setenv("OPENCOMPLAI_OFFLINE", "1")

    with pytest.raises(egress.OfflineModeError):
        downloader.ensure_model("pinned")


def test_non_interactive_download_does_not_block_forever(tmp_path, monkeypatch) -> None:
    """
    The prompt used to call console.input() unconditionally, which blocks
    forever on a non-interactive stdin (the documented suite-hang hazard).
    It must raise instead.
    """
    from opencomplai_ai import downloader
    from opencomplai_ai.models import ModelSpec

    spec = ModelSpec(
        model_id="pinned",
        display_name="Pinned",
        size_mb=1,
        license="MIT",
        runtime="llama-cpp",
        hf_repo="org/repo",
        filename="m.gguf",
        requires_deep=False,
        revision="abc123",
    )
    monkeypatch.setattr(downloader, "MODEL_CATALOG", {"pinned": spec})
    monkeypatch.setattr(downloader, "get_cache_dir", lambda: tmp_path / "models")
    monkeypatch.setattr(downloader, "stdin_is_interactive", lambda: False)
    monkeypatch.delenv("OPENCOMPLAI_OFFLINE", raising=False)

    with pytest.raises(RuntimeError, match="not interactive"):
        downloader.ensure_model("pinned")
