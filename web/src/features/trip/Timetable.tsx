import { UnstyledButton } from "@mantine/core";
import { IconBell, IconChevronDown, IconChevronUp, IconCornerDownRight } from "@tabler/icons-react";
import { Link } from "@tanstack/react-router";
import { useLayoutEffect, useRef, useState } from "react";
import { LineBadge } from "../../components/LineBadge";
import { PlatformBox } from "../../components/PlatformBox";
import { TimeBox } from "../../components/TimeBox";
import { timeState } from "../../lib/punctuality";
import { reducedMotion } from "../../lib/motion";
import { keepInPlace } from "../../lib/scroll";
import { CLOSED, anchorIndex, collapse, type Opened } from "../../lib/timetable";
import { formatClock } from "../../lib/time";
import { callTimes, isPassed, type RunCall } from "./runCalls";
import { foldAway, unfold } from "./timetableMotion";
import classes from "./Timetable.module.css";

/** An expander's height (`.expander`), until one has been measured. */
const EXPANDER_PX = 36;

function stopsWord(n: number): string {
  if (n === 1) return "1 další zastávka";
  if (n >= 2 && n <= 4) return `${n} další zastávky`;
  return `${n} dalších zastávek`;
}

/** The run's calls (Figma "Timetable stop"): collapsed to the boarding stop, the two after it,
 * an expander and the last stop; each hidden range opens on its own. */
export function Timetable({
  calls,
  boardedAt,
  now,
  stale,
}: {
  calls: RunCall[];
  boardedAt: string | undefined;
  now: number;
  stale: boolean;
}) {
  const [open, setOpen] = useState<Opened>(CLOSED);
  const list = useRef<HTMLOListElement>(null);
  // What the change being made animates once it is on screen: where the anchor row stood (it
  // stays there), the rows shown before, and the expander's height (middle calls unfold from it).
  const pending = useRef<{ anchorTop: number | null; shown: Set<string>; expanderPx: number }>(
    null,
  );
  // A closing range folds away before it is removed; clicks wait for it.
  const folding = useRef(false);
  const expanderPx = useRef(EXPANDER_PX);
  const anchorRow = () => list.current?.querySelector<HTMLElement>("[data-anchor]") ?? null;
  const rows = () => [...(list.current?.querySelectorAll<HTMLElement>("[data-row]") ?? [])];

  const commit = (next: Opened) => {
    const expander = list.current?.querySelector<HTMLElement>('[data-row="expander:middle"]');
    if (expander) expanderPx.current = expander.offsetHeight;
    pending.current = {
      anchorTop: anchorRow()?.getBoundingClientRect().top ?? null,
      shown: new Set(rows().map((r) => r.dataset.row!)),
      expanderPx: expanderPx.current,
    };
    setOpen(next);
  };
  const change = (next: Opened) => {
    if (folding.current) return;
    const leaving = rows().filter(
      (r) =>
        (r.dataset.range === "before" && open.before && !next.before) ||
        (r.dataset.range === "middle" && open.middle && !next.middle),
    );
    if (leaving.length === 0 || reducedMotion()) {
      commit(next);
      return;
    }
    folding.current = true;
    void foldAway(leaving, expanderPx.current).finally(() => {
      folding.current = false;
      commit(next);
    });
  };
  useLayoutEffect(() => {
    const change = pending.current;
    pending.current = null;
    if (!change) return;
    const row = anchorRow();
    if (row && change.anchorTop !== null) keepInPlace(row, change.anchorTop);
    if (reducedMotion()) return;
    const added = rows().filter((r) => r.dataset.range && !change.shown.has(r.dataset.row!));
    unfold(added, change.expanderPx);
  }, [open]);
  const passed = calls.map((c) => isPassed(c.call, now));
  const anchor = anchorIndex(
    calls.map((c) => c.call),
    passed,
    boardedAt,
  );
  const next = passed.findIndex((p) => !p);
  const plan = collapse(calls.length, anchor, open);
  const anyOpen = open.before || open.middle;
  // Which hidden range each call belongs to (the plan with every range closed).
  const closed = collapse(calls.length, anchor, CLOSED);
  const within = (range: [number, number] | null | undefined, i: number) =>
    Boolean(range && i >= range[0] && i <= range[1]);
  const rangeOf = (i: number) =>
    within(closed?.before, i) ? "before" : within(closed?.middle, i) ? "middle" : undefined;

  const row = (i: number) => {
    const item = calls[i];
    if (!item) return null;
    return (
      <Stop
        key={`${item.tripId}/${item.call.sequence}`}
        item={item}
        state={passed[i] ? "passed" : i === next ? "next" : "upcoming"}
        first={i === 0}
        last={i === calls.length - 1}
        stale={stale}
        anchor={i === anchor}
        range={rangeOf(i)}
      />
    );
  };

  return (
    <div className={classes.root}>
      <div className={classes.columns} aria-hidden>
        <span>Čas</span>
        <span>Zastávka</span>
        <span>Nást.</span>
      </div>
      <ol ref={list} className={classes.list}>
        {plan ? (
          <>
            {plan.before && (
              <Expander
                id="before"
                label={`Předchozí zastávky (${plan.before[1] + 1})`}
                onOpen={() => change({ ...open, before: true })}
              />
            )}
            {plan.head.map(row)}
            {plan.middle && (
              <Expander
                id="middle"
                label={stopsWord(plan.middle[1] - plan.middle[0] + 1)}
                onOpen={() => change({ ...open, middle: true })}
              />
            )}
            {plan.last !== null && row(plan.last)}
          </>
        ) : (
          calls.map((_, i) => row(i))
        )}
      </ol>
      {anyOpen && (
        <UnstyledButton className={classes.collapse} onClick={() => change(CLOSED)}>
          Sbalit zastávky
          <IconChevronUp size={14} stroke={2} />
        </UnstyledButton>
      )}
    </div>
  );
}

