// test-setup.ts — global test setup for vitest + @testing-library/jest-dom
import "@testing-library/jest-dom";

// JSDOM does not implement ResizeObserver — stub it for any component that uses it.
if (!globalThis.ResizeObserver) {
  globalThis.ResizeObserver = class ResizeObserver {
    observe()    {}
    unobserve()  {}
    disconnect() {}
  };
}

// Stub fetch so HistoryView (and any other components using fetch) don't throw in JSDOM.
// Individual tests can override this with vi.stubGlobal('fetch', ...) as needed.
if (!globalThis.fetch) {
  // @ts-expect-error – minimal stub, not full Response API
  globalThis.fetch = () => Promise.resolve({ ok: false, json: () => Promise.resolve({}) });
}
