import { Modal, Select, Skeleton, UnstyledButton } from "@mantine/core";
import { IconChevronDown, IconChevronRight } from "@tabler/icons-react";
import { Link } from "@tanstack/react-router";
import { useMemo, useState } from "react";
import { ApiError } from "../../api/client";
import { BoardMessage } from "../../components/BoardStates";
import {
  callDelay,
  delayTone,
  delayWords,
  segmentDays,
  segmentSummary,
  type Delay as DelayValue,
  type HistoryCall,
  type SegmentDay,
  type TripHistory,
} from "../../lib/history";
import { usePhone } from "../../lib/layout";
import { useTripHistory } from "./queries";
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
  const query = useTripHistory(tripId, true);
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
  return <Days history={query.data} stopId={stopId} />;
}

function Days({ history, stopId }: { history: TripHistory; stopId?: string }) {
  const last = history.stops.length - 1;
  const boarded = history.stops.findIndex((s) => s.location_id === stopId);
  const [from, setFrom] = useState(boarded >= 0 && boarded < last ? boarded : 0);
  const [to, setTo] = useState(last);
  const [open, setOpen] = useState<string | null>(null);
  const days = useMemo(() => segmentDays(history, from, to), [history, from, to]);
  const summary = segmentSummary(days);
  const stops = history.stops.map((s, i) => ({ value: String(i), label: s.name }));
  const span = `${dayMonth.format(asDate(history.since))} – ${dayMonth.format(asDate(history.until))}`;

  return (
    <div className={classes.body}>
      <div className={classes.segment}>
        <Select
          label="Odkud"
          data={stops.slice(0, last)}
          value={String(from)}
          onChange={(v) => {
            if (v === null) return;
            setFrom(Number(v));
            if (Number(v) >= to) setTo(last);
          }}
          allowDeselect={false}
          searchable
          comboboxProps={{ withinPortal: false }}
        />
        <Select
          label="Kam"
          data={stops.slice(from + 1)}
          value={String(to)}
          onChange={(v) => v !== null && setTo(Number(v))}
          allowDeselect={false}
          searchable
          comboboxProps={{ withinPortal: false }}
        />
      </div>
      <p className={classes.summary}>
        {span}
        {summary
          ? ` · v cíli včas ${Math.round(summary.onTime * 100)} % jízd · obvykle ${delayWords(summary.median)}`
          : ""}
        {history.cancelled > 0 ? ` · ${history.cancelled}× zrušeno` : ""}
      </p>
      {days.length === 0 ? (
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
          {days.map((d) => (
            <DayRow
              key={d.day.service_date}
              row={d}
              open={open === d.day.service_date}
              onToggle={() => setOpen(open === d.day.service_date ? null : d.day.service_date)}
            />
          ))}
        </ol>
      )}
    </div>
  );
}

/** A delay; an inferred one (passed out of sight) marked "~", with a title saying so. */
function Delay({ delay: { delayS, estimated } }: { delay: DelayValue }) {
  const mark = estimated && delayS !== null;
  return (
    <span
      className={classes.delay}
      data-tone={delayTone(delayS)}
      data-estimated={mark || undefined}
      title={mark ? "odhad: vůz tu nebyl vidět, ale před i za zastávkou jel stejně" : undefined}
    >
      {delayS === null ? "–" : `${mark ? "~" : ""}${delayWords(delayS)}`}
    </span>
  );
}

function DayRow({ row, open, onToggle }: { row: SegmentDay; open: boolean; onToggle: () => void }) {
  const { day } = row;
  const vehicle = day.worked_by.find((v) => v.physical) ?? day.worked_by[0];
  const name =
    vehicle && (vehicle.fleet_number ?? vehicle.public_label ?? vehicle.source_vehicle_id);
  return (
    <li className={classes.day} data-open={open || undefined}>
      <UnstyledButton
        className={classes.dayLine}
        onClick={onToggle}
        disabled={row.calls.length === 0}
        aria-expanded={open}
      >
        <span className={classes.date}>
          {row.calls.length > 0 && (
            <IconChevronDown size={14} stroke={2} className={classes.chevron} aria-hidden />
          )}
          {weekdayDate.format(asDate(day.service_date))}
        </span>
        {day.cancelled ? (
          <span className={classes.cancelled}>zrušeno</span>
        ) : (
          <>
            <Delay delay={row.from} />
            <Delay delay={row.to} />
            <Delay delay={row.worst} />
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
      {open && <Calls calls={row.calls} />}
    </li>
  );
}

function Calls({ calls }: { calls: HistoryCall[] }) {
  return (
    <ol className={classes.calls}>
      {calls.map((c, i) => (
        <li key={`${c.location_id}:${i}`} className={classes.call}>
          <span className={classes.callName}>{c.name}</span>
          <Delay delay={callDelay(c, i === calls.length - 1)} />
        </li>
      ))}
    </ol>
  );
}
