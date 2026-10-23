# Oběhy progress

Short status and the working backlog. Log entries before 2026-10-01 are in git history;
`BASE_PLAN.md` holds the long-term architecture.

## Current state

### `obehy build`

One command (`src/obehy/cli.py`) resolves the GVD year, validates or refreshes the shared OSM
snapshot, builds JrUtil once, and runs these stages. Production runs daily on GitHub Actions
(`.github/workflows/build.yml`, `BASE_PLAN.md` section 15) and publishes each run as a
`build-<run-id>` GitHub release; green since 2026-10-08.

0. **fetch-sources** — VLD and dráhy CIS JŘ archives, CZPTT (inventory, KADR, SR70), PID and IDS
   JMK GTFS, all before JrUtil is built; three attempts per source, `sources/fetch-log.json`.
1. **national-jdf** — the fetched VLD and dráhy archives → `fix-jdf` (stop matching,
   coordinate estimation, `regional-adjacent` international policy) → `merge-jdf` (name-based
   stop reconciliation) → `jdf-to-bundle` → `validate-package`.
2. **regional-overlay** — overlay the fetched PID and IDS JMK GTFS onto
   the national JDF package in one `regional-gtfs-overlay` pass.
3. **filtered-jdf** — a plain GTFS derived from the pre-overlay national JDF, filtered like
   gtfs-processor (see Next steps §4).
4. **national-czptt** — the fetched CZPTT snapshot → `czptt-to-bundle`. By default
   operational (non-passenger) points go only to the Parquet sidecars.
5. **validate & publish** — both production packages are validated and published atomically with
   `release.json` and an `artifact_root/current.json` pointer.

`--jobs` and `--memory-budget` are forwarded to JDF, the overlay and CZPTT. No process or .NET
heap hard limit is configured; memory figures are telemetry.

### Pipelines

- **JDF:** coordinates come from the `jrunify-ext-geodata/other` CSVs (checked `gapfill.csv`,
  regional catalogues) and an OSM stop extract. Unmatched stops are route-estimated
  (`estimated:route-time`, or `estimated:route-end-north` past the last anchor) and shown with
  ` [?]`. Bundles carry `coordinate_precision`, `coordinate_source` and `coordinates_missing`.
- **CZPTT:** SR70 coordinates are authoritative; OSM fills gaps. The build handles NAD (rail
  replacement), notes, accessibility, bicycles, request stops, same-month cancellation ordering,
  inventory convergence during live acquisition, and inconsistent-time correction (R10).
- **Post estimator:** opt-in (`--estimated-posts`). Oběhy passes the learned scorer (policy v3,
  `src/obehy/data/post-inference/learned-v1.json`) unless `--post-inference-policy` names
  another; capture-only runs take no policy. JrUtil's own default is still the heuristic
  `conservative-routed-v4|tuned-safe-v3|kostany-diagnostic-best-safe`, which feature export uses
  as the training baseline. Evidence-v2 packs are captured only for retraining
  (`--capture-post-inference-evidence`); bundles always route live.
- **Serving:** JrUtil production packages (`jrutil-production` bundle v3, serving schema 5.0):
  GTFS projected from the relations, bounded diagnostics and 19 typed Parquet relations (§6).
- **Mirror:** `obehy db migrate`, `obehy release load <release-dir>`, `obehy release activate
  <run-id>|--rollback`, `obehy release status` (`src/obehy/release/`). One LIST partition per
  package load, built and checked standalone and attached in one transaction; readers use the
  `active.*` views. Derived: `service_date`, location `geom`, `shape_line`. `obehy release
  fetch` downloads the newest GitHub build release into `data/releases/<run-id>`, verified.

### Last validation evidence

- Live `obehy build` (estimated posts, learned-v1, routing cache), both published:
  - `20261002T184241Z`: 45.3 min (JDF 19.4, overlay 4.7, CZPTT 20.1 min).
  - `20261003T144231Z` (serving schema 4, seeded stop registry): 39.2 min (JDF 16.8,
    filtered JDF 0.5, overlay 5.0, CZPTT 16.0, validation 0.8 min). Zero stop, post or
    overlay-place registry candidates.
  - `20261006T194555Z` (local, serving schema 5.0): all three packages valid; the dev
    database and the pinned corpus use it.
  - GitHub Actions builds daily since 2026-10-08; the server runs `20261009T030215Z-8cf199243726`.
  - Not run on either: MobilityData GTFS validator, MOTIS import check.
- JrUtil Release suite: 309 tests (2026-10-06).
- Oběhy: 232 unit + 36 PostgreSQL tests, ruff, pyright and import contracts clean; the pinned
  DÚK corpus replays to its golden digests (2026-10-10).
- Mirror load of `20261006T194555Z` into PostGIS 17 over a ~10 MB/s LAN link: JDF 466 s
  (COPY 352 s, network-bound; trip_call 7.38M rows 270 s), CZPTT 81 s; activation instant.
  Departure boards at Česká Lípa, Duchcov and Nymburk hl.n. mix both feeds with platforms/posts.
- Default JDF conversion: 179.4 s, 2.88 GB peak private memory, package valid (2026-09-15).
- National finalizer replay with overlays: 4.07 GiB peak, package valid (2026-09-19).
- Post-estimator frozen replay: bundle 13m04s vs ~43m33s live baseline, byte-identical payloads
  (2026-08-15).

## Known limitations

- The live releases have not been through the GTFS validator or a MOTIS import check, so none
  is formally accepted yet.
- The stop-ID registry is seeded (37,598 stops, 57,237 posts, 119 overlay places), but ID
  stability across consecutive builds has not been compared yet. Authored `:post:id:<n>` posts
  keep the carrier's Označníky number; only inferred `:est:<k>` posts are pinned.
- CZPTT `location` rows carry no municipality, district or country metadata (the CZPTT
  package has no stop metadata).
- Serving validation checks keys, hashes and foreign keys; cross-representation content
  validation (GTFS vs serving relations) is incomplete.
- No route shapes are generated yet (MOTIS shape generation is future work).
- DPmÚL realtime coverage is poor: weekend trip numbers on weekdays and Friday nights (DUK-Q3,
  DUK-Q16) stay unmatched by the literal-key rule, and some trip numbers are missing from the
  release (DUK-Q17). Parked deliberately on 2026-10-10; `docs/sources/duk.md` has the details.

## Next steps

Work order: §5 (core runtime) in `BASE_PLAN.md` section 34 order. R2 (DÚK trains + SŽ, rail
fusion, platforms, `docs/R2_SLICE.md`) is implemented; its acceptance waits for a CI release
with serving 5.1 and a 25 h corpus recorded against it. Platforms are off in GTFS-RT
(2026-10-10): boards number tracks for passengers, CZPTT by internal track numbers, so equal
designations were often different tracks. Board values are still recorded in
`history.platform_evidence`. After a few weeks: publish the board-numbered platforms as static
child stops unused by `stop_times` (as PID does), keyed by station and shown value, and match
board values to them in RT; never to CZPTT tracks. Then R3 (PID). §1, §4 and §6 are done; §2 and §3 wait on comparing consecutive Actions builds,
and static acceptance (MobilityData GTFS validator, MOTIS import) is still open.

### 1. Stop coordinates and easy `[?]` clusters

- **Goal:** fewer route-estimated JDF stops, starting with runs of consecutive `[?]` stops and
  invented `route-end-north` positions.
- **Actions:**
  - refresh the geodata catalogues (`download.py` into staging, diff, replace);
  - run `gapfill.py audit --coordinate-status estimated --minimum-run-length 3
    --include-unresolved-termini` on the latest bundle;
  - resolve unique OSM/Mapy/Nominatim identities into `other/gapfill.csv`;
  - have the build write a `stop-coordinate-report.json`.
- **Accept:** estimated and route-end-north counts drop against a recorded baseline; geodata tests
  pass.
- **Status:** done (catalogues refreshed and gapfill residuals resolved on 2026-10-02).

### 2. Fixed JDF stop-ID registry

- **Goal:** stop IDs stay the same between exports.
- **Design:** the reviewed, append-only registry lives in `jrunify-ext-geodata/registry/`
  (`stops.csv`, `posts.csv`, `overlay_places.csv`; see its README). JrUtil `--stop-registry`:
  - `merge-jdf` preloads registered identities, so `jdf:stop:N` does not depend on batch order;
    same-named stops are split by reference coordinates and ambiguity is quarantined.
    Unregistered stops get a provisional number ≥ 1e9 hashed from name, okres and country.
  - `jdf-to-bundle` keeps inferred `est:<k>` ordinals within 25 m of a registered post.
  - `regional-gtfs-overlay` uses pinned IDs for source-native stop places.
  - `--stop-registry-candidates` lists what is missing; `registry.py promote` adds reviewed rows.
- **Oběhy:** passes the registry when `registry/stops.csv` exists, records its hashes in the
  national-JDF run manifest, and keeps the candidate CSVs in `releases/<run>/stop-registry/`.
- **Accept:** identical IDs across batch orders and consecutive builds; ambiguous stops are
  quarantined, not merged.
- **Status:** implemented, and seeded on 2026-10-03 (`jrunify-ext-geodata` `410457b`): the
  planned stop-ID break is done. The 2026-10-03 release was built with it and produced zero
  candidates. Next: compare stop/post IDs of the next build against that release to confirm
  stability.

### 3. Faster post estimator

- **Goal:** a repeat build's post phase takes ≤15 min.
- **Actions:**
  1. profile one live run;
  2. add a persistent routing-graph cache keyed by PBF hash and format version;
  3. add a per-context evidence cache so a live build routes only new contexts;
  4. only if still dominant, deduplicate/one-to-many A*.
