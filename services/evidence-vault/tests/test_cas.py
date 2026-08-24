"""Tests for the content-addressable store (REQ-EV-002)."""

from __future__ import annotations

from pathlib import Path

import pytest
from opencomplai_evidence_vault.cas import CASStore


@pytest.fixture
def cas(tmp_path: Path) -> CASStore:
    return CASStore(str(tmp_path / "cas"))


def test_write_returns_sha256_prefixed_hash(cas: CASStore):
    content_hash = cas.write(b"hello world")
    assert content_hash.startswith("sha256:")


def test_write_is_idempotent(cas: CASStore):
    h1 = cas.write(b"idempotent content")
    h2 = cas.write(b"idempotent content")
    assert h1 == h2


def test_read_returns_original_content(cas: CASStore):
    content = b"test evidence payload"
    content_hash = cas.write(content)
    retrieved = cas.read(content_hash)
    assert retrieved == content


def test_read_missing_raises_file_not_found(cas: CASStore):
    with pytest.raises(FileNotFoundError):
        cas.read("sha256:" + "0" * 64)


def test_read_tampered_raises_value_error(cas: CASStore):
    content = b"original content"
    content_hash = cas.write(content)
    path = cas._path_for(content_hash)
    path.write_bytes(b"tampered content")
    with pytest.raises(ValueError, match="Integrity violation"):
        cas.read(content_hash)


def test_exists(cas: CASStore):
    content_hash = cas.write(b"existence test")
    assert cas.exists(content_hash) is True
    assert cas.exists("sha256:" + "f" * 64) is False


def test_storage_uri_matches_path_for(cas: CASStore):
    """FINDING 48.7: storage_uri() must return the same value main.py used
    to persist via _path_for(), so previously stored evidence_objects rows
    keep resolving to the same filesystem path."""
    content_hash = cas.write(b"storage uri check")
    assert cas.storage_uri(content_hash) == str(cas._path_for(content_hash))


# ---------------------------------------------------------------------------
# Path traversal (M-02) — content_hash must be validated before any
# filesystem operation, not merely have "sha256:" stripped.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_hash",
    [
        "sha256:../../../../etc/passwd",
        "sha256:" + "../" * 20 + "etc/passwd",
        "sha256:" + "a" * 63,  # too short
        "sha256:" + "a" * 65,  # too long
        "sha256:" + "A" * 64,  # uppercase not allowed
        "sha256:" + "g" * 64,  # non-hex char
        "not-a-hash",
        "sha256:",
    ],
)
def test_read_rejects_traversal_and_malformed_hashes(cas: CASStore, bad_hash: str):
    with pytest.raises(ValueError, match="Invalid content hash format"):
        cas.read(bad_hash)


def test_path_for_rejects_traversal_hash(cas: CASStore):
    with pytest.raises(ValueError, match="Invalid content hash format"):
        cas._path_for("sha256:../../../../etc/passwd")


def test_path_for_never_escapes_base_dir(cas: CASStore):
    content_hash = cas.write(b"containment check")
    resolved = cas._path_for(content_hash)
    assert resolved.is_relative_to(cas.base_dir.resolve())


def test_exists_rejects_traversal_hash(cas: CASStore):
    with pytest.raises(ValueError, match="Invalid content hash format"):
        cas.exists("sha256:../../../../etc/passwd")
