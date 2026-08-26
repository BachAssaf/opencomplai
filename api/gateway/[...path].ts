/**
 * Vercel serverless entry point for the gateway-api service.
 *
 * Fastify doesn't expose a raw http.Handler, so we wrap it with a small
 * adapter: inject the incoming VercelRequest into Fastify's inject() method
 * and pipe the response back. This keeps all routing/auth logic in the
 * existing buildApp() function with zero changes.
 *
 * Vercel serves this function at /api/gateway/* and forwards the literal
 * request URL unmodified (no vercel.json rewrite strips the mount prefix),
 * but the Fastify app registers only bare routes ("/health", "/v1/*" — see
 * services/gateway-api/src/routes/index.ts). rewriteUrl() strips the
 * /api/gateway prefix before injection so those routes match (finding
 * 48.12).
 */
import type { InjectOptions } from "../../services/gateway-api/src/index";
import { buildApp } from "../../services/gateway-api/src/index";
import { rewriteUrl } from "./_lib/rewriteUrl";
import type { VercelLikeRequest, VercelLikeResponse } from "./_lib/types";

const GATEWAY_MOUNT_PREFIX = "/api/gateway";

// Build once per cold start (Fastify instance is reused across warm invocations).
const app = buildApp();
// Fastify must be ready before inject() can be called.
const ready = app.ready();

export default async function handler(
  req: VercelLikeRequest,
  res: VercelLikeResponse,
): Promise<void> {
  await ready;

  const url = rewriteUrl(req.url ?? "/", GATEWAY_MOUNT_PREFIX);
  const method = req.method ?? "GET";

  // Collect raw body bytes if present.
  const bodyChunks: Buffer[] = [];
  for await (const chunk of req) {
    bodyChunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
  }
  const payload = bodyChunks.length > 0 ? Buffer.concat(bodyChunks) : undefined;

  const response = await app.inject({
    method: method as InjectOptions["method"],
    url,
    headers: req.headers as Record<string, string>,
    payload,
  });

  res.status(response.statusCode);
  for (const [key, value] of Object.entries(response.headers)) {
    if (value !== undefined) res.setHeader(key, value as string);
  }
  res.send(response.rawPayload);
}
