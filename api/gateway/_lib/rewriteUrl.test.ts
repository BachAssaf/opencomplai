import assert from "node:assert/strict";
import { test } from "node:test";

import { rewriteUrl } from "./rewriteUrl.ts";

const PREFIX = "/api/gateway";

test("strips the mount prefix and preserves the remaining path", () => {
  assert.equal(
    rewriteUrl("/api/gateway/v1/manifests/validate", PREFIX),
    "/v1/manifests/validate",
  );
});

test("strips the mount prefix and preserves a query string", () => {
  assert.equal(
    rewriteUrl("/api/gateway/v1/risk/classify?foo=bar&baz=1", PREFIX),
    "/v1/risk/classify?foo=bar&baz=1",
  );
});

test("maps the bare mount prefix itself to root", () => {
  assert.equal(rewriteUrl("/api/gateway", PREFIX), "/");
});

test("maps the mount prefix with a trailing slash to root, keeping the query", () => {
  assert.equal(rewriteUrl("/api/gateway/?x=1", PREFIX), "/?x=1");
});

test("leaves an already-bare /v1/* URL untouched", () => {
  assert.equal(rewriteUrl("/v1/health", PREFIX), "/v1/health");
});

test("leaves an already-bare /health URL untouched", () => {
  assert.equal(rewriteUrl("/health", PREFIX), "/health");
});

test("does not strip a route that merely shares the prefix string", () => {
  assert.equal(rewriteUrl("/api/gatewayfoo", PREFIX), "/api/gatewayfoo");
});

test("does not treat a later literal '?' in the query as a second delimiter", () => {
  assert.equal(
    rewriteUrl("/api/gateway/v1/verify/claims?source_ref=https://x?y=1", PREFIX),
    "/v1/verify/claims?source_ref=https://x?y=1",
  );
});
