import "maplibre-gl/dist/maplibre-gl.css";

import { MapboxOverlay } from "@deck.gl/mapbox";
import { useComputedColorScheme } from "@mantine/core";
import { useMediaQuery, useNetwork } from "@mantine/hooks";
import { Outlet, useNavigate, useParams } from "@tanstack/react-router";
import * as maplibregl from "maplibre-gl";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { BottomSheet, type SheetSnap } from "../../components/BottomSheet";
import { freshnessOf, reportFreshness } from "../../lib/freshness";
import { useNow } from "../../lib/hooks";
import { useTrip } from "../trip/queries";
import { isPassed } from "../trip/runCalls";
import { MapContext, type MapState } from "./context";
import { AREA, type Bounds } from "./grid";
import { GLIDE_MS, drawnPositions, positionsAt, progress, startGlide, type Glide } from "./motion";
import { mergeVehicles, useVehicle, useVehiclesIn, type Positioned } from "./queries";
import { atlasWith, hexColour, puckKey } from "./badges";
import { mapStyle } from "./style";
import type { Vehicle } from "../../api/client";
import { PUCK_ZOOM, vehicleLayers, type TripOverlay } from "./VehicleLayers";
import classes from "./MapPage.module.css";

// The panel loads with the map (one chunk), so a click opens it without another download.
export { VehiclePanel } from "./VehiclePanel";

const VIEW_KEY = "obehy.map.view";
const DEFAULT_VIEW = { lng: 15.45, lat: 49.8, zoom: 6.6 };
/** A vehicle opened from a link is shown at least this close. */
const OPEN_ZOOM = 14;
/** The tokens' action colour, for the selection halo. */
const ACCENT = { light: "#246568", dark: "#c5ed62" };

function savedView() {
  try {
    const v = JSON.parse(localStorage.getItem(VIEW_KEY) ?? "null") as typeof DEFAULT_VIEW | null;
    if (v && [v.lng, v.lat, v.zoom].every(Number.isFinite)) return v;
  } catch {
    // Storage blocked or unreadable: the default view.
  }
  return DEFAULT_VIEW;
}

function saveView(map: maplibregl.Map) {
  try {
    const c = map.getCenter();
    localStorage.setItem(VIEW_KEY, JSON.stringify({ lng: c.lng, lat: c.lat, zoom: map.getZoom() }));
  } catch {
    // Not remembered.
  }
}

function boundsOf(map: maplibregl.Map): Bounds {
  const b = map.getBounds();
  return { west: b.getWest(), south: b.getSouth(), east: b.getEast(), north: b.getNorth() };
}

/** Which layer set a zoom draws, so layers are rebuilt only when it changes. */
const zoomBand = (z: number) => (z >= PUCK_ZOOM ? 2 : z >= 9 ? 1 : 0);

/** The map (home tab): every vehicle in view, drawn by deck.gl into MapLibre's canvas; a vehicle
 * opened in a sidebar (web) or a bottom sheet (phone), and followed on request. */
