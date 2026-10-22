import { UnstyledButton } from "@mantine/core";
import { IconBell, IconChevronDown, IconChevronUp, IconCornerDownRight } from "@tabler/icons-react";
import { useLayoutEffect, useRef, useState } from "react";
import { LineBadge } from "../../components/LineBadge";
import { PlatformBox } from "../../components/PlatformBox";
import { TimeBox } from "../../components/TimeBox";
import { timeState } from "../../lib/punctuality";
import { keepInPlace } from "../../lib/scroll";
import { CLOSED, anchorIndex, collapse, type Opened } from "../../lib/timetable";
import { formatClock } from "../../lib/time";
import { callTimes, isPassed, type RunCall } from "./runCalls";
import classes from "./Timetable.module.css";

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
  // Opening or closing the earlier calls keeps the anchor row still on screen.
  const list = useRef<HTMLOListElement>(null);
  const anchorTop = useRef<number | null>(null);
  const anchorRow = () => list.current?.querySelector<HTMLElement>("[data-anchor]") ?? null;
  const change = (next: Opened) => {
    anchorTop.current = anchorRow()?.getBoundingClientRect().top ?? null;
    setOpen(next);
  };
  useLayoutEffect(() => {
    const row = anchorRow();
    if (row && anchorTop.current !== null) keepInPlace(row, anchorTop.current);
    anchorTop.current = null;
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
                label={`Předchozí zastávky (${plan.before[1] + 1})`}
                onOpen={() => change({ ...open, before: true })}
              />
            )}
            {plan.head.map(row)}
            {plan.middle && (
              <Expander
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

function Expander({ label, onOpen }: { label: string; onOpen: () => void }) {
  return (
    <li className={classes.expander}>
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
}: {
  item: RunCall;
  state: "passed" | "next" | "upcoming";
  first: boolean;
  last: boolean;
  stale: boolean;
  /** The row the timetable is anchored on (kept still while ranges open). */
  anchor: boolean;
}) {
  const { call } = item;
  const { scheduled, estimated } = callTimes(call);
  const time = scheduled ? timeState(scheduled, estimated, call.status, stale) : null;
  const showScheduled = time && time.delayMin !== null && time.delayMin !== 0;
  const requestStop = call.flags.includes("request_stop");
  return (
    <>
      {item.continuesAs && (
        <li className={classes.continuation}>
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
          {call.name}
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
