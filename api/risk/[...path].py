"""
Vercel serverless entry point for the risk-engine service.

Adds the service source and shared core package to sys.path so the existing
FastAPI app can be imported without any code changes.

Wrapped in StripMountPrefixASGI (api/_shared/mount_prefix.py): Vercel
forwards this function's own mount prefix (/api/risk) unstripped, but the
FastAPI app's routes are registered bare ("/health", "/v1/*") — see that
module's docstring for why (finding 48.12).
"""

import os
import sys

_root = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, os.path.join(_root, "services", "risk-engine", "src"))
sys.path.insert(0, os.path.join(_root, "packages", "core", "src"))
sys.path.insert(0, os.path.join(_root, "api", "_shared"))

from mount_prefix import StripMountPrefixASGI  # noqa: E402
from opencomplai_risk_engine.main import app as _risk_app  # noqa: E402

# Vercel detects an ASGI app assigned to a module-level `app` variable.
app = StripMountPrefixASGI(_risk_app, "/api/risk")
__all__ = ["app"]
