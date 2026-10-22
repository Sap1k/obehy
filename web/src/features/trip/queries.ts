import { useQueries, useQuery } from "@tanstack/react-query";
import { api, unwrap, type Trip } from "../../api/client";
import { POLL_MS } from "../board/queries";

function tripQuery(tripId: string, date: string, shape = false) {
  return {
    queryKey: ["trip", tripId, date, shape],
    queryFn: async ({ signal }: { signal: AbortSignal }) =>
      unwrap(
        await api.GET("/api/v1/trips/{trip_id}/{service_date}", {
          params: {
            path: { trip_id: tripId, service_date: date },
            query: shape ? { include: "shape" } : {},
          },
          signal,
        }),
      ),
    refetchInterval: POLL_MS,
  };
}

/** A trip, with its GeoJSON path when `shape` (the map draws it). */
export function useTrip(tripId: string | undefined, date: string | undefined, shape = false) {
  return useQuery({
    ...tripQuery(tripId ?? "", date ?? "", shape),
    enabled: Boolean(tripId && date),
  });
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
