"""
Vercel serverless entry point for the egress-proxy service.

On Vercel, outbound network access is unrestricted at the infrastructure level;
the egress-proxy still enforces the application-level allowlist via ALLOWED_DESTINATIONS.

Wrapped in StripMountPrefixASGI (api/_shared/mount_prefix.py): Vercel
forwards this function's own mount prefix (/api/egress) unstripped, but the
FastAPI app's routes are registered bare ("/health", "/egress-health",
"/v1/*") — see that module's docstring for why (finding 48.12).
"""

import os
import sys

_root = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, os.path.join(_root, "services", "egress-proxy", "src"))
sys.path.insert(0, os.path.join(_root, "packages", "core", "src"))
sys.path.insert(0, os.path.join(_root, "api", "_shared"))

from mount_prefix import StripMountPrefixASGI  # noqa: E402
from opencomplai_egress_proxy.main import app as _egress_app  # noqa: E402

app = StripMountPrefixASGI(_egress_app, "/api/egress")
__all__ = ["app"]
