"""Tests for HMAC-signed internal service tokens — mint/verify round trip."""

import time

import pytest
from opencomplai_core.service_auth import (
    ServiceTokenError,
    load_shared_secret,
    mint_service_token,
    verify_service_token,
)

SECRET = "test-shared-secret"


def test_mint_and_verify_round_trip() -> None:
    token = mint_service_token("gateway-api", SECRET)
    claims = verify_service_token(token, SECRET)
    assert claims.issuer == "gateway-api"


def test_verify_fails_with_wrong_secret() -> None:
    token = mint_service_token("gateway-api", SECRET)
    with pytest.raises(ServiceTokenError):
        verify_service_token(token, "wrong-secret")


def test_verify_fails_for_tampered_payload() -> None:
    token = mint_service_token("gateway-api", SECRET)
    payload_b64, signature_b64 = token.split(".", 1)
    tampered = f"{payload_b64}x.{signature_b64}"
    with pytest.raises(ServiceTokenError):
        verify_service_token(tampered, SECRET)


def test_verify_fails_for_malformed_token() -> None:
    with pytest.raises(ServiceTokenError):
        verify_service_token("not-a-valid-token", SECRET)


def test_verify_fails_for_expired_token() -> None:
    token = mint_service_token("gateway-api", SECRET, ttl_seconds=-1)
    with pytest.raises(ServiceTokenError):
        verify_service_token(token, SECRET)


def test_different_issuers_produce_different_tokens() -> None:
    token_a = mint_service_token("gateway-api", SECRET)
    token_b = mint_service_token("risk-engine", SECRET)
    assert token_a != token_b
    assert verify_service_token(token_a, SECRET).issuer == "gateway-api"
    assert verify_service_token(token_b, SECRET).issuer == "risk-engine"


def test_token_carries_expiry_close_to_requested_ttl() -> None:
    before = int(time.time())
    token = mint_service_token("doc-generator", SECRET, ttl_seconds=60)
    claims = verify_service_token(token, SECRET)
    assert before + 55 <= claims.expires_at <= before + 65


def test_load_shared_secret_reads_env_var() -> None:
    assert load_shared_secret({"INTERNAL_SERVICE_TOKEN_SECRET": "abc"}) == "abc"


def test_load_shared_secret_returns_none_when_unset() -> None:
    assert load_shared_secret({}) is None


def test_load_shared_secret_returns_none_for_blank() -> None:
    assert load_shared_secret({"INTERNAL_SERVICE_TOKEN_SECRET": "   "}) is None