- **Accept:** payload hashes equal a cold live run on the same inputs.
- **Status:** 3 done (JrUtil `--routing-cache`, on by default in Oběhy; entries revalidated against
  per-tile graph fingerprints). Routing also runs on up to 8 workers. 2 and 4 not started.

### 4. Build outputs: filtered JDF + CZPTT sidecar mode

- **Goal:** `obehy build` publishes JDF, CZPTT and a filtered JDF GTFS.
- **Filtered feed:** drop lines of FlixBus CZ/DACH/Polska, PMDP and DPMO (operator or `Altdop`)
  and of IDS PID/IDS JMK/IDZK (preferred `LinExt` row) in the merged national JDF, every version
  valid on or after the build date. Also drop the checked-in line-number prefixes. Remove
  calls at stops without coordinates and keep `[?]` stops. Customs stops are handled in JrUtil, not
  in the filter.
- **CZPTT:** operational points go to the sidecars only by default (`--czptt-operational-points
  gtfs` restores the old behavior).
- **Status:** done. Implemented (`src/obehy/filtered_jdf.py`, rules in
  `jrunify-ext-geodata/filtered-jdf/rules-v1.json`) and published by both live builds
  (`jdf-filtered/`, about 30 s). MobilityData validation of the filtered feed is not run.

### 5. Core runtime

- **Design:** `BASE_PLAN.md` sections 1, 16 and 18–23 (release mirror, fact-based trip
  inference, interval timeline engine with progress integrity, own GPS delay, circulations).
  Feeds are built on GitHub Actions; the server only fetches, loads and serves. Realtime order:
  DÚK + SŽ, then PID (Golemio APIs + GTFS-RT alerts), then Arriva Express.
- **Steps:**
  1. `obehy rt record` and dossiers for DÚK, SŽ and Arriva Express (`docs/sources/`), checked
     by replaying a 25-hour capture against the v5 release: DÚK buses 99.1% of running trips to
     exactly one trip (DPmÚL pending the JrUtil merge fix), DÚK trains 95.3%, SŽ 99.7% of
     train-days to one run, Arriva Express 61/61 scoreable trips by line, destination and
     next-stop times.
  2. Serving schema 5 (§6; replaces the planned CZPTT contract check).
  3. Database foundation and `obehy release fetch|load|activate --rollback`.
  4. Realtime skeleton: model, clock, archive, core loop, `rt` migrations, replay.
  5. Inference engine (facts, scorers, decision rule, date inference, vehicle binding, rail
     runs).
  6. Timeline engine (intervals, delay semantics, progress integrity, arbitration).
  7. DÚK connector and per-feed GTFS-RT; 8. SŽ and rail fusion; 9. project API; 10. PID;
     11. Arriva Express with own GPS delay; 12. circulation learning.
- **Accept:** per step as listed in `BASE_PLAN.md` sections 33–34; scenario tables for inference
  and the timeline engine; deterministic replay; GTFS-RT validator on replayed days.
- **Status:** step 1 done (2026-10-06; recording continues). Step 2 done (§6). Step 3 done
  (`release fetch` 2026-10-09). The server runs `deploy/compose.yaml` from the CI-built image
  since 2026-10-09: hourly fetch → load → `activate --if-newer`, the realtime worker, nightly
  jobs at 03:30, and nginx serving `/get-feeds/` (static feeds, GTFS-RT, feed list) behind the
  host's reverse proxy. The MOTIS node takes its feeds from there.
  Step 4: `obehy rt replay` (archive → episodes → trips of a Parquet release, deterministic
  report) done 2026-10-08. The core architecture is decided (2026-10-08, `BASE_PLAN.md`
  sections 5, 18–22, 29–30). The first slice (DÚK buses with history, warm-replay restart,
  `docs/R1_SLICE.md`) is implemented (2026-10-09): times, model, lazy index, keyed binding,
  progress as map matching, history, DÚK connector, worker, warm replay and replay on the core.
  On the pinned 25 h corpus the match rate is DÚK 99.4%, Teplice 99.1%, DPmÚL 54.7% and the
  GTFS-RT consistency check is clean. Replay is deterministic (byte-identical snapshots across
  runs; golden digests in `tests/golden`, `tests/db/test_corpus.py` with `OBEHY_PINNED_ROOT`).
  Open for R1 acceptance (§6 there): history rebuilt twice and the warm-restart comparison on
  the real corpus (both pass as DB tests on fixtures; the corpus run was stopped, slow over the
  LAN), and the MobilityData GTFS-RT validator (needs a `read:packages` token).
  R2 (DÚK trains + SŽ, `docs/R2_SLICE.md`) is implemented (2026-10-10): proxied anonymous SŽ
  egress, serving 5.1 `sr70`/`sr70:track` keys (JrUtil), rail runs with train-number journeys
  and `journey_link`, the SŽ map connector, DÚK + SŽ fusion, station boards with platforms. A
  2 h replay (2026-10-06 07-09, keys derived for the measurement) binds 1,653/1,660 SŽ and
  142/150 DÚK train key groups, places 99.5% of SŽ point events, and its rail GTFS-RT passes the
  consistency check. Open for R2 acceptance: a CI release with serving 5.1, then a 25 h
  DÚK + SŽ + boards corpus pinned against it (golden digests, validator, live hour).
  Predictions follow `BASE_PLAN.md` 20.5 (dwell recovery, early running carried over, hop
  running times learned live for road vehicles, no uncertainty); hop ratios by time of day,
  per-stop holding and knock-on to the next trip of a tour wait for history learning and
  circulations (section 22). JrUtil's
  `serving-v5.json` time description still says noon − 12 h and needs correcting.
  Step 11 (Arriva Express): connector and keyless inference implemented (2026-10-12), replay
  replayed clean on the server capture of 2026-10-09 to 10-12, live on jdf. Open: `rt evaluate`
  of own GPS delay against the source's, the travel-time provider interface.
  Step 9 (project API, 2026-10-20): `obehy api serve` (FastAPI, `src/obehy/api/`) with status,
  stop search and viewport, stop places, departures, trips by feed trip ID + service date
  (source keys redirect, older trip IDs answer 410 with their key) and vehicles; the `api`
  service and nginx `/api/` in `deploy/`. The worker also writes `rt.trip_part_current` (each
  feed trip's slice of its journey's calls) and a vehicle's current part. Expanded
  2026-10-20 (phase A): readable static codes, arrivals, nearby stops, multi-stop boards with
  a cursor, the vehicle on boards and trips, lines, connections and journey links, journey
  history and line punctuality, debug endpoints. Phase B (2026-10-21): the vehicle registry
  (`obehy ref import`: our own list over the DÚK register and PID model seeds; keys by source ID,
  plate, EVN or series number), live attributes from PID, DÚK, DPMD, MPVnet and Arriva,
  co-located reports of two sources paired into one vehicle, precedence curated > live >
  model with conflicts flagged, all resolved in the worker (`rt.vehicle_profile`); vehicle
  details and traits on boards and trips; the sources behind each trip's and vehicle's
  realtime; features as one per source code with official JDF 1.11 / KJŘ texts; line
  overview (validity, notes, day summary) and timetable; debug for vehicle identity,
  pairings, registry and release. Phase C (2026-10-21): cancelled trips. PID's cancelled
  entries (PID-Q4, 10 on 2026-10-15) become `TripCancelled` without a vehicle; the core
  cancels a journey not yet running (every call `cancelled`), a vehicle seen starting it
  restores it; GTFS-RT `CANCELED`; `history.journey_cancellation`; boards, trips and history
  days show it. Replayed on dev: line 394 trip 1028 cancelled at 07:24. DÚK and the SŽ map have
  no cancellation signal; SŽ boards not checked (no local capture). Phase D (2026-10-21):
  alerts from any source in one shape (`rt.alert`), resolved per feed in SQL against the
  active release, republished in each feed's GTFS-RT and served at `/alerts`, on boards and on
  trips. PID: VYMI events (GTFS-RT, routes) and infotexts (stops), polled every 60 s; on dev
  47/56 routes and 36/36 stops resolve (the rest PID rail lines, PID-Q9). The full VYMI export
  is internal to Golemio (403). Phase E (2026-10-21): lookups and compositions. The API
  records what it is asked (`rt.lookup_request`, 202 `pending`, `stale` while asked again);
  the worker's lookup broker plans through the connector from a SQL view of the trip, reuses
  answers by cache key, keeps a token bucket and a daily budget, archives and logs each answer
  (`rt.lookup_fetch`). ČD connector (`docs/sources/cd.md`): ČD's trains by train number and
  the next passenger call's SR70 (CD-Q1 `54` + SR70), coaches as units with types, numbers,
  classes and services on the vehicle vocabulary. Compositions per journey key with per-unit
  spans; a later answer closes an earlier one, identical ones merge; segments in SQL
  (`rt.composition_spans`). On dev: 607 from Cheb plans `5475035` (the captured answer's
  station); today's 607 (Chomutov město – Praha) ran the whole flow with the captured answer
  (broker 137 ms, API 108 ms), rows deleted after. ČD itself not asked yet. Next: SŽ alerts for trains; 55p.cz only with permission. Open
  beyond: PMTiles, `no_realtime`, merged station boards.
  Step 10 (PID, R3): Golemio vehicle positions connector (2026-10-15), live on jdf and
  czptt after the server capture replayed at 97–100 % per mode (`docs/sources/pid.md`).
  GTFS-RT alerts and departures later.
- **Pin 2026-10-25** (DST fall-back) from the running recorder as the first permanent replay
  corpus.

### 6. Serving schema 5

- **Goal:** a minimal, easy-to-query serving contract before the loader is written. v4 has 30
  relations, keeps rail operational points apart from trips, and spreads realtime keys over seven
  binding relations (JDF `source_call_map`: 16.57M rows, all but 10,209 identity).
