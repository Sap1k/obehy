import { useRouterState } from "@tanstack/react-router";
import type { Trip, Vehicle } from "../../api/client";
import { useStopPlace } from "../board/queries";
import { useTrip, useTripShape } from "../trip/queries";
import { useVehicle } from "./queries";
import { useMapStore } from "./store";

// What is open, read from the URL (a board, a trip opened from it, a vehicle on the map), with
// what the map and the panels show of it: the stop, the trip, its vehicle. The map and the
// vehicle panel ask the same queries; React Query asks the API once.

export interface Opened {
  /** Which navigation: each new one is brought into view once. */
  key: string;
  kind: "none" | "stop" | "trip" | "vehicle";
  stopId?: string;
  trip?: { tripId: string; date: string };
  vehicleId?: string;
}

/** The route's parameters, from any of the routes. */
export interface RouteParams {
  stopId?: string;
  tripId?: string;
  date?: string;
  vehicleId?: string;
}

export function openedFrom(params: RouteParams): Opened {
  const { stopId, tripId, date, vehicleId } = params;
  if (vehicleId) return { key: `vehicle:${vehicleId}`, kind: "vehicle", vehicleId };
  if (stopId && tripId && date) {
    return { key: `trip:${tripId}/${date}`, kind: "trip", stopId, trip: { tripId, date } };
  }
  if (stopId) return { key: `stop:${stopId}`, kind: "stop", stopId };
  return { key: "", kind: "none" };
}

/** What is open: from the deepest route's parameters (the shell sits above every route). */
export function useOpened(): Opened {
  return useRouterState({
    select: (s) => openedFrom((s.matches.at(-1)?.params ?? {}) as RouteParams),
    structuralSharing: true,
  });
}

/** The trip a vehicle's panel shows while its next one loads: its own previous trip. */
const carriedBy = (vehicleId: string | undefined) => (previous: Trip) =>
  previous.vehicles.some((v) => v.vehicle_id === vehicleId);

export function useSelection() {
  const opened = useOpened();
  const place = useStopPlace(opened.stopId);
  const routeTrip = useTrip(opened.trip?.tripId, opened.trip?.date);
  // The route's vehicle, else the trip's.
  const vehicleId = opened.vehicleId ?? routeTrip.data?.vehicles[0]?.vehicle_id;
  const detail = useVehicle(vehicleId);
  /** Undefined while unknown; null when it has no position now. */
  const vehicle: Vehicle | null | undefined = detail.data ? detail.data.vehicle : undefined;
  const vehicleTrip = useTrip(
    opened.trip ? undefined : (vehicle?.trip_id ?? undefined),
    opened.trip ? undefined : (vehicle?.service_date ?? undefined),
    carriedBy(vehicleId),
  );
  const trip = opened.trip ? routeTrip : vehicleTrip;
  const shape = useTripShape(trip.data?.trip_id, trip.data?.service_date);
  const followedId = useMapStore((s) => s.followedId);
  return {
    opened,
    stop: place.data?.stop,
    trip,
    paths: shape.data,
    vehicleId,
    vehicle,
    /** The vehicle's own answer (registry, sources), once it is in. */
    detail: detail.isPlaceholderData ? undefined : detail.data,
    vehicleQuery: detail,
    /** A vehicle without a position is not followed. */
    following: Boolean(vehicleId) && followedId === vehicleId && Boolean(vehicle),
  };
}

export type Selection = ReturnType<typeof useSelection>;
