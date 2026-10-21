import { UnstyledButton } from "@mantine/core";
import { IconBell, IconChevronDown, IconChevronUp, IconCornerDownRight } from "@tabler/icons-react";
import { useState } from "react";
import { LineBadge } from "../../components/LineBadge";
import { PlatformBox } from "../../components/PlatformBox";
import { TimeBox } from "../../components/TimeBox";
import { timeState } from "../../lib/punctuality";
import { anchorIndex, collapse } from "../../lib/timetable";
import { formatClock } from "../../lib/time";
import { callTimes, isPassed, type RunCall } from "./runCalls";
import classes from "./Timetable.module.css";

function stopsWord(n: number): string {
  if (n === 1) return "1 další zastávka";
  if (n >= 2 && n <= 4) return `${n} další zastávky`;
  return `${n} dalších zastávek`;
}

/** The run's calls (Figma "Timetable stop"): collapsed to the boarding stop, the two after it,
 * an expander and the last stop; expandable in full. */
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
  const [expanded, setExpanded] = useState(false);
  const passed = calls.map((c) => isPassed(c.call, now));
  const anchor = anchorIndex(
    calls.map((c) => c.call),
    passed,
    boardedAt,
  );
  const next = passed.findIndex((p) => !p);
  const plan = expanded ? null : collapse(calls.length, anchor);

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
      />
    );
  };

  const expander = (count: number, label?: string) => (
    <li className={classes.expander}>
      <span className={classes.rail} aria-hidden>
        <span className={classes.dots} />
      </span>
      <UnstyledButton className={classes.expanderButton} onClick={() => setExpanded(true)}>
        {label ?? stopsWord(count)}
        <IconChevronDown size={14} stroke={2} />
      </UnstyledButton>
    </li>
  );

  return (
    <div className={classes.root}>
      <div className={classes.columns} aria-hidden>
        <span>Čas</span>
        <span>Zastávka</span>
        <span>Nást.</span>
      </div>
      <ol className={classes.list}>
        {plan ? (
          <>
            {plan.before &&
              expander(
                plan.before[1] - plan.before[0] + 1,
                `Předchozí zastávky (${plan.before[1] + 1})`,
              )}
            {plan.head.map(row)}
            {plan.middle && expander(plan.middle[1] - plan.middle[0] + 1)}
            {plan.last !== null && row(plan.last)}
          </>
        ) : (
          calls.map((_, i) => row(i))
        )}
      </ol>
      {expanded && calls.length > 6 && (
        <UnstyledButton className={classes.collapse} onClick={() => setExpanded(false)}>
          Sbalit zastávky
          <IconChevronUp size={14} stroke={2} />
        </UnstyledButton>
      )}
    </div>
  );
}

function Stop({
  item,
  state,
  first,
  last,
  stale,
}: {
  item: RunCall;
  state: "passed" | "next" | "upcoming";
  first: boolean;
  last: boolean;
  stale: boolean;
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
      <li className={classes.stop} data-state={state} data-first={first} data-last={last}>
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
