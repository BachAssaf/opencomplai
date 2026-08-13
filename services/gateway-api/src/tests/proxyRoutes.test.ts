import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { FastifyInstance } from 'fastify';
import { buildApp } from '../index';

/**
 * Handler tests for the proxying route modules (TESTING).
 *
 * risk, portfolio, pro, sync and verify were externally reachable with zero
 * handler tests. (`status` gained its own suite in OPS-HEALTH.) These are
 * thin proxies, so the properties worth pinning are the ones a thin proxy
 * gets wrong: does it target the right service and upstream path, does it
 * forward the body and method, does it relay the upstream status rather than
 * flattening it, and does it fail 503 rather than 500 when the upstream is
 * down.
 */

/** `RequestInit` is not in the lint env's globals; derive it from `fetch`. */
type FetchInit = NonNullable<Parameters<typeof fetch>[1]>;

interface Captured {
  url: string;
  method: string;
  body: unknown;
  headers: Record<string, string>;
}

let captured: Captured[] = [];

/**
 * The compose defaults, used rather than overriding the env vars in a hook.
 *
 * `pro.ts` and `portfolio.ts` read their service URL into a module-level
 * `const` at import time, while `risk.ts`, `verify.ts` and `sync.ts` read
 * `process.env` inside the handler. Setting env in `beforeAll` therefore takes
 * effect for some routes and not others — an inconsistency in the route
 * modules, harmless in production (env is fixed at boot) but a trap for tests.
 * Asserting against the defaults pins the routing without depending on it.
 */
const RISK_ENGINE = 'http://risk-engine:8001';
const EVIDENCE_VAULT = 'http://evidence-vault:8002';
const EGRESS_PROXY = 'http://egress-proxy:8004';

/** Stub fetch, recording every outbound call and replying with `reply`. */
function stubUpstream(reply: { status?: number; body?: unknown } = {}): void {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init: FetchInit) => {
      captured.push({
        url: String(url),
        method: String(init?.method ?? 'GET'),
        body: init?.body ? JSON.parse(String(init.body)) : undefined,
        headers: (init?.headers ?? {}) as Record<string, string>,
      });
      return new Response(JSON.stringify(reply.body ?? { ok: true }), {
        status: reply.status ?? 200,
        headers: { 'Content-Type': 'application/json' },
      });
    }),
  );
}

function stubUnreachableUpstream(): void {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => {
      throw new Error('ECONNREFUSED');
    }),
  );
}

