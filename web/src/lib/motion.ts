// What the app's own animations share (the CSS ones are in theme/motion.css).

/** Things that move into place: fast out, gentle stop. */
export const EASE_OUT = "cubic-bezier(0.2, 0.8, 0.2, 1)";
/** Things that fold away. */
export const EASE_IN_OUT = "cubic-bezier(0.4, 0, 0.2, 1)";

/** The reader asked for less motion: things change in place. */
export function reducedMotion(): boolean {
  return window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
}
