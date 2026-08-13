/**
 * Per-downstream health aggregation for GET /v1/status (OPS-HEALTH).
 *
 * `/v1/status` previously returned a hardcoded `services: {}` and never
 * contacted anything, so it could not be used for orchestration or a status
 * page (finding 40). This module probes each internal Python service and
 * reports what it found.
 *
 * No credentials are minted: SEC-SERVICE-AUTH deliberately exempted `/health`,
 * `/metrics`, and `/egress-health` from the signed-service-token requirement.
 * Not sending a token is deliberate rather than incidental — it keeps the
 * status endpoint working when the shared secret is misconfigured, which is
 * exactly the situation an operator most needs reported.
 */

/**
 * Per-probe timeout. These are same-network hops returning static JSON, so
 * anything approaching this is itself a health signal. Probes run
 * concurrently, so this bounds the whole aggregate, not each service.
 */
export const HEALTH_CHECK_TIMEOUT_MS = Number(process.env.STATUS_CHECK_TIMEOUT_MS) || 2000;

/**
 * How long an aggregate result is reused. `/v1/status` is authenticated and
 * globally rate-limited, so a stampede is already bounded per client — but the
 * limit is per client, and each request otherwise fans out to four upstreams.
 * A short TTL caps total probe load regardless of how many clients poll, and
 * a few seconds of staleness is immaterial for a status view.
 */
export const STATUS_CACHE_TTL_MS = Number(process.env.STATUS_CACHE_TTL_MS) || 5000;

export type DownstreamState = 'ok' | 'degraded' | 'unreachable';

/** Coarse, closed set. Deliberately not the raw exception text — see below. */
export type DownstreamReason = 'timeout' | 'connection_error' | 'http_error';

export interface DownstreamHealth {
  status: DownstreamState;
  latency_ms: number;
  /** The service's self-reported version, when it returned a parseable body. */
  version?: string;
  /** Why the probe failed. Absent when `status` is `ok`. */
  reason?: DownstreamReason;
}

export interface DownstreamTarget {
  name: string;
  url: string;
  /**
   * Liveness path. Everything uses `/health` except egress-proxy: its
   * `/health` *proxies to the gateway* and returns 503 when the gateway is
   * unreachable, so probing it from the gateway would measure "can
   * egress-proxy reach us" and mark egress-proxy down for a gateway-side
   * fault — a circular signal. `/egress-health` is its real self-liveness.
   */
  healthPath: string;
}

/**
 * The four internal services gateway-api proxies to, resolved from the same
 * env vars the route modules use so a deployment cannot probe one address
 * while proxying to another. Read at call time, not module load, so tests and
 * a reconfigured process see current values.
 */
export function downstreamTargets(): DownstreamTarget[] {
  return [
    {
      name: 'risk-engine',
      url: process.env.RISK_ENGINE_URL || 'http://risk-engine:8001',
      healthPath: '/health',
    },
    {
      name: 'evidence-vault',
      url: process.env.EVIDENCE_VAULT_URL || 'http://evidence-vault:8002',
      healthPath: '/health',
    },
    {
      name: 'doc-generator',
      url: process.env.DOC_GENERATOR_URL || 'http://doc-generator:8003',
      healthPath: '/health',
    },
    {
      name: 'egress-proxy',
      url: process.env.EGRESS_PROXY_URL || 'http://egress-proxy:8004',
      healthPath: '/egress-health',
    },
  ];
}

function elapsedMs(startedAt: number): number {
  return Math.round(performance.now() - startedAt);
}

export async function checkDownstream(target: DownstreamTarget): Promise<DownstreamHealth> {
  const startedAt = performance.now();
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), HEALTH_CHECK_TIMEOUT_MS);

  try {
    const response = await fetch(`${target.url}${target.healthPath}`, {
      method: 'GET',
      signal: controller.signal,
    });

    if (!response.ok) {
      // The service answered, so it is reachable — it just reported itself
      // unhealthy. Operationally that is a different situation from a network
      // failure, and the two must not collapse into one label.
      return { status: 'degraded', latency_ms: elapsedMs(startedAt), reason: 'http_error' };
    }

    let version: string | undefined;
    try {
      const body = (await response.json()) as { version?: unknown };
      if (typeof body?.version === 'string') version = body.version;
    } catch {
      // A 200 with an unparseable body still means the process is up and
      // serving. Report ok without a version rather than inventing a failure.
    }

    return { status: 'ok', latency_ms: elapsedMs(startedAt), version };
  } catch (err) {
    // Reasons are a coarse enum, never the raw error or the target URL.
    // `/v1/status` is authenticated, but internal hostnames and exception
    // detail still do not belong in a routine response body — and this
    // document is the most likely thing to end up feeding a public status
    // page later. (`proxyToService` does echo the internal URL in its 503;
    // that is not a pattern to copy here.)
    const timedOut = err instanceof Error && err.name === 'AbortError';
    return {
      status: 'unreachable',
      latency_ms: elapsedMs(startedAt),
      reason: timedOut ? 'timeout' : 'connection_error',
    };
  } finally {
    clearTimeout(timer);
  }
}

export interface AggregateStatus {
  status: 'ok' | 'degraded';
  services: Record<string, DownstreamHealth>;
}

async function probeAll(): Promise<AggregateStatus> {
  const targets = downstreamTargets();
  // Concurrent, not sequential: four sequential probes against a fully down
  // stack would take 4x the timeout, turning a status check into an
  // eight-second request exactly when the system is already unhealthy.
  const results = await Promise.all(targets.map((t) => checkDownstream(t)));

  const services: Record<string, DownstreamHealth> = {};
  targets.forEach((target, i) => {
    services[target.name] = results[i];
  });

  const allOk = results.every((r) => r.status === 'ok');
  return { status: allOk ? 'ok' : 'degraded', services };
}

let cached: { at: number; value: AggregateStatus } | undefined;
let inFlight: Promise<AggregateStatus> | undefined;

/** Drops the cache. For tests, and for any future explicit-refresh path. */
export function resetStatusCache(): void {
  cached = undefined;
  inFlight = undefined;
}

/**
 * Probe every downstream, reusing a recent result and collapsing concurrent
 * callers onto a single fan-out (single-flight). Without the in-flight share,
 * N simultaneous requests on a cold cache would each launch their own four
 * probes — the stampede the TTL exists to prevent.
 */
export async function aggregateDownstreamHealth(): Promise<AggregateStatus> {
  if (cached && performance.now() - cached.at < STATUS_CACHE_TTL_MS) {
    return cached.value;
  }
  if (inFlight) return inFlight;

  inFlight = probeAll()
    .then((value) => {
      cached = { at: performance.now(), value };
      return value;
    })
    .finally(() => {
      inFlight = undefined;
    });

  return inFlight;
}
