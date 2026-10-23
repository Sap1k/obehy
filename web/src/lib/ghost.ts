import { useLayoutEffect, useRef, type RefObject } from "react";
import { reducedMotion } from "./motion";

// Exits for elements React removes at once (a panel whose route closed, the phone's sheet): a
// static copy takes the element's place over the page and animates out, then goes. It takes no
// clicks, so nothing waits for it, unlike a view transition, which holds the page while it runs.

/** Animate a copy of `el` (still in the page) out with `keyframes`, where it stands. */
export function ghostOut(el: HTMLElement | null, keyframes: Keyframe[], duration: number): void {
  if (!el || !el.isConnected || reducedMotion()) return;
  const rect = el.getBoundingClientRect();
  if (rect.width === 0 || rect.height === 0) return;
  const copy = el.cloneNode(true) as HTMLElement;
  if (typeof copy.animate !== "function") return;
  Object.assign(copy.style, {
    position: "fixed",
    left: `${rect.left}px`,
    top: `${rect.top}px`,
    width: `${rect.width}px`,
    height: `${rect.height}px`,
    maxHeight: "none",
    margin: "0",
    transform: "none",
    pointerEvents: "none",
    zIndex: "50",
  });
  // Its own entrances (CSS animations) must not play again on the copy.
  copy.classList.add("ob-ghost");
  copy.setAttribute("aria-hidden", "true");
  copy.removeAttribute("id");
  document.body.append(copy);
  copy.scrollTop = el.scrollTop;
  const run = copy.animate(keyframes, { duration, easing: "ease-in", fill: "forwards" });
  run.onfinish = () => copy.remove();
  run.oncancel = () => copy.remove();
}

/** `ref`'s element animates a copy of itself out (`ghostOut`) when React removes it. In
 * development, the unmount right after mounting (StrictMode's double mount) shows nothing. */
export function useGhostExit(
  ref: RefObject<HTMLElement | null>,
  keyframes: Keyframe[],
  duration: number,
): void {
  const exit = useRef({ keyframes, duration });
  useLayoutEffect(() => {
    const el = ref.current;
    const mountedAt = performance.now();
    const { keyframes: frames, duration: ms } = exit.current;
    return () => {
      if (!import.meta.env.DEV || performance.now() - mountedAt > 100) ghostOut(el, frames, ms);
    };
  }, [ref]);
}
