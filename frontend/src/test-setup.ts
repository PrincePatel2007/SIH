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
