/**
 * Minimal structural types for the Vercel serverless request/response, in
 * place of `import type { ... } from "@vercel/node"` — that package is not
 * a dependency anywhere in this repo, so the handler could never actually
 * be type-checked against it (tsc fails with TS2307), and adding it just
 * for two type names would grow the audit surface for a runtime we don't
 * pin. These declare only the members `[...path].ts` actually uses; the
 * shapes mirror Node's http.IncomingMessage (async-iterable body, url,
 * method, headers) and Vercel's chainable response helpers.
 */

export interface VercelLikeRequest extends AsyncIterable<Buffer | string> {
  url?: string;
  method?: string;
  headers: Record<string, string | string[] | undefined>;
}

export interface VercelLikeResponse {
  status(code: number): VercelLikeResponse;
  setHeader(name: string, value: string): void;
  send(body: unknown): void;
}
