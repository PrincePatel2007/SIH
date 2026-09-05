/**
 * api.ts — HTTP helper layer.
 *
 * All paths are relative so they go through the Vite proxy in dev
 * and hit the same origin in production.
 */

const API_BASE = "/api";

export interface HealthResponse {
  status: "ok" | string;
}

export async function checkHealth(): Promise<HealthResponse> {
  const res = await fetch(`${API_BASE}/health`);
  if (!res.ok) throw new Error(`Health check failed: HTTP ${res.status}`);
  return (await res.json()) as HealthResponse;
}

// ---------------------------------------------------------------------------
// Layout save / load
// ---------------------------------------------------------------------------

export type SaveResult = { ok: true; name: string } | { ok: false; errors: string[] };

/** POST /api/simulations/{name} — validate + persist the layout. */
export async function saveLayout(name: string, payload: object): Promise<SaveResult> {
  const res = await fetch(`${API_BASE}/simulations/${encodeURIComponent(name)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (res.ok) {
    return (await res.json()) as { ok: true; name: string };
  }
  // 400 or 422 — extract errors from detail
  let errors: string[] = [`HTTP ${res.status}`];
  try {
    const body = await res.json();
    if (body?.detail?.errors && Array.isArray(body.detail.errors)) {
      errors = body.detail.errors as string[];
    } else if (typeof body?.detail === "string") {
      errors = [body.detail];
    }
  } catch {
    /* ignore parse failure */
  }
  return { ok: false, errors };
}

/** GET /api/simulations/{name} — load a saved layout. */
export async function loadLayout(name: string): Promise<object | null> {
  const res = await fetch(`${API_BASE}/simulations/${encodeURIComponent(name)}`);
  if (res.status === 404) return null;
  if (!res.ok) throw new Error(`Load failed: HTTP ${res.status}`);
  return res.json() as Promise<object>;
}
