import { useLayoutEffect, useRef, type RefObject } from "react";

// Lists that change under the reader (a board every poll) move instead of jumping: after each
// change, an item that stayed glides from where it was to where it is (FLIP), and a new one
// fades in. Items are the container's `[data-flip]` elements, keyed by its value. Nothing moves
// on the first render or when the reader asks for less motion.

const MOVE_MS = 260;
const ENTER_MS = 220;
const EASE = "cubic-bezier(0.2, 0.8, 0.2, 1)";

function reducedMotion(): boolean {
  return window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
}

/** Animate the `[data-flip]` items of `container` whenever `version` changes. */
export function useFlip(container: RefObject<HTMLElement | null>, version: string): void {
  const tops = useRef<Map<string, number> | null>(null);
  useLayoutEffect(() => {
    const el = container.current;
    if (!el) return;
    const before = tops.current;
    const after = new Map<string, number>();
    const still = !before || reducedMotion();
    for (const item of el.querySelectorAll<HTMLElement>("[data-flip]")) {
      const key = item.dataset.flip!;
      const top = item.offsetTop;
      after.set(key, top);
      if (still || typeof item.animate !== "function") continue;
      const was = before.get(key);
      if (was === undefined) {
        item.animate([{ opacity: 0 }, { opacity: 1 }], { duration: ENTER_MS, easing: "ease-out" });
      } else if (was !== top) {
        item.animate([{ transform: `translateY(${was - top}px)` }, { transform: "none" }], {
          duration: MOVE_MS,
          easing: EASE,
        });
      }
    }
    tops.current = after;
  }, [container, version]);
}
