/**
 * End-to-end test of the Vercel adapter's actual handler() export —
 * rewriteUrl.test.ts only covers the pure URL helper, so a regression in
 * the handler itself (argument order to rewriteUrl, body collection,
 * app.inject wiring, response piping) was previously invisible to CI.
 *
 * buildApp() runs at module load time in [...path].ts and throws without
 * an auth config, so the env is set before a dynamic import.
 */
import assert from "node:assert/strict";
import { test } from "node:test";

process.env.OPENCOMPLAI_API_KEY = "handler-test-key";
process.env.NODE_ENV = "test";

function fakeReq(
  url: string,
  method = "GET",
  headers: Record<string, string> = {},
) {
  return {
    url,
    method,
    headers,
    async *[Symbol.asyncIterator]() {},
  };
}

function fakeRes() {
  const res = {
    statusCode: 0,
    headers: {} as Record<string, string>,
    body: undefined as unknown,
    status(code: number) {
      res.statusCode = code;
      return res;
    },
    setHeader(name: string, value: string) {
      res.headers[name] = value;
    },
    send(payload: unknown) {
      res.body = payload;
    },
  };
  return res;
}

test("handler strips the mount prefix and reaches the bare /health route", async () => {
  const { default: handler } = await import("./[...path].ts");
  const req = fakeReq("/api/gateway/health");
  const res = fakeRes();

  await handler(req, res);

  assert.equal(res.statusCode, 200);
  const parsed = JSON.parse(String(res.body));
  assert.equal(parsed.status, "ok");
  assert.equal(parsed.service, "gateway-api");
});

test("handler passes an already-bare URL through unchanged", async () => {
  const { default: handler } = await import("./[...path].ts");
  const req = fakeReq("/health");
  const res = fakeRes();

  await handler(req, res);

  assert.equal(res.statusCode, 200);
});

test("handler forwards headers and returns 404 for an unknown route", async () => {
  const { default: handler } = await import("./[...path].ts");
  // Auth guards every non-health route, so the request must carry the API
  // key to get past the middleware and prove headers reach app.inject().
  const req = fakeReq("/api/gateway/no-such-route", "GET", {
    "x-api-key": "handler-test-key",
  });
  const res = fakeRes();

  await handler(req, res);

  assert.equal(res.statusCode, 404);
});

test("handler rejects an unauthenticated non-health route with 401", async () => {
  const { default: handler } = await import("./[...path].ts");
  const req = fakeReq("/api/gateway/no-such-route");
  const res = fakeRes();

  await handler(req, res);

  assert.equal(res.statusCode, 401);
});
