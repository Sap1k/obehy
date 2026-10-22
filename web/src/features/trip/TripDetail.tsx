import { Collapse, Tooltip, UnstyledButton } from "@mantine/core";
import {
  IconCalendar,
  IconChevronDown,
  IconChevronRight,
  IconChevronUp,
  IconClock,
} from "@tabler/icons-react";
import { Link } from "@tanstack/react-router";
import { useState } from "react";
import type { Alert, Trip } from "../../api/client";
import { AlertBanner } from "../../components/AlertBanner";
import { LineBadge, lineLabel } from "../../components/LineBadge";
import { tripAlerts } from "../../lib/alerts";
import { timeState } from "../../lib/punctuality";
import { Timetable } from "./Timetable";
import { noteGroups } from "./notes";
import { callTimes, isPassed, type RunCall } from "./runCalls";
import classes from "./TripPanel.module.css";

// A trip's detail, shared by the board's trip panel and the map's vehicle panel: the header
// (line, headsign, delay), then alerts, the run's timetable, notes and connections.

/** The delay chip's state at a call. */
export function headTime(call: Trip["calls"][number], stale: boolean) {
  const { scheduled, estimated } = callTimes(call);
  return scheduled ? timeState(scheduled, estimated, call.status, stale) : null;
}

export function TripHeader({
  trip,
  at,
  stale,
  children,
}: {
  trip: Trip;
  /** The call whose delay the header shows. */
  at: RunCall | undefined;
  stale: boolean;
  /** More chips after the delay (the map's vehicle state). */
  children?: React.ReactNode;
}) {
  const headState = at ? headTime(at.call, stale) : null;
  return (
    <header className={`${classes.header} ob-fade-in`}>
      <div className={classes.titleRow}>
        <LineBadge line={trip.line} tripShortName={trip.trip_short_name} size="lg" />
        <h2 className={classes.title}>{trip.headsign ?? lineLabel(trip.line)}</h2>
      </div>
      <div className={classes.meta}>
        {headState && (
          <span className={`${classes.delay} ${classes[headState.tone]}`}>
            <IconClock size={14} stroke={2} aria-hidden />
            {headState.caption}
          </span>
        )}
        {children}
        <span>{[trip.trip_short_name, trip.agency].filter(Boolean).join(" · ")}</span>
      </div>
    </header>
  );
}

export function TripBody({
  trip,
  parts,
  calls,
  boardedAt,
  aheadFrom,
  now,
  stale,
  stopId,
}: {
  trip: Trip;
  /** The trip and the run's parts after it that are loaded. */
  parts: Trip[];
  calls: RunCall[];
  /** The stop one boards at, which anchors the timetable. */
  boardedAt: string | undefined;
  /** Severe stop alerts show for the stops after this call. */
  aheadFrom: RunCall | undefined;
  now: number;
  stale: boolean;
  /** The board the trip is open on; connections link to trips on it. */
  stopId?: string;
}) {
  // The run's alerts: its parts' own and lines', and severe ones of the stops still ahead of
  // where one boards (the boarded stop's are on the board one came from).
  const ahead = calls
    .slice(aheadFrom ? calls.indexOf(aheadFrom) + 1 : 0)
    .filter((c) => !isPassed(c.call, now))
    .map((c) => c.call);
  const alerts = tripAlerts(
    [...new Map(parts.flatMap((p) => p.alerts).map((a) => [a.alert_id, a])).values()],
    { routeIds: parts.map((p) => p.line.route_id), tripIds: parts.map((p) => p.trip_id), ahead },
  );
  const alertContext = (alert: Alert, stopName: string | null) => {
    if (stopName) return `Zastávka ${stopName}`;
    const part =
      parts.find((p) =>
        alert.informs.some(
          (t) =>
            (t.kind === "trip" && t.id === p.trip_id) ||
            (t.kind === "route" && t.id === p.line.route_id),
        ),
      ) ?? trip;
    return `Linka ${lineLabel(part.line, part.trip_short_name)}`;
  };

  return (
    <>
      {alerts.map(({ alert, stopName }) => (
        <AlertBanner
          key={alert.alert_id}
          alert={alert}
          now={now}
          context={alertContext(alert, stopName)}
        />
      ))}

      <section className={classes.section}>
        <h3 className={classes.sectionTitle}>Jízdní řád</h3>
        <Timetable calls={calls} boardedAt={boardedAt} now={now} stale={stale} />
      </section>

      <Notes trip={trip} />
      <Connections trip={trip} stopId={stopId} />
    </>
  );
}

