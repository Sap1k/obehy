# Oběhy progress

Short status of the static feeds and the working backlog. Earlier dated handoffs (July–September
2026) are in git history; `BASE_PLAN.md` holds the long-term architecture.

## Current state

### `obehy build`

One command (`src/obehy/cli.py`) resolves the GVD year, validates or refreshes the shared OSM
snapshot, builds JrUtil once, and runs these stages. Production runs daily on GitHub Actions
(`.github/workflows/build.yml`, `BASE_PLAN.md` section 15; not yet run there).

1. **national-jdf** — download VLD and dráhy CIS JŘ archives → `fix-jdf` (stop matching,
   coordinate estimation, `regional-adjacent` international policy) → `merge-jdf` (name-based
   stop reconciliation) → `jdf-to-bundle` → `validate-package`.
2. **regional-snapshots / regional-overlay** — freeze PID and IDS JMK GTFS and overlay both onto
   the national JDF package in one `regional-gtfs-overlay` pass.
3. **filtered-jdf** — a plain GTFS derived from the pre-overlay national JDF, filtered like
   gtfs-processor (see Next steps §4).
4. **national-czptt** — CZPTT annual + monthly changes, KADR, SR70 → `czptt-to-bundle`. By default
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
  `active.*` views. Derived: `service_date`, location `geom`, `shape_line`. `release fetch` is
  not written yet.

### Last validation evidence

- Live `obehy build` (estimated posts, learned-v1, routing cache), both published:
  - `20261002T184241Z`: 45.3 min (JDF 19.4, overlay 4.7, CZPTT 20.1 min).
  - `20261003T144231Z` (serving schema 4, seeded stop registry): 39.2 min (JDF 16.8,
    filtered JDF 0.5, overlay 5.0, CZPTT 16.0, validation 0.8 min). Zero stop, post or
    overlay-place registry candidates.
  - `20261006T194555Z` (current; local, serving schema 5.0): all three packages valid.
  - Not run on either: MobilityData GTFS validator, MOTIS import check.
- JrUtil Release suite: 309 tests (2026-10-06).
- Oběhy: 116 unit + 13 PostgreSQL tests, ruff and pyright clean (2026-10-07).
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
- The live post estimator still builds the routing graph on every run; there is no persistent
  graph cache.
- Serving validation checks keys, hashes and foreign keys; cross-representation content
  validation (GTFS vs serving relations) is incomplete.
- No route shapes are generated yet (MOTIS shape generation is future work).

## Next steps

Work order: §6 (serving schema 5) first, then §5 (core runtime) against it. The rest of §2 and §3
waits on the first GitHub Actions build; §5.1 needs no database and continues in parallel.

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
- **Filtered feed:** drop lines found on portal.radekpapez.cz for FlixBus CZ/DACH/Polska, PMDP
  and DPMO, and for IDS PID/IDS JMK/IDZK. Also drop the checked-in line-number prefixes. Remove
  calls at stops without coordinates and keep `[?]` stops. Customs stops are handled in JrUtil, not
  in the filter.
- **CZPTT:** operational points go to the sidecars only by default (`--czptt-operational-points
  gtfs` restores the old behavior).
- **Status:** done. Implemented (`src/obehy/filtered_jdf.py`, rules in
  `src/obehy/data/filtered-jdf/rules-v1.json`) and published by both live builds
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
- **Status:** step 1 done (2026-10-06; the replay ran as exploratory scripts, not yet a repo
  tool; recording continues). Step 2 done (§6). Step 3 done except `release fetch`, which waits
  for the first GitHub Actions release (2026-10-07). Next: step 4, the realtime skeleton. Steps
  5–12 are design only.

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

## Recent log

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
  - Findings for later: departure boards must drop each trip's final call (PID leaves pickup
    allowed there); CZPTT has two trips for Os 8503 at Nymburk (S2 and S31) at the same time.
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
  JrUtil `dotnet test` 309/309. Remaining: the replay as a repo tool.

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

