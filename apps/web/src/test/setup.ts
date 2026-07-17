import "@testing-library/jest-dom/vitest";

Object.defineProperty(HTMLCanvasElement.prototype, "getContext", {
  value: () => ({
    measureText: (text: string) => ({ width: text.length * 6 })
  })
});
