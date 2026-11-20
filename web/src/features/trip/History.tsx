import { Modal, Select, Skeleton, UnstyledButton } from "@mantine/core";
import { IconChevronDown, IconChevronRight } from "@tabler/icons-react";
import { Link } from "@tanstack/react-router";
import { useState } from "react";
import { ApiError } from "../../api/client";
import { BoardMessage } from "../../components/BoardStates";
import {
  delayWords,
  type HistoryCall,
  type HistoryDay,
  type HistoryDelay,
  type TripHistory,
} from "../../lib/history";
import { toneOf } from "../../lib/punctuality";
import { usePhone } from "../../lib/layout";
import { useTripHistory, type HistorySegmentQuery } from "./queries";
import classes from "./History.module.css";

// "Historie zpoždění": how late the trip's journey ran on each recorded day, between the stop
// the rider boards at and the one they get off at, in a dialog, so the trip's own view stays as
// it is.

const weekdayDate = new Intl.DateTimeFormat("cs-CZ", {
  weekday: "short",
  day: "numeric",
  month: "numeric",
});
const dayMonth = new Intl.DateTimeFormat("cs-CZ", { day: "numeric", month: "numeric" });

const asDate = (key: string) => new Date(`${key}T12:00:00Z`);

/** The link that opens the dialog; the history is asked for only once it is open. `stopId` is
 * where the trip was opened from (a board's stop): the segment starts there when it is one of
 * the journey's stops. */
export function HistoryLink({ tripId, stopId }: { tripId: string; stopId?: string }) {
  const [opened, setOpened] = useState(false);
  const phone = usePhone();
  return (
    <>
      <UnstyledButton className={classes.link} onClick={() => setOpened(true)}>
        Historie zpoždění
        <IconChevronRight size={14} stroke={2} aria-hidden />
      </UnstyledButton>
      <Modal
        opened={opened}
        onClose={() => setOpened(false)}
        title="Historie zpoždění"
        fullScreen={phone}
        size="lg"
        classNames={{ content: classes.dialog, header: classes.dialog, title: classes.dialogTitle }}
      >
        {opened && <HistoryBody tripId={tripId} stopId={stopId} />}
      </Modal>
    </>
  );
}

function HistoryBody({ tripId, stopId }: { tripId: string; stopId?: string }) {
  // The segment starts where the trip was opened from; the API falls back to the journey's
  // first stop when that is none of its stops.
  const [segment, setSegment] = useState<HistorySegmentQuery>({ from: stopId });
  const query = useTripHistory(tripId, true, segment);
  if (query.isPending) {
    return (
      <div className={classes.loading} aria-busy="true" aria-label="Načítání">
        <Skeleton height={36} />
        <Skeleton height={18} width="60%" />
        {[0, 1, 2, 3].map((i) => (
          <Skeleton key={i} height={44} />
        ))}
      </div>
    );
  }
  if (query.isError) {
    return query.error instanceof ApiError && query.error.status === 404 ? (
      <BoardMessage
        title="Historie není k dispozici"
        text="U tohoto spoje se průběh jízd nezaznamenává."
      />
    ) : (
      <BoardMessage
        title="Historii se nepodařilo načíst"
        text="Zkontrolujte připojení."
        action={{ label: "Zkusit znovu", onClick: () => void query.refetch(), primary: true }}
      />
    );
  }
  return <Days history={query.data} onSegment={setSegment} />;
}

