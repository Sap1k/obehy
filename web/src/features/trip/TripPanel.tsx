import { ActionIcon, Skeleton } from "@mantine/core";
import { IconArrowLeft, IconX } from "@tabler/icons-react";
import { useNavigate, useParams, useSearch } from "@tanstack/react-router";
import { ApiError } from "../../api/client";
import { BoardMessage } from "../../components/BoardStates";
import { VehicleCard } from "../../components/VehicleCard";
import { useNow } from "../../lib/hooks";
import { TripBody, TripHeader } from "./TripDetail";
import { useFollowingParts, useTrip } from "./queries";
import { runCalls } from "./runCalls";
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
  const parts = [data, ...following.flatMap((q) => (q.data ? [q.data] : []))];
  const vehicle = data.vehicles[0];

  return (
    <div className={classes.root}>
      {top}
      <TripHeader trip={data} at={boarded} stale={stale} />
      <TripBody
        trip={data}
        parts={parts}
        calls={calls}
        boardedAt={search.od}
        aheadFrom={boarded}
        now={now}
        stale={stale}
        stopId={stopId}
      />
      {vehicle && (
        <section className={classes.section}>
          <h3 className={classes.sectionTitle}>Vozidlo</h3>
          <VehicleCard vehicle={vehicle} mode={data.line.mode} />
        </section>
      )}
    </div>
  );
}