- **Design:** JrUtil `contracts/serving-v5.json` (5.0 draft) and `docs/PRODUCTION_CONTRACT.md`:
  19 relations; one `trip_call` sequence including CZPTT railway points (NAD bus parts carry
  none); `source_key`/`call_key` lookups with per-namespace identifier encodings; closed
  enumerations, including the typed JDF feature kinds; feed-prefixed IDs; GTFS-style times; zones
  on route stops with call exceptions; calendars as in v4; lossless typed semantics in
  `service_note`, `assignment` (with CZPTT call ranges), `connection_claim`,
  `travel_restriction`; boarding-point platform/post codes; GTFS projected from the relations.
  Versions are `major.minor`; minors only add.
- **Validation:** a v5 prototype (DuckDB views over the 2026-10-03 v4 packages) ran the
  consumer scenarios. DÚK (CIS line + trip), SŽ (TR id, train number, run timeline at Nový Bor),
  Arriva Express (line + destination + time) each resolve to exactly the trip their dossier
  names. A mixed rail/bus departure board at Česká Lípa hl.n. and a vehicle-detail query
  (schedule, zones, notes, features, restrictions, claims) run as plain joins. The NeTEx mapping
  in `JDF_SEMANTICS.md` finds a home for every fact. Contract fixes found on the way: typed
  kinds, platform codes, CZPTT note ranges, source-qualified namespaces, feed-prefixed IDs, and
  no reserved words (`key`, `method`).
- **Status:** done (2026-10-06). JrUtil writes and validates 5.0 natively (bundle 3): typed JDF
  kinds, CZPTT trip parts with railway points and `call_range` notes, `source_key`/`call_key`,
  GTFS projected from the relations, CZPTT input digest in the manifest. Oběhy accepts serving
  major 5. A full local build produced three valid packages and the realtime replay (§5.1)
  resolves against them; the v4-vs-v5 comparison was dropped (different source data). Supersedes
  §5.2: the CZPTT trip ↔ operational-call link is `trip_call` itself.

### 7. Web frontend

- **Built:** `web/` (Vite, React, Mantine, TanStack Router and Query; types from the API's
  OpenAPI document). The app shell has four tabs: Mapa (home), Odjezdy, Linky, Mimořádnosti.
  Departure and arrival boards come with a time select, stop alerts, day dividers, paging, and
  live, delayed and offline states. A trip opened from a board shows a collapsible timetable
  over the whole run ("Dále jako" where the line changes), then notes, connections and the
  vehicle. Served by the web image (nginx + the app); in development the app proxies to
  obehy.cz. Mapa (home): every vehicle in Czechia on an OpenFreeMap base map in our style,
  drawn by deck.gl into MapLibre's canvas (dots, then line pucks with bearing from z11), gliding
  between answers; fetched by fixed 1° grid cells (a cell at the API's 3000 is split into
  quarters) behind a 5 s nginx micro-cache. On the desktop the map is the base under every
  tab, with the tab's panels over it (board, trip beside it, vehicle), a board's stop and a
  trip's path drawn; the navbar collapses. On the phone a vehicle opens in a bottom sheet
  (peek, 45 %, 70 %). One marker per journey; a vehicle can be followed.
- **Next:**
  - stops and per-release route PMTiles on the map;
  - Linky and Mimořádnosti;
  - the composition card once ČD is asked live.

## Recent log

- **2026-10-23** — Registry seed `pid-vehicles`: PID's vehicle models and equipment from
  Golemio's per-vehicle descriptors (`/v2/public/vehiclepositions/{id}?scopes=vehicle_descriptor`;
  the position feed has no model), fetched slowly into a CSV (`obehy ref fetch`) and imported
  only for vehicles with a model; the server's jobs service fetches and imports it at start and
  nightly. The DÚK register is no longer private: normalised into our vehicle list's format as
  `jrunify-ext-geodata/vehicles/duk.csv` (522 vehicles, makers split where the name starts with
  one), imported by hand until vehicle datasets ship with releases. The API's vehicle traits
  carry the manufacturer. Web: a plate links to seznam-autobusu.cz; a vehicle is named by its
  model or plate. Validated: unit and API DB tests, a live 5-vehicle fetch, the DÚK file read
  and checked; not yet imported on the server.
- **2026-10-23** — The map's open trip is drawn by MapLibre (smooth, wider, with a halo and
  direction arrows; deck.gl's lines were aliased in MapLibre's context), its stops with names
  placed by MapLibre's collision (the board's and next stop first, then the ends; a town with
  most of the trip's stops left out of their names). `/trips` places a call at its post when
  the post's position is exact, else at the stop place. Validated: DB test, Vitest, style validation,
  browser (metro B from Národní třída); posts show on the dev map once the API is deployed.
- **2026-10-23** — Web state rework: what is open comes from the URL only, server data from one
  query per resource (a trip's path is its own query, asked once), and the map's own UI state
  (followed vehicle, panel insets, sheet snap) from one selector store. One `MapController`
  owns MapLibre, deck.gl and the glide; every camera move is decided by `planCamera`
  (`web/src/features/map/camera.ts`). Fixed: far shows jumped (MapLibre's `maxDuration` skips
  the animation), a resize counted as a gesture and dropped a pending show, the stop marker
  was built from `window.stop`, the selected puck's rim was unclickable. No view transitions;
  closing panels animate as copies. Validated: Vitest (camera rules, selection, store, sheet),
  lint, build, and the scenarios in the browser with the camera log; animation smoothness not
  observed (the pane ran at 1 fps).
- **2026-10-22** — Vehicle search: `/vehicles?q=` finds vehicles by line, train number,
  headsign, fleet number or plate (folded, every word somewhere), exact line or train number
  first, the nearest first with `near`. The search box lists vehicles beside stops; it uses a
  position only when the site already has it (Moje poloha or a granted permission) and sends it
  rounded to about a kilometre. Validated: DB test, Vitest (component with a mocked API).

- **2026-10-22** — Data status. It read "delayed" whenever one board row was stale and judged
  the map by the answer's own time; `/status` listed no sources (`rt.source_health` is never
  written). `/status` now gives each source's newest vehicle report and `realtime_at`; the
  shell polls it (delayed after 2 min without a report, offline when requests fail).
  Validated: unit + DB status test, Vitest, build.

- **2026-10-22** — Map polish (migration 0019). `/vehicles` showed 75 of 1,646 trips twice (the
  SŽ map's train and the unit's GPS) and flipped a vehicle two sources bind differently to the
  other's trip every few pings; it now lists one vehicle per journey (the GTFS-RT rule, written
  by the worker as `represents`) and takes a vehicle's trip from its best-ranked report.
  `/vehicles/{id}` answers IDs with slashes (every SŽ train was a 404). Web: the map is the
  desktop base with fixed-width panels; timetable ranges open on their own without moving the
  anchor; notes grouped (chips for train notes, long texts behind a toggle); a crisp selection
  ring; sheet snaps 45/70 %. Validated: unit + DB tests, ruff, pyright, lint-imports, Vitest,
  build, live against obehy.cz (desktop and 375 px). Needs deploying to check the duplicate
  and flip counts on production.

- **2026-10-22** — Vehicle map. MapLibre 5.24 (deck.gl 9.4 does not support MapLibre 6 yet) with
  deck.gl drawing every vehicle on the canvas, a style of our own over OpenFreeMap's tiles,
  grid-cell fetching, a vehicle sidebar or bottom sheet, and following. The board's trip panel
  now shares `TripDetail` with it. Validated: Vitest (grid, glide, style), typecheck, lint,
  build (main chunk 165 KB gzip, map chunk 465 KB), live against obehy.cz. The nginx cache
  config is untested locally (no Docker here).

- **2026-10-22** — A trip's alerts are its own, its line's, and the severe ones of its stops.
  The trip panel shows a stop's severe alert only while that stop is still ahead of where one
  boarded. A stop's other alerts (most PID infotexts) show only on that stop's board.
  Validated: unit + DB tests, Vitest.

- **2026-10-22** — API performance (migrations 0017, 0018). Before this, obehy.cz answered
  boards in 5–7 s at any window, a trip in 5.5 s, and `/alerts` alone in 5–6 s (32 alerts).
  `rt.alert_target` hashed every `source_key` partition of every retained load: it joined
  `control.publication`, so nothing pruned. It now looks each entity up by PK, per active
  load (0018). With 3M keys locally that's 2.7 s → 2 ms, and it also speeds up the worker's
  alert feed. Boards and trips resolve current alerts only. Boards range-scan a stop's calls by
  scheduled time (new `trip_call` indexes, also built per load) and check "not the last call"
  only for trips running on the date. `vehicle_state_trip` index. The `rt.*_current` tables
  get fillfactor 50 and eager autovacuum. The web board asks for 90 min and widens to 360 only
  when the page isn't full. A stop place lists its lines only with `include=lines`: Praha
  hl.n.'s 408 were 66 of its 67 kB. API connections run with `jit = off`.
  `obehy api serve --workers` (compose: `OBEHY_API_WORKERS`, 4). Postgres gets
  pg_stat_statements, a 250 ms slow-statement log, and WAL, checkpoint and autovacuum tuning
  (needs a DB restart).
  - **Validated:** unit + DB tests on PostgreSQL 16 + PostGIS. EXPLAIN on a fixture inflated
    to 80k calls and 3M source keys: the range indexes and the `source_key` PK are used, with
    run-time partition pruning. 33 Vitest tests, ESLint, tsc. Two-worker serve smoke test.
  - **Still slow, not used by the web app yet:** `/lines/{id}` 1.4 s; `/vehicles` in a Prague
    bbox is 887 kB.
  - **Not run:** prod timings. Once deployed, read `pg_stat_statements`. Run
    `VACUUM (FULL, ANALYZE)` once on the `rt.*_current` tables to shed existing bloat.

