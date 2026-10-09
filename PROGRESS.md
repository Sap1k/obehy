# Oběhy progress

Short status and the working backlog. Log entries before 2026-10-01 are in git history;
`BASE_PLAN.md` holds the long-term architecture.

## Current state

### `obehy build`

One command (`src/obehy/cli.py`) resolves the GVD year, validates or refreshes the shared OSM
snapshot, builds JrUtil once, and runs these stages. Production runs daily on GitHub Actions
(`.github/workflows/build.yml`, `BASE_PLAN.md` section 15; not yet run there).

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

Work order: §5 (core runtime) in `BASE_PLAN.md` section 34 order. §1, §4 and §6 are done; the
rest of §2 and §3 and static acceptance wait on the first GitHub Actions build.

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
  (`release fetch` 2026-10-09; the server runs as `deploy/compose.yaml` from the CI-built
  image: hourly fetch → load → `activate --if-newer`, worker, nightly jobs, Caddy; not yet
  deployed).
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
  Predictions follow `BASE_PLAN.md` 20.5 (dwell recovery, early running carried over, no
  uncertainty); per-stop holding and knock-on to the next trip of a tour wait for history
  learning and circulations (section 22). JrUtil's
  `serving-v5.json` time description still says noon − 12 h and needs correcting.
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

## Recent log

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
