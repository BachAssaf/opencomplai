import { createHmac } from 'node:crypto';

/**
 * Mints internal service tokens (SEC-SERVICE-AUTH) for gateway-api's outbound
 * calls to the four unauthenticated-until-now Python services. Mirrors
 * opencomplai_core.service_auth's mint_service_token byte-for-byte — same
 * token format, same HMAC-SHA256 construction, same base64url encoding —
 * so a token minted here verifies correctly against the Python side, and
 * vice versa, under the shared INTERNAL_SERVICE_TOKEN_SECRET.
 *
 * Token format: `<payload_b64>.<signature_b64>` where payload_b64 is the
 * base64url encoding of `{"iss": <caller>, "exp": <unix-ts>}` with sorted
 * keys (exp before iss), and signature_b64 is the base64url HMAC-SHA256 of
 * payload_b64 under the shared secret.
 */

const DEFAULT_TOKEN_TTL_SECONDS = 300;

function base64urlEncode(buf: Buffer): string {
  return buf.toString('base64url');
}

function sign(payloadB64: string, secret: string): string {
  const digest = createHmac('sha256', secret).update(payloadB64, 'ascii').digest();
  return base64urlEncode(digest);
}

export function mintServiceToken(
  issuer: string,
  secret: string,
  ttlSeconds: number = DEFAULT_TOKEN_TTL_SECONDS,
): string {
  const exp = Math.floor(Date.now() / 1000) + ttlSeconds;
  // json.dumps(..., sort_keys=True) on the Python side sorts keys
  // alphabetically — "exp" before "iss" — so this must match exactly.
  const payloadJson = JSON.stringify({ exp, iss: issuer });
  const payloadB64 = base64urlEncode(Buffer.from(payloadJson, 'utf-8'));
  const signatureB64 = sign(payloadB64, secret);
  return `${payloadB64}.${signatureB64}`;
}

let cachedSecret: string | undefined | null = null;

/** Reads INTERNAL_SERVICE_TOKEN_SECRET once; undefined if unset. */
export function loadSharedSecret(): string | undefined {
  if (cachedSecret === null) {
    const value = process.env.INTERNAL_SERVICE_TOKEN_SECRET?.trim();
    cachedSecret = value && value.length > 0 ? value : undefined;
  }
  return cachedSecret;
}

/** Test-only: clears the cached secret so tests can vary the env var. */
export function _resetSharedSecretCacheForTests(): void {
  cachedSecret = null;
}
