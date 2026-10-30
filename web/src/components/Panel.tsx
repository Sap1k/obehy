import { useRef, type ReactNode } from "react";
import { useGhostExit } from "../lib/ghost";
import classes from "./Panel.module.css";

const SINK_OUT: Keyframe[] = [{ opacity: 1 }, { opacity: 0, transform: "translateY(8px)" }];

/** A panel over the desktop's map, of a fixed width so opening another beside it moves
 * nothing; on the phone, plain page content. It rises into place when it opens and slides out
 * when it closes. `flush`: its content brings its own padding. `wide`: as wide as there is room. */
export function Panel({
  label,
  narrow = false,
  wide = false,
  flush = false,
  children,
}: {
  label: string;
  narrow?: boolean;
  /** As wide and tall as the panels' area (a timeline): its content scrolls itself. */
  wide?: boolean;
  flush?: boolean;
  children: ReactNode;
}) {
  // Closing: a copy sinks out where the panel stood (React removes the panel at once).
  const root = useRef<HTMLElement>(null);
  useGhostExit(root, SINK_OUT, 160);
  return (
    <section
      ref={root}
      className={`${classes.panel} ob-rise-in`}
      aria-label={label}
      data-narrow={narrow || undefined}
      data-wide={wide || undefined}
      data-flush={flush || undefined}
    >
      {children}
    </section>
  );
}
