import type { Schemas } from "../api/client";

// Whether one can board in a wheelchair is the API's (`step_free`: the timetable's statement
// unless the vehicle on it is known); here how it reads and the sign that says it.

export type StepFree = NonNullable<Schemas["Departure"]["step_free"]>;

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
