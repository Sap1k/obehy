import { useQueries, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api, unwrap, type Vehicle, type VehicleDetail } from "../../api/client";
import { VEHICLES_LIMIT, cellsFor, type Bounds } from "./grid";

/** Vehicles poll at the worker's tick. */
export const VEHICLES_POLL_MS = 10_000;

export interface Positioned extends Vehicle {
  latitude: number;
  longitude: number;
}

interface CellAnswer {
  vehicles: Vehicle[];
  generated_at: string;
}

const placed = (v: Vehicle): v is Positioned => v.latitude !== null && v.longitude !== null;

/** One row per vehicle across cells: one on a cell's edge comes in both, the fresher kept. */
export function mergeVehicles(answers: readonly (readonly Vehicle[] | undefined)[]): Positioned[] {
  const byId = new Map<string, Positioned>();
  for (const vehicles of answers) {
    for (const v of vehicles ?? []) {
      if (!placed(v)) continue;
      const had = byId.get(v.vehicle_id);
      if (!had || Date.parse(v.last_seen) > Date.parse(had.last_seen)) byId.set(v.vehicle_id, v);
    }
  }
  return [...byId.values()];
}

/** A vehicle's freshest row among the cell answers (the one it was clicked on). */
export function rowAmong(
  answers: readonly (CellAnswer | undefined)[],
  vehicleId: string,
): Vehicle | undefined {
  let best: Vehicle | undefined;
  for (const answer of answers) {
    for (const v of answer?.vehicles ?? []) {
      if (v.vehicle_id !== vehicleId) continue;
      if (!best || Date.parse(v.last_seen) > Date.parse(best.last_seen)) best = v;
    }
  }
  return best;
}

function cellAnswers(client: QueryClient): (CellAnswer | undefined)[] {
  return client.getQueriesData<CellAnswer>({ queryKey: ["vehicles"] }).map(([, data]) => data);
}

/** Every vehicle answer on the map is asked again at the same moment, on the wall clock's 10 s
 * (plus a little, after the worker's tick): the cells come back together, so vehicles glide once
 * per tick, and viewers ask within the same second (nginx answers them from one fetch). */
function useAlignedRefresh() {
  const client = useQueryClient();
  useEffect(() => {
    let timer = 0;
    const schedule = () => {
      const wait = VEHICLES_POLL_MS - (Date.now() % VEHICLES_POLL_MS) + TICK_LAG_MS;
      timer = window.setTimeout(() => {
        if (document.visibilityState === "visible") {
          void client.refetchQueries({ queryKey: ["vehicles"], type: "active" });
          void client.refetchQueries({ queryKey: ["vehicle"], type: "active" });
        }
        schedule();
      }, wait);
    };
    schedule();
    return () => window.clearTimeout(timer);
  }, [client]);
}

/** After the 10 s mark, so the worker's tick is in. */
const TICK_LAG_MS = 1500;

/** The vehicles in view (none for `null`), from the grid cells it covers. */
export function useVehiclesIn(bounds: Bounds | null) {
  useAlignedRefresh();
  const client = useQueryClient();
  // A cell whose answer is full may be missing vehicles: it is asked as quarters instead.
  const full = (key: string) =>
    (client.getQueryData<CellAnswer>(["vehicles", key])?.vehicles.length ?? 0) >= VEHICLES_LIMIT;
  const cells = bounds ? cellsFor(bounds, full) : [];
  const results = useQueries({
    queries: cells.map((c) => ({
      queryKey: ["vehicles", c.key],
      queryFn: async ({ signal }: { signal: AbortSignal }) =>
        unwrap(await api.GET("/api/v1/vehicles", { params: { query: { bbox: c.key } }, signal })),
      staleTime: VEHICLES_POLL_MS - 1000,
      meta: { status: true },
    })),
  });

  // One list per round: published once no cell is still fetching, so a round glides once.
  const fetching = results.some((r) => r.isFetching);
  const key = cells.map((c, i) => `${c.key}@${results[i]?.dataUpdatedAt ?? 0}`).join("|");
  const [merged, setMerged] = useState<{ key: string; vehicles: Positioned[] }>({
    key: "",
    vehicles: [],
  });
  if (!fetching && merged.key !== key) {
    setMerged({ key, vehicles: mergeVehicles(results.map((r) => r.data?.vehicles)) });
  }
  return merged.vehicles;
}

/** A vehicle's detail; polled with the cells (useAlignedRefresh). Until it is in, its row from
 * the cells stands in (registry and reports empty), so a clicked vehicle shows at once. */
export function useVehicle(vehicleId: string | undefined) {
  const client = useQueryClient();
  return useQuery<VehicleDetail>({
    queryKey: ["vehicle", vehicleId],
    queryFn: async ({ signal }) =>
      unwrap(
        await api.GET("/api/v1/vehicles/{vehicle_id}", {
          params: { path: { vehicle_id: vehicleId! } },
          signal,
        }),
      ),
    enabled: Boolean(vehicleId),
    staleTime: VEHICLES_POLL_MS - 1000,
    placeholderData: () => {
      const row = vehicleId ? rowAmong(cellAnswers(client), vehicleId) : undefined;
      return row && standIn(row);
    },
  });
}

function standIn(row: Vehicle): VehicleDetail {
  return {
    vehicle_id: row.vehicle_id,
    vehicle: row,
    registry: null,
    reports: [],
    attributes: [],
    sources: [],
    generated_at: row.last_seen,
    release_id: "",
  };
}
