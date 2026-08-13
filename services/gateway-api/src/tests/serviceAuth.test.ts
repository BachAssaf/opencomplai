import { afterEach, describe, expect, it } from 'vitest';
import {
  _resetSharedSecretCacheForTests,
  loadSharedSecret,
  mintServiceToken,
} from '../serviceAuth';

describe('mintServiceToken', () => {
  it('produces a token verifiable by opencomplai_core.service_auth (cross-language format)', () => {
    // This test only exercises the TS side's own shape; cross-language
    // verification is covered by packages/core/tests/test_service_auth.py
    // and by the fact that both sides sort JSON keys identically.
    const token = mintServiceToken('gateway-api', 'shared-secret');
    const [payloadB64, signatureB64] = token.split('.');
    expect(payloadB64).toBeTruthy();
    expect(signatureB64).toBeTruthy();

    const payload = JSON.parse(Buffer.from(payloadB64, 'base64url').toString('utf-8'));
    expect(payload.iss).toBe('gateway-api');
    expect(typeof payload.exp).toBe('number');
  });

  it('produces different tokens for different issuers', () => {
    const a = mintServiceToken('gateway-api', 'shared-secret');
    const b = mintServiceToken('risk-engine', 'shared-secret');
    expect(a).not.toBe(b);
  });

  it('sets expiry close to the requested TTL', () => {
    const before = Math.floor(Date.now() / 1000);
    const token = mintServiceToken('gateway-api', 'shared-secret', 60);
    const [payloadB64] = token.split('.');
    const payload = JSON.parse(Buffer.from(payloadB64, 'base64url').toString('utf-8'));
    expect(payload.exp).toBeGreaterThanOrEqual(before + 55);
    expect(payload.exp).toBeLessThanOrEqual(before + 65);
  });
});

describe('loadSharedSecret', () => {
  const original = process.env.INTERNAL_SERVICE_TOKEN_SECRET;

  afterEach(() => {
    if (original === undefined) {
      delete process.env.INTERNAL_SERVICE_TOKEN_SECRET;
    } else {
      process.env.INTERNAL_SERVICE_TOKEN_SECRET = original;
    }
    _resetSharedSecretCacheForTests();
  });

  it('returns undefined when unset', () => {
    delete process.env.INTERNAL_SERVICE_TOKEN_SECRET;
    _resetSharedSecretCacheForTests();
    expect(loadSharedSecret()).toBeUndefined();
  });

  it('returns the configured secret', () => {
    process.env.INTERNAL_SERVICE_TOKEN_SECRET = 'my-secret';
    _resetSharedSecretCacheForTests();
    expect(loadSharedSecret()).toBe('my-secret');
  });
});
