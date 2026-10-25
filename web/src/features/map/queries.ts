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

/** When a vehicle's position was fixed (its report's time when the answer gives none). */
const fixedAt = (v: Vehicle): number => Date.parse(v.position_at ?? v.last_seen);

/** One row per vehicle across cells: one on a cell's edge comes in both, the newer fix kept.
 * A vehicle `held` (drawn from an earlier answer) at a newer fix keeps that position: an answer
 * older than one already drawn (a proxy's stale copy, a cell's cached answer after a pan or a
 * zoom) would draw it back along its way, and the next answer forward again. */
export function mergeVehicles(
  answers: readonly (readonly Vehicle[] | undefined)[],
  held: readonly Positioned[] = [],
): Positioned[] {
  const byId = new Map<string, Positioned>();
  for (const vehicles of answers) {
    for (const v of vehicles ?? []) {
      if (!placed(v)) continue;
      const had = byId.get(v.vehicle_id);
      if (!had || fixedAt(v) > fixedAt(had)) byId.set(v.vehicle_id, v);
    }
  }
  for (const was of held) {
    const now = byId.get(was.vehicle_id);
    if (now && fixedAt(was) > fixedAt(now)) {
      byId.set(was.vehicle_id, {
        ...now,
        latitude: was.latitude,
        longitude: was.longitude,
        bearing: was.bearing,
        position_at: was.position_at,
      });
    }
  }
  return [...byId.values()];
}

/** A vehicle's newest fix among the cell answers (the one it was clicked on). */
export function rowAmong(
  answers: readonly (CellAnswer | undefined)[],
  vehicleId: string,
): Vehicle | undefined {
  let best: Vehicle | undefined;
  for (const answer of answers) {
    for (const v of answer?.vehicles ?? []) {
      if (v.vehicle_id !== vehicleId) continue;
      if (!best || fixedAt(v) > fixedAt(best)) best = v;
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
    setMerged({
      key,
      vehicles: mergeVehicles(
        results.map((r) => r.data?.vehicles),
        merged.vehicles,
      ),
    });
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
