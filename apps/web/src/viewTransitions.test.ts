import { afterEach, describe, expect, it, vi } from "vitest";

import { runSharedElementTransition, runWithViewTransition, viewTransitionsSupported } from "./viewTransitions";

type StartViewTransition = (update: () => void) => { finished: Promise<void> };

function stubStartViewTransition(impl: StartViewTransition) {
  Object.defineProperty(document, "startViewTransition", { configurable: true, value: impl });
}

describe("viewTransitions", () => {
  afterEach(() => {
    // @ts-expect-error removing the stub restores the unsupported default
    delete document.startViewTransition;
    vi.unstubAllGlobals();
  });

  it("falls back to a plain synchronous update when unsupported (jsdom / legacy browsers)", () => {
    expect(viewTransitionsSupported()).toBe(false);
    const calls: string[] = [];
    runSharedElementTransition(
      () => calls.push("prepare"),
      () => calls.push("update"),
      () => calls.push("cleanup")
    );
    // prepare/cleanup 只服务于过渡命名，回退时不得执行
    expect(calls).toEqual(["update"]);
    runWithViewTransition(() => calls.push("plain"));
    expect(calls).toEqual(["update", "plain"]);
  });

  it("runs prepare → update → cleanup around the transition when supported", async () => {
    const calls: string[] = [];
    stubStartViewTransition((update) => {
      update();
      calls.push("transition");
      return { finished: Promise.resolve() };
    });
    vi.stubGlobal("matchMedia", vi.fn().mockReturnValue({ matches: false }));

    runSharedElementTransition(
      () => calls.push("prepare"),
      () => calls.push("update"),
      () => calls.push("cleanup")
    );
    expect(calls).toEqual(["prepare", "update", "transition"]);
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(calls).toEqual(["prepare", "update", "transition", "cleanup"]);
  });

  it("stays silent when the user prefers reduced motion", () => {
    const start = vi.fn((update: () => void) => {
      update();
      return { finished: Promise.resolve() };
    });
    stubStartViewTransition(start);
    vi.stubGlobal("matchMedia", vi.fn().mockReturnValue({ matches: true }));

    const update = vi.fn();
    runWithViewTransition(update);
    expect(viewTransitionsSupported()).toBe(false);
    expect(start).not.toHaveBeenCalled();
    expect(update).toHaveBeenCalledTimes(1);
  });
});
