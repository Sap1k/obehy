import type { Line, Trip, TripCall } from "../../api/client";

/** A call of the whole run: this trip's calls, then those of the parts it goes on as. */
export interface RunCall {
  call: TripCall;
  /** Set on the first call of a later part: the run goes on as this line from here. */
  continuesAs: Line | null;
  tripId: string;
}

/** The trip's calls followed by its later parts' calls. A part's first call repeats the stop
 * the previous part ends at: the two merge into one, with the departure of the later part. */
export function runCalls(trip: Trip, following: readonly (Trip | undefined)[]): RunCall[] {
  const calls: RunCall[] = trip.calls.map((call) => ({
    call,
    continuesAs: null,
    tripId: trip.trip_id,
  }));
  for (const part of following) {
    if (!part) break;
    let partCalls = part.calls;
    const last = calls[calls.length - 1];
    const first = partCalls[0];
    let merged = false;
    if (last && first && last.call.location_id === first.location_id) {
      last.call = {
        ...last.call,
        scheduled_departure: first.scheduled_departure,
        estimated_departure: first.estimated_departure,
        platform: last.call.platform ?? first.platform,
      };
      partCalls = partCalls.slice(1);
      merged = true;
    }
    const lineChanges = part.line.route_id !== trip.line.route_id;
    partCalls.forEach((call, i) => {
      calls.push({
        call,
        continuesAs: i === 0 && lineChanges ? part.line : null,
        tripId: part.trip_id,
      });
    });
    if (merged && partCalls.length === 0) break;
  }
  return calls;
}

/** The time that counts for a call: its departure, or its arrival at the terminus. */
export function callTimes(call: TripCall): { scheduled: string | null; estimated: string | null } {
  return {
    scheduled: call.scheduled_departure ?? call.scheduled_arrival,
    estimated: call.estimated_departure ?? call.estimated_arrival,
  };
}

/** Passed: the source reports it done, or its time is behind us. */
export function isPassed(call: TripCall, now: number): boolean {
  if (call.status === "actual") return true;
  if (call.status === "cancelled") return false;
  const { scheduled, estimated } = callTimes(call);
  const t = estimated ?? scheduled;
  return t !== null && Date.parse(t) < now - 30_000;
}
