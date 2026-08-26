"""
Tests for VercelBlobCASBackend (STORAGE_BACKEND=vercel_blob) — FINDING 48.7.

No test previously covered this backend at all: it has no ``_path_for``
(only LocalCASBackend has that), so main.py's
``storage_uri = str(cas._path_for(content_hash))`` raised AttributeError on
every store *after* the blob upload had already succeeded, orphaning it —
including on the dedup path, which hit the same call. These tests exercise
the backend directly, with no network (the ``vercel_blob`` SDK import is
stubbed via the ``fake_vercel_blob_module`` fixture in conftest.py), plus
the ``get_cas_backend()`` dispatch that selects this backend.
"""

from __future__ import annotations

import pytest
from opencomplai_evidence_vault.cas import VercelBlobCASBackend, get_cas_backend


def test_get_cas_backend_selects_vercel_blob(monkeypatch, fake_vercel_blob_module):
    monkeypatch.setenv("STORAGE_BACKEND", "vercel_blob")
    backend = get_cas_backend()
    assert isinstance(backend, VercelBlobCASBackend)


def test_write_returns_sha256_prefixed_hash(fake_vercel_blob_module):
    backend = VercelBlobCASBackend()
    content_hash = backend.write(b"hello vercel blob")
    assert content_hash.startswith("sha256:")


def test_storage_uri_uses_key_scheme_and_does_not_raise(fake_vercel_blob_module):
    """Regression test for finding 48.7: VercelBlobCASBackend has no
    _path_for — storage_uri must exist and use the backend's _key scheme
    instead, and main.py must be able to call it without AttributeError."""
    backend = VercelBlobCASBackend()
    content_hash = backend.write(b"storage uri check")

    uri = backend.storage_uri(content_hash)

    hex_part = content_hash.removeprefix("sha256:")
    assert uri == f"evidence/{hex_part[:2]}/{hex_part}"
    assert uri == backend._key(content_hash)
    assert not hasattr(backend, "_path_for")


def test_read_round_trips_content(fake_vercel_blob_module):
    backend = VercelBlobCASBackend()
    content = b"round trip payload"
    content_hash = backend.write(content)
    assert backend.read(content_hash) == content


def test_read_missing_raises_file_not_found(fake_vercel_blob_module):
    backend = VercelBlobCASBackend()
    with pytest.raises(FileNotFoundError):
        backend.read("sha256:" + "0" * 64)


def test_write_is_idempotent_and_skips_reupload(fake_vercel_blob_module):
    """Covers the early-return dedup path at cas.py:136-137 — a second
    write() of the same content must not re-upload to the blob store."""
    backend = VercelBlobCASBackend()
    h1 = backend.write(b"dedup content")
    h2 = backend.write(b"dedup content")
    assert h1 == h2
    assert fake_vercel_blob_module.put_calls == [backend._key(h1)]


def test_exists(fake_vercel_blob_module):
    backend = VercelBlobCASBackend()
    content_hash = backend.write(b"existence check")
    assert backend.exists(content_hash) is True
    assert backend.exists("sha256:" + "f" * 64) is False
