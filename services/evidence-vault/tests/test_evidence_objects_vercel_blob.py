"""
Endpoint-level regression tests for the evidence object store route running
against STORAGE_BACKEND=vercel_blob (FINDING 48.7).

Before the fix, ``store_evidence_object`` called
``cas._path_for(content_hash)`` unconditionally — a LocalCASBackend-only
method — so every ``POST /v1/evidence/objects`` raised AttributeError
against this backend, *after* the blob put had already succeeded (orphaning
it). The dedup path (storing the same content twice) hit the same call and
failed identically. No test previously exercised this backend through the
HTTP layer at all. No network is involved — the ``vercel_blob`` SDK import
is stubbed via the ``fake_vercel_blob_module`` fixture in conftest.py.
"""

from __future__ import annotations

import base64
import os

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from opencomplai_core.service_auth import mint_service_token
from opencomplai_evidence_vault.badges import _BadgeBase
from opencomplai_evidence_vault.bias_alerts import _Base as _BiasBase
from opencomplai_evidence_vault.cas import get_cas_backend
from opencomplai_evidence_vault.main import create_app
from opencomplai_evidence_vault.models import Base as _LedgerBase
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

TEST_ISSUER = "test-caller"


@pytest_asyncio.fixture
async def client(tmp_path, _service_token_secret, monkeypatch, fake_vercel_blob_module):
    monkeypatch.setenv("STORAGE_BACKEND", "vercel_blob")

    db_path = tmp_path / "test-evidence-objects-vercel-blob.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(_LedgerBase.metadata.create_all)
        await conn.run_sync(_BiasBase.metadata.create_all)
        await conn.run_sync(_BadgeBase.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    app = create_app()
    app.state.engine = engine
    app.state.sessionmaker = session_factory
    # STORAGE_BACKEND=vercel_blob (set above) -> VercelBlobCASBackend,
    # mirroring how main.py's lifespan wires app.state.cas in production.
    app.state.cas = get_cas_backend()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={
            "Authorization": f"Bearer {mint_service_token(TEST_ISSUER, os.environ['INTERNAL_SERVICE_TOKEN_SECRET'])}"
        },
    ) as ac:
        yield ac

    await engine.dispose()


def _b64(content: bytes) -> str:
    return base64.b64encode(content).decode("ascii")


async def test_store_object_against_vercel_blob_backend_returns_storage_uri(client):
    resp = await client.post(
        "/v1/evidence/objects", json={"content_base64": _b64(b"vercel-blob-content")}
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["content_hash"].startswith("sha256:")
    assert body["storage_uri"].startswith("evidence/")


async def test_storing_same_content_twice_dedups_against_vercel_blob_backend(client):
    content = _b64(b"vercel-blob-dedup-content")
    first = await client.post("/v1/evidence/objects", json={"content_base64": content})
    second = await client.post("/v1/evidence/objects", json={"content_base64": content})

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["content_hash"] == second.json()["content_hash"]
    assert first.json()["storage_uri"] == second.json()["storage_uri"]


async def test_get_evidence_object_round_trips_against_vercel_blob_backend(client):
    content = b"vercel-blob-round-trip"
    store_resp = await client.post(
        "/v1/evidence/objects", json={"content_base64": _b64(content)}
    )
    content_hash = store_resp.json()["content_hash"]

    get_resp = await client.get(f"/v1/evidence/objects/{content_hash}")

    assert get_resp.status_code == 200
    assert base64.b64decode(get_resp.json()["content_base64"]) == content
