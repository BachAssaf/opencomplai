/**
 * Strip this function's own Vercel mount prefix from an incoming request URL
 * before it is injected into Fastify's inject(), so a route registered as
 * bare "/v1/x" (see services/gateway-api/src/routes/index.ts) matches a
 * request that actually arrived as "/api/gateway/v1/x".
 *
 * Vercel's filesystem routing serves this function at /api/gateway/* and
 * forwards the literal incoming request URL unmodified — there is no
 * vercel.json (or any other rewrite config) that trims the mount prefix on
 * the platform side (finding 48.12).
 *
 * Leaves an already-bare URL (e.g. "/v1/x", "/health") untouched, so this is
 * harmless if a future rewrite config ever forwards bare paths instead.
 */
export function rewriteUrl(rawUrl: string, mountPrefix: string): string {
  const queryIndex = rawUrl.indexOf("?");
  const pathname = queryIndex === -1 ? rawUrl : rawUrl.slice(0, queryIndex);
  const query = queryIndex === -1 ? "" : rawUrl.slice(queryIndex);

  if (pathname === mountPrefix) {
    return `/${query}`;
  }
  if (pathname.startsWith(`${mountPrefix}/`)) {
    return `${pathname.slice(mountPrefix.length)}${query}`;
  }
  return rawUrl;
}
