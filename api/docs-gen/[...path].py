"""
Vercel serverless entry point for the doc-generator service.

Dossier generation targets a 120s SLO, so maxDuration should be set to 120s
wherever this function's deployment is configured — there is no vercel.json
checked into this repo (verified at HEAD).

Wrapped in StripMountPrefixASGI (api/_shared/mount_prefix.py): Vercel
forwards this function's own mount prefix (/api/docs-gen) unstripped, but
the FastAPI app's routes are registered bare ("/health", "/v1/*") — see that
module's docstring for why (finding 48.12).
"""

import os
import sys

_root = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, os.path.join(_root, "services", "doc-generator", "src"))
sys.path.insert(0, os.path.join(_root, "packages", "core", "src"))
sys.path.insert(0, os.path.join(_root, "api", "_shared"))

from mount_prefix import StripMountPrefixASGI  # noqa: E402
from opencomplai_doc_generator.main import app as _docs_app  # noqa: E402

app = StripMountPrefixASGI(_docs_app, "/api/docs-gen")
__all__ = ["app"]
