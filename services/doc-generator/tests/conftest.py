"""
Shared test fixtures for doc-generator.

Every /v1/* route now requires a signed internal service token
(SEC-SERVICE-AUTH). Tests set a fixed shared secret and expose a ready-made
Authorization header so each test file's own `client` fixture can attach it.
"""

from __future__ import annotations

import pytest
from opencomplai_core.service_auth import mint_service_token

TEST_SERVICE_TOKEN_SECRET = "doc-generator-test-secret"


@pytest.fixture(autouse=True)
def _service_token_secret(monkeypatch):
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN_SECRET", TEST_SERVICE_TOKEN_SECRET)


@pytest.fixture
def service_auth_headers() -> dict[str, str]:
    token = mint_service_token("test-caller", TEST_SERVICE_TOKEN_SECRET)
    return {"Authorization": f"Bearer {token}"}
