import { IconAlertTriangle, IconChevronRight, IconSnowflake } from "@tabler/icons-react";
import type { Departure } from "../api/client";
import { timeState } from "../lib/punctuality";
import { STEP_FREE_LABEL, STEP_FREE_SIGN, stepFree } from "../lib/wheelchair";
import { LineBadge } from "./LineBadge";
import { CodeSign } from "./Pictogram";
import { PlatformBox } from "./PlatformBox";
import { TimeBox } from "./TimeBox";
import classes from "./DepartureRow.module.css";

export const rowClasses = classes;

/** One row of a board (Figma "Departure row"): time, line, destination, platform. Offline, the
 * last answer's times read neutral, as when stale. The parent makes it a link. */
export function DepartureRow({
  departure,
  offline = false,
}: {
  departure: Departure;
  offline?: boolean;
}) {
  const state = timeState(
    departure.scheduled,
    departure.estimated,
    departure.status,
    Boolean(departure.stale) || offline,
  );
  const airConditioned = departure.vehicle?.traits?.air_conditioning === true;
  const access = stepFree(departure.wheelchair, departure.vehicle?.traits);
  const hasAlert = (departure.alert_ids?.length ?? 0) > 0;
  return (
    <>
      <span className={classes.time}>
        <TimeBox state={state} />
      </span>
      <span className={classes.line}>
        <LineBadge line={departure.line} tripShortName={departure.trip_short_name} />
      </span>
      <span className={classes.main}>
        <span className={classes.headsign}>
          {departure.headsign ?? departure.line.long_name ?? ""}
        </span>
        <span className={classes.meta}>
          {departure.trip_short_name && <span>{departure.trip_short_name}</span>}
          {access && (
            <span className={classes.access} role="img" aria-label={STEP_FREE_LABEL[access]}>
              <CodeSign code={STEP_FREE_SIGN[access]} />
            </span>
          )}
          {airConditioned && <IconSnowflake size={14} stroke={2} aria-label="Klimatizace" />}
          {hasAlert && (
            <IconAlertTriangle
              size={14}
              stroke={2}
              aria-label="Mimořádnost"
              className={classes.alert}
            />
          )}
        </span>
      </span>
      <span className={classes.platform}>
        <PlatformBox code={departure.platform} />
      </span>
      <IconChevronRight size={20} stroke={2} className={classes.chevron} aria-hidden />
    </>
  );
}
