import "maplibre-gl/dist/maplibre-gl.css";

import { useComputedColorScheme } from "@mantine/core";
import { useNavigate } from "@tanstack/react-router";
import { useEffect, useEffectEvent, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useNow } from "../../lib/hooks";
import { usePhone } from "../../lib/layout";
import { rememberPosition } from "../../lib/position";
import { sheetHeights } from "../../lib/sheet";
import { isPassed } from "../trip/runCalls";
import { hexColour } from "./badges";
import { focusOf, type LngLat } from "./camera";
import { MapController, type MapInput } from "./controller";
import type { Bounds } from "./grid";
import { mergeVehicles, useVehiclesIn } from "./queries";
import { useSelection } from "./selection";
import { unfollow, useMapStore } from "./store";
import { RANK, stopLabels, type TripOverlay } from "./tripLayer";
import classes from "./MapPage.module.css";

/** The map under the app (the shell's, on every tab; on the phone shown on the map tab only):
 * every vehicle in view, and what is open (a board's stop, a trip, a vehicle) marked, brought
 * into view and followed on request. What to show comes from the URL and the queries
 * (selection.ts); the map itself is MapController's. `hidden`: not shown, asks for nothing. */
export function MapStage({ hidden = false }: { hidden?: boolean }) {
  const { opened, stop, trip, paths, vehicleId, vehicle, following } = useSelection();
  const panelsRight = useMapStore((s) => s.panelsRight);
  const snap = useMapStore((s) => s.snap);
  const phone = usePhone();
  const navigate = useNavigate();
  const dark = useComputedColorScheme("light") === "dark";
  const now = useNow(5000);
  const [bounds, setBounds] = useState<Bounds | null>(null);
  const [fontsReady, setFontsReady] = useState(false);
  const cells = useVehiclesIn(hidden ? null : bounds);

  // The open vehicle's own answer joins the cells' (the fresher row wins), so it is drawn and
  // followed even outside the cells in view.
  const vehicles = useMemo(
    () => (vehicle ? mergeVehicles([cells, [vehicle]]) : cells),
    [cells, vehicle],
  );
  const drawn = vehicleId ? vehicles.find((v) => v.vehicle_id === vehicleId) : undefined;
  // Undefined while the vehicle is unknown, null when it has no position now.
  const vehicleAt: LngLat | null | undefined = drawn
    ? [drawn.longitude, drawn.latitude]
    : vehicle === undefined
      ? undefined
      : null;
  const stopAt = useMemo<LngLat | null | undefined>(
    () =>
      !stop
        ? undefined
        : stop.latitude === null || stop.longitude === null
          ? null
          : [stop.longitude, stop.latitude],
    [stop],
  );

  const tripData = trip.data;
  const boardStop = opened.stopId;
  const tripOverlay = useMemo<TripOverlay | null>(() => {
    if (!tripData) return null;
    const calls = tripData.calls.filter(
      (c) => c.latitude !== null && c.longitude !== null && c.boarding !== "none",
    );
    const names = stopLabels(calls.map((c) => c.name));
    const next = calls.findIndex((c) => !isPassed(c, now));
    const stops = calls.map((c, i) => ({
      position: [c.longitude!, c.latitude!] as [number, number],
      name: names[i]!,
      passed: isPassed(c, now),
      rank:
        c.location_id === boardStop || i === next
          ? RANK.focus
          : i === 0 || i === calls.length - 1
            ? RANK.end
            : RANK.other,
    }));
    return { paths: paths ?? [], stops, colour: hexColour(tripData.line.color, "#153438") };
  }, [tripData, paths, now, boardStop]);

  const input: MapInput = {
    dark,
    ready: fontsReady,
    vehicles,
    selectedId: vehicleId,
    trip: tripOverlay,
    stop: opened.kind === "stop" || opened.kind === "trip" ? (stopAt ?? null) : null,
    camera: {
      focus: focusOf({
        key: opened.key,
        kind: opened.kind,
        tripLoaded: Boolean(tripData),
        tripHasVehicle: Boolean(tripData?.vehicles[0]),
        vehicleAt,
        stopAt,
      }),
      selected: vehicleAt ?? null,
      follow: following ? (vehicleAt ?? null) : null,
    },
    panelsRight: phone ? 0 : panelsRight,
    sheetBottom:
      phone && opened.kind === "vehicle" ? (mapHeight) => sheetHeights(mapHeight)[snap] : null,
  };

  // The map's own events, with this render's state.
  const pick = useEffectEvent((vehicleId: string) => {
    void navigate({ to: "/vozidlo/$vehicleId", params: { vehicleId } });
  });
  // A click on no vehicle closes the vehicle panel (not a board or a trip), unless followed.
  const clickEmpty = useEffectEvent(() => {
    if (opened.kind === "vehicle" && !following) void navigate({ to: "/" });
  });
  const initial = useEffectEvent(() => input);

  const container = useRef<HTMLDivElement>(null);
  const controller = useRef<MapController | null>(null);
  useLayoutEffect(() => {
    const map = new MapController(container.current!, initial(), {
      pick: (v) => pick(v.vehicle_id),
      clickEmpty: () => clickEmpty(),
      drag: unfollow,
      settle: setBounds,
      locate: rememberPosition,
      locateStart: unfollow,
    });
    controller.current = map;
    return () => {
      map.destroy();
      controller.current = null;
    };
  }, []);
  useLayoutEffect(() => {
    controller.current?.update(input);
  });

  // Pucks are drawn in Manrope: wait for it once.
  useEffect(() => {
    let live = true;
    document.fonts
      .load("700 20px 'Manrope Variable'")
      .catch(() => undefined)
      .then(() => live && setFontsReady(true));
    return () => {
      live = false;
    };
  }, []);

  return <div ref={container} className={classes.map} />;
}