- **2026-10-22** — Server disk at 96 %: with PID, `rt.observation` is about 7 GB per UTC day,
  and the two kept days plus today's partition made 21 GB. Policy v12 keeps one day
  (yesterday's partition and today's), enough for the 6-hour warm replay across midnight. Unit
  tests pass. Dropping the two oldest partitions by hand frees 14 GB now. Next to watch:
  `history.*` at 9 GB with no retention yet.

- **2026-10-21** — Web frontend (`web/`, section 7 above): shell, boards and trip panel after
  the redesigned Figma file; `obehy api openapi` writes `web/openapi.json`, and CI fails when
  it or the generated types drift. The `obehy-web` image (nginx with the app) replaces plain
  nginx in `deploy/compose.yaml`.
  - **Validated:** 31 Vitest tests, ESLint, Prettier, tsc, a production build; checked in a
    browser against obehy.cz (Děčín hl.n. boards; U1 6556 going on as U1 (NAD) to Kadaň;
    phone and dark mode).
  - **Not run:** the Docker image build (no Docker locally; CI builds it).

- **2026-10-20** — API phase A (migration 0012): fixed codes readable through
  `codes-v1.toml` (39 kinds, calendar designations, boarding codes, 34 integrated transport
  systems from the CIS JŘ MHD and IDS lists), arrivals, `near`, multi-stop boards with a
  cursor, vehicles on boards and trips, `/lines` (search, patterns, trips, punctuality),
  connections and journey links, `/journeys/…/history`, `/debug`. Validated: 23 API DB tests,
  unit tests, ruff, pyright, contracts; on the dev DB (static only): boards 40–340 ms (Florenc),
  multi-stop 60–125 ms, a PID tram line's patterns 1.1 s and its 486 trips 0.8 s. Starlette's
  test client decodes `%25` twice (uvicorn once), so encoded CZPTT trip IDs are checked against
  a live server only.
- **2026-10-20** — Project API v1 (`BASE_PLAN.md` section 30, migration 0011). Trips are
  addressed by feed `trip_id` + service date; realtime joins by trip ID through the new
  `rt.trip_part_current`, so rail parts get their slice of the run. Validated: 14 API DB tests,
  unit tests, ruff, pyright, import contracts; on the dev DB (release 20261015, static only)
  boards answer in 15–60 ms, Praha Florenc (15 k calls) in ~200 ms, stop search in 25–50 ms;
  stop-place detail takes ~0.8–1.6 s (its lines; cached by release ETag).
- **2026-10-20** — Warm restart: since the publishing deadband (2026-10-19) estimates carry
  state from tick to tick, but warm replay ticked without estimating, so a restarted worker
  published differently from an uninterrupted one (`test_warm_restart_continues_like_an_
  uninterrupted_run` failed). Every tick now estimates; `emit` only gates files and tables.
  Replay snapshots every 60 s now see 10 s estimates, so the pinned-corpus golden digests
  (not regenerated since 2026-10-10) need regenerating with `OBEHY_PINNED_ROOT`.
- **2026-10-19** — Flicker and misread early buses. A published prediction moves only by
  `publish_deadband_s` = 20 s or more (rail 0): most moves of the measured lateness are
  stop-and-go traffic, and smoothing the lateness itself (EMA, trend, the last departure) cost
  2+ s MAE wherever tried. PID-Q8: Golemio starts a vehicle on its next trip at the scheduled
  time wherever it is and projects it onto the path (line 388 deadheading out of Prague, put
  12 km along, 23 min early; Golemio's own delay −1421 s): DUK-Q19's start rule now holds after
  the scheduled start too, and a trip not yet departed is never shown early. Off the path a
  held reading never gains time. The floor at now applies only while the last fix is at most
  `floor_fresh_s` = 90 s old (a reception gap may pass calls unseen). PID peak with deadband and
  no-gain: bus flicker 1.19 → 0.97 for +0.3 s MAE; with the start fix bus bias two to five
  stops on −9…−19 → −2…−12 s and predictions over a minute early 14.4k → 10.9k samples (MAE
  271 → 175 s). Carrying a floored call's time to the calls after it doubled tram flicker and
  was dropped; the final combination's replay was not finished at commit. Unit tests pass.
- **2026-10-19** — Road predictions from hop running times learned live (policy v11). A
  constant lateness carried forward predicted Prague buses early, more so further ahead (PID
  2026-10-15 06:45-09:00: bias −31 s at the next stop, −55 s five stops on), and 12.5 % of
  next-stop predictions were already in the past when published (fixes ~31 s old; the floor
  was the fix time). Now each call ahead adds the time its hop takes now beyond the timetable:
  the recency-weighted ratio (`runtime_decay_s` 30 min, `runtime_prior_weight` 0.5) of every
  journey's arrival-to-arrival time over the same two stops (`timeline/runtimes.py`,
  `FeedState.hops`); a road call not yet reached is never predicted before now. Rail runs are
  unchanged (floored at now, trains two and three stations ahead got 7-11 s worse). PID bus
  MAE/bias next stop 64.6/−31 → 56.7/−15 s, third 77.4/−34 → 66.9/−10 s, fifth 98.7/−55 →
  85.1/−19 s; tram 38.5 → 31.6 s next stop (fifth 56.2 → 57.7 s, bias +14…+29 s); metro
  24 → 17 s; none in the past. 2026-10-12 peak (DPMD, MPV net, SŽ map): bus 51.7/−6.7 →
  48.9/−0.1 s next stop, 71.1 → 68.2 s third; trolleybus 36.9 → 32.8 s, third 62.9 → 52.0 s;
  rail identical. Flicker (reversals per call) up: PID bus 1.00 → 1.19, tram 0.82 → 1.10, DPMD
  + MPV net bus 0.81 → 0.90. Dropping the dwell credit, the waiting rule and the early fade
  was measured and rejected (PID: 4× as many buses predicted over a minute early, flicker
  nearly doubled). Unit tests pass; DB tests and corpus goldens not run (no test database).
- **2026-10-19** — Delays at stops, on roads the shape misses, and SŽ minutes (policy v10).
  A stop without a timetabled dwell credits the dwell a vehicle is seen to make, up to
  `dwell_credit_s` = 30 s, fading out over the next leg (a train at a call not on request even
  when its arrival went unseen; `on_request` from pickup/drop-off 3). Off a real shape a held
  reading advances by the distance its fixes moved, and a reading may not fall far behind the
  fixes, so a bus on a parallel road no longer stalls where the roads split (synthetic block:
  worst made-up lateness 210 s → 15 s); chords keep the leg estimate; a trigger passed off the
  path waits for a fix past it. SZ-Q12: `de` and `nsp` are truncated minutes, taken at their
  middle (decoder 2). `obehy rt replay --quality` (and `--since`/`--until`) reports accuracy by
  horizon and predicted delay, flicker, dwell and off-shape bumps. 2026-10-12 morning peak
  (DPMD 05-10, MPV net 07:00-07:30, SŽ map 07-08), next-stop MAE/bias before → after: DPMD bus
  33.4/+14.5 → 31.1/+5.9 s, MPV net bus 47.7/+7.0 → 46.1/+0.2 s, trolleybus 34.8 → 33.1 s,
  MPV net rail 55.5 → 54.2 s, SŽ rail 93.0/−44.3 → 92.3/−16.0 s; flicker reversals per call
  1.42 → 1.03 (DPMD), 1.26 → 0.97 (MPV net bus). Two and three stops ahead unchanged or better.
  Early running holds to the next call and fades beyond it (`early_keep` = 0.5): buses
  predicted over a minute early were real early running mid-trip (all GPS, 452 trips), right at
  the next call (−94 s predicted, −86 s actual) but mostly gone two calls on (−91 / −32 s); their
  bias −74 → −38 s; MPV net bus flicker 0.97 → 0.84, DPMD 1.03 → 0.61, MAE two and three calls
  ahead within a second of before. Unit and DB tests pass; not run on PID.
- **2026-10-18** — TripUpdates no longer leave the feed when realtime ends (MOTIS reverted the
  whole trip, the calls made included, to the timetable): policy v10 `publish_finished_s` keeps
  a finished trip's calls made (no vehicle) 30 min, rail 60 min; a lost trip keeps its calls
  made with `NO_DATA` ahead until forgotten. Unit tests pass.
- **2026-10-15** — PID and DÚK published two TripUpdates and two vehicles for one regional bus:
  a non-rail journey was named by the key that bound it (PID's GTFS trip ID, DÚK's CIS line and
  trip), so one trip was two journeys. A journey whose trip and day the observation's
  `cis:line_trip` key also finds is now named by it (PID-Q7); 99 % of PID's regional entries
  carry the CIS key. Unit and DB tests pass.
- **2026-10-15** — Warm replay `read` at 90 % of wall time with PID live: the by-vehicle query's
  `OR EXISTS` tests ran per window row against every live vehicle (window rows × live vehicles;
  synthetic 6 h of 1,500 vehicles: 102 s before the first row). Rewritten as left joins: same
  rows in 11.7 s. DB tests pass.
- **2026-10-15** — PID live on jdf and czptt. Raw archive retention: policy v9 `[retention]
  raw_days = 14`, and `obehy jobs nightly` deletes older archive day directories (all sources
  with PID are about 1 GB a day; zstd level 10 is within 12 % of level 19, so the window is the
  lever). BASE_PLAN's 3 months does not fit the 60 GB server; pinned corpora are copies and stay.