const NOTES_OPEN_KEY = "obehy.notes.open";

function readNotesOpen(): boolean {
  try {
    return sessionStorage.getItem(NOTES_OPEN_KEY) === "1";
  } catch {
    return false;
  }
}

/** The timetable's notes: when it runs, the train's short notes as chips (the full wording on
 * hover), and the longer texts behind a toggle remembered for the session. */
function Notes({ trip }: { trip: Trip }) {
  const { operates, short, long } = noteGroups(trip);
  const [open, setOpen] = useState(readNotesOpen);
  if (operates.length + short.length + long.length === 0) return null;
  const toggle = () => {
    setOpen(!open);
    try {
      sessionStorage.setItem(NOTES_OPEN_KEY, open ? "0" : "1");
    } catch {
      // Not remembered.
    }
  };
  return (
    <section className={classes.notes}>
      <h3 className={classes.notesTitle}>Poznámky jízdního řádu</h3>
      {operates.map((text) => (
        <p key={text} className={classes.operates}>
          <IconCalendar size={14} stroke={2} aria-hidden />
          {text}
        </p>
      ))}
      {short.length > 0 && (
        <ul className={classes.noteChips}>
          {short.map((n) => (
            <li key={n.label}>
              <Tooltip label={n.text} disabled={!n.text} multiline w={260} withArrow>
                <span className={classes.noteChip}>{n.label}</span>
              </Tooltip>
            </li>
          ))}
        </ul>
      )}
      {long.length > 0 && (
        <Collapse expanded={open} transitionDuration={180}>
          <ul className={classes.noteTexts}>
            {long.map((text) => (
              <li key={text}>{text}</li>
            ))}
          </ul>
        </Collapse>
      )}
      {long.length > 0 && (
        <UnstyledButton className={classes.notesToggle} onClick={toggle} aria-expanded={open}>
          {open ? "Skrýt poznámky" : `Další poznámky (${long.length})`}
          {open ? <IconChevronUp size={14} stroke={2} /> : <IconChevronDown size={14} stroke={2} />}
        </UnstyledButton>
      )}
    </section>
  );
}

function Connections({ trip, stopId }: { trip: Trip; stopId: string | undefined }) {
  if (trip.connections.length === 0) return null;
  const callName = (sequence: number | null) =>
    sequence === null ? null : (trip.calls.find((c) => c.sequence === sequence)?.name ?? null);
  return (
    <section className={classes.section}>
      <h3 className={classes.sectionTitle}>Návaznosti</h3>
      {trip.connections.map((c, i) => {
        const what = [c.line, c.destination].filter(Boolean).join(" · ") || "spoj";
        const title = c.direction === "waits_for" ? `Čeká na ${what}` : `Návaznost na ${what}`;
        const meta = [
          callName(c.at_sequence),
          c.wait_minutes ? `nejvýše ${c.wait_minutes} min` : null,
          c.note,
        ]
          .filter(Boolean)
          .join(" · ");
        const linked = c.trip && stopId;
        const body = (
          <>
            <span className={classes.connectionTexts}>
              <span className={classes.connectionTitle}>{title}</span>
              {meta && <span className={classes.connectionMeta}>{meta}</span>}
            </span>
            {linked && <IconChevronRight size={20} stroke={2} aria-hidden />}
          </>
        );
        return c.trip && stopId ? (
          <Link
            key={i}
            className={classes.connection}
            to="/odjezdy/$stopId/spoj/$tripId/$date"
            params={{ stopId, tripId: c.trip.trip_id, date: c.trip.service_date }}
            search={(prev) => ({ ...prev, od: undefined })}
          >
            {body}
          </Link>
        ) : (
          <div key={i} className={classes.connection}>
            {body}
          </div>
        );
      })}
    </section>
  );
}
