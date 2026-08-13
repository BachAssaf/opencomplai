import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { exportJWK, generateKeyPair, SignJWT, type JWK } from 'jose';
import { buildApp } from '../index';

/**
 * Covers H-04: the OIDC JWT verifier must reject expired/not-yet-valid/wrong-
 * audience/wrong-issuer/bad-signature/malformed/alg-none tokens, not just
 * check the signature. Uses a locally generated RSA keypair and a stubbed
 * global fetch standing in for the JWKS endpoint — no network required.
 */

const ISSUER = 'https://issuer.example.test/';
const AUDIENCE = 'opencomplai-gateway-test';
const JWKS_URI = 'https://issuer.example.test/.well-known/jwks.json';
const KID = 'test-signing-key';

let signingKey: CryptoKey;
let attackerKey: CryptoKey;
let jwks: { keys: JWK[] };

beforeAll(async () => {
  const pair = await generateKeyPair('RS256', { extractable: true });
  signingKey = pair.privateKey;
  const jwk = await exportJWK(pair.publicKey);
  jwk.kid = KID;
  jwk.alg = 'RS256';
  jwk.use = 'sig';
  jwks = { keys: [jwk] };

  const attacker = await generateKeyPair('RS256', { extractable: true });
  attackerKey = attacker.privateKey;
});

function stubJwksEndpoint(): void {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string | URL) => {
      const url = typeof input === 'string' ? input : input.toString();
      if (url === JWKS_URI) {
        return new Response(JSON.stringify(jwks), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        });
      }
      return new Response('not found', { status: 404 });
    }),
  );
}

async function signValidToken(
  overrides: {
    issuer?: string;
    audience?: string;
    expiresInSec?: number;
    notBeforeInSec?: number;
    key?: CryptoKey;
    kid?: string;
    tenantId?: string | null;
  } = {},
): Promise<string> {
  const now = Math.floor(Date.now() / 1000);
  const claims: Record<string, unknown> = { sub: 'user-1' };
  if (overrides.tenantId !== null) {
    claims.tenant_id = overrides.tenantId ?? 'tenant-a';
  }
  const jwt = new SignJWT(claims)
    .setProtectedHeader({ alg: 'RS256', kid: overrides.kid ?? KID })
    .setIssuedAt(now)
    .setIssuer(overrides.issuer ?? ISSUER)
    .setAudience(overrides.audience ?? AUDIENCE)
    .setExpirationTime(now + (overrides.expiresInSec ?? 300));
  if (overrides.notBeforeInSec !== undefined) {
    jwt.setNotBefore(overrides.notBeforeInSec);
  }
  return jwt.sign(overrides.key ?? signingKey);
}

