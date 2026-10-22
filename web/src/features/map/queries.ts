import { useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { api, unwrap, type Vehicle } from "../../api/client";
import { VEHICLES_LIMIT, cellsFor, type Bounds } from "./grid";

/** Vehicles poll at the worker's tick. */
export const VEHICLES_POLL_MS = 10_000;

export interface Positioned extends Vehicle {
  latitude: number;
  longitude: number;
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

/** The vehicles in view, from the grid cells it covers. */
export function useVehiclesIn(bounds: Bounds | null) {
  useAlignedRefresh();
  const [split, setSplit] = useState<ReadonlySet<string>>(() => new Set());
  const cells = useMemo(() => (bounds ? cellsFor(bounds, split) : []), [bounds, split]);
  const results = useQueries({
    queries: cells.map((c) => ({
      queryKey: ["vehicles", c.key],
      queryFn: async ({ signal }: { signal: AbortSignal }) =>
        unwrap(await api.GET("/api/v1/vehicles", { params: { query: { bbox: c.key } }, signal })),
      staleTime: VEHICLES_POLL_MS - 1000,
    })),
  });

  // A full answer may be missing vehicles: that cell is asked again as quarters.
  const full = cells.filter((_, i) => (results[i]?.data?.vehicles.length ?? 0) >= VEHICLES_LIMIT);
  if (full.length > 0) setSplit(new Set([...split, ...full.map((c) => c.key)]));

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

  const generatedAt = results.reduce<string | null>((latest, r) => {
    const at = r.data?.generated_at ?? null;
    return at && (!latest || at > latest) ? at : latest;
  }, null);
  return {
    vehicles: merged.vehicles,
    generatedAt,
    failing: results.some((r) => r.isError),
  };
}

/** A vehicle's detail; polled with the cells (useAlignedRefresh). */
export function useVehicle(vehicleId: string | undefined) {
  return useQuery({
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
  });
}
