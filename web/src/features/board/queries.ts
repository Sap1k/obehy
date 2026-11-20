import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { api, unwrap, type Alert, type Departure, type Stop } from "../../api/client";

/** Boards and trips poll this often while the page is visible. */
export const POLL_MS = 15_000;

export function useStopPlace(stopId: string | undefined) {
  return useQuery({
    queryKey: ["stop-place", stopId],
    queryFn: async ({ signal }) =>
      unwrap(
        await api.GET("/api/v1/stop-places/{location_id}", {
          params: { path: { location_id: stopId! } },
          signal,
        }),
      ),
    enabled: Boolean(stopId),
    staleTime: 60 * 60_000,
  });
}

/** A board row's identity: a trip may call at the stop place twice (at two of its posts), so
 * the call's sequence tells its rows apart. */
export function boardRowKey(row: Departure): string {
  return `${row.trip_id}/${row.service_date}/${row.location_id}/${row.sequence}`;
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
  /** Of `alerts`, those of the board's stop places (the API says which): banners above the rows. */
  stopAlerts: Alert[];
}

export interface BoardScope {
  stopId: string;
  /** One stop of the stop's group; undefined: the whole group (a station with the stop in
   * front of it), or the stop alone when it has none. */
  only: string | undefined;
  arrivals: boolean;
  /** The board's start (ISO); undefined: now. */
  from: string | undefined;
  limit: number;
}

/** One answer: the API looks far enough ahead to fill the page. */
export async function fetchBoard(scope: BoardScope, signal: AbortSignal): Promise<Board> {
  const params = {
    path: { location_id: scope.only ?? scope.stopId },
    query: { from: scope.from, limit: scope.limit, group: scope.only === undefined },
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
      stopAlerts: a.alerts.filter((x) => a.stop_alerts.includes(x.alert_id)),
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
    stopAlerts: d.alerts.filter((x) => d.stop_alerts.includes(x.alert_id)),
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
      scope.only ?? "group",
    ],
    queryFn: ({ signal }) => fetchBoard(scope, signal),
    refetchInterval: POLL_MS,
    meta: { status: true },
    placeholderData: (previous, previousQuery) => {
      const key = previousQuery?.queryKey;
      const sameScope =
        key?.[1] === scope.stopId &&
        key?.[2] === (scope.arrivals ? "arrivals" : "departures") &&
        key?.[3] === (scope.from ?? "now") &&
        key?.[5] === (scope.only ?? "group");
      return sameScope ? keepPreviousData(previous) : undefined;
    },
  });
}