- **2026-09-30** — Refactor of JrUtil and Oběhy before optimisation work.
  - JrUtil: dead upstream code, v1 packages, CZ extensions, field-level provenance, staging
    directories and relation sorting removed; bundle v2 / serving schema v3. Removed options
    include `--stop-ids-cis`, `--international-route-overrides` and `--sr70-name20`. The overlay
    takes `--converter-version` (Oběhy passes the base package's). God-modules split
    (JdfBundle, JdfToGtfs, CzPttToGtfs, PackageWriter, Utils, overlay Support, multitool).
  - Oběhy: serving-v1 stack removed; shared `obehy.pipeline` package (errors, files, reporting,
    process, download, jrutil, args, staging); JrUtil runs as the built DLL everywhere; one
    User-Agent; one `failure.json` at the staging root; overlay stage in `regional_overlay.py`.
  - Validated: JrUtil 278/278, Oběhy 76/76 with ruff/pyright. The bounded golden semantic
    comparison against the pre-refactor baseline shows only the allow-listed contract changes in
    every stage. Peak private memory is lower in every stage; overlay wall time -47%, CZPTT -13%.
    Single-sample JDF fix/merge/routing times moved +8-31% with unchanged code and worker plans;
    earlier runs of identical code spread by up to 35%, so a repeated benchmark should confirm.

- **2026-09-29** — JDF output: one route per line, detour routes, GVD-bounded validity.
  - `merge-jdf` requires `--gvd-year`/`--reference-date`. It drops expired and next-GVD
    versions and clamps the rest to the GVD, so bogus "forever" international validity ends
    at the cutover. `jdf-to-bundle --gvd-year` records `service_horizon`, and the overlay
    rejects a base from another GVD. `obehy build` passes both values.
  - Merged versions collapse to `jdf:route:<line>`, plus `jdf:route:<line>:detour` for
    výluka timetables with amber text (`ffd23f`, or `7a3500` on light colours). Other
    semantics get a hashed suffix. Serving `route.timetable_kind` is `regular`/`detour`.
    Route-stop keys carry the version. The overlay attaches source trips to the regular route
    and keeps detour colours.
  - Validated: JrUtil 304/304, Python unit tests, and a subset run (36 dráhy + 4
    international VLD batches, PID GTFS cut to 8 tram lines). Unmatched PID trips on those
    lines fell from 3,488 to 1,307, and additions now land on the CIS routes. The full
    feed was not run.

- **2026-09-29** — JdfMerger: open-ended detour versions no longer suppress later versions.
  - CIS publishes PID tram detours open-ended (same end as regular versions). An older detour
    used to delete every later regular version: 255 removals on 181 lines, including trams 12,
    13, 18, 19 and 24, so about 40 % of the unmatched PID overlay trips were projected instead
    of matched. Bounded detours keep their priority.
  - Validated: new JdfMerger unit tests (fail before the fix), and a merge-jdf run on 36 dráhy
    batches, where every affected line chain ends on its current version. A PID overlay
    re-run is still needed (it requires a full base bundle). The rest of the PID gap is a CIS
    data gap: temporary lines 32/40/X*/XS*, P1/P2, and diversions missing from the CIS export.

- **2026-09-28** — Post estimator: learned scorer for terminals where bays lost to a
  lower-penalty street post (Most/Litvínov, nádraží).
  - Model: two-stage conditional logit (`jrutil/scripts/post-scorer`) trained on PID + IDS JMK
    GTFS posts (about 140k labelled contexts), traffic-weighted. OSM `route_ref` is not used.
  - Results on held-out stops (weighted precision / share of contexts placed):
    PID 0.885 / 78% vs policy 0.846 / 20%; JMK 0.839 / 74% vs 0.787 / 19%.
  - JrUtil port: policy schema v3 embeds the model (`JdfPostScorer.fs`, new `Area`
    resolution) via `repo/src/obehy/data/post-inference/learned-v1.json`. Python/F# parity:
    0 decision differences in 261k Ústecký contexts. Also: region-restricted capture,
    `jdf-export-post-features`, OSM `local_ref` in evidence tags.
  - Fixes found on the way:
    - overlay transfer conflicts merged or quarantined;
    - growth-gated memory reclaim;
    - `route_stop` published at the stop place, not a post (national estimated-posts
      builds crashed on it);
    - `obehy build` now passes absolute policy/evidence paths and gives CZPTT the geodata
      root (`SR70.csv` was not found).
  - National run with `learned-v1.json`: JDF bundle (57 min, post evaluation 13 min), overlay
    (16 min) and package validation passed. CZPTT stopped on the SR70 path before the fix;
    CZPTT was not rerun.
  - Validation: JrUtil Release 293 passed; post-scorer 26; Oběhy unit 96, ruff clean.
    Pyright: 4 errors, all in `test_national_czptt.py`, also present without these changes.
  - Remaining:
    - ~~make `learned-v1.json` the default~~ (done 2026-09-30);
    - noisy JMK bus-station labels;
    - posts off the evidence router's corridor are never chosen (e.g. the highway post at
      Teplice, Zámecká zahrada).

