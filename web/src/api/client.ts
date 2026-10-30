import createClient from "openapi-fetch";
import type { components, paths } from "./schema";

export type Schemas = components["schemas"];
export type Departure = Schemas["Departure"];
export type Departures = Schemas["Departures"];
export type Arrivals = Schemas["Arrivals"];
export type Trip = Schemas["Trip"];
export type TripCall = Schemas["TripCall"];
export type TripVehicle = Schemas["TripVehicle"];
export type Vehicle = Schemas["Vehicle"];
export type VehicleDetail = Schemas["VehicleDetail"];
export type VehicleAttributeValue = Schemas["VehicleAttributeValue"];
export type Line = Schemas["Line"];
export type Stop = Schemas["Stop"];
export type GroupMember = Schemas["GroupMember"];
export type StopPlace = Schemas["StopPlace"];
export type Alert = Schemas["Alert"];
export type Problem = Schemas["Problem"];
export type TripHistory = Schemas["TripHistory"];
export type Circulations = Schemas["Circulations"];
export type CirculationScopes = Schemas["CirculationScopes"];
export type ScopedChain = Schemas["ScopedChain"];
export type ScopeRoute = Schemas["ScopeRoute"];
export type ChainJourney = Schemas["ChainJourney"];

/** Same origin: nginx in production, Vite's proxy in development. */
export const api = createClient<paths>({ baseUrl: "" });

/** An answer that is not 2xx, with the API's problem+json when it sent one. */
export class ApiError extends Error {
  readonly status: number;
  readonly problem: Problem | null;

  constructor(status: number, problem: Problem | null) {
    super(problem?.detail ?? problem?.title ?? `HTTP ${status}`);
    this.status = status;
    this.problem = problem;
  }
}

/** The answer's data, or an ApiError. A network failure rejects as the fetch's TypeError. */
export function unwrap<T>(result: { data?: T; error?: unknown; response: Response }): T {
  if (result.data !== undefined && result.response.ok) return result.data;
  const problem = (result.error ?? null) as Problem | null;
  throw new ApiError(result.response.status, problem);
}
