import { MapboxOverlay } from "@deck.gl/mapbox";
import * as maplibregl from "maplibre-gl";
import { atlasWith, puckKey, resetAtlas } from "./badges";
import {
  afterGesture,
  initialCamera,
  planCamera,
  type CameraInput,
  type CameraMove,
  type CameraState,
  type Insets,
  type LngLat,
} from "./camera";
import { AREA, type Bounds } from "./grid";
import { drawnPositions, positionsAt, progress, startGlide, type Glide } from "./motion";
import type { Positioned } from "./queries";
import { TRIP_ARROW, TRIP_SOURCE, TRIP_STOP_LAYERS, mapStyle } from "./style";
import { arrowImage, tripFeatures, type TripOverlay } from "./tripLayer";
import { PUCK_ZOOM, vehicleLayers } from "./VehicleLayers";

// The map outside React: MapLibre (the base map and the open trip) with deck.gl drawing the
// vehicles into its canvas, the vehicles' glide
// (every frame, without React), and the camera (camera.ts decides, this applies). React gives
// it what to show with `update`, every render; it compares and does only what changed.
//
// A phone's browser drops the WebGL context of a page in the background. MapLibre then destroys
// its style (`getSource` and the like throw until it is back) and cannot bring back deck.gl's
// layer, so while the context is lost the controller only takes the input, and once it is back
// (or the page shows again without it) it asks to be rebuilt.

const VIEW_KEY = "obehy.map.view";
const DEFAULT_VIEW = { lng: 15.45, lat: 49.8, zoom: 6.6 };
/** The map's own controls in Czech. */
const LOCALE = {
  "GeolocateControl.FindMyLocation": "Moje poloha",
  "GeolocateControl.LocationNotAvailable": "Poloha není k dispozici",
  "NavigationControl.ZoomIn": "Přiblížit",
  "NavigationControl.ZoomOut": "Oddálit",
  "AttributionControl.ToggleAttribution": "Zdroje mapy",
};
/** The tokens' action colour, for the selection ring. */
const ACCENT = { light: "#246568", dark: "#c5ed62" };
/** A page shown again with its context still lost waits this long for the browser to restore
 * it before the map is rebuilt. */
const RESTORE_WAIT_MS = 1000;
/** A point this close to a panel's edge counts as covered. */
const MARGIN_PX = 32;
/** A show across the map flies this long; one nearby eases this long. */
const FLY_MS = 1600;
const NEAR_MS = 600;

export interface MapInput {
  dark: boolean;
  /** The pucks' font has loaded: vehicles are drawn from then on. */
  ready: boolean;
  vehicles: Positioned[];
  selectedId: string | undefined;
  trip: TripOverlay | null;
  /** A board's stop place, [lon, lat]. */
  stop: LngLat | null;
  camera: Omit<CameraInput, "insets">;
  /** How far the desktop's panels reach from the map's left edge. */
  panelsRight: number;
  /** The phone's sheet height over the map's bottom, from the map's own height. */
  sheetBottom: ((mapHeight: number) => number) | null;
}

export interface MapHandlers {
  pick(vehicle: Positioned): void;
  /** A click on no vehicle and no stop of the open trip. */
  clickEmpty(): void;
  /** A click on a stop of the open trip (its dot or its name): the stop place's ID. */
  openStop(stopId: string): void;
  /** The reader dragged the map (following ends). */
  drag(): void;
  /** The map came to rest showing these bounds. */
  settle(bounds: Bounds): void;
  /** The browser's position, for the search's distances. */
  locate(latitude: number, longitude: number): void;
  /** The map's "Moje poloha" started keeping the map on the reader. */
  locateStart(): void;
  /** The map lost its WebGL context and cannot recover in place: build a new one. */
  rebuild(): void;
}

interface CameraLogEntry {
  at: number;
  move: CameraMove["kind"];
  center?: LngLat;
}

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

