import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { api, unwrap, type Alert, type Departure, type Stop } from "../../api/client";

/** Boards and trips poll this often while the page is visible. */
export const POLL_MS = 15_000;

/** A board looks this far ahead (the API's `departures.minutes_max`), so a quiet stop still
 * fills a page; the row limit decides how much of it shows. */
export const WINDOW_MIN = 360;

export function useStopPlace(stopId: string) {
  return useQuery({
    queryKey: ["stop-place", stopId],
    queryFn: async ({ signal }) =>
      unwrap(
        await api.GET("/api/v1/stop-places/{location_id}", {
          params: { path: { location_id: stopId } },
          signal,
        }),
      ),
    staleTime: 60 * 60_000,
  });
}

export interface Board {
  generatedAt: string;
  stop: Stop;
  stops: Stop[];
  since: string;
  until: string;
  rows: Departure[];
  next: string | null;
  alerts: Alert[];
}

export interface BoardScope {
  stopId: string;
  arrivals: boolean;
  /** The board's start (ISO); undefined: now. */
  from: string | undefined;
  limit: number;
}

async function fetchBoard(scope: BoardScope, signal: AbortSignal): Promise<Board> {
  const params = {
    path: { location_id: scope.stopId },
    query: { from: scope.from, limit: scope.limit, minutes: WINDOW_MIN },
  };
  if (scope.arrivals) {
    const a = unwrap(
      await api.GET("/api/v1/stop-places/{location_id}/arrivals", { params, signal }),
    );
    return {
      generatedAt: a.generated_at,
      stop: a.stop,
      stops: a.stops,
      since: a.since,
      until: a.until,
      rows: a.arrivals,
      next: a.next ?? null,
      alerts: a.alerts,
    };
  }
  const d = unwrap(
    await api.GET("/api/v1/stop-places/{location_id}/departures", { params, signal }),
  );
  return {
    generatedAt: d.generated_at,
    stop: d.stop,
    stops: d.stops,
    since: d.since,
    until: d.until,
    rows: d.departures,
    next: d.next ?? null,
    alerts: d.alerts,
  };
}

/** One request per scope, polled. Showing more raises the limit (up to the API's), so the rows
 * on screen are always one answer; the previous answer stays while only the limit changes. */
export function useBoard(scope: BoardScope) {
  return useQuery({
    queryKey: [
      "board",
      scope.stopId,
      scope.arrivals ? "arrivals" : "departures",
      scope.from ?? "now",
      scope.limit,
    ],
    queryFn: ({ signal }) => fetchBoard(scope, signal),
    refetchInterval: POLL_MS,
    placeholderData: (previous, previousQuery) => {
      const key = previousQuery?.queryKey;
      const sameScope =
        key?.[1] === scope.stopId &&
        key?.[2] === (scope.arrivals ? "arrivals" : "departures") &&
        key?.[3] === (scope.from ?? "now");
      return sameScope ? keepPreviousData(previous) : undefined;
    },
  });
}
