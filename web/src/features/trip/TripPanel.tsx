import { ActionIcon, Skeleton } from "@mantine/core";
import { IconArrowLeft, IconChevronRight, IconClock, IconX } from "@tabler/icons-react";
import { Link, useNavigate, useParams, useSearch } from "@tanstack/react-router";
import { ApiError, type Trip } from "../../api/client";
import { AlertBanner } from "../../components/AlertBanner";
import { BoardMessage } from "../../components/BoardStates";
import { LineBadge, lineLabel } from "../../components/LineBadge";
import { VehicleCard } from "../../components/VehicleCard";
import { useNow } from "../../lib/hooks";
import { timeState } from "../../lib/punctuality";
import { Timetable } from "./Timetable";
import { useFollowingParts, useTrip } from "./queries";
import { callTimes, runCalls } from "./runCalls";
import classes from "./TripPanel.module.css";

/** A trip opened from a board (Figma 11 panel, 15 phone): header, alerts, the run's timetable,
 * notes, connections, then the vehicle. */
export function TripPanel() {
  const { stopId, tripId, date } = useParams({ from: "/odjezdy/$stopId/spoj/$tripId/$date" });
  const search = useSearch({ from: "/odjezdy/$stopId/spoj/$tripId/$date" });
  const navigate = useNavigate();
  const trip = useTrip(tripId, date);
  const following = useFollowingParts(trip.data);
  const now = useNow(15_000);

  const close = () =>
    void navigate({
      to: "/odjezdy/$stopId",
      params: { stopId },
      search: (prev) => ({ smer: prev.smer, cas: prev.cas }),
    });

  const top = (
    <div className={classes.top}>
      <ActionIcon
        variant="subtle"
        color="gray"
        onClick={close}
        aria-label="Zpět na odjezdy"
        className={classes.back}
      >
        <IconArrowLeft size={20} stroke={2} />
      </ActionIcon>
      <span className={classes.topTitle}>Detail spoje</span>
      <ActionIcon
        variant="subtle"
        color="gray"
        onClick={close}
        aria-label="Zavřít"
        className={classes.close}
      >
        <IconX size={20} stroke={2} />
      </ActionIcon>
    </div>
  );

  if (!trip.data) {
    return (
      <div className={classes.root}>
        {top}
        {trip.isError ? (
          <BoardMessage
            title={
              trip.error instanceof ApiError && trip.error.status === 410
                ? "Spoj už neplatí"
                : "Spoj se nepodařilo načíst"
            }
            text="Zkontrolujte připojení. Zkusíme to znovu automaticky."
            action={{ label: "Zkusit znovu", onClick: () => void trip.refetch(), primary: true }}
          />
        ) : (
          <div className={classes.loading}>
            <Skeleton height={32} width="70%" />
            <Skeleton height={20} width="40%" />
            <Skeleton height={200} />
          </div>
        )}
      </div>
    );
  }

  const data = trip.data;
  const calls = runCalls(
    data,
    following.map((q) => q.data),
  );
  const stale = Boolean(data.realtime?.stale) || trip.isError;
  const boarded = calls.find((c) => c.call.location_id === search.od) ?? calls[0];
  const headState = boarded ? headTime(boarded.call, stale) : null;
  const vehicle = data.vehicles[0];

  return (
    <div className={classes.root}>
      {top}
      <header className={classes.header}>
        <div className={classes.titleRow}>
          <LineBadge line={data.line} tripShortName={data.trip_short_name} size="lg" />
          <h2 className={classes.title}>{data.headsign ?? lineLabel(data.line)}</h2>
        </div>
        <div className={classes.meta}>
          {headState && (
            <span className={`${classes.delay} ${classes[headState.tone]}`}>
              <IconClock size={14} stroke={2} aria-hidden />
              {headState.caption}
            </span>
          )}
          <span>{[data.trip_short_name, data.agency].filter(Boolean).join(" · ")}</span>
        </div>
      </header>

      {data.alerts.map((a) => (
        <AlertBanner
          key={a.alert_id}
          alert={a}
          now={now}
          context={`Linka ${lineLabel(data.line, data.trip_short_name)}`}
        />
      ))}

      <section className={classes.section}>
        <h3 className={classes.sectionTitle}>Jízdní řád</h3>
        <Timetable calls={calls} boardedAt={search.od} now={now} stale={stale} />
      </section>

      <Notes trip={data} />
      <Connections trip={data} stopId={stopId} />

      {vehicle && (
        <section className={classes.section}>
          <h3 className={classes.sectionTitle}>Vozidlo</h3>
          <VehicleCard vehicle={vehicle} mode={data.line.mode} />
        </section>
      )}
    </div>
  );
}

function headTime(call: Trip["calls"][number], stale: boolean) {
  const { scheduled, estimated } = callTimes(call);
  return scheduled ? timeState(scheduled, estimated, call.status, stale) : null;
}

function Notes({ trip }: { trip: Trip }) {
  // Notes repeat per call range; each text once.
  const lines = [
    ...new Set([
      ...trip.amenities.operates.map((l) => l.cs),
      ...trip.notes.map((n) => n.text ?? n.label).filter((t): t is string => Boolean(t)),
    ]),
  ];
  if (lines.length === 0) return null;
  return (
    <section className={classes.notes}>
      <h3 className={classes.notesTitle}>Poznámky jízdního řádu</h3>
      {lines.map((text, i) => (
        <p key={i}>{text}</p>
      ))}
    </section>
  );
}

function Connections({ trip, stopId }: { trip: Trip; stopId: string }) {
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
        const body = (
          <>
            <span className={classes.connectionTexts}>
              <span className={classes.connectionTitle}>{title}</span>
              {meta && <span className={classes.connectionMeta}>{meta}</span>}
            </span>
            {c.trip && <IconChevronRight size={20} stroke={2} aria-hidden />}
          </>
        );
        return c.trip ? (
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
