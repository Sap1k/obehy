import type { Schemas, Trip, TripCall } from "../../api/client";

// A trip's calls as the API sends them: each says whether it is passed, and with
// `include=run` the API merges the run's later parts in (`Trip.run`). Here only which call the
// rider opened the trip at, and which time a call shows.

/** A call of the whole run: this trip's calls, then those of the parts it goes on as. */
export type RunCall = Schemas["RunCall"];

/** The run's calls: the API's, else (no run asked) the trip's own. */
export function runCalls(trip: Trip): RunCall[] {
  return (
    trip.run?.calls ??
    trip.calls.map((call) => ({ call, continues_as: null, trip_id: trip.trip_id }))
  );
}

/** The call one boards at: the board row's (`sequence` of the opened trip), else the first at
 * the stop `od` not yet passed, else the first at it. */
export function boardedCall(
  calls: readonly RunCall[],
  tripId: string,
  od: string | undefined,
  sequence: number | undefined,
): RunCall | undefined {
  if (!od) return undefined;
  const at = calls.filter((c) => c.call.location_id === od);
  return (
    at.find((c) => c.trip_id === tripId && c.call.sequence === sequence) ??
    at.find((c) => !c.call.passed) ??
    at[0]
  );
}

/** The time a call shows: its departure, or its arrival at the terminus. */
export function callTimes(call: TripCall): { scheduled: string | null; estimated: string | null } {
  return {
    scheduled: call.scheduled_departure ?? call.scheduled_arrival,
    estimated: call.estimated_departure ?? call.estimated_arrival,
  };
}
