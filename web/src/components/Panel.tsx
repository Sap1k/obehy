import type { ReactNode } from "react";
import classes from "./Panel.module.css";

/** Each kind of panel's view-transition name (one of each at a time): a panel that closes
 * slides out under it (theme/motion.css). */
export type PanelName = "board" | "trip" | "vehicle" | "page";

/** A panel over the desktop's map, of a fixed width so opening another beside it moves
 * nothing; on the phone, plain page content. It rises into place when it opens and slides out
 * when it closes. `flush`: its content brings its own padding. */
export function Panel({
  name,
  label,
  narrow = false,
  flush = false,
  children,
}: {
  name: PanelName;
  label: string;
  narrow?: boolean;
  flush?: boolean;
  children: ReactNode;
}) {
  return (
    <section
      className={`${classes.panel} ob-rise-in`}
      style={{ viewTransitionName: `panel-${name}` }}
      aria-label={label}
      data-narrow={narrow || undefined}
      data-flush={flush || undefined}
    >
      {children}
    </section>
  );
}
