import { flushSync } from "react-dom";

/* View Transitions（document.startViewTransition）共享封装。
   只用于低频的用户主动导航（如列表→详情、打开抽屉）；高频轮询区域
   （活跃 run 进度、workspace 流式输出）不经过这里，天然豁免。
   不支持的浏览器（含 jsdom）与 prefers-reduced-motion 一律静默回退
   为普通的同步更新。 */

export function viewTransitionsSupported(): boolean {
  return (
    typeof document !== "undefined"
    && "startViewTransition" in document
    && !window.matchMedia("(prefers-reduced-motion: reduce)").matches
  );
}

/** 在 View Transition 中执行一次 React 状态更新；不支持时直接同步执行。 */
export function runWithViewTransition(update: () => void): void {
  if (!viewTransitionsSupported()) {
    update();
    return;
  }
  try {
    document.startViewTransition(() => {
      flushSync(update);
    });
  } catch {
    update();
  }
}

/**
 * 共享元素过渡：先同步渲染 prepare（给旧快照中的元素挂上
 * view-transition-name），再在过渡里执行 update（导航/开抽屉），
 * 动画结束后 cleanup 摘掉名字，避免常驻名字干扰后续过渡。
 */
export function runSharedElementTransition(prepare: () => void, update: () => void, cleanup: () => void): void {
  if (!viewTransitionsSupported()) {
    update();
    return;
  }
  try {
    flushSync(prepare);
    const transition = document.startViewTransition(() => {
      flushSync(update);
    });
    void transition.finished.catch(() => undefined).then(cleanup);
  } catch {
    update();
    cleanup();
  }
}
