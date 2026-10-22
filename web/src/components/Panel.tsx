import type { ReactNode } from "react";
import classes from "./Panel.module.css";

/** A panel over the desktop's map, of a fixed width so opening another beside it moves
 * nothing; on the phone, plain page content. `flush`: its content brings its own padding. */
export function Panel({
  label,
  narrow = false,
  flush = false,
  children,
}: {
  label: string;
  narrow?: boolean;
  flush?: boolean;
  children: ReactNode;
}) {
  return (
    <section
      className={classes.panel}
      aria-label={label}
      data-narrow={narrow || undefined}
      data-flush={flush || undefined}
    >
      {children}
    </section>
  );
}
