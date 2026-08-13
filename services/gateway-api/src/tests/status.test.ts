import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { FastifyInstance } from 'fastify';
import { buildApp } from '../index';
import {
  aggregateDownstreamHealth,
  checkDownstream,
  downstreamTargets,
  resetStatusCache,
} from '../downstreamHealth';

/**
 * GET /v1/status used to return a hardcoded `services: {}`. These tests pin
 * that it now really probes downstreams, that the probe targets are correct
 * (notably egress-proxy's `/egress-health`, not its gateway-proxying
 * `/health`), and the 200-by-default / 503-under-`?strict=1` contract.
 */

function okResponse(body: unknown = { status: 'ok', version: '1.2.3' }): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('downstream health probes', () => {
  beforeEach(() => {
    resetStatusCache();
    vi.restoreAllMocks();
  });

  afterEach(() => {
    resetStatusCache();
    vi.restoreAllMocks();
  });

  it('probes egress-proxy on /egress-health, not /health', () => {
    // egress-proxy's /health proxies to GATEWAY_API_URL and 503s when the
    // gateway is unreachable. Probing it from the gateway would measure "can
    // egress-proxy reach us" and mark egress-proxy down for a gateway-side
    // fault. /egress-health is its real self-liveness.
    const targets = downstreamTargets();
    const byName = Object.fromEntries(targets.map((t) => [t.name, t.healthPath]));

    expect(byName['egress-proxy']).toBe('/egress-health');
    expect(byName['risk-engine']).toBe('/health');
    expect(byName['evidence-vault']).toBe('/health');
    expect(byName['doc-generator']).toBe('/health');
  });

  it('resolves target URLs from the same env vars the proxy routes use', () => {
    process.env.RISK_ENGINE_URL = 'http://risk.test:9999';
    try {
      const target = downstreamTargets().find((t) => t.name === 'risk-engine');
      expect(target?.url).toBe('http://risk.test:9999');
    } finally {
      delete process.env.RISK_ENGINE_URL;
    }
  });

  it('reports ok and the upstream version on a healthy probe', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => okResponse({ status: 'ok', version: '0.4.2' })),
    );

    const health = await checkDownstream({
      name: 'risk-engine',
      url: 'http://risk.test',
      healthPath: '/health',
    });

    expect(health.status).toBe('ok');
    expect(health.version).toBe('0.4.2');
    expect(health.reason).toBeUndefined();
    expect(typeof health.latency_ms).toBe('number');
  });

  it('still reports ok when a 200 body is not parseable JSON', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response('not json', { status: 200 })),
    );

    const health = await checkDownstream({
      name: 'risk-engine',
      url: 'http://risk.test',
      healthPath: '/health',
    });

    // The process is up and serving; a missing version is not a failure.
    expect(health.status).toBe('ok');
    expect(health.version).toBeUndefined();
  });

  it('distinguishes an unhealthy answer from an unreachable service', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response('{}', { status: 500 })),
    );
    const answered = await checkDownstream({
      name: 'risk-engine',
      url: 'http://risk.test',
      healthPath: '/health',
    });

    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        throw new Error('ECONNREFUSED');
      }),
    );
    const unreachable = await checkDownstream({
      name: 'risk-engine',
      url: 'http://risk.test',
      healthPath: '/health',
    });

    expect(answered.status).toBe('degraded');
    expect(answered.reason).toBe('http_error');
    expect(unreachable.status).toBe('unreachable');
    expect(unreachable.reason).toBe('connection_error');
  });

  it('never leaks the target URL or raw error text into the result', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        throw new Error('connect ECONNREFUSED 10.1.2.3:8001');
      }),
    );

    const health = await checkDownstream({
      name: 'risk-engine',
      url: 'http://internal-risk-engine.svc.cluster.local:8001',
      healthPath: '/health',
    });

    const serialised = JSON.stringify(health);
    expect(serialised).not.toContain('internal-risk-engine');
    expect(serialised).not.toContain('10.1.2.3');
    expect(health.reason).toBe('connection_error');
  });

  it('probes concurrently rather than serially', async () => {
    let active = 0;
    let peak = 0;
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        active += 1;
        peak = Math.max(peak, active);
        await new Promise((r) => setTimeout(r, 5));
        active -= 1;
        return okResponse();
      }),
    );

    await aggregateDownstreamHealth();

    // Four sequential probes against a down stack would take 4x the timeout.
    expect(peak).toBe(4);
  });

  it('caches the aggregate so repeated calls do not re-probe', async () => {
    const fetchMock = vi.fn(async () => okResponse());
    vi.stubGlobal('fetch', fetchMock);

    await aggregateDownstreamHealth();
    await aggregateDownstreamHealth();

    expect(fetchMock).toHaveBeenCalledTimes(4);
  });

  it('collapses concurrent cold-cache callers onto a single fan-out', async () => {
    const fetchMock = vi.fn(async () => {
      await new Promise((r) => setTimeout(r, 5));
      return okResponse();
    });
    vi.stubGlobal('fetch', fetchMock);

    await Promise.all([
      aggregateDownstreamHealth(),
      aggregateDownstreamHealth(),
      aggregateDownstreamHealth(),
    ]);

    // Without single-flight this would be 12.
    expect(fetchMock).toHaveBeenCalledTimes(4);
  });
});