- **2026-10-15** — PID server capture (12.75 h overnight to late morning) replayed with SŽ map
  against release 20261015T014009Z: running key groups bound bus 99.9 %, tram 99.2 % (67 %
  before the JrUtil night-trip fix), metro, trolleybus and ferry 100 %, train 97.0 %. The capture
  showed Golemio lists every trip of a vehicle's block at its one position (PID-Q6): about 45 %
  of vehicles in a poll had 2+ entries, and each upcoming trip was claimed as pre-trip. Decoder
  2 keeps one entry per vehicle and payload (the running one, else the next trip, else the
  vehicle alone; two running at once bind neither) and binds no trip in the `canceled` state.
- **2026-10-15** — Server disk (60 GB, 94 % full): PostgreSQL held 38 GB, `rt.observation`
  ~3.7 GB a day kept 30 days and three releases at ~7 GB. Policy v8 keeps two days of
  observations (only warm replay reads them; delay history and tours are in `history.*`),
  activation keeps one predecessor release, `obehy release prune` frees it on demand and also
  drops what killed loads left, and two fetched release directories are kept. Unit and DB tests
  pass. Next to watch: `history.*` at ~300 MB a day before PID goes live.
- **2026-10-15** — PID connector (`sources/pid.py`, `docs/sources/pid.md`), recorded only:
  Golemio `/v2/vehiclepositions` polled every 10 s as deltas (`updatedSince` from the last
  `x-last-modified`, a new generic manifest `cursor`), with the API key as a runtime secret (new
  generic `auth`, `OBEHY_SECRET_<NAME>` or `[realtime.secrets]`; a source without it is not
  polled). City vehicles bind by `pid:gtfs_trip_id`, regional buses also by CIS line + trip,
  trains by train number on czptt (PID-Q1–Q4). Policy v7 puts PID between DÚK and SŽ in the
  position order. Unit and DB tests pass. A short local recording at midnight polled deltas as
  intended and replayed against the 10-12 release (bus 49 %, tram 61 %, metro 6/6, trains
  16/17 of running key groups); night lines lack `pid:gtfs_trip_id` keys in that release
  (overlay gap to check in the day capture).
- **2026-10-14** — First prod warm start: index loads were ~70 % of wall time at ~8 % CPU (waits
  on PostgreSQL). Migration 0010 indexes the loader's scanning lookups (`static.trip` by
  `run_key`, `source_key` by public ID and by identifier prefix; loads build them per
  partition); the compose database is sized from `SYSTEM_RAM_GB` (an eighth as shared buffers,
  half as cache) with JIT off. Unit and DB tests pass; the speed-up is not measured yet.
- **2026-10-14** — Faster warm start (prod: about 8 min after peak hours, 4 h window). (1) The
  worker polls and archives during warm replay (was a gap on every restart); observations
  received meanwhile run from a backlog once it is done; the replay runs on a thread with a
  read-ahead reader, ticks on the replay clock (was one tick at the end) and logs progress
  every 30 s. (2) Core step 70 → 43 µs per observation on the real traces, bit-identical
  (every `core.step` of the unit suite and the traces fingerprinted against the old code):
  integer-microsecond tracker arithmetic, one instance rebuild per fix instead of four or
  five `dataclasses.replace`, facts indexed by type, cached spans and local dates; static data
  loads run off the event loop; the summary reports core µs per observation. (3) Policy v6
  `warm_replay.mode = "vehicles"` with migration 0009 (`rt.observation.vehicle_id`,
  `state_since`): a restart replays only the live vehicles from where their state begins and
  the journeys they are on (journeys already finished are not restored); a property test over
  the traces and a two-journey bus checks live state equals an uninterrupted run. Unit tests
  pass; DB tests (incl. the new SQL selection test), `obehy rt bench` and a real warm start
  not run (no database reachable then; DB tests passed later).
- **2026-10-13** — The feed list's realtime sources come from the connector manifests: each
  manifest has a `title` and `covers`, and the GTFS-RT files a source feeds follow its
  channels' `live_feeds`, so the list no longer goes stale (it named only DÚK and SŽ). Unit
  tests, ruff, pyright and import-linter pass; the page was not regenerated on the server.
- **2026-10-12** — Stops on a *závlek* were placed on the wrong leg of the path: each stop took
  its nearest projection after the previous one, so a stop pin a metre nearer the return lane
  took the return visit's place and pushed every later stop to the end of the path (552650:1135
  stuck before Brozany, off route). Stops are now placed all at once, in call order, nearest to
  the shape in total; a stop over 500 m from every part of the shape is interpolated by time.
  On the Litoměřice-area lines (5526xx) 144 of 1571 trips change, none left unplaced. Unit test;
  not replayed.
- **2026-10-12** — A bus reported by several sources (DÚK and a city's own feed) was two
  vehicles in GTFS-RT. Its journey now publishes the vehicles of one source only, the one the
  tracker follows, and its TripUpdate names that vehicle. Unit test; not replayed.
- **2026-10-12** — Realtime worker CPU and memory pass (policy v5). Memory: the lazy index and
  plan cache are swept hourly in place of keys not named for 4 h that no live journey needs,
  and rebuilt daily at 03:30 from the live journeys; tracker history keeps only
  the ends of a standstill (was one placement per fix, copied per fix); warm start streams
  stored observations in chunks; worker poll stats are summarized and cleared every 10 min;
  `MALLOC_ARENA_MAX=2` and `gc.freeze()` after warm start. CPU: current-state tables are
  upserted by change instead of deleted and re-COPYed with every journey's calls JSON every
  10 s; GTFS-RT reuses each entity's bytes while its journey is unchanged (byte-identical
  output); estimates skip `dataclasses.replace` for unchanged calls; a repeated identical fix
  is no news (not stepped, not stored); faster path building, MPV net/DPMD time parsing and
  DPMD reader (4×, same output); one shared TLS context and proxy openers. New
  `obehy rt bench` (worker path over archived polls, CPU per phase, no DB writes): MPV net +
  DPMD 2026-10-12 06–07 local, under cProfile, 9.6 % → 6.6 % of a core (estimate 95 → 41 s,
  GTFS-RT 75 → 46 s, decode 29 → 18 s); the state-table rewrite, not in the bench, was the
  largest prod cost. Unit and DB tests pass; no soak run, and the corpus replay diff against
  the old code was skipped.
- **2026-10-12** — Off-path lateness (policy v4): while a vehicle is held off its path, its
  lateness follows a leg estimate instead of freezing. The share of the way between the last call
  left and the next call comes from the straight-line distances to both, is mapped onto the path,
  and is never behind the held reading, never faster than the speed cap and never past the next
  stop. There is no estimate on a detour longer than twice the leg plus 3 km. It gives lateness
  only, never events. Next-stop arrival predicted while off the path, error p50/p90, estimate
  vs frozen, against `20261012T080259Z` (shaped): Arriva 2026-10-09 to 10-12, 8.7 % of fixes
  off the path, 3.2/8.8 vs 4.5/10.1 min, better on 68 % of 1694 predictions, worse on 31 %;
  DÚK 2026-10-05/06, 3.9 % off the path, 1.1/6.3 vs 1.2/7.2 min, better on 62 % of 16755,
  worse on 36 %. Against `20261006T194555Z`, where Arriva trips had no shape (stop-to-stop chords
  5–12 km off the motorway, 50 % of fixes off the path): 3.0/9.1 vs 5.4/10.6 min.
- **2026-10-12** — Arriva Express on the server's capture (2026-10-09 to 10-12, over a weekend):
  158/158 episodes with a departure bound, 98.8 % of reports, none to two trips, every binding
  within its trip's scheduled times, GTFS-RT check clean. Found ARRIVA-Q9: the next stop can stay
  on the origin for a whole motorway leg, so a late name change looked like a departure an hour
  later (one episode briefly bound to the wrong trip). A change far from the stop left now gives
  no time, and the position falls back to the whole path; the 2026-10-05/06 capture now binds
  64/65 episodes. Arriva Express is live on jdf.
- **2026-10-12** — Arriva Express connector and keyless inference (`infer/keyless/`, policy v3
  `[keyless]`), replay only. Candidates are the line's trips to the destination; the departures
  seen as next-stop changes, else the position, pick one by time with a 15-minute margin; the
  plate keeps the binding while its episode agrees, and stop events reach history after two
  agreeing departures. New facts `LineRef`, `Destination`, `NextStopName` (fact schema 2; 1
  still reads). The 25 h capture against `20261006T194555Z`: all 60 episodes with a departure
  bound, each to the trip it fits, 96.2 % of reports; GTFS-RT check clean. ARRIVA-Q8: the
  source's next stop can be a motorway junction. Own GPS lateness differs from the source's
  delay by 132 s at the median; not yet evaluated against actual departures.
- **2026-10-12** — DPMD and MPV net buses live on jdf. A day of capture (2026-10-11 16:09 UTC to
  2026-10-12 12:51 UTC, over midnight and the Monday start) replayed against `20261006T194555Z`:
  DPMD 617/617 running key groups bound, MPV net buses 20,685/20,941 (98.8 %), GTFS-RT check clean
  for both. The MPV bus fields are settled against the timetable: field 6 is the current delay,
  field 4 the delay at the last stop (frozen on departure), field 7 the scheduled departure at
  the next stop. `ds` lags field 6 (MPVNET-Q9), so buses now read field 6 (decoder version 2).
  MPV trains stay off live (MPVNET-Q8).
