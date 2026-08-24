"""
Shared ASGI mount-prefix stripping for Vercel Python serverless entry points.

Vercel's Python runtime forwards the literal incoming request path into the
ASGI scope unmodified — a function mounted at /api/<name>/* (Vercel's
filesystem routing for api/<name>/[...path].py) receives requests with the
/api/<name> prefix still attached, unless VERCEL_SERVICE_ROUTE_PREFIX_STRIP
is explicitly enabled (nothing in this repo sets it; there is no
vercel.json). The platform's own runtime ships the equivalent mechanism —
opt-in and globally configured — as
vercel_runtime.routing.strip_service_route_prefix; this mirrors it on a
per-function basis instead.

Every service app imported by the api/*/[...path].py entry points registers
its routes bare ("/health", "/v1/*", ...), so without stripping this prefix
every request 404s. This is the Python-side equivalent of the fix applied to
api/gateway/[...path].ts (finding 48.12).

Files and directories under api/ whose name starts with "_" are not turned
into their own Vercel Serverless Functions, so importing this module from a
sibling entry point does not add a deployed route.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


def strip_mount_prefix(path: str, prefix: str) -> str:
    """
    Strip *prefix* from *path* if present; otherwise return *path* unchanged.

    >>> strip_mount_prefix("/api/risk", "/api/risk")
    '/'
    >>> strip_mount_prefix("/api/risk/v1/x", "/api/risk")
    '/v1/x'
    >>> strip_mount_prefix("/v1/x", "/api/risk")
    '/v1/x'
    >>> strip_mount_prefix("/api/riskfoo", "/api/risk")
    '/api/riskfoo'
    """
    if path == prefix:
        return "/"
    if path.startswith(f"{prefix}/"):
        return path[len(prefix) :]
    return path


class StripMountPrefixASGI:
    """
    ASGI middleware that strips a Vercel function's own mount prefix from
    the scope path on http requests before delegating to the wrapped app.

    Non-http scopes (e.g. "lifespan") pass through unchanged, so ASGI
    lifespan startup/shutdown still reaches the wrapped app directly.
    """

    __slots__ = ("_app", "_prefix")

    def __init__(self, app: ASGIApp, prefix: str) -> None:
        self._app = app
        self._prefix = prefix

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") == "http":
            path = scope.get("path") or "/"
            new_path = strip_mount_prefix(path, self._prefix)
            if new_path != path:
                scope = {**scope, "path": new_path}
                raw_path = scope.get("raw_path")
                if isinstance(raw_path, (bytes, bytearray)):
                    # Keep raw_path in sync with path (same encode/decode
                    # convention as vercel_runtime.routing's own prefix
                    # strip) — nothing here reads raw_path today, but a
                    # stale, un-stripped raw_path next to a stripped path
                    # would be a landmine for whichever consumer reads it
                    # next.
                    decoded = bytes(raw_path).decode("utf-8", "surrogateescape")
                    stripped = strip_mount_prefix(decoded, self._prefix)
                    scope["raw_path"] = stripped.encode("utf-8", "surrogateescape")
        await self._app(scope, receive, send)
