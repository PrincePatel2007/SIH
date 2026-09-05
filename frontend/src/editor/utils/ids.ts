/**
 * ids.ts — lightweight deterministic ID generator.
 *
 * Uses crypto.randomUUID() when available (all modern browsers + Node 18+),
 * falls back to Math.random for test environments that stub crypto.
 */

let _counter = 0;

export function newId(prefix: string): string {
  if (typeof crypto !== "undefined" && crypto.randomUUID) {
    return `${prefix}-${crypto.randomUUID()}`;
  }
  // Fallback (tests / old environments)
  return `${prefix}-${Date.now()}-${(_counter++).toString(36)}`;
}