- **2026-10-11** — Recording two new sources (no connectors yet):
  - `mpvnet` (`docs/sources/mpvnet.md`): the MPV net maps of IDOL, JIKORD, ODIS and IDZK, one
    channel each, every 15 s. Regional buses carry literal CIS keys and trains their numbers:
    83–99 % name a trip running at the fix time (2026-10-06 release). City buses of DPO
    (prefix `915`) and DSZO (`825`/`826`) carry the bare line and a course number, not a trip
    number; they are out of scope (MPVNET-Q3).
  - `dpmd/vehicles` every 10 s. The `dpmd@1` filter reads the JavaScript literals and keeps the
    vehicles in service as JSON, without the stops, shapes or drivers: 530 KB becomes 24 KB.

  The fetcher fills `{epoch_ms}` in a URL and stores gzip responses decompressed. The `mpvnet`
  connector is written and replayable but not live (new manifest field `live_feeds`, empty for
  now): on the 40-second capture 95.9 % of running bus key groups bound and the GTFS-RT check is
  clean. Next: about 24 h of capture, then settle the bus delay fields, replay and set
  `live_feeds = ["jdf"]`; trains only once their positions prove to be GPS (MPVNET-Q8). The
  DPMD connector is written the same way (decodes the archived JSON; the worker now decodes the
  stored, filtered body, as replay does): 11/11 running trips bound on the capture, GTFS-RT
  clean, not live yet. Then matching to a trip with an identical timetable (DPmÚL, DPMD) as a per-source
  binding option, then shapes, then Arriva Express, then the API.
- **2026-10-11** — SZT-Q9: boards headed `Nást./kolej` (Ústí n.L. hl.n., Teplice v Čechách)
  show `<platform> / <track>` and were skipped whole by the decoder (read, never recorded);
  now split into a platform and a track assignment. Ústí fixture: 14 assignments from 11 rows.
- **2026-10-10** — DUK-Q1 corrected: Teplice buses and RJÚK trains apply the local offset
  twice to their GPS time (`LastActivityDT` is right); the old "UTC digits" reading put them
  4 h ahead, so all their positions were dropped. Now detected per entry against the unit's
  activity time, only ever subtracted. Corpus: every Teplice/`206xx` entry fixed, no other
  vehicle touched; Teplice positions 0 → 5,357 in the bus golden snapshots (digests
  regenerated). Open: a Teplice bus switching to its next trip while finishing the last over
  the same streets reads as 19 min early (`585101:65`).
- **2026-10-10** — Early running only when observed. 28 DÚK corpus journeys were "30+ min
  early": the tracker took a loop route's second pass at the start (Klášterec `527356`), and
  DPmÚL vehicles say running on a stand past the first stop under the next trip (DUK-Q19).
  Fixed with an asymmetric first-fix prior (early sigma 2 min); before the start, a running state
  counts only at the first stop or when the vehicle is seen moving along the path, plausibly
  early. A vehicle standing at a stop ahead of time is waiting, not running early (7% of bus
  stop predictions later, median +42 s). Trains never get a predicted early departure
  (`max_early_departure_s`, rail 0). Now 0 journeys under −30 min; the rest under −10 min are
  GPS-observed (line `001590` runs ~10 min ahead of its 2024-12 timetable all day). Golden
  digests regenerated.
- **2026-10-10** — R2 implemented (`docs/R2_SLICE.md`):
  - SŽ requests only through a proxy pool from secret config, with no User-Agent, failing
    closed; a 403 opens the circuit.
  - JrUtil serving 5.1 publishes `sr70`/`sr70:track` location keys.
  - Rail runs: one instance per CZPTT path, journeys per train number with `journey_link`,
    GTFS-RT per part (`0007_rail.sql`).
  - SŽ map connector (Křovák via pyproj, NÁZEV20 names) and DÚK + SŽ fusion with the
    `anchor_change` predictor, which beat SŽ's own prediction and plain propagation in replay.
  - Station boards read on demand (about 64/min nationally at noon, 60/min ceiling) give
    platforms; only tracks reach GTFS-RT.
  - Fixed replay tagging every poll with the last channel when replaying several.
  - A train is one GTFS-RT vehicle (opaque per-run ID, train-number label), not one per
    coupled DÚK unit plus SŽ.
  - Validated: 283 unit + 39 DB tests, JrUtil 313 tests, the DÚK golden corpus unchanged, a 2 h
    rail replay clean. Open: the R2 corpus and acceptance.
- **2026-10-10** — `docs/R2_SLICE.md` (rail slice contract) and `docs/sources/sz-tabule.md`
  (SŽ station boards, probed by hand: one request per station, about 20 arrivals and 20
  departures, platforms about 60 min ahead, numbered by platform at big stations and by track at
  small ones; big-station platforms are shown labels only and stay out of GTFS-RT). SŽ is reported to
  IP-ban services: all SŽ requests will go through a proxy pool without a User-Agent. No code yet.
- **2026-10-10** — `docs/sources/dpmd.md`: dossier for the DPMD (Děčín city buses) vehicle map,
  from its page script and a 3-minute capture. Keys are `515<line>:<trip>` (all 15 DPMD lines in
  the release), 8 of 9 live trips resolve; quirks DPMD-Q1–Q9. No connector or 25-hour capture yet.
- **2026-10-10** — Source-specific readings in the core come from the manifest's
  `[channel.semantics]` (`SourceSemantics`): DÚK sets `pre_trip_delay_is_elapsed` (DUK-Q14)
  and `pre_trip_after_end_is_stale` (DUK-Q15); other sources take their facts at face value.
  The DUK-Q18 missed-departure rule applies to every source.
- **2026-10-10** — DUK-Q18: a bus whose onboard unit missed the departure (State 3 all trip long,
  `TODepartureDT` rolled to the next run) runs once it is on the path past the first stop after
  the scheduled start, so it gets GPS delays instead of a bare position. New `SourceDeparture`
  fact; DÚK decoder version 2.
- **2026-10-10** — The feed list page and `feeds.json` name the realtime sources and what they
  cover (DÚK vehicle positions → `cz-jdf-gtfs-rt.pb`).
- **2026-10-10** — The server stack serves feeds with stock nginx instead of Caddy, for its
  per-download `limit_rate` (about 100 Mbit/s); the legacy `/gtfs-rt/jdf.pb` route is gone,
  the realtime feed is `/get-feeds/cz-jdf-gtfs-rt.pb`. Config not run here (no Docker).
