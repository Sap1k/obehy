import { keepPreviousData, useQueries, useQuery } from "@tanstack/react-query";
import { api, unwrap, type Trip } from "../../api/client";
import { POLL_MS } from "../board/queries";

// One query per trip, polled, shared by everything that shows it (a board's trip panel, the
// map's vehicle panel, the map's path and stops). Its path is a query of its own, asked once:
// it does not change while the trip runs, and it is large.

function fetchTrip(tripId: string, date: string, shape: boolean, signal: AbortSignal) {
  return api.GET("/api/v1/trips/{trip_id}/{service_date}", {
    params: {
      path: { trip_id: tripId, service_date: date },
      query: shape ? { include: "shape" } : {},
    },
    signal,
  });
}

function tripQuery(tripId: string, date: string) {
  return {
    queryKey: ["trip", tripId, date],
    queryFn: async ({ signal }: { signal: AbortSignal }) =>
      unwrap(await fetchTrip(tripId, date, false, signal)),
    refetchInterval: POLL_MS,
    meta: { status: true },
  };
}

/** A trip. `keep`: the trip shown before stays while another one loads (all of them, or those
 * `keep` accepts), so a panel does not collapse in between. */
export function useTrip(
  tripId: string | undefined,
  date: string | undefined,
  keep: boolean | ((previous: Trip) => boolean) = false,
) {
  return useQuery({
    ...tripQuery(tripId ?? "", date ?? ""),
    enabled: Boolean(tripId && date),
    placeholderData:
      keep === true
        ? keepPreviousData
        : keep
          ? (previous: Trip | undefined) => (previous && keep(previous) ? previous : undefined)
          : undefined,
  });
}

/** A trip's path (GeoJSON LineString or MultiLineString), as [lon, lat] lines. */
export function useTripShape(tripId: string | undefined, date: string | undefined) {
  return useQuery({
    queryKey: ["trip-shape", tripId, date],
    queryFn: async ({ signal }) => unwrap(await fetchTrip(tripId!, date!, true, signal)),
    select: (trip: Trip) => pathsOf(trip.shape),
    enabled: Boolean(tripId && date),
    staleTime: Infinity,
  });
}

export function pathsOf(shape: unknown): [number, number][][] {
  const g = shape as { type?: string; coordinates?: unknown } | null | undefined;
  if (g?.type === "LineString") return [g.coordinates as [number, number][]];
  if (g?.type === "MultiLineString") return g.coordinates as [number, number][][];
  return [];
}

/** The parts of the run after this trip (the same train going on as another line), in order. */
export function useFollowingParts(trip: Trip | undefined) {
  const parts = trip?.run_parts ?? [];
  const at = trip ? parts.findIndex((p) => p.trip_id === trip.trip_id) : -1;
  const following = at >= 0 ? parts.slice(at + 1) : [];
  return useQueries({
    queries: following.map((p) => tripQuery(p.trip_id, trip?.service_date ?? "")),
  });
}
