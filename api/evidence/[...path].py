"""
Vercel serverless entry point for the evidence-vault service.

Neon PostgreSQL is used for the ledger DB (DATABASE_URL env var).
Vercel Blob is used for the CAS (STORAGE_BACKEND=vercel_blob + BLOB_READ_WRITE_TOKEN).

Wrapped in StripMountPrefixASGI (api/_shared/mount_prefix.py): Vercel
forwards this function's own mount prefix (/api/evidence) unstripped, but
the FastAPI app's routes are registered bare ("/health", "/v1/*") — see that
module's docstring for why (finding 48.12).
"""

import os
import sys

_root = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, os.path.join(_root, "services", "evidence-vault", "src"))
sys.path.insert(0, os.path.join(_root, "packages", "core", "src"))
sys.path.insert(0, os.path.join(_root, "api", "_shared"))

from mount_prefix import StripMountPrefixASGI  # noqa: E402
from opencomplai_evidence_vault.main import app as _evidence_app  # noqa: E402

app = StripMountPrefixASGI(_evidence_app, "/api/evidence")
__all__ = ["app"]
