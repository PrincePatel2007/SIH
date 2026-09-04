/**
 * api.ts — thin HTTP/WS helper layer.
 *
 * All paths are relative so they go through the Vite proxy in dev
 * and hit the same origin in production.
 */

const API_BASE = "/api";

export interface HealthResponse {
  status: "ok" | string;
}

/**
 * Pings the backend health endpoint.
 * Resolves to the parsed JSON body on success.
 * Rejects (throws) on network error or non-2xx response.
 */
export async function checkHealth(): Promise<HealthResponse> {
  const res = await fetch(`${API_BASE}/health`);
  if (!res.ok) {
    throw new Error(`Health check failed: HTTP ${res.status}`);
  }
  return (await res.json()) as HealthResponse;
}