export class MapController {
  private readonly map: maplibregl.Map;
  private readonly overlay: MapboxOverlay;
  private readonly resize: ResizeObserver;
  private input: MapInput;
  private dark: boolean;
  private zoom: number;
  // The glide: positions drawn now, advanced every frame.
  private drawn: Positioned[] = [];
  private keys: string[] = [];
  private glide: Glide | null = null;
  private positions = new Float64Array(0);
  private frame = 0;
  private raf = 0;
  /** The WebGL context is lost: the map has no style and must not be touched. */
  private lost = false;
  private restoreTimer = 0;
  // The camera.
  private camera: CameraState = initialCamera();
  private flying = false;
  private applying = false;
  private followZoom: boolean | null = null;
  /** Development only: the moves applied, newest last (window.__obehyCamera). */
  private readonly log: CameraLogEntry[] = [];

  constructor(
    container: HTMLElement,
    input: MapInput,
    private readonly handlers: MapHandlers,
  ) {
    this.input = input;
    this.dark = input.dark;
    // A map built anew after a lost context: the atlas's canvas may have been wiped with it.
    resetAtlas();
    const view = savedView();
    const map = new maplibregl.Map({
      container,
      style: mapStyle(input.dark),
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
      locale: LOCALE,
    });
    this.map = map;
    this.zoom = map.getZoom();
    map.touchZoomRotate.disableRotation();
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
    // Where one is: the browser's position (it never leaves the browser), the map kept on it
    // until one pans. Keeping the map on oneself and on a vehicle exclude each other.
    const locate = new maplibregl.GeolocateControl({
      positionOptions: { enableHighAccuracy: true, maximumAge: 10_000, timeout: 15_000 },
      trackUserLocation: true,
      showAccuracyCircle: true,
      fitBoundsOptions: { maxZoom: 15 },
    });
    locate.on("trackuserlocationstart", () => handlers.locateStart());
    locate.on("geolocate", (e: GeolocationPosition) =>
      handlers.locate(e.coords.latitude, e.coords.longitude),
    );
    map.addControl(locate, "top-right");
    this.overlay = new MapboxOverlay({
      interleaved: true,
      layers: [],
      pickingRadius: 6,
      onHover: (info) => {
        const stop = info.object ? undefined : this.tripStopAt(info.x, info.y);
        map.getCanvas().style.cursor = info.object || stop ? "pointer" : "";
      },
      onClick: (info) => {
        if (info.object) return;
        const stop = this.tripStopAt(info.x, info.y);
        if (stop) handlers.openStop(stop);
        else handlers.clickEmpty();
      },
    });
    map.addControl(this.overlay);

    // A new style (the colour scheme) starts with an empty trip and no arrow.
    map.on("style.load", () => this.drawTrip());
    map.on("styleimagemissing", (e: { id: string }) => {
      if (e.id === TRIP_ARROW) map.addImage(TRIP_ARROW, arrowImage(), { pixelRatio: 2 });
    });
    map.on("load", this.onMoveEnd);
    map.on("moveend", this.onMoveEnd);
    map.on("movestart", this.onMoveStart);
    map.on("dragstart", this.onDragStart);
    map.on("zoom", this.onZoom);
    map.on("webglcontextlost", () => {
      this.lost = true;
      cancelAnimationFrame(this.raf);
    });
    map.on("webglcontextrestored", () => handlers.rebuild());
    document.addEventListener("visibilitychange", this.onVisibility);
    this.resize = new ResizeObserver(() => {
      if (this.lost) return;
      map.resize();
      this.plan();
    });
    this.resize.observe(container);
    if (import.meta.env.DEV) {
      Object.assign(window, { __obehyCamera: this.log, __obehyMap: this });
    }
    this.update(input, true);
  }

  update(input: MapInput, first = false): void {
    const was = this.input;
    this.input = input;
    if (this.lost) return;
    if (input.dark !== this.dark) {
      this.dark = input.dark;
      this.map.setStyle(mapStyle(input.dark));
    }
    if (first || input.trip !== was.trip) this.drawTrip();
    if (first || input.vehicles !== was.vehicles) this.startGlide(input.vehicles);
    else if (
      input.ready !== was.ready ||
      input.dark !== was.dark ||
      input.selectedId !== was.selectedId ||
      input.stop !== was.stop
    ) {
      this.render();
    }
    this.plan();
  }