- **2026-10-10** — `/get-feeds/` on the server stack: the active release's JDF, filtered JDF and
  CZPTT GTFS plus the live GTFS-RT under stable names (those of the MOTIS node's old directory),
  with a list page and `feeds.json` written by `obehy release feed-list` after each release
  update. Unit-tested; the Caddy routes are not run here (no Docker).
- **2026-10-10** — DPmÚL gaps from the first live night documented and parked: Friday-night
  weekend numbers on night lines (DUK-Q16), trip numbers missing from the release (DUK-Q17).
- **2026-10-10** — First live day exposed DUK-Q14/Q15: a bus in the depot in State 3 with its
  evening trip's key got that trip predicted 113 min late from DÚK's pre-departure `Delay`.
  Source delay is now ignored before departure, and a pre-departure key after the trip's
  scheduled end is `stale_key` (unbound, unstarted journey dropped). Pinned corpus: 15,666
  observations newly `stale_key`, running match rates unchanged, GTFS-RT check clean, golden
  digests regenerated.
- **2026-10-09** — Server image (`Dockerfile`, built and pushed to GHCR by CI on green main, with
  a smoke test) and `deploy/compose.yaml`: PostGIS, migrate, hourly release update, realtime
  worker, nightly jobs at 03:30, Caddy serving the GTFS-RT and the active `gtfs.zip`. Replaces
  the systemd units. Not built locally (no Docker here); the first CI run is the check.
- **2026-10-09** — `obehy jobs nightly` (vehicle days of the last two service dates, then drop
  `rt.observation` days older than 30, policy `[retention]`) run at 03:30 Prague by the server stack. DB test for the drop; raw-archive retention is still missing.
- **2026-10-09** — `release activate --if-newer` (newer than every release published before, so a
  rollback sticks) and `deploy/` systemd service + hourly timer running fetch → load → activate.
  DB test added; the units are not yet installed on a server.
- **2026-10-09** — `obehy release fetch`: newest (or named) `build-*` GitHub release, assets
  checked against GitHub's digests while streaming, packages against `release.json` and their
  manifests, unpacked atomically, three newest kept. Unit-tested with a fake GitHub; the real
  `20261009T030215Z-8cf199243726` (800 MB) fetched and verified in 43 s, a re-run skips it.
- **2026-10-09** — Realtime through reception gaps: predictions use the tracker's GPS lateness
  (source delay as fallback) and hold it through gaps; a silent journey loses its position
  after `stale_after_s` and its predictions after `predict_without_data_s` (per mode, longer
  for rail), counted on the reception clock; calls passed in a gap get an ex-post `inferred`
  time but no history event. Tracking: lateness against the dwell window (waiting at a stop is
  not early), off-path option instead of killing a reading, chord sigma capped, speed slack
  is GPS jitter only, commit agreement among non-negligible readings, crossing times placed by
  timetable time. 522586:107 checked tick by tick (frozen GPS clock at the origin, loop off
  the chord, gap before Vejprty).
- **2026-10-09** — Progress rebuilt as map matching (`timeline/progress.py`): beam of
  hypotheses, commit only events all hypotheses agree on (same bracketing fixes), events over
  `max_event_interval_s` dropped, DUK-Q13 (Azimut 0 = no bearing). 12 scenario tests + real
  závlek traces (001521:104, 522586:103/107/111, 522591:112) pass. Not yet re-run: full replay
  GTFS-RT check on this commit, history replay, golden digests (`tests/golden` uncommitted).
- **2026-10-09** — Progress redesigned (docs): greedy per-fix projection could not handle
  *závleky*, loops, stops passed before being served, corner-cutting chords and reception gaps
  (001521:104 skipped a branch; 522586:103 jumped across one after a gap). Replaced in
  `BASE_PLAN.md` 20.4 / `docs/R1_SLICE.md` 9 by map matching over the trip's path with a beam
  of hypotheses and commit-when-all-agree. Implementation next.
- **2026-10-09** — Realtime speed: 290 → 85 µs per observation (25 h DÚK replay 7m17s → 2m20s):
  projection only within reachable distance of current progress (policy `max_speed_mps`; full
  scan after gaps), shapes simplified to ~5 m, out-of-tolerance segments skipped, estimates only
  at emit time, scheduled instants cached, replay prefetches keys 40 polls ahead. Bindings
  unchanged (identical reasons and rates); 3 % of trip updates changed, because a fix can no
  longer snap several stops ahead at once. Real replay of 2026-10-05/06 on release
  20261006T194555Z: running key groups bound DÚK 99.4 %, Teplice 99.1 %, DPmÚL 54.7 % (old
  resolver 99.1 / 96.7 / 52.7 % on the same release).
- **2026-10-09** — R1 pipeline complete in code: per-connector manifests
  (`realtime/sources/*.toml`, recorder unchanged in behaviour), DÚK connector with quirk tests,
  shared scheduler, `Runner` (lazy index, invariants), `obehy rt replay` rebuilt on it
  (GTFS-RT snapshots, `--write-history`), `obehy realtime` worker (warm replay, emit tick,
  release rebase on NOTIFY), `obehy jobs vehicle-day`, `obehy rt corpus pin`. Legacy
  `resolve`/`episodes`/`decode`/`release_index` removed. End-to-end DB tests: deterministic
  replay and history rebuild, warm restart equals an uninterrupted run, mid-day release switch
  orphans vanished calls. Not yet run on the real corpus.
- **2026-10-09** — Migrations 0005 `rt` and 0006 `history` (journey-keyed, partitioned by
  service-date month, `tour_id` reserved); `emit/db.Writer` (COPY observations, revisioned
  snapshots with orphaning, events, assignments, current state, `clear_history`, partitions on
  demand); per-feed GTFS-RT (matched only, deterministic); `jobs.vehicle_day` SQL (T13). DB tests
  pass on the remote dev database.
- **2026-10-09** — Timeline (single source): path from shape or straight stops, monotone
  progress with backtrack tolerance, off-route hold/flag/rejoin, arrival and departure events as
  fix-bounded intervals (loops attach to the right visit), predictions from the source delay with
  monotone repair, `pre_trip` → `running` → `finished`. 10 scenario tests incl. DUK-Q5/Q6.
  `stale` is set at emit time (ticket 6).
- **2026-10-09** — `core.step` with keyed binding (`infer/keyed.py`: date rule of §19.3,
  binding continuity, reason codes), vehicle states and the pre-trip lifecycle; scenario tests
  T2–T5, DUK-Q3, DUK-Q4, DUK-Q11 pass. The timeline is still a stub that only tracks delay.
- **2026-10-09** — Realtime foundation: `model.py` (facts, journeys, state, effects, JSON
  round-trip), `policy-v1.toml` + loader, `index.py`, the test timetable builder and the
  import-linter purity contract; `index_sql.IndexLoader` fills the index lazily by source key from
  `static.*` by load_id, and `ensure_release` loads a release directory on demand. DB tests (index
  equals builder) pass against the remote dev database.
- **2026-10-09** — `realtime/times.py` (T1, T6–T12, T14, T15 pass) and its SQL twin
  `control.obehy_instant` (migration 0004; agrees with Python on the DST cases against the remote
  dev database). ruff `DTZ` on, with ignores only for the legacy `decode.py` and its test. DB tests
  read `[database] test_url` from the local config.
- **2026-10-09** — `docs/R1_SLICE.md`: core types, time scenario table T1–T14, `rt`/`history`
  DDL sketch, connector manifest and policy shapes, fixtures, R1 acceptance. Restart by warm
  replay replaces the checkpoint; stable `tour_id` added to section 22. Docs only. Schedule times
  are wall-clock (DST nights may be slightly wrong); nonexistent or skewed source times are
  dropped, never guessed.
- **2026-10-08** — `ARCHITECTURE.md` (Mermaid diagrams) and the remaining long-lived decisions
  (docs only).
  - Connector contract: polled channels and lazy lookups (`plan` → `fetch` → `decode`), run by
    a generic scheduler and lookup broker; the API never calls upstream.
  - Capability routing, including minimal "line + trip + delay" sources.
  - Vehicles: identity levels, minted `vehicle_id`, curated files via `obehy ref import`, the
    effective-attribute view, media.
  - Compositions as journey segments, separate from `vehicle_assignment`.
  - Four circulation sources with their precedence (§22, §27–28).
  - Fixed now:
    - rail journey = train number + date with `journey_link`;
    - core partitioned by feed;
    - observation envelope and lookup replay;
    - journey-key public URLs and a minimal public realtime model;
    - no human input.
  - Validated: diagrams previewed in light and dark mode, `git diff --check`; no code changed.

- **2026-10-08** — Core runtime architecture decided (docs only).
  - `BASE_PLAN.md`:
    - module layout with an import-linter layering rule; `core.step` shared by worker and
      replay; SQL for set-wise work;
    - release index built from `active.*` in PostgreSQL, `release_id` on every realtime row;
    - literal keyed matching, with keyless inference isolated;
    - time rules in one `times.py` (§19.3);
    - multi-source arbitration with the "anchor plus predicted change" rule (§20.6);
    - trip lifecycle and off-route grace (§20.7);
    - circulation edges keyed by literal keys, with a fingerprint validity guard (§22);
    - journey-keyed, self-describing, revisioned history (§29);
    - API v1, PMTiles and vehicle states (§30);
    - rolling raw window plus pinned corpora (§18.4).
  - `AGENTS.md` gains realtime code rules and documentation roles. The dossiers gain quirk
    ledgers (DUK-Q1–Q12, SZ-Q1–Q9, ARRIVA-Q1–Q7).
  - Stale v4 references fixed in README, STATIC_PIPELINE and JDF_SEMANTICS (v5 counts from
    `20261006T194555Z`).
  - Validated: `git diff --check` and review only; no code changed.

- **2026-10-08** — DPmÚL replay against the 2026-10-08 packages (JrUtil `17179c9`): 1,573 of
  1,973 running episodes resolve (79.7%; 52.7% before the merge fix).
  - Most of the rest is upstream, not a JrUtil bug: 268 of 275 off-calendar episodes are
    weekend (`3xx`) trip numbers reported briefly on weekdays, mostly between two resolved
    trips. Line 595200's JDF (batches 4312, 10217) and the release calendar agree.
  - Inference must not rebind a vehicle on such keys. Details are in `duk.md`.
  - The 7 DÚK-range losses are lines whose 2026-10-06 version is no longer in the release.

- **2026-10-08** — `obehy rt replay` (§5 step 4, replay only).
  - `src/obehy/realtime/`: `decode` (DÚK/SŽ/Arriva payloads → rows: fleets, Teplice UTC
    and Arriva local-time fixes, SŽ TR keys), `release_index` (pyarrow indexes over the
    serving Parquet: keys, services, trip/run spans, calls), `episodes`, `resolve` (the
    dossier rules, SŽ train-number fallback, Arriva next-stop scoring, stale-repeat
    collapse), `replay` (`report.json` + `episodes.parquet`). No DB, no new dependencies.
  - Validated: 6 new unit tests (134 total), ruff, pyright. The 25-hour capture against
    `20261006T194555Z` reproduces every dossier table and is byte-identical across runs:
    41 s, 2.5 GiB peak (all decoded rows are held in memory, so it grows with the range).
  - Correction: the 19 unresolved DÚK train episodes have CZPTT train-number keys, but no
    timetable for them runs that day (`duk.md` said they were missing from the package).

- **2026-10-08** — CZPTT source cache and download concurrency.
  - GH Actions spent 1h+ fetching ~170k sub-KB objects over 8 connections (round-trip bound).
    Objects are now cached across runs (`cache/czptt-sources`, rolling Actions cache) and fetched
    over 32 connections independent of `--jobs`. Unit tests, ruff, pyright pass; no live-portal or
    GH run yet.

- **2026-10-08** — Reviewed Czech data moved to jrunify-ext-geodata; route presentation overrides.
  - `routes/transport-modes.csv` (was `obehy/data/jdf_transport_mode_rules.csv`),
    `overlay/pid-stop-overrides.csv` and `filtered-jdf/rules-v1.json` now live in the geodata
    checkout; the run manifest records them under `route_rules`. The empty CZPTT OSM alias file and
    JrUtil's `--osm-aliases` are gone: foreign passenger CZPTT points without coordinates fail.
  - New `routes/presentation.csv` overrides route marking and colours by licence (exact, range,
    prefix) and optional agency IČO; JrUtil applies it in `jdf-to-bundle` and again after
    `regional-gtfs-overlay`, so it wins over PID/IDS JMK values. Route IDs are unaffected.
  - Validated: JrUtil tests (313), obehy unit tests, ruff, pyright, geodata `test_routes`. No
    real-data run yet; the rules file is still empty.

- **2026-10-08** — Sources fetched up front; filtered JDF from LinExt instead of the line portal.
  - Three CI builds failed ~45 min in on a TLS handshake timeout to portal.radekpapez.cz (no retry,
    no source named). The portal's IDS query equals preferred `LinExt` rows (PID 1921/1921, IDS
    JMK 1208/1208 on 2026-10-08); on that day's merged JDF the derived filter matches the portal
    except 235005 (no LinExt row in any source version, accepted) and adds DPMO 895xxx, PMDP
    446007 and six FlixBus-co-operated international lines (`Altdop`). `--line-filter-snapshot`
    is gone.
  - New `fetch-sources` stage before the JrUtil build (`build_sources.py`): national JDF and
    CZPTT become source snapshots for their builders; every download retries 3× (5 s, 15 s) and
    fails as `Download <source> failed after N attempts: <url>: <reason>`; `release.json` gains
    `retrieval`. Validated: unit tests, ruff, pyright; live fetch of JDF/PID/IDS JMK (17.5 s) and a
    refused host (named error after 26 s). CZPTT fetching only via unit tests; no CI run yet.
  - Not done: LinExt is not in the serving contract; its secondary rows (another system's tariff
    valid on a line) would need a `route_integration` relation for fares.

