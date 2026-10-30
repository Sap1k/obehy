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
  });
}

export interface Scope {
  source: string | undefined;
  date: string;
  agencyIds: string[];
  modes: string[];
  routeId: string | undefined;
}

/** The scope's chains; none is asked for until it has a source and companies or a route. */
export function useCirculations(scope: Scope, today: boolean) {
  const { source, date, agencyIds, modes, routeId } = scope;
  const complete = Boolean(source && (routeId || agencyIds.length > 0));
  return useQuery({
    queryKey: ["circulations", source, date, routeId ?? null, agencyIds, modes],
    queryFn: async ({ signal }) =>
      unwrap(
        await api.GET("/api/v1/circulations", {
          params: {
            query: routeId
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
    enabled: complete,
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
