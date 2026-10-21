import type { Line } from "../api/client";
import classes from "./LineBadge.module.css";

function hex(colour: string | null | undefined): string | undefined {
  if (!colour) return undefined;
  return colour.startsWith("#") ? colour : `#${colour}`;
}

/** The line's name; a train without a line is named after itself ("Os 6658"), and then only
 * its category shows, the number being printed beside it. */
export function lineLabel(
  line: Pick<Line, "short_name" | "long_name">,
  tripShortName?: string | null,
): string {
  const name = line.short_name ?? line.long_name ?? "?";
  if (tripShortName && name === tripShortName) return name.split(" ")[0] ?? name;
  return name;
}

/** A line's badge in the colours the API supplies (ink on white without them). */
export function LineBadge({
  line,
  tripShortName,
  size = "md",
}: {
  line: Line;
  tripShortName?: string | null;
  size?: "sm" | "md" | "lg";
}) {
  const sizeClass = size === "md" ? "" : classes[size];
  return (
    <span className={`${classes.container} ${sizeClass ?? ""}`}>
      <span
        className={classes.badge}
        style={{ background: hex(line.color), color: hex(line.text_color) }}
      >
        {lineLabel(line, tripShortName)}
      </span>
    </span>
  );
}
