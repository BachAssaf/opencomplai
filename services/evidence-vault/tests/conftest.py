"""
Shared test fixtures for evidence-vault.

Every /v1/* route now requires a signed internal service token
(SEC-SERVICE-AUTH). Tests set a fixed shared secret and expose a ready-made
Authorization header so each test file's own `client` fixture can attach it.
"""

from __future__ import annotations

import sys

import pytest
from opencomplai_core.service_auth import mint_service_token

TEST_SERVICE_TOKEN_SECRET = "evidence-vault-test-secret"


@pytest.fixture(autouse=True)
def _service_token_secret(monkeypatch):
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN_SECRET", TEST_SERVICE_TOKEN_SECRET)


@pytest.fixture
def service_auth_headers() -> dict[str, str]:
    token = mint_service_token("test-caller", TEST_SERVICE_TOKEN_SECRET)
    return {"Authorization": f"Bearer {token}"}


class _FakeVercelBlobModule:
    """In-memory stand-in for the ``vercel_blob`` SDK module.

    VercelBlobCASBackend.__init__ does ``import vercel_blob`` and stores the
    module object on ``self._blob`` — that import is the only seam the class
    exposes for testing (cas.py:107-165), so tests install an instance of
    this class into ``sys.modules["vercel_blob"]`` instead of hitting the
    network or requiring the real ``vercel-blob`` package to be installed
    (FINDING 48.7 — no test previously covered this backend at all).
    """

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.put_calls: list[str] = []

    def put(self, key: str, content: bytes, options: dict) -> dict:
        self.put_calls.append(key)
        self.store[key] = content
        return {"url": f"https://fake-blob.test/{key}"}

    def head(self, key: str) -> dict:
        if key not in self.store:
            raise FileNotFoundError(key)
        return {"url": f"https://fake-blob.test/{key}"}

    def download(self, key: str) -> bytes:
        return self.store[key]


@pytest.fixture
def fake_vercel_blob_module(monkeypatch) -> _FakeVercelBlobModule:
    fake = _FakeVercelBlobModule()
    monkeypatch.setitem(sys.modules, "vercel_blob", fake)
    return fake