describe('proxying route modules', () => {
  let app: FastifyInstance;

  beforeAll(async () => {
    delete process.env.OPENCOMPLAI_API_KEY;
    process.env.OPENCOMPLAI_AUTH_DISABLED = '1';
    // Set before the first request: loadSharedSecret() memoises on first call
    // (env is fixed at boot in production), so setting it later has no effect.
    process.env.INTERNAL_SERVICE_TOKEN_SECRET = 'gateway-route-test-secret';
    app = buildApp();
    await app.ready();
  });

  afterAll(async () => {
    await app.close();
    delete process.env.OPENCOMPLAI_AUTH_DISABLED;
    delete process.env.INTERNAL_SERVICE_TOKEN_SECRET;
  });

  beforeEach(() => {
    captured = [];
    vi.restoreAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  const cases: {
    name: string;
    method: 'GET' | 'POST';
    url: string;
    payload?: Record<string, unknown>;
    upstream: string;
    upstreamPath: string;
  }[] = [
    {
      name: 'risk',
      method: 'POST',
      url: '/v1/risk/classify',
      payload: { system_id: 'sys-1', model: { use_case: 'credit scoring' } },
      upstream: RISK_ENGINE,
      upstreamPath: '/v1/risk/classify',
    },
    {
      name: 'portfolio',
      method: 'GET',
      url: '/v1/portfolio',
      upstream: EVIDENCE_VAULT,
      upstreamPath: '/v1/portfolio',
    },
    {
      name: 'verify',
      method: 'POST',
      url: '/v1/verify/claims',
      payload: { claims: [] },
      upstream: RISK_ENGINE,
      upstreamPath: '/v1/verify/claims',
    },
    {
      name: 'sync',
      method: 'POST',
      url: '/v1/sync/metadata',
      payload: { system_id: 'sys-1' },
      upstream: EGRESS_PROXY,
      upstreamPath: '/v1/sync/metadata',
    },
    {
      name: 'pro (badge issue)',
      method: 'POST',
      url: '/v1/pro/badges/issue',
      payload: { system_id: 'sys-1' },
      upstream: EVIDENCE_VAULT,
      upstreamPath: '/v1/pro/badges/issue',
    },
    {
      name: 'pro (ingest metrics)',
      method: 'POST',
      url: '/v1/pro/ingest/metrics',
      payload: { pass_count: 1 },
      upstream: EGRESS_PROXY,
      upstreamPath: '/v1/pro/ingest/metrics',
    },
  ];

  for (const c of cases) {
    describe(`${c.method} ${c.url}`, () => {
      it('proxies to the correct service and upstream path', async () => {
        stubUpstream();

        await app.inject({ method: c.method, url: c.url, payload: c.payload });

        expect(captured).toHaveLength(1);
        expect(captured[0].url).toBe(`${c.upstream}${c.upstreamPath}`);
        expect(captured[0].method).toBe(c.method);
      });

      it('relays the upstream status rather than flattening it', async () => {
        stubUpstream({ status: 422, body: { error_code: 'VALIDATION_ERROR' } });

        const res = await app.inject({ method: c.method, url: c.url, payload: c.payload });

        expect(res.statusCode).toBe(422);
        expect(JSON.parse(res.body).error_code).toBe('VALIDATION_ERROR');
      });

      it('returns 503 DEPENDENCY_UNAVAILABLE when the upstream is down', async () => {
        stubUnreachableUpstream();

        const res = await app.inject({ method: c.method, url: c.url, payload: c.payload });

        expect(res.statusCode).toBe(503);
        expect(JSON.parse(res.body).error_code).toBe('DEPENDENCY_UNAVAILABLE');
      });

      it('attaches a service-auth token to the outbound call', async () => {
        stubUpstream();

        await app.inject({ method: c.method, url: c.url, payload: c.payload });

        expect(captured[0].headers.Authorization).toMatch(/^Bearer /);
      });
    });
  }

  it('forwards the request body verbatim to the upstream', async () => {
    stubUpstream();
    const payload = { system_id: 'sys-1', model: { use_case: 'recidivism prediction' } };

    await app.inject({ method: 'POST', url: '/v1/risk/classify', payload });

    expect(captured[0].body).toEqual(payload);
  });

  it('does not send a body on a GET proxy', async () => {
    stubUpstream();

    await app.inject({ method: 'GET', url: '/v1/portfolio' });

    expect(captured[0].body).toBeUndefined();
  });
});

/**
 * The roadmap's integration criterion: an authenticated request through the
 * gateway asserting on real proxied *response content*, not just a status
 * code. risk-engine is stubbed at the network boundary rather than run for
 * real — the assertion is that the gateway relays a genuine classification
 * body end to end without reshaping it.
 */
describe('gateway → risk-engine integration', () => {
  let app: FastifyInstance;

  beforeAll(async () => {
    process.env.OPENCOMPLAI_API_KEY = 'integration-test-key';
    delete process.env.OPENCOMPLAI_AUTH_DISABLED;
    app = buildApp();
    await app.ready();
  });

  afterAll(async () => {
    await app.close();
    delete process.env.OPENCOMPLAI_API_KEY;
  });

  beforeEach(() => {
    captured = [];
    vi.restoreAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  const CLASSIFICATION = {
    system_id: 'sys-credit-1',
    risk_tier: 'high_risk',
    annex_iii_area: 5,
    obligations: ['art_9_risk_management', 'art_11_technical_documentation'],
    determination_path: ['annex_iii_match', 'natural_person_subject'],
  };

  it('relays a real classification body to an authenticated caller', async () => {
    stubUpstream({ status: 200, body: CLASSIFICATION });

    const res = await app.inject({
      method: 'POST',
      url: '/v1/risk/classify',
      headers: { 'x-api-key': 'integration-test-key' },
      payload: {
        system_id: 'sys-credit-1',
        model: { use_case: 'credit scoring for loan applicants' },
      },
    });

    expect(res.statusCode).toBe(200);
    const body = JSON.parse(res.body);
    // Assert on content, not just the status: a proxy that dropped or
    // reshaped the classification would still return 200.
    expect(body.risk_tier).toBe('high_risk');
    expect(body.annex_iii_area).toBe(5);
    expect(body.obligations).toContain('art_9_risk_management');
    expect(body).toEqual(CLASSIFICATION);
  });

  it('rejects the same request without credentials, before reaching upstream', async () => {
    stubUpstream({ status: 200, body: CLASSIFICATION });

    const res = await app.inject({
      method: 'POST',
      url: '/v1/risk/classify',
      payload: { system_id: 'sys-credit-1' },
    });

    expect(res.statusCode).toBe(401);
    // The important half: an unauthenticated request must not reach the
    // internal service at all.
    expect(captured).toHaveLength(0);
  });
});
