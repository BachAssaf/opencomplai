"""Tests for the evidence object store endpoint, including provenance metadata (EVID-PROV)."""

from __future__ import annotations

import base64
import os

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from opencomplai_core.service_auth import mint_service_token
from opencomplai_evidence_vault.badges import _BadgeBase
from opencomplai_evidence_vault.bias_alerts import _Base as _BiasBase
from opencomplai_evidence_vault.cas import CASStore
from opencomplai_evidence_vault.main import create_app
from opencomplai_evidence_vault.models import Base as _LedgerBase
from opencomplai_evidence_vault.models import EvidenceObjectDB
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

TEST_ISSUER = "test-caller"


@pytest_asyncio.fixture
async def client(tmp_path, _service_token_secret):
    db_path = tmp_path / "test-evidence-objects.db"
    cas_path = tmp_path / "cas"
    cas_path.mkdir()

    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(_LedgerBase.metadata.create_all)
        await conn.run_sync(_BiasBase.metadata.create_all)
        await conn.run_sync(_BadgeBase.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    app = create_app()
    app.state.engine = engine
    app.state.sessionmaker = session_factory
    app.state.cas = CASStore(str(cas_path))

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={
            "Authorization": f"Bearer {mint_service_token(TEST_ISSUER, os.environ['INTERNAL_SERVICE_TOKEN_SECRET'])}"
        },
    ) as ac:
        ac._test_sessionmaker = session_factory  # type: ignore[attr-defined]
        yield ac

    await engine.dispose()


def _b64(content: bytes) -> str:
    return base64.b64encode(content).decode("ascii")


async def test_store_object_without_provenance_defaults_source_to_principal(client):
    resp = await client.post(
        "/v1/evidence/objects", json={"content_base64": _b64(b"hello-world")}
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["source"] == TEST_ISSUER
    assert body["collected_at"] is not None
    assert body["source_version"] is None
    assert body["valid_until"] is None


async def test_store_object_with_explicit_provenance(client):
    resp = await client.post(
        "/v1/evidence/objects",
        json={
            "content_base64": _b64(b"with-provenance"),
            "source": "risk-engine",
            "source_version": "1.2.3",
            "collected_at": "2026-01-01T00:00:00+00:00",
            "valid_until": "2026-06-01T00:00:00+00:00",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["source"] == "risk-engine"
    assert body["source_version"] == "1.2.3"
    assert body["collected_at"] == "2026-01-01T00:00:00+00:00"
    assert body["valid_until"] == "2026-06-01T00:00:00+00:00"


async def test_storing_same_content_twice_does_not_error(client):
    content = _b64(b"idempotent-content")
    first = await client.post("/v1/evidence/objects", json={"content_base64": content})
    second = await client.post("/v1/evidence/objects", json={"content_base64": content})
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["content_hash"] == second.json()["content_hash"]


async def test_stored_object_persists_evidence_objects_row(client):
    resp = await client.post(
        "/v1/evidence/objects",
        json={
            "content_base64": _b64(b"row-check"),
            "source": "risk-engine",
            "source_version": "9.9.9",
        },
    )
    assert resp.status_code == 201
    content_hash = resp.json()["content_hash"]

    session_factory: async_sessionmaker = client._test_sessionmaker  # type: ignore[attr-defined]
    async with session_factory() as session:
        row = (
            await session.execute(
                select(EvidenceObjectDB).where(
                    EvidenceObjectDB.content_hash == content_hash
                )
            )
        ).scalar_one_or_none()
        assert row is not None
        assert row.source == "risk-engine"
        assert row.source_version == "9.9.9"
        assert row.collected_at is not None
