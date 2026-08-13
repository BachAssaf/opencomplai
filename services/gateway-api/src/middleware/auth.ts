import { FastifyInstance, FastifyReply, FastifyRequest } from 'fastify';
import { timingSafeEqual } from 'node:crypto';
import { createRemoteJWKSet, jwtVerify } from 'jose';
import rateLimit from '@fastify/rate-limit';

declare module 'fastify' {
  interface FastifyRequest {
    /**
     * The authenticated caller's identity, resolved once by authMiddleware:
     * the verified JWT `sub` claim in OIDC mode, or a fixed synthetic value
     * in API-key mode (a shared key authenticates the deployment, not an
     * individual actor). Undefined only when OPENCOMPLAI_AUTH_DISABLED=1.
     *
     * HITL routes (hitl.ts) bind this — not any client-supplied actor_id/
     * reviewer_id field — into the immutable ledger's signer_id, so a
     * network-adjacent caller can no longer forge who made a compliance
     * decision (SEC-SERVICE-AUTH).
     */
    principalId?: string;

    /**
     * The authenticated caller's tenant, resolved once by authMiddleware
     * (TEN-VAULT): the JWT's OIDC_TENANT_CLAIM claim in OIDC mode — same
     * configurable-claim-name convention as dashboard_auth.Principal, since
     * different IdPs name their org/tenant claim differently — or the fixed
     * OPENCOMPLAI_API_KEY_TENANT_ID value in API-key mode (a shared key
     * authenticates one deployment, which maps to one tenant). proxy.ts
     * forwards this as X-Tenant-Id on every outbound call to the internal
     * Python services so evidence-vault's RLS fence has something to scope
     * to. Undefined only when OPENCOMPLAI_AUTH_DISABLED=1, in which case
     * evidence-vault falls back to its own OSS_DEFAULT_TENANT_ID.
     */
    tenantId?: string;
  }
}

/**
 * Auth middleware (ISO 27001 A.8.5 / SOC 2 CC6.1 / NIST PR.AA / FedRAMP IA-2).
 *
 * Two modes — see docs/deployment/authentication.md for when to use each:
 *
 * 1. OIDC JWT mode (multi-user / SaaS): set OIDC_JWKS_URI, OIDC_ISSUER, OIDC_AUDIENCE.
 *    Every non-health request must carry a valid Bearer JWT: signature verified against
 *    the JWKS (RS256 only), with exp/nbf/iss/aud all enforced.
 *
 * 2. API-key mode (self-hosted / single-operator): set OPENCOMPLAI_API_KEY.
 *    Every non-health request must carry x-api-key header.
 *    Fallback when OIDC_JWKS_URI is not set.
 *
 * Both modes sit behind a global rate limit plus a stricter budget that tracks
 * auth failures specifically, to slow down credential stuffing / token guessing.
 */

/** Pinned explicitly so 'none' and HS/RS key-confusion attacks are rejected outright. */
const OIDC_ALGORITHMS = ['RS256'];
const OIDC_CLOCK_TOLERANCE_SEC = Number(process.env.OIDC_CLOCK_TOLERANCE_SEC) || 5;

const GLOBAL_RATE_LIMIT_MAX = Number(process.env.OPENCOMPLAI_RATE_LIMIT_MAX) || 300;
const GLOBAL_RATE_LIMIT_WINDOW_MS = Number(process.env.OPENCOMPLAI_RATE_LIMIT_WINDOW_MS) || 60_000;
const AUTH_FAILURE_RATE_LIMIT_MAX =
  Number(process.env.OPENCOMPLAI_AUTH_FAILURE_RATE_LIMIT_MAX) || 10;
const AUTH_FAILURE_RATE_LIMIT_WINDOW_MS =
  Number(process.env.OPENCOMPLAI_AUTH_FAILURE_RATE_LIMIT_WINDOW_MS) || 60_000;

function isHealthCheck(request: FastifyRequest): boolean {
  return request.url === '/health' || request.url.startsWith('/health?');
}

function sendPolicyDenied(reply: FastifyReply, request: FastifyRequest, message: string): void {
  reply.status(401).send({
    error_code: 'POLICY_DENIED',
    message,
    category: 'policy',
    retryable: false,
    correlation_id: request.id,
  });
}