function Expander({
  id,
  label,
  onOpen,
}: {
  id: "before" | "middle";
  label: string;
  onOpen: () => void;
}) {
  return (
    <li className={classes.expander} data-row={`expander:${id}`}>
      <span className={classes.rail} aria-hidden>
        <span className={classes.dots} />
      </span>
      <UnstyledButton className={classes.expanderButton} onClick={onOpen}>
        {label}
        <IconChevronDown size={14} stroke={2} />
      </UnstyledButton>
    </li>
  );
}

function Stop({
  item,
  state,
  first,
  last,
  stale,
  anchor,
  range,
}: {
  item: RunCall;
  state: "passed" | "next" | "upcoming";
  first: boolean;
  last: boolean;
  stale: boolean;
  /** The row the timetable is anchored on (kept still while ranges open). */
  anchor: boolean;
  /** The hidden range the call is in, which opens and closes it (timetableMotion.ts). */
  range: "before" | "middle" | undefined;
}) {
  const key = `${item.tripId}/${item.call.sequence}`;
  const { call } = item;
  const { scheduled, estimated } = callTimes(call);
  const time = scheduled ? timeState(scheduled, estimated, call.status, stale) : null;
  const showScheduled = time && time.delayMin !== null && time.delayMin !== 0;
  const requestStop = call.flags.includes("request_stop");
  return (
    <>
      {item.continuesAs && (
        <li className={classes.continuation} data-row={`${key}:then`} data-range={range}>
          <span className={classes.rail} aria-hidden>
            <span className={classes.line} />
          </span>
          <span className={classes.continuationText}>
            <IconCornerDownRight size={14} stroke={2} aria-hidden />
            Dále jako
            <LineBadge line={item.continuesAs} size="sm" />
          </span>
        </li>
      )}
      <li
        className={classes.stop}
        data-state={state}
        data-anchor={anchor || undefined}
        data-row={key}
        data-range={range}
        data-first={first}
        data-last={last}
      >
        <span className={classes.times}>
          {showScheduled && scheduled && (
            <span className={classes.scheduled}>{formatClock(scheduled)}</span>
          )}
          {time && <TimeBox state={time} compact withCaption={false} />}
        </span>
        <span className={classes.rail} aria-hidden>
          <span className={classes.line} />
          <span className={classes.dot} />
        </span>
        <span className={classes.name}>
          {/* The stop's own board. */}
          <Link
            to="/odjezdy/$stopId"
            params={{ stopId: call.location_id }}
            className={classes.nameLink}
          >
            {call.name}
          </Link>
          {requestStop && (
            <IconBell size={14} stroke={2} aria-label="Na znamení" className={classes.flag} />
          )}
        </span>
        <span className={classes.platform}>
          <PlatformBox code={call.platform} small />
        </span>
      </li>
    </>
  );
}
