import { Collapse, Tooltip, UnstyledButton } from "@mantine/core";
import { IconChevronDown, IconChevronRight, IconChevronUp, IconClock } from "@tabler/icons-react";
import { Link } from "@tanstack/react-router";
import { useState } from "react";
import type { Alert, Trip } from "../../api/client";
import { AlertBanner } from "../../components/AlertBanner";
import { LineBadge, lineLabel } from "../../components/LineBadge";
import { CodeSign } from "../../components/Pictogram";
import { tripAlerts } from "../../lib/alerts";
import { timeState } from "../../lib/punctuality";
import { linkify } from "../../lib/linkify";
import { usePersistentFlag } from "../../lib/storage";
import { HistoryLink } from "./History";
import { Timetable } from "./Timetable";
import { noteGroups, type MarkedNote } from "./notes";
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
        <div className={classes.sectionHead}>
          <h3 className={classes.sectionTitle}>Jízdní řád</h3>
          <CirculationLink trip={trip} />
          <HistoryLink tripId={trip.trip_id} />
        </div>
        <Timetable calls={calls} boardedAt={boardedAt} now={now} stale={stale} />
      </section>

      <Notes trip={trip} />
      <Connections trip={trip} stopId={stopId} />
    </>
  );
}

/** The oběhy of the trip's line that day, by the source of the vehicle on it now. */
function CirculationLink({ trip }: { trip: Trip }) {
  const source = trip.vehicles.find((v) => v.sources?.length)?.sources?.[0];
  if (!source) return null;
  return (
    <Link
      className={classes.headLink}
      to="/obehy"
      search={{
        zdroj: source,
        datum: trip.service_date,
        linka: trip.line.route_id,
        spoj: trip.trip_id,
      }}
    >
      Oběhy linky
      <IconChevronRight size={14} stroke={2} aria-hidden />
    </Link>
  );
}

const NOTES_OPEN_KEY = "obehy.notes.open";

/** The timetable's notes: when it runs and the marked codes, each with its sign and text in
 * print order, then the longer texts behind a toggle remembered for the session. */
function Notes({ trip }: { trip: Trip }) {
  const { operates, plain, stated, legend, detail, long } = noteGroups(trip);
  const [open, setOpen] = usePersistentFlag(NOTES_OPEN_KEY, "session");
  if (operates.length + stated.length + legend.length + long.length === 0) return null;
  const more = legend.length + long.length;
  const toggle = () => setOpen(!open);
  return (
    <section className={classes.notes}>
      <h3 className={classes.notesTitle}>Poznámky jízdního řádu</h3>
      {operates.length > 0 && <SignedNotes notes={operates} className={classes.operates} />}
      {plain.length > 0 && (
        <ul className={classes.signs}>
          {plain.map((n) => (
            <li key={n.key}>
              <Tooltip label={n.text} multiline w={260} withArrow>
                <span className={classes.signChip} role="img" aria-label={n.text}>
                  <CodeSign code={n.key} />
                </span>
              </Tooltip>
            </li>
          ))}
        </ul>
      )}
      {stated.length > 0 && <SignedNotes notes={stated} className={classes.marked} />}
      {more > 0 && (
        <Collapse expanded={open} transitionDuration={180}>
          <div className={classes.legend}>
            {legend.length > 0 && (
              <div className={classes.legendGroup}>
                <h4 className={classes.legendTitle}>Vysvětlivky značek</h4>
                <SignedNotes notes={legend} className={classes.noteTexts} />
              </div>
            )}
            {long.length > 0 && (
              <div className={classes.legendGroup}>
                <h4 className={classes.legendTitle}>Další poznámky</h4>
                <ul className={classes.longNotes}>
                  {long.map((text) => (
                    <LongNote key={text} text={text} />
                  ))}
                </ul>
              </div>
            )}
          </div>
        </Collapse>
      )}
      {more > 0 && (
        <UnstyledButton className={classes.notesToggle} onClick={toggle} aria-expanded={open}>
          {/* The low-priority signs, muted, on the toggle that explains them. */}
          {!open && detail.length > 0 && (
            <span className={classes.minorSigns} aria-hidden>
              {detail.map((n) => (
                <CodeSign key={n.key} code={n.key} />
              ))}
            </span>
          )}
          {open ? "Skrýt" : moreLabel(legend.length, long.length)}
          {open ? <IconChevronUp size={14} stroke={2} /> : <IconChevronDown size={14} stroke={2} />}
        </UnstyledButton>
      )}
    </section>
  );
}

/** A free-text note on one line; a tap opens it in full. */
function LongNote({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  const chevron = (
    <IconChevronRight size={14} stroke={2} className={classes.longNoteChevron} aria-hidden />
  );
  if (!open) {
    return (
      <li>
        <UnstyledButton
          className={classes.longNote}
          onClick={() => setOpen(true)}
          aria-expanded={false}
        >
          {chevron}
          <span className={classes.longNoteText}>{text}</span>
        </UnstyledButton>
      </li>
    );
  }
  // Open, the text carries its contacts as links, so the row is no button: the chevron closes
  // it, and so does a tap on the text outside a link.
  const close = (event: React.MouseEvent) => {
    if (!(event.target as HTMLElement).closest("a")) setOpen(false);
  };
  return (
    <li className={classes.longNote} data-open>
      <UnstyledButton
        className={classes.longNoteClose}
        onClick={() => setOpen(false)}
        aria-expanded
        aria-label="Sbalit poznámku"
      >
        {chevron}
      </UnstyledButton>
      <span className={classes.longNoteText} onClick={close}>
        {linkify(text, classes.noteLink)}
      </span>
    </li>
  );
}

/** What the toggle opens: the signs' legend, the other notes (counted), or both. */
function moreLabel(signs: number, notes: number): string {
  if (notes === 0) return "Vysvětlivky značek";
  const counted = `další poznámky (${notes})`;
  return signs > 0 ? `Vysvětlivky a ${counted}` : counted[0]!.toUpperCase() + counted.slice(1);
}

function SignedNotes({ notes, className }: { notes: MarkedNote[]; className?: string }) {
  return (
    <ul className={className}>
      {notes.map((n) => (
        <li key={n.key}>
          <CodeSign code={n.key} />
          <span>{n.text}</span>
        </li>
      ))}
    </ul>
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