  destroy(): void {
    this.resize.disconnect();
    cancelAnimationFrame(this.raf);
    clearTimeout(this.restoreTimer);
    document.removeEventListener("visibilitychange", this.onVisibility);
    try {
      this.map.remove();
    } catch {
      // A map whose context is gone may fail to tear down: clear its container by hand.
      const container = this.map.getContainer();
      container.replaceChildren();
      container.classList.remove("maplibregl-map");
    }
  }

  /** Shown again with the context still lost: give the browser a moment, then rebuild. */
  private onVisibility = () => {
    clearTimeout(this.restoreTimer);
    if (!this.lost || document.visibilityState !== "visible") return;
    this.restoreTimer = window.setTimeout(() => {
      if (this.lost) this.handlers.rebuild();
    }, RESTORE_WAIT_MS);
  };

  // Drawing.

  private render(): void {
    const input = this.input;
    if (!input.ready || this.lost) return;
    // The pucks drawn: all of them at street zoom, otherwise only the selected vehicle's (the
    // country's dots would fill the atlas with pucks no one sees).
    const selected = this.drawn.findIndex((v) => v.vehicle_id === input.selectedId);
    const shown = this.zoom >= PUCK_ZOOM ? this.keys : selected >= 0 ? [this.keys[selected]!] : [];
    this.overlay.setProps({
      layers: vehicleLayers({
        vehicles: this.drawn,
        keys: this.keys,
        atlas: atlasWith(shown),
        positions: this.positions,
        frame: this.frame,
        zoom: this.zoom,
        selectedId: input.selectedId,
        stop: input.stop as [number, number] | null,
        accent: input.dark ? ACCENT.dark : ACCENT.light,
        onPick: (v) => this.handlers.pick(v),
      }),
    });
  }

  /** The open trip's stop (its dot or its name) at a point on the map, if any. */
  private tripStopAt(x: number, y: number): string | undefined {
    if (!this.input.trip || !this.map.getLayer(TRIP_STOP_LAYERS[0])) return undefined;
    const hit = this.map.queryRenderedFeatures([x, y], { layers: TRIP_STOP_LAYERS });
    const id: unknown = hit[0]?.properties.id;
    return typeof id === "string" ? id : undefined;
  }

  /** The open trip into MapLibre's source (absent until the style has loaded). */
  private drawTrip(): void {
    const source = this.map.getSource<maplibregl.GeoJSONSource>(TRIP_SOURCE);
    source?.setData(tripFeatures(this.input.trip));
  }

  /** Each new answer glides every vehicle from where it is drawn to where it is reported. */
  private startGlide(vehicles: Positioned[]): void {
    const drawn = this.glide ? drawnPositions(this.glide, this.positions) : new Map();
    this.glide = startGlide(
      drawn,
      vehicles.map((v) => ({ id: v.vehicle_id, position: [v.longitude, v.latitude] })),
      performance.now(),
    );
    this.drawn = vehicles;
    this.keys = vehicles.map(puckKey);
    this.positions = new Float64Array(vehicles.length * 2);
    cancelAnimationFrame(this.raf);
    const step = () => {
      const glide = this.glide!;
      const t = progress(glide, performance.now());
      positionsAt(glide, t, this.positions);
      this.frame += 1;
      this.render();
      if (t < 1) this.raf = requestAnimationFrame(step);
    };
    step();
  }

  private onZoom = () => {
    if (this.lost) return;
    const z = this.map.getZoom();
    const changed = zoomBand(z) !== zoomBand(this.zoom);
    this.zoom = z;
    if (changed) this.render();
  };

  // The camera.

  private insets(): Insets {
    const { panelsRight, sheetBottom } = this.input;
    const height = this.map.getContainer().clientHeight;
    return { left: panelsRight, bottom: sheetBottom ? sheetBottom(height) : 0 };
  }

