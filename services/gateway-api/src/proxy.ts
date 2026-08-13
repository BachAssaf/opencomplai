import { FastifyReply } from 'fastify';
import { loadSharedSecret, mintServiceToken } from './serviceAuth';

/**
 * The four internal Python services (risk-engine, evidence-vault,
 * doc-generator, egress-proxy) require a signed service token on every
 * non-health route (SEC-SERVICE-AUTH). gateway-api is their sole authenticated
 * caller, so it mints one per outbound call here — the single choke point
 * every route module proxies through.
 */
function serviceAuthHeaders(): Record<string, string> {
  const secret = loadSharedSecret();
  if (!secret) return {};
  const token = mintServiceToken('gateway-api', secret);
  return { Authorization: `Bearer ${token}` };
}

export async function proxyToService(
  serviceBaseUrl: string,
  path: string,
  method: string,
  body: unknown,
  reply: FastifyReply,
): Promise<void> {
  const url = `${serviceBaseUrl}${path}`;
  // Forward the gateway-verified caller's tenant (TEN-VAULT) so evidence-vault's
  // RLS fence has something to scope to. Absent only when
  // OPENCOMPLAI_AUTH_DISABLED=1 (local dev), in which case evidence-vault falls
  // back to its own OSS_DEFAULT_TENANT_ID.
  const tenantId = reply.request?.tenantId;
  const tenantHeaders: Record<string, string> = tenantId ? { 'X-Tenant-Id': tenantId } : {};
  try {
    const response = await fetch(url, {
      method,
      headers: {
        'Content-Type': 'application/json',
        ...serviceAuthHeaders(),
        ...tenantHeaders,
      },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });

    const raw = await response.text();
    let data: unknown = undefined;
    if (raw.length > 0) {
      try {
        data = JSON.parse(raw);
      } catch {
        data = { raw };
      }
    }

    reply.status(response.status).send(data);
  } catch {
    reply.status(503).send({
      error_code: 'DEPENDENCY_UNAVAILABLE',
      message: `Upstream service unavailable: ${url}`,
      category: 'dependency',
      retryable: true,
      correlation_id: reply.request.id,
    });
  }
}