describe('Gateway OIDC JWT verification (H-04)', () => {
  const savedEnv = { ...process.env };

  beforeEach(() => {
    delete process.env.OPENCOMPLAI_API_KEY;
    delete process.env.OPENCOMPLAI_AUTH_DISABLED;
    process.env.OIDC_JWKS_URI = JWKS_URI;
    process.env.OIDC_ISSUER = ISSUER;
    process.env.OIDC_AUDIENCE = AUDIENCE;
    process.env.OIDC_TENANT_CLAIM = 'tenant_id';
    stubJwksEndpoint();
  });

  afterEach(() => {
    process.env = { ...savedEnv };
    vi.unstubAllGlobals();
  });

  it('refuses to start when OIDC_ISSUER/OIDC_AUDIENCE are missing', () => {
    delete process.env.OIDC_ISSUER;
    delete process.env.OIDC_AUDIENCE;
    expect(() => buildApp()).toThrow(/OIDC_ISSUER/);
  });

  it('refuses to start when OIDC_TENANT_CLAIM is missing (TEN-VAULT)', () => {
    delete process.env.OIDC_TENANT_CLAIM;
    expect(() => buildApp()).toThrow(/OIDC_TENANT_CLAIM/);
  });

  it('accepts a validly signed token with correct claims', async () => {
    const app = buildApp();
    await app.ready();
    try {
      const token = await signValidToken();
      const res = await app.inject({
        method: 'GET',
        url: '/v1/status',
        headers: { authorization: `Bearer ${token}` },
      });
      expect(res.statusCode).not.toBe(401);
    } finally {
      await app.close();
    }
  });

  it('rejects an expired token', async () => {
    const app = buildApp();
    await app.ready();
    try {
      const token = await signValidToken({ expiresInSec: -60 });
      const res = await app.inject({
        method: 'GET',
        url: '/v1/status',
        headers: { authorization: `Bearer ${token}` },
      });
      expect(res.statusCode).toBe(401);
      expect(JSON.parse(res.body).error_code).toBe('POLICY_DENIED');
    } finally {
      await app.close();
    }
  });

  it('rejects a not-yet-valid (nbf) token', async () => {
    const app = buildApp();
    await app.ready();
    try {
      const future = Math.floor(Date.now() / 1000) + 3600;
      const token = await signValidToken({ notBeforeInSec: future });
      const res = await app.inject({
        method: 'GET',
        url: '/v1/status',
        headers: { authorization: `Bearer ${token}` },
      });
      expect(res.statusCode).toBe(401);
    } finally {
      await app.close();
    }
  });

  it('rejects a token with the wrong audience', async () => {
    const app = buildApp();
    await app.ready();
    try {
      const token = await signValidToken({ audience: 'someone-else' });
      const res = await app.inject({
        method: 'GET',
        url: '/v1/status',
        headers: { authorization: `Bearer ${token}` },
      });
      expect(res.statusCode).toBe(401);
    } finally {
      await app.close();
    }
  });

  it('rejects a token with the wrong issuer', async () => {
    const app = buildApp();
    await app.ready();
    try {
      const token = await signValidToken({ issuer: 'https://attacker.example.test/' });
      const res = await app.inject({
        method: 'GET',
        url: '/v1/status',
        headers: { authorization: `Bearer ${token}` },
      });
      expect(res.statusCode).toBe(401);
    } finally {
      await app.close();
    }
  });

  it('rejects a token signed with the wrong key (bad signature)', async () => {
    const app = buildApp();
    await app.ready();
    try {
      // Same kid the real key would use, but signed by an attacker-controlled key.
      const token = await signValidToken({ key: attackerKey });
      const res = await app.inject({
        method: 'GET',
        url: '/v1/status',
        headers: { authorization: `Bearer ${token}` },
      });
      expect(res.statusCode).toBe(401);
    } finally {
      await app.close();
    }
  });

  it('rejects a malformed token', async () => {
    const app = buildApp();
    await app.ready();
    try {
      const res = await app.inject({
        method: 'GET',
        url: '/v1/status',
        headers: { authorization: 'Bearer not-a-jwt' },
      });
      expect(res.statusCode).toBe(401);
    } finally {
      await app.close();
    }
  });

  it("rejects a token with alg 'none'", async () => {
    const app = buildApp();
    await app.ready();
    try {
      const header = Buffer.from(JSON.stringify({ alg: 'none', kid: KID })).toString('base64url');
      const now = Math.floor(Date.now() / 1000);
      const payload = Buffer.from(
        JSON.stringify({ iss: ISSUER, aud: AUDIENCE, exp: now + 300, sub: 'user-1' }),
      ).toString('base64url');
      const noneToken = `${header}.${payload}.`;
      const res = await app.inject({
        method: 'GET',
        url: '/v1/status',
        headers: { authorization: `Bearer ${noneToken}` },
      });
      expect(res.statusCode).toBe(401);
    } finally {
      await app.close();
    }
  });

  it('rejects requests with no Bearer token at all', async () => {
    const app = buildApp();
    await app.ready();
    try {
      const res = await app.inject({ method: 'GET', url: '/v1/status' });
      expect(res.statusCode).toBe(401);
    } finally {
      await app.close();
    }
  });

  it('rejects a token missing the configured tenant claim (TEN-VAULT)', async () => {
    const app = buildApp();
    await app.ready();
    try {
      const token = await signValidToken({ tenantId: null });
      const res = await app.inject({
        method: 'GET',
        url: '/v1/status',
        headers: { authorization: `Bearer ${token}` },
      });
      expect(res.statusCode).toBe(401);
    } finally {
      await app.close();
    }
  });
});