  private covered = (at: LngLat, insets: Insets): boolean => {
    const p = this.map.project(at as [number, number]);
    const { clientWidth: w, clientHeight: h } = this.map.getContainer();
    return !(
      p.x >= insets.left + MARGIN_PX &&
      p.x <= w - MARGIN_PX &&
      p.y >= MARGIN_PX &&
      p.y <= h - insets.bottom - MARGIN_PX
    );
  };

  /** Decide and apply the camera's next move (camera.ts). */
  private plan(): void {
    if (this.applying || this.lost) return;
    const following = this.input.camera.follow !== null;
    if (following !== this.followZoom) {
      // Following: zooming keeps the vehicle in the middle.
      this.followZoom = following;
      const around = following ? { around: "center" as const } : undefined;
      this.map.scrollZoom.disable();
      this.map.scrollZoom.enable(around);
      this.map.touchZoomRotate.disable();
      this.map.touchZoomRotate.enable(around);
      this.map.touchZoomRotate.disableRotation();
    }
    const { state, move } = planCamera(
      this.camera,
      { ...this.input.camera, insets: this.insets() },
      {
        moving: this.map.isMoving(),
        flying: this.flying,
        zoom: this.map.getZoom(),
        covered: this.covered,
      },
    );
    this.camera = state;
    if (move) this.apply(move);
  }

  private apply(move: CameraMove): void {
    const map = this.map;
    if (import.meta.env.DEV) {
      this.log.push({
        at: Math.round(performance.now()),
        move: move.kind,
        center: move.kind === "pad" ? undefined : move.center,
      });
      if (this.log.length > 100) this.log.shift();
    }
    // Starting a move ends the one under way, which fires its moveend: that must not plan again
    // in the middle of this one.
    this.applying = true;
    try {
      switch (move.kind) {
        case "fly": {
          // Near: an ease. Far: a flight (out and back in) of a fixed length; MapLibre's
          // `maxDuration` would not shorten a long one but jump instead.
          const target = { center: move.center as [number, number], zoom: move.zoom };
          const p = map.project(target.center);
          const { clientWidth: w, clientHeight: h } = map.getContainer();
          const far = Math.hypot(p.x - w / 2, p.y - h / 2) > 2 * Math.max(w, h);
          if (far) map.flyTo({ ...target, padding: move.padding, duration: FLY_MS });
          else map.easeTo({ ...target, padding: move.padding, duration: NEAR_MS });
          this.flying = map.isMoving();
          break;
        }
        case "ease":
          this.flying = false;
          map.easeTo({
            center: move.center as [number, number],
            padding: move.padding,
            duration: move.duration,
            // Only when linear: an `easing: undefined` replaces MapLibre's default and throws.
            ...(move.linear ? { easing: (t: number) => t } : {}),
          });
          break;
        case "pad": {
          const { clientWidth: w, clientHeight: h } = map.getContainer();
          const p = move.padding;
          const middle = map.unproject([
            p.left + (w - p.left - p.right) / 2,
            p.top + (h - p.top - p.bottom) / 2,
          ]);
          map.jumpTo({ center: middle, padding: p });
          break;
        }
      }
    } finally {
      this.applying = false;
    }
  }

  private onMoveStart = (e: { originalEvent?: unknown }) => {
    // The reader moves the map: a show still waiting would yank it away. MapLibre also passes
    // its resizes (the window's event, its observer's entries) as the original event.
    const input = e.originalEvent;
    if (input instanceof Event && input.type !== "resize") {
      this.camera = afterGesture(this.camera, this.input.camera.focus.key);
    }
  };

  private onDragStart = (e: { originalEvent?: unknown }) => {
    if (e.originalEvent) this.handlers.drag();
  };

  private onMoveEnd = () => {
    if (this.lost) return;
    saveView(this.map);
    this.handlers.settle(boundsOf(this.map));
    // The end of a move that a new one replaced (apply sets `flying` itself).
    if (this.applying) return;
    this.flying = false;
    // Panels that changed while the map moved are taken now.
    this.plan();
  };
}