describe('GET /v1/status', () => {
  let app: FastifyInstance;

  beforeAll(async () => {
    delete process.env.OPENCOMPLAI_API_KEY;
    process.env.OPENCOMPLAI_AUTH_DISABLED = '1';
    app = buildApp();
    await app.ready();
  });

  afterAll(async () => {
    await app.close();
    delete process.env.OPENCOMPLAI_AUTH_DISABLED;
  });

  beforeEach(() => {
    resetStatusCache();
    vi.restoreAllMocks();
  });

  afterEach(() => {
    resetStatusCache();
    vi.restoreAllMocks();
  });

  it('reports every downstream instead of an empty object', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => okResponse()),
    );

    const res = await app.inject({ method: 'GET', url: '/v1/status' });
    const body = JSON.parse(res.body);

    expect(res.statusCode).toBe(200);
    expect(body.status).toBe('ok');
    expect(Object.keys(body.services).sort()).toEqual([
      'doc-generator',
      'egress-proxy',
      'evidence-vault',
      'risk-engine',
    ]);
    expect(body.checked_at).toBeTruthy();
  });

  it('reports degraded, still 200, when a downstream is unreachable', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        if (String(url).includes('doc-generator')) throw new Error('ECONNREFUSED');
        return okResponse();
      }),
    );

    const res = await app.inject({ method: 'GET', url: '/v1/status' });
    const body = JSON.parse(res.body);

    // 200 by default: the request succeeded and the body is the real answer.
    // A non-2xx would be discarded unparsed by many clients.
    expect(res.statusCode).toBe(200);
    expect(body.status).toBe('degraded');
    expect(body.services['doc-generator'].status).toBe('unreachable');
    expect(body.services['risk-engine'].status).toBe('ok');
  });

  it('returns 503 under ?strict=1 when degraded, with the same body', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        if (String(url).includes('doc-generator')) throw new Error('ECONNREFUSED');
        return okResponse();
      }),
    );

    const res = await app.inject({ method: 'GET', url: '/v1/status?strict=1' });
    const body = JSON.parse(res.body);

    expect(res.statusCode).toBe(503);
    expect(body.status).toBe('degraded');
    // The detail survives the 503 — this is the status document, not an
    // error envelope.
    expect(body.services['doc-generator'].status).toBe('unreachable');
  });

  it('returns 200 under ?strict=1 when everything is healthy', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => okResponse()),
    );

    const res = await app.inject({ method: 'GET', url: '/v1/status?strict=1' });

    expect(res.statusCode).toBe(200);
    expect(JSON.parse(res.body).status).toBe('ok');
  });

  it('leaves GET /health independent of downstream state', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        throw new Error('ECONNREFUSED');
      }),
    );

    const res = await app.inject({ method: 'GET', url: '/health' });

    // Liveness must not depend on downstreams, or one dead service restarts
    // a healthy gateway.
    expect(res.statusCode).toBe(200);
    expect(JSON.parse(res.body).status).toBe('ok');
  });
});
