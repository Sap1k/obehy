import type { Departure, Trip } from "../api/client";

// Whether one can board in a wheelchair: what the timetable states for the whole trip, unless
// the vehicle on it is known. A known step-free vehicle confirms it (a booking the timetable
// asks for still stands); a vehicle known to have steps overrides the timetable.

export type StepFree = "accessible" | "booking" | "partly";

type Traits = NonNullable<NonNullable<Departure["vehicle"]>["traits"]>;

export function stepFree(
  timetable: Trip["amenities"]["wheelchair"] | undefined,
  traits: Traits | null | undefined,
): StepFree | null {
  const stated: StepFree | null =
    timetable === "booking_required"
      ? "booking"
      : timetable === "partly"
        ? "partly"
        : timetable
          ? "accessible"
          : null;
  const known = [traits?.low_floor, traits?.wheelchair_accessible].filter(
    (v): v is boolean => v !== null && v !== undefined,
  );
  if (known.length === 0) return stated;
  if (!known.includes(true)) return null;
  return stated === "booking" ? "booking" : "accessible";
}

export const STEP_FREE_LABEL: Record<StepFree, string> = {
  accessible: "Bezbariérový",
  booking: "Bezbariérový, nutno objednat",
  partly: "Částečně bezbariérový, nutná dopomoc",
};

/** The timetable sign that says it (JDF @, KADR 34, JDF {). */
export const STEP_FREE_SIGN: Record<StepFree, string> = {
  accessible: "jdf:@",
  booking: "czptt_note:34",
  partly: "jdf:{",
};
