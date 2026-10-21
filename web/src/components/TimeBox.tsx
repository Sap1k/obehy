import type { TimeState } from "../lib/punctuality";
import { formatClock } from "../lib/time";
import classes from "./TimeBox.module.css";

/** The coloured time with its caption: "14:36" over "+4 min". */
export function TimeBox({
  state,
  compact = false,
  withCaption = true,
}: {
  state: TimeState;
  compact?: boolean;
  withCaption?: boolean;
}) {
  const className = [
    classes.root,
    classes[state.tone],
    compact ? classes.compact : "",
    state.struck ? classes.struck : "",
  ].join(" ");
  return (
    <span className={className}>
      <time className={classes.time} dateTime={state.shown}>
        {formatClock(state.shown)}
      </time>
      {withCaption && <span className={classes.caption}>{state.caption}</span>}
    </span>
  );
}