- **2026-09-28** — Docs refocused on static-feed readiness; old PROGRESS entries left to git
  history.
- **2026-09-28** — `obehy build` gained a `filtered-jdf` stage that publishes
  `release/jdf-filtered/{gtfs.zip,filter-report.json,line-snapshot.json}` and
  `current.json:jdf_filtered`. Flags: `--skip-filtered-jdf` and `--line-filter-snapshot` (offline
  replay). CZPTT now defaults to `sidecar`.
  - Standalone run against the retained 2026-09-16 national JDF bundle with a live portal query:
    70 s. It removed 4,442 of 12,734 routes (1,717 lines; 1,587 from the portal) and kept 309,200
    trips and 50,298 stops. It removed no calls for missing coordinates (the six `0,0` stops are
    on removed PID lines).
  - The output passed `verify_gtfs_stops`. Unit suite 96 passed; Ruff clean. Pyright is clean on
    the changed files; the four errors left in `test_national_czptt.py` predate this change.
  - MobilityData validation of the filtered feed and a complete live build have not been run.
- **2026-09-28** — JrUtil JDF→GTFS: timed calls at stops with fixed code `$` (border/customs
  stop, e.g. `Varnsdorf,CLO`, `Neugersdorf,ZOLL`) are kept with `pickup_type=1`/`drop_off_type=1`.
  Previously JrUtil ignored `$`, so these calls were boardable.
  - Why CLO/ZOLL stops look missing: on regional lines such as 001401, the source marks every
    CLO/ZOLL call `|` (passes) or `<` (not served), so GTFS has no stop there. This is correct.
  - Of the 205 lines with timed `$` calls in the 2026-09-16 merged JDF, 203 are rejected whole by
    the `regional-adjacent` international policy. Only 000297 and 000326 reach the bundle.
  - New regression test in `JdfToGtfsTests`; the full JrUtil Release suite passes (278). No
    national rebuild yet.
- **2026-09-25** — CZPTT: flagged inconsistent interior times are forced monotonic when bounded
  (fixes the 29 rejected R10 replacement PAs in a direct replay). Live acquisition rechecks the
  inventory until stable (max five passes). No national rebuild yet.
- **2026-09-20** — CZPTT split-trip edges get GTFS-only approximate times. NAD transfers are
  limited to the same station. KADR agency names drop ` - ` qualifiers.
- **2026-09-19** — National finalization is bounded, 4.07 GiB peak on replay. `obehy build`
  forwards `--jobs`/`--memory-budget`.
- **2026-09-17** — CZPTT gains notes/accessibility/bicycle/request-stop semantics, NAD modelling
  with transfers, and cancellation ordering by source timestamp. The regional overlay's quadratic
  coverage stall is fixed (~3 min package validation vs ~18 min).
- **2026-09-15** — Streaming JDF compiler: 179 s / 2.88 GB. Optimization was stopped at the
  user's request.
- **2026-09-14** — Two-package `obehy build` with atomic `current.json` publication.