- **2026-10-07** — Release mirror (§5.3, milestone C1 without fetch).
  - `src/obehy/release/`: migration runner (`control.schema_migration`, checksums), `control`
    schema (release, package, load, publication + history), static DDL generated from the
    vendored `serving-v5.json` (19 relations + `service_date`, `shape_line`, location `geom`),
    loader (hash/contract verification, COPY via pyarrow CSV, PK/index build, set-wise FK and
    row-count checks, unknown-enum warnings, attach in one transaction), activation through
    regenerated `active.*` views with NOTIFY, stack rollback, retention (active + 2
    predecessors + staged loads). New deps: psycopg 3, pyarrow. `compose.yaml` for PostGIS 17;
    CI runs the DB tests in a postgis service.
  - Validation: 129 tests (13 against PostgreSQL), ruff, pyright. Real load and activation of
    `20261006T194555Z`; the serving-v4 release `20261003T144231Z` is rejected.
  - Finding for the API: departure boards must drop each trip's final passenger call. PID leaves
    pickup allowed there, and CZPTT run parts meet at the line change (Os 8503 arrives at
    Nymburk as S31 and continues as S2 → Poděbrady, linked by an in-seat transfer).
  - Remaining: `release fetch`; GTFS paths and resolver version in the publication; inference
    indexes.

- **2026-10-06** — Realtime replay (§5.1): 25 hours of DÚK, SŽ and Arriva Express payloads
  resolved against the v5 release; dossiers updated with the results. Findings: Arriva's
  `lastStopName` is the next stop and `updated` is local time; DÚK `Delay` is signed (unsigned
  for the DPmÚL and Teplice fleets), DÚK also carries trains (resolved through CZPTT by train
  number), and vehicles keep stale trip keys while positioning; DÚK vehicle chains are 90%
  same-stop links. Fixes in JrUtil found along the way: overlay `call_key` positions (`0d85ece`),
  CZPTT call features and stopless in-seat transfers (`b65cc9b`), and date-by-date JDF version
  resolution for DPmÚL's parallel versions (`6a86278`; bounded old/new merge comparison on 804
  urban-rail batches and 518 bus lines: no overlapping versions, only corrections). Validation:
  JrUtil `dotnet test` 309/309. The replay became a repo tool on 2026-10-08.

- **2026-10-06** — Serving schema 5.0 implemented in JrUtil (§6): writer, `validate-package`
  enumeration/prefix/key-encoding/trip-part/GTFS-projection/source-digest checks, contract
  `serving-v5.json` normative, `serving-v4.json` removed, golden expectation `serving-v5.txt`.
  Oběhy's manifest gate accepts any `5.<minor>`. Validation: JrUtil `dotnet test` 304/304; Oběhy
  unit tests, ruff, pyright clean. Skipped: the bounded real-data run (no golden inputs on disk).

- **2026-10-05** — Serving schema 5 drafted (§6) after a v4 fitness check on the 2026-10-03
  release: CZPTT trips already link to their PA through `czptt_pa_id` bindings (all 583,171
  calls align, 53 corrected times differ); GTFS `stop_times` equal the passenger `trip_call`s in
  both feeds; CZPTT `source_snapshot_sha256` is all zeros; `location.domain` is `scheduled`
  everywhere. The planned CZPTT contract checks and the DB foundation were dropped in favour of
  v5. `BASE_PLAN.md` now targets v5. Validation: documentation and draft JSON only.

- **2026-10-05** — daily `build` workflow (`.github/workflows/build.yml`).
  - 02:30 UTC and on demand: `obehy build --estimated-posts --memory-budget 8GiB` on
    `ubuntu-latest`, work on `/mnt`, JrUtil and geodata from their standalone `main`.
  - Caches the merged OSM snapshot plus derived extracts per month (`--refresh-osm` only on a
    miss) and the routing cache per run; publishes `build-<run-id>` Releases (release.json + one
    tar per package), keeps 14; run logs/diagnostics as 14-day artifacts.
  - Validation: actionlint clean. Not yet run on Actions.
- **2026-10-05** — `obehy rt record` (core runtime step 1).
  - Polls the channels in `src/obehy/data/realtime/sources.toml` (DÚK 15 s, SŽ 30 s, Arriva
    30 s) into `data/rt-raw/<source>/<channel>/<UTC date>/`: an `index.jsonl` line per poll
    (errors included) and content-addressed zstd objects. New dependency: `zstandard`.
  - Arriva's fleet-wide GraphQL feed is reduced to Arriva Express before storage
    (`arriva-express@1`; source size and hash are kept in the index). Arriva exposes no trip
    number: introspection is off, no trip fields or arguments exist, and there is no detail
    endpoint.
  - Validation: Oběhy unit 91, ruff and pyright clean; a live `--once` run and a 2-minute run of
    all three channels. Storage is about 6.5 KB per DÚK poll and 28 KB per SŽ poll (about
    120 MB/day). Remaining: record several days on Linux, then answer the dossiers' open
    questions from the capture.

- **2026-10-05** — JrUtil `d26bf0b`: `location.district_code` now carries the JDF okres code,
  and the regional overlay keeps base location metadata (municipality, district, nearby place,
  country, coordinate precision); before, every one of these columns was null in the published
  `jdf` package. New overlay test; Release suite 303 passed. Takes effect with the next build.

- **2026-10-05** — Architecture rewrite of `BASE_PLAN.md` for the core runtime.
  - Decided: two feeds (`jdf`, `czptt`); the file registry is the permanent identity model (no
    identity service; `IDENTITY_REGISTRY.md` removed; `jrutil-identity-v1` is final); builds on
    GitHub Actions; one realtime process with in-memory state and a Postgres log.
  - New realtime design: fact-based trip inference with date inference, rail runs, interval
    timeline engine, progress integrity on A→B→A routes, own GPS delay, pluggable travel-time
    providers for long segments only, learned circulations.
  - Validation: documentation only; no code or tests changed.

- **2026-10-02/03** — Two complete live `obehy build` runs published (`20261002T184241Z`,
  `20261003T144231Z`; timings under Last validation evidence). The 2026-10-03 run used the
  freshly seeded stop registry and serving schema 4. An earlier 2026-10-03 attempt failed on a
  missing JDF post-candidate OSM extract (`obehy-osm build` fixes it).

- **2026-10-03** — Serving schema 4 (bundle v3), the final pre-core contract revision.
  - Zones: `fare_system`, `fare_zone` removed; zones are codes with an optional system, kept on
    the route stop slot when all its calls agree, otherwise on the calls; `location_zone` is the
    per-stop union.
  - `route_stop`: merged per route direction from the final calls of every producer (LCS
    alignment, one slot per visit); `trip_call.route_stop_id` and restrictions point at it.
  - Removed `route_segment`, `identifier_alias`, `object_origin`, `binding_evidence` and the
    null `source_run_id`/`source_duty_id`; `validate-package` checks foreign keys.
  - Oběhy accepts only bundle v3 / schema v4.
  - Validation: JrUtil Release 302 tests; Oběhy unit 75, ruff and pyright clean.
    No golden-subset or live run yet: route-stop merge cost, zone row counts and FK validation
    time on national data are unmeasured.

- **2026-10-03** — Stop-ID registry (§2).
  - JrUtil `--stop-registry`/`--stop-registry-candidates` for `merge-jdf`, `jdf-to-bundle` and
    `regional-gtfs-overlay`; `source_stop_metadata` gains `okres` and `stop_id_provisional`.
  - jrunify-ext-geodata `registry/` plus `registry.py validate|promote`.
  - Oběhy passes the registry and keeps the review CSVs in the release.
  - Validation: JrUtil Release 296 tests; Oběhy unit 75, ruff and pyright clean; geodata
    `test_registry` passes. No live or bounded real-data run yet.

- **2026-10-02** — Speed pass and routing cache.
  - JrUtil: CZPTT 305 → 33 s, bundle-posts 392 → 182 s, overlay 232 → 170 s on the golden
    subset; CZPTT, overlay and the no-posts bundle are byte-identical.
  - Road thread identities are now deterministic (canonical walk); a few estimated posts change.
  - `obehy build` passes `--routing-cache` by default (`workdir/cache/routing`,
    `--no-routing-cache` to disable). Entries are revalidated per graph tile, so they survive daily
    demand clips and monthly OSM updates where the network did not change.

- **2026-10-01** — Learned post scorer by default; post-inference and overlay cuts.
  - `obehy build --estimated-posts` passes `learned-v1.json` unless another policy is named.
    On the golden subset, calls left at the stop centroid fall from 354k to 186k.
  - Removed evidence-backed replay, review GeoJSON, diagnostic labels, the
    `derived_post_scores` table, side groups and same-stop pairs. Bundles always route live;
    capture and `jdf-export-post-features` remain for retraining.
  - Overlay: one combined-source path (profile schema v4, capabilities as a list); calibration,
    publication flags, coverage floors, route/trip overrides and unread reports removed. The
    PID Národní třída stop override now applies (it was silently ignored).
  - Validation: JrUtil Release 274, post-scorer 26, Oběhy unit 70, ruff and pyright clean.
    Golden gate `learned` → `cuts`: GTFS, serving and diagnostics identical in all four
    packages; only compiler provenance and build-spec hashes differ.