function Days({
  history,
  onSegment,
}: {
  history: TripHistory;
  onSegment: (segment: HistorySegmentQuery) => void;
}) {
  const ids = history.stops.map((s) => s.location_id);
  const from = history.segment?.start_location_id ?? ids[0];
  const to = history.segment?.end_location_id ?? ids.at(-1);
  const after = ids.indexOf(from ?? "") + 1;
  const [open, setOpen] = useState<string | null>(null);
  const summary = history.segment;
  const stops = history.stops.map((s) => ({ value: s.location_id, label: s.name }));
  const span = `${dayMonth.format(asDate(history.since))} – ${dayMonth.format(asDate(history.until))}`;

  return (
    <div className={classes.body}>
      <div className={classes.segment}>
        <Select
          label="Odkud"
          data={stops.slice(0, -1)}
          value={from ?? null}
          onChange={(v) => {
            if (v === null) return;
            // A destination before the new start falls back to the journey's last stop.
            onSegment({ from: v, to: ids.indexOf(v) < ids.indexOf(to ?? "") ? to : undefined });
          }}
          allowDeselect={false}
          searchable
          comboboxProps={{ withinPortal: false }}
        />
        <Select
          label="Kam"
          data={stops.slice(after)}
          value={to ?? null}
          onChange={(v) => v !== null && onSegment({ from, to: v })}
          allowDeselect={false}
          searchable
          comboboxProps={{ withinPortal: false }}
        />
      </div>
      <p className={classes.summary}>
        {span}
        {summary?.on_time_share != null && summary.median_delay_min != null
          ? ` · v cíli včas ${Math.round(summary.on_time_share * 100)} % jízd · obvykle ${delayWords(summary.median_delay_min)}`
          : ""}
        {history.cancelled > 0 ? ` · ${history.cancelled}× zrušeno` : ""}
      </p>
      {history.days.length === 0 ? (
        <BoardMessage
          title="Zatím žádné záznamy"
          text="Za toto období nemáme o jízdách tohoto spoje záznam."
        />
      ) : (
        <ol className={classes.days} aria-label="Jízdy po dnech">
          <li className={classes.dayHead} aria-hidden>
            <span>Den</span>
            <span>Odjezd</span>
            <span>Příjezd</span>
            <span>Nejvíc</span>
            <span>Vůz</span>
          </li>
          {history.days.map((d) => (
            <DayRow
              key={d.service_date}
              day={d}
              open={open === d.service_date}
              onToggle={() => setOpen(open === d.service_date ? null : d.service_date)}
            />
          ))}
        </ol>
      )}
    </div>
  );
}

/** A delay; an inferred one (passed out of sight) marked "~", with a title saying so. */
function Delay({ delay }: { delay: HistoryDelay | undefined }) {
  const delayMin = delay?.delay_min ?? null;
  const mark = Boolean(delay?.estimated) && delayMin !== null;
  return (
    <span
      className={classes.delay}
      data-tone={toneOf(delay?.punctuality ?? "unknown")}
      data-estimated={mark || undefined}
      title={mark ? "odhad: vůz tu nebyl vidět, ale před i za zastávkou jel stejně" : undefined}
    >
      {delayMin === null ? "–" : `${mark ? "~" : ""}${delayWords(delayMin)}`}
    </span>
  );
}

function DayRow({ day, open, onToggle }: { day: HistoryDay; open: boolean; onToggle: () => void }) {
  const run = day.segment;
  const calls = run ? day.calls.slice(run.first_call, run.last_call + 1) : [];
  const vehicle = day.worked_by.find((v) => v.physical) ?? day.worked_by[0];
  const name = vehicle?.display_name;
  return (
    <li className={classes.day} data-open={open || undefined}>
      <UnstyledButton
        className={classes.dayLine}
        onClick={onToggle}
        disabled={calls.length === 0}
        aria-expanded={open}
      >
        <span className={classes.date}>
          {calls.length > 0 && (
            <IconChevronDown size={14} stroke={2} className={classes.chevron} aria-hidden />
          )}
          {weekdayDate.format(asDate(day.service_date))}
        </span>
        {day.cancelled ? (
          <span className={classes.cancelled}>zrušeno</span>
        ) : (
          <>
            <Delay delay={run?.start} />
            <Delay delay={run?.end} />
            <Delay delay={run?.worst} />
          </>
        )}
      </UnstyledButton>
      <span className={classes.vehicle}>
        {vehicle?.physical ? (
          <Link
            to="/obehy"
            search={{
              zdroj: vehicle.source,
              datum: day.service_date,
              vozidlo: vehicle.source_vehicle_id,
              spoj: day.trip_id,
            }}
            className={classes.vehicleLink}
            title="Oběh vozu ten den"
          >
            {name}
          </Link>
        ) : (
          (name ?? "")
        )}
      </span>
      {open && run && <Calls calls={calls} delays={run.delays} />}
    </li>
  );
}

function Calls({ calls, delays }: { calls: HistoryCall[]; delays: HistoryDelay[] }) {
  return (
    <ol className={classes.calls}>
      {calls.map((c, i) => (
        <li key={`${c.location_id}:${i}`} className={classes.call}>
          <span className={classes.callName}>{c.name}</span>
          <Delay delay={delays[i]} />
        </li>
      ))}
    </ol>
  );
}
