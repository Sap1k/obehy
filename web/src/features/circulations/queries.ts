import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { ApiError, api, unwrap } from "../../api/client";
import { POLL_MS } from "../board/queries";

// The oběhy page's answers: what a day can be scoped by (its sources, and a source's companies
// modes and lines), the scoped chains (polled only for today), and a vehicle's detail.

export function useScopes(source: string | undefined, date: string) {
  return useQuery({
    queryKey: ["circulation-scopes", source, date],
    queryFn: async ({ signal }) =>
      unwrap(
        await api.GET("/api/v1/circulations/scopes", {
          params: { query: { source, date } },
          signal,
        }),
      ),
    staleTime: 60_000,
    // Another date's or source's choices stay up until the new ones are in.
    placeholderData: keepPreviousData,
  });
}

export interface Scope {
  source: string | undefined;
  date: string;
  agencyIds: string[];
  modes: string[];
  routeId: string | undefined;
  /** One of the source's vehicles. */
  vehicle: string | undefined;
}

/** Whether `scope` names what to show: a source and a vehicle, a route or companies. */
export function scopeComplete(scope: Scope): boolean {
  return Boolean(scope.source && (scope.vehicle || scope.routeId || scope.agencyIds.length > 0));
}

/** The scope's chains; none is asked for until the scope is complete. */
export function useCirculations(scope: Scope, today: boolean) {
  const { source, date, agencyIds, modes, routeId, vehicle } = scope;
  return useQuery({
    queryKey: ["circulations", source, date, vehicle ?? null, routeId ?? null, agencyIds, modes],
    queryFn: async ({ signal }) =>
      unwrap(
        await api.GET("/api/v1/circulations", {
          params: {
            query: vehicle
              ? { source: source!, date, source_vehicle_id: vehicle }
              : routeId
                ? { source: source!, date, route_id: routeId }
                : {
                    source: source!,
                    date,
                    agency_id: agencyIds,
                    mode: modes.length ? modes : undefined,
                  },
          },
          signal,
        }),
      ),
    enabled: scopeComplete(scope),
    refetchInterval: today ? POLL_MS : false,
    // A past day's oběhy are done.
    staleTime: today ? 0 : 5 * 60_000,
    placeholderData: keepPreviousData,
    meta: { status: true },
  });
}

/** A vehicle opened on the page: what the registry and the sources say of it. A vehicle no
 * source reports now and the registry does not know is a 404. */
export function useVehicleDetail(vehicleId: string, enabled: boolean) {
  return useQuery({
    queryKey: ["vehicle-detail", vehicleId],
    queryFn: async ({ signal }) =>
      unwrap(
        await api.GET("/api/v1/vehicles/{vehicle_id}", {
          params: { path: { vehicle_id: vehicleId } },
          signal,
        }),
      ),
    enabled,
    staleTime: 60_000,
    retry: (count, error) => !(error instanceof ApiError && error.status === 404) && count < 2,
  });
}