export function MapPage() {
  const params = useParams({ strict: false });
  const vehicleId = (params as { vehicleId?: string }).vehicleId;
  const navigate = useNavigate();
  const phone = useMediaQuery("(max-width: 48em)", false, { getInitialValueInEffect: false });
  const dark = useComputedColorScheme("light") === "dark";
  const network = useNetwork();
  const now = useNow(5000);

  const container = useRef<HTMLDivElement>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const overlayRef = useRef<MapboxOverlay | null>(null);
  /** The scheme the map's style was made for. */
  const appliedDark = useRef<boolean | null>(null);
  const [bounds, setBounds] = useState<Bounds | null>(null);
  // The vehicle followed: following ends when another opens or the panel closes.
  const [followedId, setFollowedId] = useState<string | null>(null);
  const [snap, setSnap] = useState<SheetSnap>("half");
  const [fontsReady, setFontsReady] = useState(false);

  const cells = useVehiclesIn(bounds);
  const detail = useVehicle(vehicleId);
  // The open vehicle: its own answer once it is in (null when it has no position now), until
  // then the row it was clicked on, so the panel and its trip start at once.
  const own = detail.data && detail.data.vehicle_id === vehicleId ? detail.data.vehicle : undefined;
  const listed = vehicleId ? cells.vehicles.find((v) => v.vehicle_id === vehicleId) : undefined;
  const selected: Vehicle | null = !vehicleId ? null : own !== undefined ? own : (listed ?? null);
  const trip = useTrip(selected?.trip_id ?? undefined, selected?.service_date ?? undefined, true);
  // A vehicle that lost its position is not followed.
  const following = Boolean(vehicleId) && followedId === vehicleId && selected !== null;

  // What the map's own handlers need of the latest render.
  const live = useRef({ following, vehicleId, stopFollowing: () => {} });
  useEffect(() => {
    live.current = { following, vehicleId, stopFollowing: () => setFollowedId(null) };
  });

  // The open vehicle's own answer joins the cells' (the fresher row wins), so it is drawn and
  // followed even outside the cells in view.
  const vehicles = useMemo(
    () => (own ? mergeVehicles([cells.vehicles, [own]]) : cells.vehicles),
    [cells.vehicles, own],
  );
  const drawnSelected = vehicleId ? vehicles.find((v) => v.vehicle_id === vehicleId) : undefined;

  const tripOverlay = useMemo<TripOverlay | null>(() => {
    const data = trip.data;
    if (!data) return null;
    const g = data.shape as { type?: string; coordinates?: unknown } | null | undefined;
    const paths =
      g?.type === "LineString"
        ? [g.coordinates as [number, number][]]
        : g?.type === "MultiLineString"
          ? (g.coordinates as [number, number][][])
          : [];
    const stops = data.calls.flatMap((c) =>
      c.latitude !== null && c.longitude !== null && c.boarding !== "none"
        ? [{ position: [c.longitude, c.latitude] as [number, number], passed: isPassed(c, now) }]
        : [],
    );
    return { paths, stops, colour: hexColour(data.line.color, "#153438") };
  }, [trip.data, now]);

  // Drawing state lives in refs: a glide redraws every frame without React.
  const draw = useRef({
    vehicles: [] as Positioned[],
    keys: [] as string[],
    glide: null as Glide | null,
    positions: new Float64Array(0),
    frame: 0,
    raf: 0,
  });
  const props = useRef({
    zoom: DEFAULT_VIEW.zoom,
    selectedId: undefined as string | undefined,
    trip: null as TripOverlay | null,
    accent: ACCENT.light,
    ready: false,
  });

  const render = useCallback(() => {
    const overlay = overlayRef.current;
    const d = draw.current;
    const p = props.current;
    if (!overlay || !p.ready) return;
    overlay.setProps({
      layers: vehicleLayers({
        vehicles: d.vehicles,
        keys: d.keys,
        atlas: atlasWith(d.keys),
        positions: d.positions,
        frame: d.frame,
        zoom: p.zoom,
        selectedId: p.selectedId,
        trip: p.trip,
        accent: p.accent,
        onPick: (v) =>
          void navigate({ to: "/vozidlo/$vehicleId", params: { vehicleId: v.vehicle_id } }),
      }),
    });
  }, [navigate]);

  // The map, once.
  useEffect(() => {
    const el = container.current;
    if (!el) return;
    const view = savedView();
    const map = new maplibregl.Map({
      container: el,
      style: mapStyle(dark),
      center: [view.lng, view.lat],
      zoom: view.zoom,
      minZoom: 5,
      maxBounds: [
        [AREA.west - 3, AREA.south - 1.5],
        [AREA.east + 3, AREA.north + 1.5],
      ],
      dragRotate: false,
      pitchWithRotate: false,
      touchPitch: false,
      attributionControl: { compact: true },
    });
    appliedDark.current = dark;
    map.touchZoomRotate.disableRotation();
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
    const overlay = new MapboxOverlay({
      interleaved: true,
      layers: [],
      pickingRadius: 6,
      onHover: (info) => {
        map.getCanvas().style.cursor = info.object ? "pointer" : "";
      },
      onClick: (info) => {
        // A click on no vehicle closes the open one, unless it is followed.
        if (!info.object && !live.current.following && live.current.vehicleId) {
          void navigate({ to: "/" });
        }
      },
    });
    map.addControl(overlay);
    mapRef.current = map;
    overlayRef.current = overlay;

    const onMoveEnd = () => {
      setBounds(boundsOf(map));
      saveView(map);
    };
    const onZoom = () => {
      const z = map.getZoom();
      const changed = zoomBand(z) !== zoomBand(props.current.zoom);
      props.current.zoom = z;
      if (changed) render();
    };
    const stopFollowing = (e: { originalEvent?: unknown }) => {
      if (e.originalEvent) live.current.stopFollowing();
    };
    map.on("load", onMoveEnd);
    map.on("moveend", onMoveEnd);
    map.on("zoom", onZoom);
    map.on("dragstart", stopFollowing);
    props.current.zoom = map.getZoom();

    const resize = new ResizeObserver(() => map.resize());
    resize.observe(el);
    const d = draw.current;
    return () => {
      resize.disconnect();
      cancelAnimationFrame(d.raf);
      map.remove();
      mapRef.current = null;
      overlayRef.current = null;
    };
    // The scheme and handlers are applied by their own effects.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

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

  // The colour scheme: the base map's palette and the halo.
  useEffect(() => {
    props.current.accent = dark ? ACCENT.dark : ACCENT.light;
    const map = mapRef.current;
    if (map && appliedDark.current !== dark) {
      appliedDark.current = dark;
      map.setStyle(mapStyle(dark));
    }
    render();
  }, [dark, render]);

  useEffect(() => {
    props.current.ready = fontsReady;
    props.current.selectedId = vehicleId;
    props.current.trip = vehicleId ? tripOverlay : null;
    render();
  }, [fontsReady, vehicleId, tripOverlay, render]);

  // Each new answer glides every vehicle from where it is drawn to where it is reported.
  useEffect(() => {
    const d = draw.current;
    const t0 = performance.now();
    const drawn = d.glide ? drawnPositions(d.glide, d.positions) : new Map();
    d.glide = startGlide(
      drawn,
      vehicles.map((v) => ({ id: v.vehicle_id, position: [v.longitude, v.latitude] })),
      t0,
    );
    d.vehicles = vehicles;
    d.keys = vehicles.map(puckKey);
    d.positions = new Float64Array(vehicles.length * 2);
    cancelAnimationFrame(d.raf);
    const step = () => {
      const glide = d.glide!;
      const t = progress(glide, performance.now());
      positionsAt(glide, t, d.positions);
      d.frame += 1;
      render();
      if (t < 1) d.raf = requestAnimationFrame(step);
    };
    step();
  }, [vehicles, render]);

  // Following: the camera moves with the vehicle, over the same second as its glide, and
  // zooming keeps it in the middle.
  const setFollowing = useCallback(
    (on: boolean) => setFollowedId(on && vehicleId ? vehicleId : null),
    [vehicleId],
  );
  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    const around = following ? { around: "center" as const } : undefined;
    map.scrollZoom.disable();
    map.scrollZoom.enable(around);
    map.touchZoomRotate.disable();
    map.touchZoomRotate.enable(around);
    map.touchZoomRotate.disableRotation();
  }, [following]);

  const lat = drawnSelected?.latitude ?? null;
  const lon = drawnSelected?.longitude ?? null;
  useEffect(() => {
    const map = mapRef.current;
    if (!following || !map || lat === null || lon === null) return;
    map.easeTo({ center: [lon, lat], duration: GLIDE_MS, easing: (t: number) => t });
  }, [following, lat, lon]);

  // A vehicle opened from a link, or out of view, is brought into view once.
  const shownFor = useRef<string | null>(null);
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !vehicleId || lat === null || lon === null || shownFor.current === vehicleId) {
      return;
    }
    shownFor.current = vehicleId;
    if (!map.getBounds().contains([lon, lat])) {
      map.jumpTo({ center: [lon, lat], zoom: Math.max(map.getZoom(), OPEN_ZOOM) });
    }
  }, [vehicleId, lat, lon]);
  useEffect(() => {
    if (!vehicleId) shownFor.current = null;
  }, [vehicleId]);

  // The phone's sheet covers the bottom of the map: centring leaves it out.
  const onSheetHeight = useCallback((px: number) => {
    mapRef.current?.setPadding({ top: 0, right: 0, left: 0, bottom: px });
  }, []);
  useEffect(() => {
    if (!vehicleId || !phone) mapRef.current?.setPadding({ top: 0, right: 0, left: 0, bottom: 0 });
  }, [vehicleId, phone]);

  // The shell's data status follows the vehicles' answers.
  const freshness = cells.generatedAt
    ? freshnessOf({
        failing: cells.failing,
        online: network.online,
        generatedAt: cells.generatedAt,
        anyStale: false,
        now,
      })
    : null;
  useEffect(() => {
    reportFreshness(freshness);
  }, [freshness]);
  useEffect(() => () => reportFreshness(null), []);

  const state: MapState = { vehicle: selected, following, setFollowing, phone, snap, setSnap };
  const open = Boolean(vehicleId);

  return (
    <MapContext.Provider value={state}>
      <div className={classes.root} data-open={open && !phone ? true : undefined}>
        <div ref={container} className={classes.map} />
        {open &&
          (phone ? (
            <BottomSheet snap={snap} onSnap={setSnap} onHeight={onSheetHeight}>
              <Outlet />
            </BottomSheet>
          ) : (
            <aside className={classes.panel}>
              <Outlet />
            </aside>
          ))}
      </div>
    </MapContext.Provider>
  );
}
