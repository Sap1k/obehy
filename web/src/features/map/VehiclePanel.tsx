import { ActionIcon, Button, Skeleton } from "@mantine/core";
import { IconCurrentLocation, IconX } from "@tabler/icons-react";
import { useNavigate, useParams } from "@tanstack/react-router";
import { ApiError, type Vehicle, type VehicleDetail } from "../../api/client";
import { BoardMessage } from "../../components/BoardStates";
import { LineBadge, lineLabel } from "../../components/LineBadge";
import { VehicleCard } from "../../components/VehicleCard";
import { ago, useNow } from "../../lib/hooks";
import { TripBody, TripHeader } from "../trip/TripDetail";
import { useFollowingParts, useTrip } from "../trip/queries";
import { isPassed, runCalls } from "../trip/runCalls";
import { useMapState } from "./context";
import { setFollowing, useMapFocus, useMapStore } from "./store";
import { useVehicle } from "./queries";
import tripClasses from "../trip/TripPanel.module.css";
import classes from "./VehiclePanel.module.css";

const STATE_TEXT: Partial<Record<Vehicle["state"], string>> = {
  positioning: "Jede na spoj",
  layover: "Čeká na další spoj",
  unmatched: "Spoj neurčen",
  not_in_service: "Mimo provoz",
};

/** A position older than this says how old it is. */
const OLD_POSITION_MS = 60_000;

/** A vehicle opened on the map: its trip (header, alerts, timetable, notes, connections), then
 * the vehicle, with Sledovat to keep the map on it. */
export function VehiclePanel() {
  const { vehicleId } = useParams({ from: "/map/vozidlo/$vehicleId" });
  const navigate = useNavigate();
  const { phone, setSnap } = useMapState();
  useMapFocus({ vehicleId });
  const { vehicle: open, followedId } = useMapStore();
  const vehicle = open?.vehicle_id === vehicleId ? open : null;
  const following = followedId === vehicleId && vehicle !== null;
  const detail = useVehicle(vehicleId);
  const info = detail.data?.vehicle_id === vehicleId ? detail.data : undefined;
  const trip = useTrip(vehicle?.trip_id ?? undefined, vehicle?.service_date ?? undefined, true);
  const following_ = useFollowingParts(trip.data);
  const now = useNow(15_000);

  const close = () => void navigate({ to: "/" });
  const toggleFollow = () => {
    setFollowing(!following);
    if (!following && phone) setSnap("peek");
  };

  const top = (
    <div className={classes.top}>
      <Button
        size="xs"
        variant={following ? "filled" : "default"}
        leftSection={<IconCurrentLocation size={16} stroke={2} />}
        onClick={toggleFollow}
        disabled={!vehicle}
        aria-pressed={following}
      >
        {following ? "Sleduji" : "Sledovat"}
      </Button>
      <ActionIcon variant="subtle" color="gray" onClick={close} aria-label="Zavřít">
        <IconX size={20} stroke={2} />
      </ActionIcon>
    </div>
  );

  if (!vehicle && !info) {
    return (
      <div className={tripClasses.root}>
        {top}
        {detail.isError ? (
          <BoardMessage
            title={
              detail.error instanceof ApiError && detail.error.status === 404
                ? "Vozidlo nenalezeno"
                : "Vozidlo se nepodařilo načíst"
            }
            text="Zkontrolujte připojení. Zkusíme to znovu automaticky."
            action={{ label: "Zkusit znovu", onClick: () => void detail.refetch(), primary: true }}
          />
        ) : (
          <Loading />
        )}
      </div>
    );
  }

  const stateText = vehicle ? STATE_TEXT[vehicle.state] : undefined;
  const positionAge =
    vehicle && now - Date.parse(vehicle.last_seen) > OLD_POSITION_MS
      ? `Poloha ${ago(vehicle.last_seen, now)}`
      : null;
  const chips = (
    <>
      {stateText && <span className={classes.chip}>{stateText}</span>}
      {positionAge && <span>{positionAge}</span>}
    </>
  );

  if (!vehicle) {
    return (
      <div className={tripClasses.root}>
        {top}
        <BoardMessage
          title="Poloha není aktuální"
          text="Vozidlo teď neposílá polohu. Ukážeme ho znovu, jakmile se ozve."
        />
        <VehicleSection vehicleId={vehicleId} detail={info} vehicle={null} mode="bus" />
      </div>
    );
  }

  const data = trip.data;
  if (!data) {
    return (
      <div className={tripClasses.root}>
        {top}
        <header className={tripClasses.header}>
          <div className={tripClasses.titleRow}>
            {vehicle.line && <LineBadge line={vehicle.line} size="lg" />}
            <h2 className={tripClasses.title}>
              {vehicle.headsign ?? (vehicle.line ? lineLabel(vehicle.line) : "Vozidlo")}
            </h2>
          </div>
          <div className={tripClasses.meta}>{chips}</div>
        </header>
        {vehicle.trip_id && !trip.isError && <Loading />}
        <VehicleSection
          vehicleId={vehicleId}
          detail={info}
          vehicle={vehicle}
          mode={vehicle.line?.mode}
        />
      </div>
    );
  }

  const calls = runCalls(
    data,
    following_.map((q) => q.data),
  );
  const stale = Boolean(data.realtime?.stale) || trip.isError;
  const next = calls.find((c) => !isPassed(c.call, now)) ?? calls.at(-1);
  const last = calls[calls.indexOf(next!) - 1];
  const parts = [data, ...following_.flatMap((q) => (q.data ? [q.data] : []))];

  return (
    <div className={tripClasses.root}>
      {top}
      <TripHeader trip={data} at={next} stale={stale}>
        {chips}
      </TripHeader>
      <TripBody
        trip={data}
        parts={parts}
        calls={calls}
        boardedAt={undefined}
        aheadFrom={last}
        now={now}
        stale={stale}
      />
      <VehicleSection vehicleId={vehicleId} detail={info} vehicle={vehicle} mode={data.line.mode} />
    </div>
  );
}

function Loading() {
  return (
    <div className={tripClasses.loading}>
      <Skeleton height={32} width="70%" />
      <Skeleton height={20} width="40%" />
      <Skeleton height={200} />
    </div>
  );
}

function VehicleSection({
  vehicleId,
  detail,
  vehicle,
  mode,
}: {
  vehicleId: string;
  /** The vehicle's own answer, once it is in. */
  detail: VehicleDetail | undefined;
  vehicle: Vehicle | null;
  mode: string | undefined;
}) {
  const registry = detail?.registry;
  const model = [registry?.manufacturer, registry?.model].filter(Boolean).join(" ");
  const sources = [
    ...new Set((detail?.reports ?? []).map((r) => r.label?.cs ?? r.source).filter(Boolean)),
  ].join(", ");
  return (
    <section className={tripClasses.section}>
      <h3 className={tripClasses.sectionTitle}>Vozidlo</h3>
      <VehicleCard
        vehicle={
          vehicle ?? {
            vehicle_id: vehicleId,
            bearing: null,
            last_seen: detail?.generated_at ?? new Date().toISOString(),
            latitude: null,
            longitude: null,
            state: "not_in_service",
          }
        }
        mode={mode ?? vehicle?.line?.mode ?? "bus"}
      />
      {(model || sources) && (
        <dl className={classes.facts}>
          {model && (
            <>
              <dt>Typ</dt>
              <dd>{model}</dd>
            </>
          )}
          {sources && (
            <>
              <dt>Poloha od</dt>
              <dd>{sources}</dd>
            </>
          )}
        </dl>
      )}
    </section>
  );
}
