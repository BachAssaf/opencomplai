/*
 * Deployment configuration for the EU AI Act checker widget.
 *
 * Loaded by mkdocs (see `extra_javascript` in mkdocs.yml) *before*
 * checker-widget.js, which reads the global set here.
 *
 * This is a separate same-origin file rather than an inline <script> in the
 * theme override because the docs site sends
 * `Content-Security-Policy: script-src 'self'` (vercel.json) — an inline
 * script would be blocked.
 *
 * ---------------------------------------------------------------------------
 * OCOC_RISK_ENGINE_URL — origin of a reachable risk-engine, used only by the
 * checker's "email a copy" feature. Everything else in the widget runs
 * entirely in the browser and needs no backend.
 *
 * Left EMPTY deliberately. There is no public risk-engine deployment yet, and
 * pointing this at a host that does not exist would restore exactly the bug it
 * was added to fix. While it is empty the widget hides the email form instead
 * of offering a button that always fails; the JSON / Markdown / print exports
 * are unaffected.
 *
 * To enable email delivery, a deployment must do BOTH of the following —
 * setting only the first leaves the feature broken in a new way:
 *
 *   1. Set the value below to the risk-engine origin, e.g.
 *        window.OCOC_RISK_ENGINE_URL = "https://api.example.com";
 *
 *   2. Add that origin to `connect-src` in the Content-Security-Policy header
 *      in `vercel.json`. The policy has no `connect-src` directive, so it
 *      inherits `default-src 'self'` and the browser blocks any cross-origin
 *      fetch — regardless of what is configured here.
 *
 * The risk-engine also needs its SMTP variables set, or the endpoint returns
 * 503 MAILER_NOT_CONFIGURED. See docs/src/deployment/configuration.md.
 * ---------------------------------------------------------------------------
 */
window.OCOC_RISK_ENGINE_URL = "";
