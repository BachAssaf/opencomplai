"""
Unit tests for the Vercel mount-prefix stripping used by every
api/*/[...path].py Python entry point (finding 48.12).

Runs in CI as an explicit step of the test-core job (ci-python.yml) —
`api` is in pyproject.toml's testpaths too, but every CI job invokes pytest
with explicit paths, so testpaths alone wouldn't run this. Locally:
    uv run pytest api/_shared -q

Loads mount_prefix.py by file path (rather than a bare `import`) because the
repo's root conftest.py forces `--import-mode=importlib`, which does not add
a test file's own directory to sys.path.
"""

import asyncio
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "mount_prefix", Path(__file__).parent / "mount_prefix.py"
)
assert _spec is not None
assert _spec.loader is not None
_mount_prefix = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mount_prefix)

StripMountPrefixASGI = _mount_prefix.StripMountPrefixASGI
strip_mount_prefix = _mount_prefix.strip_mount_prefix


def test_strips_exact_prefix_to_root():
    assert strip_mount_prefix("/api/risk", "/api/risk") == "/"


def test_strips_prefix_and_keeps_remaining_path():
    assert (
        strip_mount_prefix("/api/risk/v1/risk/classify", "/api/risk")
        == "/v1/risk/classify"
    )


def test_leaves_already_bare_path_untouched():
    assert strip_mount_prefix("/v1/risk/classify", "/api/risk") == "/v1/risk/classify"
    assert strip_mount_prefix("/health", "/api/risk") == "/health"


def test_does_not_strip_a_route_that_merely_shares_the_prefix_string():
    assert strip_mount_prefix("/api/riskfoo", "/api/risk") == "/api/riskfoo"


def test_each_sibling_adapter_prefix():
    cases = {
        "/api/evidence/v1/evidence/verify-chain": (
            "/api/evidence",
            "/v1/evidence/verify-chain",
        ),
        "/api/docs-gen/v1/docs/generate": ("/api/docs-gen", "/v1/docs/generate"),
        "/api/egress/v1/sync/metadata": ("/api/egress", "/v1/sync/metadata"),
    }
    for path, (prefix, expected) in cases.items():
        assert strip_mount_prefix(path, prefix) == expected


def test_asgi_wrapper_rewrites_http_scope_path():
    captured_scopes = []

    async def inner_app(scope, receive, send):
        captured_scopes.append(scope)

    wrapped = StripMountPrefixASGI(inner_app, "/api/risk")

    asyncio.run(
        wrapped(
            {"type": "http", "path": "/api/risk/v1/risk/classify"},
            receive=lambda: None,
            send=lambda message: None,
        )
    )

    assert captured_scopes[0]["path"] == "/v1/risk/classify"


def test_asgi_wrapper_keeps_raw_path_in_sync_with_path():
    captured_scopes = []

    async def inner_app(scope, receive, send):
        captured_scopes.append(scope)

    wrapped = StripMountPrefixASGI(inner_app, "/api/risk")

    asyncio.run(
        wrapped(
            {
                "type": "http",
                "path": "/api/risk/v1/risk/classify",
                "raw_path": b"/api/risk/v1/risk/classify",
            },
            receive=lambda: None,
            send=lambda message: None,
        )
    )

    assert captured_scopes[0]["path"] == "/v1/risk/classify"
    assert captured_scopes[0]["raw_path"] == b"/v1/risk/classify"


def test_asgi_wrapper_passes_lifespan_scope_through_unchanged():
    captured_scopes = []

    async def inner_app(scope, receive, send):
        captured_scopes.append(scope)

    wrapped = StripMountPrefixASGI(inner_app, "/api/risk")

    lifespan_scope = {"type": "lifespan"}
    asyncio.run(
        wrapped(lifespan_scope, receive=lambda: None, send=lambda message: None)
    )

    assert captured_scopes[0] is lifespan_scope