export function authMiddleware(app: FastifyInstance): void {
  // Registered first so the plugin's `onRoute` hook is wired before any route
  // below it is declared, per @fastify/rate-limit's own registration order
  // requirement — this is what makes `global: true` actually cover every route.
  void app.register(rateLimit, {
    global: true,
    max: GLOBAL_RATE_LIMIT_MAX,
    timeWindow: GLOBAL_RATE_LIMIT_WINDOW_MS,
  });

  // `createRateLimit` is a decorator the plugin above adds once it finishes
  // registering during app.ready() — well before any request (and therefore
  // any hook below) can run — so binding it lazily on first use is safe.
  let authFailureLimiter: ReturnType<FastifyInstance['createRateLimit']> | undefined;
  const withinAuthFailureBudget = async (request: FastifyRequest): Promise<boolean> => {
    authFailureLimiter ??= app.createRateLimit({
      max: AUTH_FAILURE_RATE_LIMIT_MAX,
      timeWindow: AUTH_FAILURE_RATE_LIMIT_WINDOW_MS,
    });
    const result = await authFailureLimiter(request);
    return !(result.isAllowed === false && result.isExceeded);
  };

  const denyAuthFailure = async (
    request: FastifyRequest,
    reply: FastifyReply,
    message: string,
  ): Promise<void> => {
    if (!(await withinAuthFailureBudget(request))) {
      reply.status(429).send({
        error_code: 'RATE_LIMITED',
        message: 'Too many authentication failures. Try again later.',
        category: 'policy',
        retryable: true,
        correlation_id: request.id,
      });
      return;
    }
    sendPolicyDenied(reply, request, message);
  };

  const apiKey = process.env.OPENCOMPLAI_API_KEY;
  const authDisabled = process.env.OPENCOMPLAI_AUTH_DISABLED === '1';
  const oidcJwksUri = process.env.OIDC_JWKS_URI;

  if (oidcJwksUri) {
    const oidcIssuer = process.env.OIDC_ISSUER;
    const oidcAudience = process.env.OIDC_AUDIENCE;
    const oidcTenantClaim = process.env.OIDC_TENANT_CLAIM;
    if (!oidcIssuer || !oidcAudience || !oidcTenantClaim) {
      const msg =
        'Gateway refusing to start: OIDC_JWKS_URI is set but OIDC_ISSUER, ' +
        'OIDC_AUDIENCE, and/or OIDC_TENANT_CLAIM are not. All three are required ' +
        'to validate token claims and resolve the caller tenant.';
      app.log.error(msg);
      throw new Error(msg);
    }

    const jwks = createRemoteJWKSet(new URL(oidcJwksUri));
    app.log.info(`Gateway using OIDC JWT auth (JWKS: ${oidcJwksUri})`);

    app.addHook(
      'onRequest',
      async (request: FastifyRequest, reply: FastifyReply): Promise<void> => {
        if (isHealthCheck(request)) return;
        const authHeader = request.headers['authorization'];
        if (!authHeader || !authHeader.startsWith('Bearer ')) {
          await denyAuthFailure(request, reply, 'Missing Bearer token.');
          return;
        }
        try {
          const { payload } = await jwtVerify(authHeader.slice(7), jwks, {
            issuer: oidcIssuer,
            audience: oidcAudience,
            algorithms: OIDC_ALGORITHMS,
            clockTolerance: OIDC_CLOCK_TOLERANCE_SEC,
          });
          if (typeof payload.sub === 'string' && payload.sub.length > 0) {
            request.principalId = payload.sub;
          }
          const tenantClaimValue = payload[oidcTenantClaim];
          if (typeof tenantClaimValue === 'string' && tenantClaimValue.length > 0) {
            request.tenantId = tenantClaimValue;
          } else {
            await denyAuthFailure(request, reply, 'Token missing tenant claim.');
            return;
          }
        } catch (err) {
          app.log.warn(`JWT verification failed: ${err}`);
          await denyAuthFailure(request, reply, 'Invalid or expired JWT.');
        }
      },
    );
    return;
  }

  if (!apiKey) {
    if (!authDisabled) {
      const msg =
        'Gateway refusing to start: OPENCOMPLAI_API_KEY is not set. ' +
        'Set it to a strong shared secret, or explicitly opt out with ' +
        'OPENCOMPLAI_AUTH_DISABLED=1 for local development.';
      app.log.error(msg);
      throw new Error(msg);
    }
    app.log.warn(
      'Gateway running WITHOUT authentication (OPENCOMPLAI_AUTH_DISABLED=1). Do not use in production.',
    );
    return;
  }

  // A shared API key authenticates one deployment as a whole, which maps to
  // one tenant — self-hosted/single-operator mode has no concept of multiple
  // tenants sharing a gateway. Defaults to evidence-vault's own OSS sentinel
  // so an unconfigured deployment still lands in a well-known namespace
  // rather than an arbitrary one.
  const apiKeyTenantId = process.env.OPENCOMPLAI_API_KEY_TENANT_ID || 'oss-default';

  app.addHook('onRequest', async (request: FastifyRequest, reply: FastifyReply): Promise<void> => {
    if (isHealthCheck(request)) return;
    const provided = request.headers['x-api-key'];
    const providedKey = Array.isArray(provided) ? provided[0] : provided;
    if (!constantTimeEquals(providedKey, apiKey)) {
      await denyAuthFailure(request, reply, 'Invalid or missing API key.');
      return;
    }
    // A shared API key authenticates the deployment as a whole, not an
    // individual actor — there is no per-caller identity to extract in this
    // mode, unlike OIDC's JWT `sub`. Fixed synthetic value so HITL routes
    // still bind to something server-derived rather than falling back to
    // client-supplied actor_id/reviewer_id.
    request.principalId = 'api-key-caller';
    request.tenantId = apiKeyTenantId;
  });
}

/** Constant-time comparison; guards the length mismatch `timingSafeEqual` throws on. */
function constantTimeEquals(provided: string | undefined, expected: string): boolean {
  if (provided === undefined) return false;
  const providedBuf = Buffer.from(provided);
  const expectedBuf = Buffer.from(expected);
  if (providedBuf.length !== expectedBuf.length) return false;
  return timingSafeEqual(providedBuf, expectedBuf);
}
