import {
  createRootRoute,
  createRoute,
  createRouter,
  lazyRouteComponent,
} from "@tanstack/react-router";
import { BoardPage } from "./features/board/BoardPage";
import { TripPanel } from "./features/trip/TripPanel";
import { Shell } from "./shell/Shell";
import { AlertsPage, DeparturesIndex, LinesPage } from "./shell/pages";

// Czech paths match the tabs. A board keeps its scope in the URL (?smer=prijezdy&cas=…), and a
// trip opened from it is its child, with the stop boarded at (?od=…). The map is a layout that
// stays mounted while a vehicle opens on it (/vozidlo/…); MapLibre and deck.gl load with it.

export interface BoardSearch {
  smer?: "prijezdy";
  cas?: string;
}

export interface TripSearch extends BoardSearch {
  od?: string;
}

const text = (v: unknown): string | undefined => (typeof v === "string" && v ? v : undefined);

const rootRoute = createRootRoute({ component: Shell });

const mapRoute = createRoute({
  getParentRoute: () => rootRoute,
  id: "map",
  component: lazyRouteComponent(() => import("./features/map/MapPage"), "MapPage"),
});

const mapIndexRoute = createRoute({
  getParentRoute: () => mapRoute,
  path: "/",
  component: () => null,
});

const vehicleRoute = createRoute({
  getParentRoute: () => mapRoute,
  path: "/vozidlo/$vehicleId",
  component: lazyRouteComponent(() => import("./features/map/MapPage"), "VehiclePanel"),
});

const departuresRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/odjezdy",
  component: DeparturesIndex,
});

const boardRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/odjezdy/$stopId",
  component: BoardPage,
  validateSearch: (search: Record<string, unknown>): BoardSearch => ({
    smer: search.smer === "prijezdy" ? "prijezdy" : undefined,
    cas: text(search.cas),
  }),
});

const tripRoute = createRoute({
  getParentRoute: () => boardRoute,
  path: "/spoj/$tripId/$date",
  component: TripPanel,
  validateSearch: (search: Record<string, unknown>): TripSearch => ({
    smer: search.smer === "prijezdy" ? "prijezdy" : undefined,
    cas: text(search.cas),
    od: text(search.od),
  }),
});

const linesRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/linky",
  component: LinesPage,
});

const alertsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/mimoradnosti",
  component: AlertsPage,
});

const routeTree = rootRoute.addChildren([
  mapRoute.addChildren([mapIndexRoute, vehicleRoute]),
  departuresRoute,
  boardRoute.addChildren([tripRoute]),
  linesRoute,
  alertsRoute,
]);

// Navigations run as view transitions: a panel that closes slides out (theme/motion.css).
export const router = createRouter({
  routeTree,
  defaultViewTransition: true,
  defaultPreload: "intent",
  scrollRestoration: true,
});

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}
