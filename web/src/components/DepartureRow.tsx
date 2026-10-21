import {
  IconAlertTriangle,
  IconChevronRight,
  IconSnowflake,
  IconWheelchair,
} from "@tabler/icons-react";
import type { Departure } from "../api/client";
import { timeState } from "../lib/punctuality";
import { LineBadge } from "./LineBadge";
import { PlatformBox } from "./PlatformBox";
import { TimeBox } from "./TimeBox";
import classes from "./DepartureRow.module.css";

export const rowClasses = classes;

/** Step-free: a low-floor or wheelchair-accessible vehicle. */
export function stepFree(departure: Pick<Departure, "vehicle">): boolean {
  const traits = departure.vehicle?.traits;
  return Boolean(traits?.low_floor || traits?.wheelchair_accessible);
}

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
          {stepFree(departure) && <IconWheelchair size={14} stroke={2} aria-label="Bezbariérový" />}
          {airConditioned && <IconSnowflake size={14} stroke={2} aria-label="Klimatizace" />}
          {hasAlert && (
            <span className={classes.alert}>
              <IconAlertTriangle size={14} stroke={2} aria-hidden />
              <span className={classes.alertLabel}>Mimořádnost</span>
            </span>
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
