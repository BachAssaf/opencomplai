# Authentication Configuration

**Compliance mapping:** ISO 27001 A.8.5 · SOC 2 CC6.1 · NIST PR.AA · FedRAMP IA-2

---

## Two Authentication Modes

| Mode | When to use | Env var |
|---|---|---|
| API-key | Self-hosted, single-operator | `OPENCOMPLAI_API_KEY` |
| OIDC JWT | Multi-user / SaaS | `OIDC_JWKS_URI` |

`OIDC_JWKS_URI` takes priority. When set, `OPENCOMPLAI_API_KEY` is ignored.

---

## API-Key Mode (Self-Hosted)

=== "macOS / Linux"
    ```bash
    OPENCOMPLAI_API_KEY=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
    ```

=== "Windows (PowerShell)"
    ```powershell
    $env:OPENCOMPLAI_API_KEY = python -c "import secrets; print(secrets.token_urlsafe(32))"
    ```

All non-health requests must carry `x-api-key: <key>`.

---

## OIDC JWT Mode (Multi-User / SaaS)

```bash
# Auth0
OIDC_JWKS_URI=https://your-tenant.auth0.com/.well-known/jwks.json
OIDC_ISSUER=https://your-tenant.auth0.com/
OIDC_AUDIENCE=https://your-api-identifier

# Entra ID
OIDC_JWKS_URI=https://login.microsoftonline.com/<tenant>/discovery/v2.0/keys
OIDC_ISSUER=https://login.microsoftonline.com/<tenant>/v2.0
OIDC_AUDIENCE=<application-id-uri-or-client-id>
```

`OIDC_ISSUER` and `OIDC_AUDIENCE` are both required whenever `OIDC_JWKS_URI` is set — the gateway
refuses to start otherwise. All requests must carry `Authorization: Bearer <jwt>`. The gateway
(`services/gateway-api/src/middleware/auth.ts`, via `jose`) verifies the JWT against the JWKS
endpoint and enforces, on every request: signature (RS256 only — other algorithms, including
`none`, are rejected), `exp`, `nbf` (with a small clock-skew tolerance, `OIDC_CLOCK_TOLERANCE_SEC`,
default 5s), `iss`, and `aud`.

Both auth modes sit behind a global rate limit (`OPENCOMPLAI_RATE_LIMIT_MAX` /
`OPENCOMPLAI_RATE_LIMIT_WINDOW_MS`, default 300 requests / 60s per client) plus a stricter budget
counted only against auth failures (`OPENCOMPLAI_AUTH_FAILURE_RATE_LIMIT_MAX` /
`OPENCOMPLAI_AUTH_FAILURE_RATE_LIMIT_WINDOW_MS`, default 10 / 60s), to slow down credential
stuffing and token-guessing.

---

## MFA Enforcement

Enforce MFA for admin-role users at the IdP level, not in application code. Configure an MFA policy in your IdP (Auth0 Actions, Entra Conditional Access, Cognito MFA) before connecting to production.

---

## Local Development Only

```bash
OPENCOMPLAI_AUTH_DISABLED=1   # NEVER in production
```
