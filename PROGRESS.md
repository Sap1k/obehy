# Oběhy implementation progress

This file is the concise engineering handoff for completed work. `BASE_PLAN.md` remains the
authoritative roadmap and architecture document.

## 2026-08-04 — Shared publication retention hardened

### Delivered

- Build activation now supports multiple named publications sharing one static build. Moving one
  pointer no longer retires or permits pruning that build while another publication still uses it.

### Validation evidence and limits

- The full suite passes 73 tests with one expected opt-in live-national skip. The five-test
  PostgreSQL lifecycle suite covers shared activation, independent pointer movement, retention and
  final retirement; Ruff format/lint and `git diff --check` pass. Publication changes remain
  transactional within one database.

### Next handoff

- Continue with the PID static mapping emission and realtime vertical slice described below.

## 2026-08-04 — Namespaced contextual realtime mappings

### Delivered

- Namespaced every source entity/trip/call identifier and added optional source route, direction,
  endpoints, scheduled end, block/run/duty IDs and call-pattern context to trip mappings. The
  resolver now supports namespaced entity and call lookup and rejects supplied contradictory
  context, allowing PID regional GTFS identifiers to remain the primary path when CIS keys are
  absent.

### Validation evidence and limits

- The full suite passes 72 tests with one expected opt-in live-national skip. Ruff format/lint,
  strict Pyright, and `git diff --check` pass. The regenerated baseline completes a
  downgrade/re-upgrade against the configured remote disposable `obehy_test` database and the
  final `alembic check` reports no new operations.
- Contextual matching remains exact and deliberately non-fuzzy. JrUtil still needs to emit these
  namespaced mapping fields, and realtime claim tables will separately record observation source,
  supplied identifiers, chosen mapping path, and rejection diagnostics.

### Next handoff

- Emit the namespaced PID GTFS entity/trip/call mappings from JrUtil, then implement the PID
  vehicle-position vertical slice using `pid-vehiclepositions` as observation provenance and
  `pid-gtfs` as identifier authority.

## 2026-08-03 — JDF 1.11 semantic sidecars and database shape frozen

### Delivered

- Documented the current JrUtil loss/approximation boundary against the official JDF 1.11
  `Pevnykod`, `Caskody`, and `Navaznosti` definitions in `JDF_SEMANTICS.md`. JrUtil itself was not
  changed. Its bicycle allowed/forbidden projection remains explicitly correct; pickup/drop-off
  `phone_before` remains a useful GTFS approximation for order/conditional service, but neither is
  treated as a substitute for typed source semantics.
- Extended serving-package/database v1 to 33 relations. `service_feature_assignment` preserves
  route/trip/call reservation, accessibility, refreshments, luggage, bicycles, order, condition,
  request, and boarding semantics with source code/note/provenance. `location_feature` preserves
  WC/accessibility/request-stop and CLO/MHD/rail/line/metro/ship/airport/P+R facts.
- Extended connection claims with source-trip/public-line/destination fields and explicit
  `none`/`structured`/`spec_note` derivation. Future parsing of the specification-defined note forms
  belongs in JrUtil; Oběhy retains and resolves claims but never parses or guesses. Only uniquely
  resolved claims become routable transfers.
- Added typed notes/assignments, scoped restrictions, complete CZPTT operational relations,
  district/coordinate detail, ordered zones, and enriched source coverage to the executable
  serving schema and deterministic fixture.
- Pinned a packaged machine-readable NeTEx v2.0.0 mapping ledger. Its canonical digest is mandatory
  in the serving manifest, and tests require the ledger to cover every field in each typed semantic
  relation.
- Regenerated the sole Alembic database-v1 baseline from SQLAlchemy metadata, with hand-written DDL
  limited to `control`/`static` schemas and PostGIS setup.

### Validation evidence and limits

- The full suite passes 72 tests with one expected opt-in live-national skip, including four
  focused PostgreSQL load/activation/failure/rollback/retention tests. Ruff format/lint,
  strict Pyright, and `git diff --check` pass.
- The regenerated baseline completes downgrade/upgrade/downgrade/re-upgrade against the configured
  remote disposable `obehy_test` database; the final `alembic check` reports no new operations.
- The mapping ledger is a coverage and design contract, not yet a PostgreSQL-to-NeTEx exporter or
  proof against the official XSD. JrUtil does not yet emit the 33-relation package, preserve all
  fixed codes, or parse `Navaznosti` note forms. Those remain explicit blockers for the lossless
  NeTEx gate.

### Next handoff

- Extend JrUtil from `JDF_SEMANTICS.md`, beginning with typed fixed-code/note/connection emission;
  then implement the reference NeTEx exporter and round-trip fixture before production package
  emission is accepted.

## 2026-08-03 — PostgreSQL compiler removed; serving database v1 established

### Delivered

- Removed the national PostgreSQL compiler/importer, its slice and bundle-reader tools, source-fact
  and reconciliation schema, canonical allocator/identity services, CLI entries, migrations and
  compiler tests. National JDF/CZPTT acquisition and conversion-bundle builders remain unchanged.
- Replaced the disposable migration history with an Alembic-autogenerated database-v1 baseline in
  explicit `control` and `static` schemas. Control relations cover content-addressed artifacts,
  immutable source/config snapshots and build specs, queued jobs/attempts/events, complete build
  digests, validation/diagnostics, publication and retention. Registry-owned identity is absent.
- Added 33 LIST-partitioned finalized schedule, typed JDF semantic, operational, provenance and
  runtime-mapping relations.
  IDs are unrestricted text and support labelled provisional `v0:` values plus later railway and
  registry formats without a schema change.
- Froze the executable serving-package v1 contract as typed sorted Parquet. Validation checks the
  canonical manifest, Arrow schema/nullability and metadata, path safety, hashes, sizes, counts,
  strict key ordering and aggregate digest before database work.
- Added transactional staging/COPY, set-wise passenger/operational-call, location, coverage and
  route-segment validation, complete partition attachment, PostGIS shape derivation, atomic
  activation/rollback, failed-load cleanup, three-build retention, exact aliases and dated
  source/CIS/train resolvers. Ambiguity raises rather than choosing a row.
- Reordered the roadmap around a provisional PID overlay/realtime proof. The permanent registry is
  now explicitly a separate-repository milestone after that proof and creates one declared ID break.

### Validation evidence and limits

- The baseline completed a downgrade-to-base/re-upgrade cycle against the `.env` database verified
  as `obehy_test`/`obehy`; `alembic check` reports no new operations. Four focused PostgreSQL tests
  pass for load/activate/resolution, failed-load cleanup, job retry/events, rollback and retention.
- The full suite passes **70 tests with 1 expected opt-in live-national skip**. Ruff formatting and
  lint pass for maintained source/tests/migrations, and strict Pyright reports no errors or warnings.
- The fixture is generated directly from the frozen Python contract because JrUtil does not yet emit
  serving-package v1. COPY is bounded and streaming but currently one relation at a time; parallel
  national loading and its performance baseline require the first JrUtil production-sized package.

### Next handoff

- Implement JrUtil provisional-v0 compilation and serving-package v1 emission, then prove the PID
  posts-only overlay against this validator/loader. Do not add registry tables or static arbitration
  back to Oběhy PostgreSQL.

## 2026-08-03 — Static architecture redirected to JrUtil and a public ID registry

### Decision

- Production static conversion and overlays move to JrUtil. Oběhy owns immutable source
  acquisition, build supervision, the finalized serving mirror and all operations/realtime work;
  MOTIS owns connection search.
- A separately deployed FastAPI/PostgreSQL registry owns every public canonical namespace,
  immutable catalog snapshots and canonical location coordinates. Surface IDs are project-owned;
  railway locations use country-scoped SR70 composites. Surface and rail identities remain
  permanently separate.
- The in-database national compiler is now a prototype, not the production path. Its controlled
  100-route fixture remains useful evidence, but the interrupted full import was killed and rolled
  back with no build, publication, staging table or active backend left behind.
- The current database schema is also a prototype boundary, not the production serving/operations
  schema. Its identity-owned tables, ten-character ID assumption, source-fact compiler relations,
  incomplete build-digest contract and missing realtime/operations model must be reworked while
  the obsolete PostgreSQL importer is removed. This happens before loading production JrUtil
  serving packages, not as a later cleanup after static overlays are operational.

### Next handoff

- Remove the prototype PostgreSQL importer and separate the registry-owned, finalized-static and
  operational schema responsibilities before loading any production JrUtil serving package. Do
  not resume the full PostgreSQL baseline import.

## 2026-08-03 — Baseline compiler throughput and continuity checkpoint

### Delivered

- Replaced the single stop-time loader with four byte-range COPY streams into isolated unlogged
  staging (configurable with `--copy-workers`, bounded to 1–8). The loader verifies physical CSV
  row boundaries before splitting and falls back to one safe range when quoted embedded newlines
  make physical splitting unsafe.
- Reworked canonical call validation from per-row application triggers to one transition-table
  validator per inserted set. The append-only call fact keeps its primary key, validates its build,
  trip, location, boarding point and provenance in bulk, and is deleted explicitly with its build.
  No-op binding continuity updates are skipped, and complete-trip overlap ambiguity is checked once
  per incoming set rather than maintained through a per-row GiST exclusion index.
- Added JDF stop continuity using the normalized full GTFS stop name, current metadata `district`
  value and country, with unique prior-candidate matching and blocking ambiguity diagnostics. Exact
  generated `jdf:stop:*` values remain provenance only and are not treated as permanent IDs.
- Canonical calls now retain GTFS stop headsign and shape distance where supplied; stop descriptions,
  URLs and accessibility are also compiled. This supports an Oběhy-ID national GTFS/GTFS-RT export;
  immutable source artifacts remain the byte-lossless replay boundary.

### Validation evidence and limits

- A referentially closed **100-route / 100-PA** fixture contains 45,065 JDF trips and 1,061,546 JDF
  calls plus 114 CZPTT GTFS segments and 2,073 CZPTT operational calls. It compiles to 1,063,586
  canonical calls. Four-stream COPY takes **1.14–1.35s**; total import fell from **190.3s to
  112.0s**. Binding continuity fell from 35.2s to 0.15s and trip binding from 17.7s to 8.6s. The
  canonical call insert remains the dominant phase at 83–88s and is the next performance target.
- The focused importer suite passes 16 tests. The full suite passes **98 tests with 1 expected
  live-national skip**; strict Pyright and formatting checks pass for all changed files, and Alembic
  metadata is clean. Repository-wide Ruff still reports 16 existing issues only in the untouched
  `converters/jrunify-ext-geodata` files.
- The current JrUtil JDF bundle drops the actual JDF district code (`BM`, `KV`, `CV`, `TP`, …).
  Its `source_stop_metadata.parquet` field named `district` contains the second stop-name component
  instead. The implemented continuity key therefore remains a known fallback until JrUtil preserves
  an explicit district-code column; do not represent it as the intended final identity rule.
- The formerly planned full database import was superseded by the newer registry/JrUtil architecture
  decision above. These timings remain prototype evidence only.

## 2026-08-02 — National baseline importer, fixture-first checkpoint

### Delivered

- Added strict JDF v1/CZPTT v1 bundle readers with complete inventory, size, SHA-256, row-count,
  GTFS header and Parquet schema/metadata verification. Machine-local configuration now requires
  an artifact root, and snapshot/build artifact keys are normalized beneath it.
- Added `obehy-static-import base` and the typed source/canonical relations needed for national
  compilation and later overlays: complete-trip bindings, rail route segments, CZPTT operational
  calls/coverage and compact JDF notices, connections, restrictions and zone claims. Boarding
  points inherit their stop-place domain, and a heavy-rail stop place may be used by both passenger
  and non-passenger calls.
- Kept the storage boundary deliberately lean. The immutable bundles remain the lossless archive;
  the **17M JDF calls are stored once** as canonical call revisions rather than repeated in JDF
  source-call and call-projection tables. GTFS CSV uses native PostgreSQL COPY, with staging indexes
  built after heap loading. Upstream diagnostics are bulk-COPYed instead of inserted through the
  ORM one row at a time.
- Negative CZPTT operational times are not shifted or repaired. Any affected PA and every generated
  GTFS segment belonging to it are removed before reconciliation and reported as a non-blocking
  upstream rejection pending a JrUtil fix.
- Added a deterministic `obehy-baseline-slice` developer tool that derives referentially closed,
  strictly manifested bundles with distinct source identities from the national artifacts.

### Validation and next handoff

- A strict **2-JDF-route / 5-valid-PA** fixture imported end to end as ready build `92` without
  activation: 618 JDF trips/8,001 calls plus 5 CZPTT PAs/60 operational calls compiled into
  **623 trips, 8,061 calls and 7 rail route segments**. Peak traced Python memory was 16.8 MB;
  validation, staging and canonical compilation took 0.196s, 0.336s and 3.280s respectively.
  Replay reused build 92 and allocated nothing. Only the 60 CZPTT operational source calls exist;
  the 8,001 JDF calls have no duplicate source copy. Six JDF notices were retained.
- An overlapping 3-route/6-PA fixture B then compiled as ready build `131` with 1,004 trips. All
  **623** A-build trip identities survived unchanged, exact binding validity widened in place,
  `last_seen_snapshot_id` advanced, and no duplicate source-trip binding was created. A different
  canonical target for an overlapping accepted source trip remains a blocking ambiguity.
- Focused PostgreSQL tests pass for multi-segment PA collapse, separate non-passenger arrival and
  departure, exact rail platforms, shared passenger/pass-through locations, and complete-PA
  rejection for negative time data. The full suite passes **97 tests with 1 expected live-national
  skip** against a nonempty baseline database. Ruff, formatting, strict Pyright and `alembic check`
  pass; the migration completed downgrade/re-upgrade cycles cleanly.
- Three national attempts rolled back atomically and left no partial build. They exposed and fixed
  quoted-null CSV handling, boarding-point domain lookup and the shared rail-location constraint.
  The latest attempt spent 893.5s reaching location compilation; its single COPY stream generally
  used only 30–50 Mbit/s on a 1 Gbit link despite occasional higher bursts.
- This checkpoint's proposed next database-import step was later completed as a controlled fixture
  benchmark and then superseded by the registry/JrUtil architecture decision above.

## 2026-08-02 — Database v1 foundation

### Delivered

- Replaced the disposable Milestone 0 schema with an Alembic-autogenerated database-v1 baseline.
  Ordinary tables, indexes, constraints and foreign keys are generated from SQLAlchemy metadata;
  reviewed hand-written migration additions are limited to PostGIS/btree-gist setup, canonical ID
  sequences, seed sources and database-enforced functions/triggers.
- Added stable canonical operators (`C…`), permanent surface/heavy-rail domains, stop/boarding/
  operational separation, dedicated indexed CIS road and train-number matching keys, generic
  secondary identifiers, date-ranged bindings and aliases, content-addressed source snapshots,
  artifacts, source objects and reviewable diagnostics. JDF and CZPTT locations cannot bind across
  domains and are never unified into a stop knot.
- Added immutable build-scoped revisions for operators, locations, routes, calendars, trips, calls,
  shapes/shape points, transfers and fare-zone assignments. Revisions support nullable direction,
  untimed calls, service times beyond 24:00, exact source-object provenance, field overrides and
  schema-versioned namespaced extensions.
- Added transactional ready/activate/rollback lifecycle services, explicit build/publication reads,
  advisory MobilityData validation records, on-demand trip instances and post-activation pruning
  that retains the active database payload plus two predecessors while preserving build/snapshot
  metadata and external artifacts.
- Hardened the database boundary so immutable payload and calendar rows cannot be moved out of a
  published build, unreviewed identity bindings never resolve, ambiguous dated CIS route keys fail
  explicitly, and Alembic online migrations commit their own schema transactions.
- Added repository rules requiring Alembic autogeneration and explicitly documenting the disposable
  `obehy_test` database. The ignored repository-local `.env` carries development database URLs.

### Validation and next handoff

- The generated baseline applies cleanly to PostgreSQL 17/PostGIS, passes `alembic check`, and
  completed a full downgrade-to-base/re-upgrade cycle. Extension-owned PostGIS/Tiger tables are
  excluded from autogeneration.
- The full Python suite passes **82 tests with 1 expected live-national skip** against the PostGIS
  database. Ruff lint and formatting checks and strict Pyright pass for `src`, `tests` and
  migrations.
- No national bundle was imported in this slice. The next safe handoff is a small canonical import
  from each existing national JDF and CZPTT bundle, followed by full baseline compilation and then
  the first PID static overlay.

## 2026-08-02 — MOTIS route-shapes architecture decision

### Delivered

- Replaced the planned pfaedle production workstream in `BASE_PLAN.md` with a companion CLI around
  MOTIS `route_shapes`, initially pinned to MOTIS `v2.11.0` commit
  `dc441d684099afbf4ce82d605f26e46504c70c28`.
- Locked the boundary around a candidate-only, post-overlay GTFS projection and immutable generated
  shape sidecars. Source and retained national shapes remain outside the companion, while invalid
  or failed generated shapes leave trips shapeless without invalidating the static build.
- Defined the CLI and bundle contracts, cache provenance, structural validation, diagnostics,
  fixtures, reproducibility checks, upgrade review and pre-activation accuracy/performance gate.

### Validation and next handoff

- This is an architecture/documentation change only; no MOTIS dependency or companion executable
  has been added. The documentation diff passes `git diff --check`.
- The next safe shape-specific handoff is the Milestone 5 companion scaffold and smallest bus golden
  fixture after the preceding static-overlay milestones provide the candidate projection.

## 2026-08-01 — JDF geodata, international trips, and effective transport modes

### Delivered

- The shared OSM build now creates a cached node-only JDF stop extract using precisely the OSM node
  tags consumed by `Osm.getCzOtherStops` (`highway=bus_stop`, public-transport platforms/poles/
  stations, tram stops, and bus stations). National `fix-jdf` validates and reads this compact PBF
  instead of repeatedly scanning the multi-country merged snapshot. Municipality matching is
  unchanged and continues to use JrUtil's separate bundled Czech municipality spatial index. The
  real extract is **8.3 MiB / 288,131 nodes / zero ways or relations**, down from the **3.37 GiB**
  merged input; native generation took **125.5s**, reuse **0.32s**, and JrUtil read it in **24s**.
- Removed two merged-JDF bundle hot paths introduced with per-trip international filtering: active
  trips are indexed by route once and route decisions are indexed once, instead of scanning all
  trips/routes for each route. A phase log now separates JDF parsing from international-policy work.
- `regional-adjacent` now classifies individual trips instead of dropping an entire route because
  it also contains a foreign-only or over-limit journey. Domestic and qualifying Czech/adjacent
  trips survive, rejected and wholly foreign trips are pruned with their dependent rows, and
  qualifying cross-border buses use extended regional type `701`. The regression is explicitly
  keyed as route `001398` and retains its regional trip while removing its foreign-only
  continuation. Manifests and diagnostics report the four trip dispositions.
- Added a checked, hashed transport-mode rule file with fully guarded Ostrava and Liberec
  corrections. Matching routes use effective tram/trolleybus modes for GTFS type and colour while
  source and effective JDF modes are both retained in route provenance Parquet. Guard mismatches
  remain buses and are diagnostic. Liberec `545902`/`545903` (`X2`/`X3`) are deliberately excluded
  because they are tram-replacement buses. Extended bus mapping now reserves `202` for `D`, uses `201` for
  nonregional international service, `701` for regional/extra-district/extra-regional and regional
  cross-border service, and `704` for city service. Coaches (`201`/`202`) use deep PID-style
  petrol blue `#004F71`, regional cross-border buses use dark teal `#00695C`, and ordinary buses
  retain `#0076A3`; these avoid the CZPTT train-category palette.
- The gapfill audit now collapses platform children, detects maximal unresolved stop-place runs and
  termini, and reports trip/route impact and timed anchors. OSM/Nominatim matching now considers
  foreign-language names and localized transport terms, tolerates tightly route-supported locality
  defects, and has a conservative dominant-candidate fallback. Mapy now tries every expanded,
  locality-qualified, and localized transit-term query rather than only the first result set.
- Ran the new audit on `jdf-final-v2`: 57 priority stop places comprised 45 termini and 12 members
  of long gaps. Nominatim accepted 23 and Mapy accepted 11 candidates across the initial and
  expanded passes; after two already-checked conflicts, 32 new coordinates were merged into
  `other/gapfill.csv`. All seven Marklowice/Zebrzydowice and all five Trenčín–Drietoma long-gap
  stops now have accepted stop-level coordinates. The remaining 23 rows are isolated/short-run
  ambiguities and remain in review. The Mapy credential and raw responses were not stored.
- Production triage established that current line `001398` is absent from both downloaded CIS JŘ
  source archives before any JrUtil filtering. The service remains published by VVO/IDOS, but its
  omission is upstream source coverage; the `001398` fixture verifies policy behavior only and is
  not evidence that the national JDF source contains the route.
- The rebuilt feed exposed a 28-call estimated run on retained line `001982` between Bodenmais and
  the Czech border. Mapy resolved all 25 distinct passenger stop places; the one ambiguous result,
  Irlsaign, used the duplicated top-ranked, route-feasible stop at `49.20682, 13.02074`. All 25
  coordinates are checked into `other/gapfill.csv`, and a five-batch targeted `fix-jdf` rerun
  resolved every retained identity as `external:gapfill`. Eight additional Lohberg-area route
  estimates occur only in trips pruned from GTFS and are intentionally outside the passenger-call
  quality gate.

### Validation evidence and limits

- JrUtil Release: **108 tests passed**, formatting verification passed, and the multitool Release
  build succeeded with existing warnings. Oběhy: **62 passed, 10 expected skips**; Ruff lint and
  format checks pass for maintained `src`/`tests`.
- JrUnify-Ext-GeoData: **43 tests and 8 subtests passed**, including null Nominatim details,
  run/terminus audits, platform collapsing, route-supported candidate selection, Mapy credential
  safety, and checked gapfill integrity.
- The user will run the full national rebuild and MobilityData validation. The checked historical
  snapshot predates the per-trip policy, so production confirmation of `001398`, the effective
  Ostrava/Liberec route types, and the post-rebuild zero-long-gap gate remains a rebuild check.

## 2026-08-01 — Dense passenger estimates and municipality fallback routes

### Delivered

- CZPTT route estimation now applies only to identities referenced by an accepted passenger call.
  Every occurrence is ranked by two-sided real passenger anchors, source-call density, geographic
  anchor span, active service days, and PA/sequence. Only the winning occurrence is used;
  estimates are neither averaged nor reused as anchors. Estimated GTFS station parents and
  children receive one shared JDF-style ` [?]` suffix, while Parquet source names, headsigns, and
  route labels remain raw.
- Pure timing/operational identities are never estimated. If SR70/OSM has no real coordinate, the
  GTFS stop-time and now-unused child/parent/zone/call projection are omitted; the complete source
  point and call remain in `operational_points`/`operational_calls` for provenance.
- Eager OSM fuzzy matching now preserves distinguishing direction/locality tokens, so
  `Bad Schandau Ost` cannot collapse onto `Bad Schandau`. PLC, reviewed alias, exact, cleaned, and
  qualified fuzzy matching otherwise remain eager and corridor-vetoed.
- Synthesized fallback routes now use country-aware municipalities rather than station/facility
  names or SŽ `Název 20`. Reviewed major-city station families collapse to their city root;
  compound municipalities remain intact; terminal railway qualifiers are removed conservatively;
  and each endpoint retains the deterministic 20-character cap. The production regression is
  `Os Karlovy Vary – Johanngeorgenst.`. `SR70_Nazev20.csv` and its CLI/config interface remain
  validated, copied, and checksummed as reserved provenance data but cannot affect output.
- Full-feed MobilityData validation exposed and fixed two older serialization defects: negative
  CZPTT source-day offsets are shifted by whole days only in GTFS (raw operational seconds remain
  unchanged), schemeless KADR operator URLs receive `https://`, and calls with no source time are
  emitted as `timepoint=0` rather than exact-time rows.

### Validation evidence

- The current JrUtil Release suite passes **104 tests**; the focused CZPTT class contains **35**
  bundle/conversion regressions covering dense-vs-sparse selection, passenger-vs-operational
  anchors, non-chaining estimates, qualifier retention, municipality labels, Název20 independence,
  timing-only omission, negative day offsets, valid operator URLs, and untimed calls. `dotnet
  format --verify-no-changes` passes. The Release multitool build succeeds with the existing
  package/compiler warnings.
- Oběhy remains at **61 passed, 10 expected skips**; Ruff lint, Ruff formatting, and strict Pyright
  pass for `src` and `tests`.
- The full 135,433-message GTFS replay resolves **3,483** used locations as **3,415 authoritative
  SR70**, **68 OSM**, and **0 estimates**. Every passenger-referenced location has a real
  coordinate. The four unresolved timing-only identities—`DE:10535` (Bad Schandau Ost),
  `DE:25606` (Schmilka Überleitstelle), `PL:07397`, and `PL:63752`—remain in operational Parquet
  and diagnostics but do not occur in `stops.txt` or `stop_times.txt`. No known SR70 coordinate
  was replaced by OSM.
- MobilityData GTFS Validator **v8.0.1** (documented JAR SHA-256) parsed all **40 agencies**,
  **9,178 stops**, **47,098 trips**, and **952,854 stop times** with **zero ERROR notices**. The
  remaining 8,028 notices are warnings dominated by source travel-speed anomalies, extended rail
  route types, long combined route names, and source casing. Only `gtfs-intermediate/` was
  validated; extension foreign keys are checked separately by the national builder.

## 2026-07-26 — Shared OSM snapshots and CZPTT bundle v1

### Delivered

- Added mandatory machine-local TOML configuration with absolute `workdir`, active `osm_file`,
  JrUnify-Ext-GeoData, and exclusive JrUtil directory/command modes. National CLIs accept
  `--config`; sibling-parent discovery and the old JrUtil/geodata path flags were removed.
- Added `obehy-osm build [--verify]` for the fixed Czechia, Austria, Bavaria, Saxony, Slovakia,
  Dolnośląskie, Opolskie, and Śląskie Geofabrik set. It checks remote MD5 sidecars before and
  after downloads, caches source extracts, and writes the single configured PBF atomically with
  native `osmium merge --progress`. Its sibling manifest records ordered source hashes, Osmium
  identity, output hash, size, and local file stats; matching inputs and output skip regeneration.
  There are no versioned merged copies, hard links, replay lookup, or PyOsmium fallback. The same
  command runs native `osmium tags-filter --omit-referenced` once per changed snapshot and caches
  a node-only railway-location PBF under `workdir/osm`; Python never parses OSM objects. Windows
  automatically uses Osmium from the default WSL distribution. Every long stage reports
  progress. JDF and CZPTT validate and record the artifacts rather than processing OSM.
- Made SŽ SR70 the strict CZPTT coordinate authority. Exact OSM PLC, reviewed alias, and eager
  global exact/cleaned/fuzzy name matching fill only absent/invalid/conflicting SR70 identities.
  SR70/OSM differences retain SR70 and emit object/distance diagnostics; the converter does not
  interpret `uic_ref` or `railway:ref` as a PLC. A real synthetic-PBF smoke deliberately placed
  all three OSM stations roughly 154–172 km away and confirmed that all six generated
  parent/child stops retained SR70. No known SR70 coordinate was replaced by OSM.
- Removed the CZPTT coordinate hot path that materialized full OSM feature geometry and scanned
  the candidate array repeatedly. JrUtil now reads only raw nodes from a native railway-only
  extract, builds PLC/object/name indexes once, and treats country tags as ranking hints rather
  than match gates. A name candidate is rejected only when every usable timetable occurrence
  exceeds 150 km/h plus 2 km slack; one anomalous occurrence cannot veto a clear match. JrUtil
  then tries secondary match methods and finally route-time/end-offset estimation. IDS projection
  also uses a prebuilt PA-to-calls index instead of rescanning every operational call.
- Introduced CZPTT bundle v1: standard GTFS is isolated in `gtfs-intermediate/`, Czech extension
  tables are in `extensions/`, and the provisional four mirror Parquets were replaced by the five
  normalized operational/source-projection relations. PA/TR identities moved to
  `cz_trips.source_trip_ids`; generated-trip lists became typed call/IDS bridges, including
  interval-overlap filtering.

### Validation evidence and limits

- Oběhy unit/integration collection: **61 passed, 10 skipped** (database and live-national tests
  remain opt-in). Ruff on `src`/`tests` and strict Pyright pass.
- Full JrUtil solution tests: **97 passed**. The JrUtil multitool Release build succeeds with the
  existing package/compiler warnings. The full solution Release build reaches every relevant
  project but remains blocked in unrelated `rtview` because `sassc` is not installed. Tests
  inspect all five exact Parquet schemas and metadata and assert that the old mirrors are absent.
- A full eight-extract output was rebuilt from the existing **3.37 GiB** source cache directly
  into the configured PBF. Native merging took **90.6s**, SHA-256 **3.1s**, and the full command
  **101.9s**. A second run reused it from the source/output manifest in **6.1s** with no merge.
  The obsolete 3.37 GiB versioned merged-output directory was removed.
- Native filtering of that 3.37 GiB PBF produced a **0.8 MiB**, **19,617-node** extract containing
  only `railway=station|halt|stop` in **79.8s** under WSL. This replaced an earlier 4.5 MiB extract
  that accidentally admitted unrelated bus/trolleybus `public_transport=stop_position` nodes.
  Its next validated reuse took **0.48s** and performed no OSM scan.
- A final replay of the current national CZPTT snapshot completed in about **217s**, including
  every Parquet/GTFS write. It resolved all **3,491** used locations as **3,415 SR70**, **69 OSM**,
  and **7 route estimates**—down from the initial **1 OSM / 75 estimates**. All seven estimates are
  non-passenger operational calls; every passenger-referenced point has a real SR70 or OSM
  coordinate. OSM matches comprise **45 normalized exact**, **15 cleaned railway-name**, and
  **9 fuzzy-name** resolutions. No known SR70 coordinate was replaced. The old `index-stop-times`
  stall did not recur, and the separate post-conversion full stop-time-key materialization was
  replaced by a streaming required-key check.
- Current national JDF/CZPTT rebuilds, MobilityData validation, and both full CZPTT operational
  modes remain outstanding live acceptance work.

## 2026-07-25 — CZPTT service classes, line expiry, and SR70 Název20

### Delivered

- Unified CZPTT route type and color classification: InterCity `b91c1c`, fast `b45309`,
  regional `1c1745`, night `4c1d95`, and other rail `475569`, all with white text. Exact train
  categories remain in route/trip labels.
- Made location-level `CZPassengerServiceNumber` authoritative for the onward section. Missing or
  blank location values now clear the line. Activation remains at the newly marked location;
  expiry shares the preceding source point so the last lined passenger call already presents the
  fallback service. Root values apply only when a PA has no location-level records. Fallback
  routes continue to use complete-PA passenger endpoints.
- Added paired SR70 `Název 20` snapshot support. Synthesized Czech fallback labels prefer the
  compact SŽ name and remove only a presentation-time terminal ` z` or ` nz`; stop names and raw
  snapshots stay unchanged. Endpoints without a unique `Název 20`, especially foreign stations,
  receive a deterministic best-effort route-label abbreviation of at most 20 characters while
  their GTFS stop names and trip headsigns remain untouched. Both SR70 files are checksummed in
  immutable source and run manifests.
- Refreshed both checked rail CSVs from the official workbook effective 2026-08-15 and documented
  its source SHA-256. The deterministic converter validates identifiers, coordinates, required
  columns, uniqueness, and writes both snapshots from one workbook load.

### Validation and handoff

- The full sibling JrUtil suite passes **90 tests** and its Release multitool build succeeds with
  the existing package/compiler warnings. CZPTT tests cover every category alias, line
  activation/expiry/root fallback, full-PA fallback labels, cleaned and ambiguous `Název 20`, and
  coordinate behavior.
- The full Oběhy suite passes **45 tests** with **10 expected skips** for unavailable database/live
  environments. Ruff and strict Pyright pass on the changed Python files. Two focused SR70
  converter tests pass and preserve raw official `Název 20` values; byte-identical regeneration
  produced 4,367 rows in each CSV.
- Release bundle smokes passed in both `gtfs` and `sidecar` operational-point modes using the
  refreshed SR70 pair. No CI configuration changed.
- The next safe handoff is a local full-current-GVD rebuild in both operational-point modes and
  review of production fallback-route counts and unresolved SR70 diagnostics.

## 2026-07-24 — CZPTT GTFS presentation and station cleanup

### Delivered

- Added the selected rail palette with white text: regional `1c1745`, InterCity `0076a3`,
  express `7a4eab`, night `312e81`, and other rail `5b6472`. Train categories now carry through
  normalized calls, split a through service when they change, and prefix standard
  `trip_short_name`; `cz_trips.train_number` remains the unmodified operational number.
- Replaced stop-pattern fallback routes with operator/category/mode/canonical-endpoint grouping.
  Reverse directions and intermediate-pattern variants share a route whose combined short name
  uses the service-day-dominant direction. Mapped KADR line names are unchanged. Every linked
  segment now uses the complete PA's final passenger call as its headsign.
- Replaced legacy CZPTT generated IDs with `czptt:` colon namespaces. Every primary point now has
  one station parent and an unspecified or platform child, and all calls reference children.
  Platform values remain solely in `platform_code`.
- Made SR70 matching country-aware and propagated resolved coordinates to parents and children.
  Diagnostics schema v2 reports resolution counts, unresolved points by country, and conflicting
  used SR70 codes. The operational-call Parquet schema is now v2 and carries generated station and
  child-stop IDs. Oběhy bundle verification rejects malformed hierarchy or missing/mismatched
  coordinates for any Czech point present unambiguously in the SR70 snapshot.

### Validation and handoff

- The sibling JrUtil suite passes **85 tests**; its Release multitool build succeeds with the
  existing package/compiler warnings. Synthetic bundle conversions passed in both `gtfs` and
  `sidecar` operational-point modes and were inspected for namespaced IDs, hierarchy, route
  presentation, headsigns, and coordinate diagnostics.
- The focused Oběhy CZPTT suite passes **10 tests**. Ruff, formatting, and Pyright checks cover the
  changed Python files. No CI configuration changed.
- The next safe handoff is a full current-GVD conversion in both operational-point modes followed
  by inspection of production route/station counts; this remains a local smoke rather than a CI
  job.

## 2026-07-23 — Initial national CZPTT conversion bundle

### Delivered

- Switched live CZPTT acquisition from the pathologically slow FTP hierarchy to the official
  `portal.cisjr.cz` HTTPS mirror. Discovery now fetches monthly directory listings concurrently,
  makes no per-object probe requests, reports discovery/download/recheck progress, and rejects FTP
  overrides with HTTPS guidance. Downloaded bytes and SHA-256 hashes are the immutable source
  identity.
- Added `obehy-national-czptt build` with Europe/Prague second-Sunday GVD selection, an anonymous
  HTTPS/local-server source root, eight-worker automatic acquisition, frozen inventories,
  offline `sources/` snapshots, KADR and SR70 snapshots, deterministic message flattening, atomic
  publication, failure retention, and file/run manifests. The run manifest records the separately
  checked-out sibling JrUtil commit; `converters/jrutil` remains unchanged.
- Updated the sibling JrUtil model so repeated root and per-location
  `NetworkSpecificParameter` values survive XML deserialization. Complete transport identity now
  includes object type, company, core, variant, and timetable year. Exact duplicate payloads are
  deduplicated, conflicting PA identities are fatal, cancellations are applied in flattened
  interval order, and unknown targets are aggregated.
- Added offline CZPTT conversion and bundle commands. Only activity-`0001` creates a passenger call.
  Internal points default to exact non-boardable/non-alightable GTFS timing points, with a
  sidecar-only compatibility mode; deadhead tails remain operational metadata. Whole PAs with no
  passenger calls or backward/inconsistent chronology are rejected without day-rollover repair.
- Platform children keep the source station name and put the passenger-facing subsidiary name,
  or raw code fallback, only in `platform_code`. Consecutive subsidiary-only duplicates collapse
  in GTFS while raw call evidence remains in Parquet. SR70 coordinates are snapshotted and applied
  where their five-digit point identity is unambiguous.
- Friendly KADR lines now own route identity and names; train numbers remain trip metadata.
  Mid-journey line/operator/mode changes create linked trips with a shared PA block and
  `transfer_type=4`. Sidecar-mode internal boundaries are approximated at the following passenger
  call and diagnosed.
- Added `cz_trip_stop_zones.txt` and repeated `CZIPTS`/`CZCalendarIPTS` preservation. Explicit
  catalog fare bands such as `PID_PrahaP` become trip-stop memberships, broader IDS records remain
  coverage metadata, and standard `stops.zone_id` is set only for globally unambiguous memberships.
  Added four Snappy Parquet sidecars for source journeys, operational points, operational calls,
  and IDS coverage.

### Validation and handoff

- Eleven focused JrUtil CZPTT tests cover repeated parameters, both operational-point modes,
  unchanged platform names/friendly platform codes, linked `S1 -> S12` trips, full chronology
  rejection, passenger activity variants, values above 24 hours, omission of non-passenger PAs,
  PID fare-band projection including overlapping bands, and real Parquet sidecar creation.
- Nine Python tests cover the GVD boundary, concurrent prior-calendar-year change discovery,
  HTTPS-only source selection, persistent streaming HTTP downloads, malformed gzip, deterministic
  cancellation ordering, conflicting PA identities, byte-identical network-free snapshot
  builds/default operational mode, and incompatible source options. A read-only live discovery
  enumerated 115,077 GVD 2026 objects in 3.21 seconds; a 16-object persistent-connection download
  sample transferred in 0.33 seconds. The live official KADR SOAP schema was checked read-only:
  the snapshot parser retained
  1,162 companies, 408 public lines, 44 IDS entries, 18 train types, and 15 commercial types,
  including IDS code 11 `PID_PrahaP` / `PID pásmo P`.
- Locally generated synthetic feeds for both operational modes passed MobilityData GTFS Validator
  v8.0.1 (pinned CLI SHA-256
  `19293ddd9b6f954f216d4f12054bd8a3232921751c4484339e339764a91000e2`) with zero errors.
  The remaining production acceptance gate is the opt-in full-current-GVD build.
  Calendar-restricted `CZCalendarIPTS`
  records are preserved losslessly, but service-calendar partitioning by differing IDS membership
  still needs a real-data golden before the nationwide smoke test is enabled.

## 2026-07-23 — National-builder CI test isolation

### Delivered

- Made the mocked national JDF orchestration test self-contained by creating temporary JrUtil and
  geodata fixtures and deterministic converter provenance. The unit test no longer depends on the
  developer-only `../jrutil` and `../jrunify-ext-geodata` sibling checkouts that are intentionally
  absent when CI checks out the main repository without submodules.

### Validation and handoff

- The focused regression passed both work-retention variants. The full local suite passed with
  database-backed and large live tests skipped because their opt-in environment was unavailable;
  Ruff lint/format and strict Pyright passed for `src` and `tests`.
- Docker is unavailable in the local validation environment, so the Compose and database-backed
  CI stages were not rerun. The next safe handoff is to push the change and confirm GitHub Actions.

## 2026-07-21 — Referenced-stop pruning and refresh-first coordinate repair

### Delivered

- JrUtil now derives its stop set from the final emitted `stop_times`, retaining only called
  boarding points and required parents. The same set filters `cz_stops`, `cz_stop_zones` and
  `source_stop_metadata.parquet`, preventing dangling extension metadata and removing stops that
  occur only on filtered/pass-through calls or nowhere in the emitted service.
- Coordinate selection now preserves stop versus estimated precision and source provenance. Bundle
  schema v3 carries `coordinate_precision`, `coordinate_source` and `coordinates_missing`; invalid
  finite/range values still fail, while unresolved stops remain valid mandatory `0,0` GTFS rows
  with one aggregate warning and structured stop-ID diagnostics. GTFS stop places and their
  boarding-point/post children now append ` [?]` exactly once when coordinates are route-estimated;
  stop-precise and missing-coordinate names remain unchanged.
- The matcher keeps strict country/okres checks as its fast path, precomputes okres adjacency and
  only performs a cached 1,000-metre boundary check for exact-name same-country candidates rejected
  solely by okres. Aggregate strict/border/country/region counters replace per-candidate noise. The
  observed national JDF alias `OL` is normalized to boundary code `OC` for matching only; source JDF
  values are unchanged.
- Refreshed all 24 active `jrunify-ext-geodata` catalogues through validated temporary outputs.
  Country values are normalized to historical JDF codes, five-column files remain compatible and
  optional `S`/`T` precision is supported. Added official KHK (4,454 rows), DPMLJ (581) and DPMO
  (373) sources. DPMLJ names are municipality-qualified from its tariff zones; DPMO names are
  explicitly Olomouc-qualified. Retired Karlovy and first-generation IREDO endpoints remain as
  documented snapshots rather than active failing sources.
- Added conservative one-shot audit, Overpass and Mapy tooling. OSM covers nodes, ways, relations,
  aliases and centers, accepts exact identities with compatible municipality or okres, and uses
  cached rate-limited Nominatim only once per unresolved foreign municipality. Mapy reads its key
  only from `MAPY_API_KEY`, never persists raw/rejected responses, and accepts only unique exact
  stop identities or explicit town-level results.
- Oběhy bundle verification now enforces complete stop references, required parents, no extra
  boarding stops and numeric in-range mandatory coordinates while permitting `0,0` with one
  aggregate warning.
- Added an opt-in JrUtil `regional-adjacent` international-route policy and enabled it in the
  Oběhy national builder. It classifies whole route distinctions from service-valid emitted calls,
  catches foreign services mislabeled as city/regional, retains only neighboring-country services
  within 120/60 km timetable span/depth limits (200/80 km when integrated), supports audited CSV
  keep/drop overrides, and removes all dependent GTFS, extension and Parquet rows. Bundle manifests
  record the policy/counts and rejected routes receive structured diagnostics without per-route log
  flooding. All source batches retain stop matching because route classification can change after
  merge. Matching is owned entirely by `fix-jdf`: checked external candidates take precedence over
  OSM, town centroids are never emitted as stop coordinates, matches contradicted by scheduled
  travel time are rejected, interior gaps are interpolated by time between valid anchors and
  unmatched route ends are offset north. Untimed passing/not-passing calls are skipped when finding
  anchors, and degenerate batches with fewer than two distinct called stops are dropped completely.
  `merge-jdf` only preserves and combines those results.
- The national builder now streams nested source batches directly into deterministic staging ZIPs,
  builds JrUtil once, and uses memory-capped parallel workers for both `fix-jdf` and `merge-jdf`.
  Structured JrUtil progress reports resolved worker plans and concurrent batches; requested and
  resolved execution settings, stage timings, and merged-JDF compression metadata are recorded in
  `run-manifest.json`. Fixed batches use uncompressed ZIP intermediates, merged-JDF packaging offers
  deterministic fast/balanced/small Deflate presets, and `--keep-work` retains all intermediates.

### Validation and remaining live maintenance

- JrUtil passed **51 tests** and the multitool CLI built successfully. Geodata adapters/gap-fill
  passed **39 tests** in an isolated dependency environment. Oběhy passed **33 tests** with **10
  expected skips**; the changed national-builder Python files passed Ruff lint and formatting, and
  strict Pyright passed. The focused national builder suite accounts for 21 of the passing tests.
  A whole-tree Ruff run remains blocked by 16 pre-existing findings in the unchanged
  `converters/jrunify-ext-geodata` scripts, whose two Python files also remain unformatted.
- The regenerated `JDF-final` initially contained 75 referenced stop-place identities without
  coordinates. The audit recovered eight missing rename/spelling aliases into the checked geodata
  repository; a clean rebuild now resolves or route-estimates gaps during `fix-jdf` rather than
  geocoding isolated stops after merge.
- External geodata now has a strict five-column stop-only contract. The former OSM, Mapy, and
  recovered supplements are consolidated into one 336-row `other/gapfill.csv`; all approximate
  town rows and the `TownPrecise` model/serializer path were removed. Missing stop coordinates are
  route-estimated and remain visibly marked until a real stop-level coordinate is accepted.
- A no-cache Olomouc route fixture matched all 20 source stops. DPMO contributed to five matches;
  after the `OL`/`OC` matcher alias, all 40 relevant candidates used the strict path with no region
  rejects. No persistent JrUtil cache was created or used.
- A read-only classifier analysis of the existing merged national JDF found 619 route distinctions
  with foreign stops. The selected policy retains 98 regional cross-border distinctions and rejects
  521; applied to the current coordinate audit, the expected residual falls from 895 to about 182
  stop places before OSM/Mapy gap-filling. Targeted fixtures cover threshold boundaries,
  integrations, mislabeled routes, passing/filtered calls, missing kilometres, foreign-only trips,
  overrides and an end-to-end empty/dangling-free rejected-route bundle.
- The reproducible offline residual audit now joins the existing emitted GTFS trip set to the merged
  JDF, applies the route policy, and reconciles refreshed external CSVs without a JrUtil cache. From
  1,589 legacy `0,0` stop IDs, 810 remain on retained routes and 694 have conservative exact
  refreshed-source matches, leaving **116 actionable stop identities**: CZ 52, D 32, PL 17, SK 11
  and A 4. The generated work list includes route names and a provider/OSM/Mapy/town fallback order.
- The complete national pipeline was deliberately not rerun after these final changes because it is
  the dominant runtime cost. The original serial Overpass-box approach was stopped after poor
  progress and replaced with cached, rate-limited per-stop Nominatim searches. Municipality context
  corrections, localized municipality aliases and nearby-platform clustering repaired false
  ambiguity. The 116 actionable identities now reconcile to **14 OSM**, **100 Mapy** and **2
  refreshed-source recoveries**, leaving **zero unresolved work-list identities**. These were review
  inputs rather than the final external-geodata contract: accepted stop-level coordinates were
  consolidated into `other/gapfill.csv`, while town-level and school-POI fallbacks were removed and
  are now handled by visibly marked route estimation. The Mapy key and raw responses were not
  retained. A final national conversion and the <=5% matcher benchmark remain deferred.

## 2026-07-19 — Runnable national VLD/municipal-dráhy JDF bundle pipeline

### Delivered

- Added the `obehy-national-jdf build` Python entry point. It atomically downloads and hashes the
  official CIS JŘ VLD and municipal-dráhy archives, verifies a current Czech Geofabrik PBF against
  its MD5 sidecar, safely validates nested JDF ZIPs, and records retrieval metadata.
- Combines VLD and dráhy nested archives under deterministic source-prefixed staging names, then
  orchestrates exactly one strict `fix-jdf` pass, one name-based merge, deterministic merged-JDF
  ZIP creation, and strict `jdf-to-bundle` conversion. This loads OSM/geodata once. Numeric JDF stop
  IDs are never treated as CIS IDs, and JrUtil's experimental persistent cache is not enabled.
- Added Rich/redirect-safe terminal progress for downloads and every processing phase, incremental
  SHA-256/MD5 download hashing, byte-identical JrUtil process logs, bounded live warning/error
  display, detailed command failure summaries, and structured `logs/failure.json` reports. Failed
  staging and partial downloads are always retained.
- Added atomic activation, automatic retention of failed work data/optional retention after success,
  source/geodata/converter
  provenance, batch accounting, bundle checksum validation, required Parquet checks and rejection
  of error-severity diagnostics.
- Extended only the root-level `../jrutil` fork so `--ext-geodata` accepts a file or recursively
  loaded directory and `merge-jdf --strict` fails rather than skipping a malformed batch. The
  project submodule and its pointer remain unchanged.
- Fixed the shared JrUtil JDF filesystem writer to create a missing output directory before opening
  `VerzeJDF.txt`; this resolves the nationwide merge crash observed after all inputs had merged.

### Validation and handoff

- Root-level JrUtil passed **26 tests** with `dotnet test jrutil-sln.sln --no-restore`; the existing
  package/build warnings remain.
- Oběhy checks passed: **24 tests**, with database-backed tests and the opt-in large live test
  skipped unless their environment variables are set. The nationwide test remains gated by
  `OBEHY_RUN_NATIONAL_JDF_SMOKE=1`. Ruff, formatting and strict Pyright passed for `src` and
  `tests`; the installed `obehy-national-jdf --help` entry point also succeeded.
- A real `DP_JDF.zip` smoke verified that root-level `fix-jdf` accepts the geodata directory and
  writes one fixed batch. Its existing route-count error is expected because that local archive
  combines ten routes, unlike the one-route nested national batches.
- A real `merge-jdf --strict` smoke verified that a missing nested output directory is created and
  populated successfully.
- The full live nationwide download/conversion was not run because it downloads roughly 1 GB and
  performs the expensive stop matcher twice without the experimental cache. The next safe handoff
  is to run the documented live smoke command when that runtime and network use are acceptable,
  then review and commit the root-level JrUtil changes before later advancing the submodule pointer.

## 2026-07-19 — Non-GTFS JDF semantics added to conversion bundles

### Delivered

- Removed the ambiguous route-level `ids_system_id` and lossy `ids_zone_ids` union from
  `cz_routes.txt`; exact multi-system-capable membership remains in `cz_stop_zones.txt`.
- Replaced legacy `JDFA-`/`CISR-`/`CIST-`/`JDFS-` generated IDs and mixed `jdf-*:` source IDs with
  consistent colon-separated `jdf:…`/`cis:stop:…` namespaces, and replaced `CAL-*` with the
  derived `gtfs:service:…` namespace. Generated boarding points now also carry standard GTFS
  `parent_station`; `cz_stops.stop_place_id` remains the explicit place-level join for both place
  and post rows.
- Extended standalone JrUtil bundle v1 with narrow typed Parquet relations for JDF route/trip
  notices, reservation notes, structured `Navaznosti` connection claims and `§`/`A`/`B`/`C`
  travel-exclusion assignments. Calendar-only `Caskody` remain solely in GTFS calendars.
- Replaced stop-level zone provenance with exact route-stop scope and retained the existing
  GTFS-call-to-route-stop join, avoiding expansion of zones or route-scoped restrictions across
  every trip.
- Locked the consumer contract: Parquet is immutable import material, PostgreSQL will hold the
  normalized queryable claims, regional absence is not deletion, and the active compiler build
  materializes effective values before runtime queries.

### Validation and handoff

- Standalone JrUtil tests passed: **19 passed** with
  `dotnet test jrutil-sln.sln --no-restore`; the two existing warnings remain.
- Native tests cover exact seven-file schemas, referential joins, text/calendar deduplication,
  source-scoped exclusions, structured transfers, filtered enrichment diagnostics, unified ID
  namespaces, valid station/boarding-point parentage, UTF-8 output and byte-identical repeated
  bundles.
- Real smoke bundles succeeded for `DP_JDF.zip`, one VLD batch and one dráhy batch. DP produced
  27 notices, 8,201 call mappings and 331 route-stop zone memberships; the VLD sample exercised
  23 structured connection rows. The refreshed DP GTFS has 219 station rows, 219 unspecified
  boarding children and 428 known-post children; all 8,201 calls reference boarding-level rows.
  Every DP manifest hash and size matched across 19 payloads.
- The retained DP inspection bundle was refreshed to the seven-Parquet layout. The next safe
  handoff remains the Python bundle-v1 reader and PostgreSQL importer; CZPTT stays deferred. The
  implementation is an uncommitted standalone-fork working tree on top of `70bdaac`; record its
  final fork commit after review and do not advance the project submodule yet.

## 2026-07-19 — JDF bundle Parquet mirrors removed

### Delivered

- Revised the still-uncommitted bundle v1 contract before importer work: standard GTFS plus the
  four Oběhy extension tables are now the sole normalized entity representation.
- Replaced seven entity-mirroring Parquet files with four narrow metadata relations containing
  only non-inferable JDF facts: route distinction/source agency/validity, structured stop-name
  components and original coordinate absence, JDF route-stop IDs behind GTFS calls, and JDF
  route-stop provenance/order behind extension zone memberships.
- Removed duplicated trip, boarding-point and fare-zone Parquet tables and duplicated route/stop/
  call columns such as CIS IDs, names, modes, public numbers, coordinates, times, distances and
  pickup/drop-off behavior. Snapshot/source identity moved from every row to Parquet file metadata
  and the manifest.
- Changed call metadata to use the exact GTFS `(trip_id, stop_sequence)` join key instead of a
  separately generated one-based sequence.

### Validation and handoff

- Standalone JrUtil solution tests passed: **19 passed** with
  `dotnet test jrutil-sln.sln --no-restore`; the two pre-existing warnings remain.
- Tests assert exact slim schemas, absence of all seven former mirror files, file-level snapshot
  metadata, GTFS/extension foreign-key joins, deterministic bytes and manifest checksums.
- The retained DP inspection bundle was regenerated from the verified `DP_JDF.zip` payload with
  the slim layout. All 16 manifest entries matched their declared SHA-256 and size; total bundle
  size fell from 1,121,656 to 950,271 bytes. The next safe implementation remains the slim bundle
  importer; CZPTT Parquet design stays deferred.

## 2026-07-19 — Real regional GTFS identity paths inspected

### Delivered

- Inspected the uncommitted PID, DÚK and DPMLJ GTFS snapshots against the uncommitted national
  VLD/dráhy JDF archives and clarified the static overlay identity contract in `BASE_PLAN.md`.
- Separated three previously blurred mechanisms: explicit external-identity claims, deterministic
  identifier aliases, and evidence-backed source-to-canonical bindings. CISLineID aliasing is not
  a fallback for feeds that omit CISLineID.
- Clarified that feed-bound realtime such as PID should resolve through the active static source
  trip binding. The `582588 -> 001588` CIS alias example belongs to the DÚK custom realtime API
  path (or another source explicitly claiming a transformed CIS identifier), not generic GTFS
  route inference.
- Added an instance-first static matching contract: compare overlapping service dates, route and
  operator constraints, ordered canonical stops and times; retain date-scoped bindings when one
  regional trip corresponds to different national CISTripIDs on different dates.
- Defined flat-stop normalization and incomplete-overlay semantics: parentless regional GTFS rows
  are source boarding-point observations, source-local grouping is separate from canonical stop
  identity, and only matched calls receive exact regional posts. National-only calls retain the
  canonical unspecified boarding point.

### Inspection evidence

- PID contains 834 routes and 71,064 trips. Although `routes.txt` only shows values such as
  `L775`, `route_sub_agencies.txt` plus each trip's `sub_agency_id` gives exactly one six-digit
  licence number for all **65,664 non-rail trips**. In the inspected snapshot, `L775` maps to
  `260775`. Forty-seven PID route rows have multiple licence numbers across sub-agencies, proving
  that PID `route_id` alone is not a sufficient route-binding key.
- PID does not expose a road CISTripID directly. For the inspected line 775, all 39 PID trips found
  a national JDF candidate with the same complete stop/time pattern; 24 were unique and 15 matched
  multiple CISTripIDs with identical timetables. Calendar/operating-date comparison is therefore
  required rather than optional.
- DÚK contains 775 routes and 21,720 trips. Every one of its **16,768 non-rail trips** embeds a
  six-digit CISLineID and CISTripID in its source IDs, including the 12 urban routes whose route ID
  has an additional export/version suffix. Of 7,412 stops, 7,400 assert a CIS StopID and all rows
  contain `stop_post`. Rail remains a separate train-number/CZPTT problem.
- DPMLJ contains 44 routes and 3,727 trips without explicit CIS fields. Operator + mode + normalized
  national route name uniquely identified 40 routes; the remaining four are the `2`/`X2` and
  `3`/`X3` duplicate-name families and require a provider rule or reviewed mapping. On the 40
  uniquely mapped routes, **2,853 of 2,861** GTFS `trip_short_name` values existed as national
  CISTripIDs; the other eight must remain unmatched until schedule evidence or a newer aligned
  national snapshot resolves them.
- The stop layouts exercise both hierarchy styles. PID omits parents but exposes `asw_node_id` and
  `asw_stop_id` for grouping posts; DÚK omits parents but exposes shared CIS/DÚK stop-place IDs and
  `stop_post`; DPMLJ supplies 210 parent rows and 425 child rows. The importer therefore must not
  rely on GTFS `parent_station` being populated.
- IDS JMK contains 348 routes, 60,094 trips, 10,887 stops and 980,710 calls. Its numeric GTFS
  `trip_id` is source-local, but every trip has exactly one row in nonstandard `api.txt` mapping
  `(source line code, source course/train number)` to that static ID. The line-code component
  exactly matches the numeric component of `route_id` for all rows; it is not necessarily the
  passenger-facing `route_short_name`, especially for rail. Its route modes include 292 buses,
  13 trams, 14 extended-type-800 trolleybus routes, 28 rail routes and one ferry, confirming that
  production GTFS adapters must preserve supported extended route types.
- The IDS JMK operational key is not unique across a whole feed. There are 29,990 distinct keys:
  11,407 map to one static trip and 18,583 map to two or more calendar/timetable variants. Service
  date alone separates 18,527 of those duplicated keys; 56 keys still have two or three active
  trips on at least one date and require scheduled time/call context. The crosswalk must therefore
  be ingested as a snapshot-scoped candidate relation rather than a dictionary.
- IDS JMK road route IDs do not assert a full CISLineID and identical public route names occur
  under multiple historical/operator-specific CISLineIDs. Exact normalized route name plus the
  `api.txt` course number uniquely selected a national route/CISTrip candidate for 32,909 bus
  trips in the inspected archives; 12 remained multi-candidate and 9,755 had no exact-name
  candidate. This supports instance-level structural matching but is not sufficient as a generic
  direct-ID rule.
- IDS JMK supplies a complete explicit hierarchy: 3,255 parent stops and 7,632 boarding children,
  with every child referencing an existing parent and sharing its numeric source-local base. None
  of the 3,255 parent numeric bases appeared as a national JDF stop ID, so the `U...N...`/`U...Z...`
  namespace must not be treated as CIS. It publishes 168 single-valued IDS zones and 506 nonblank
  platform codes.
- The IDS JMK archive has neither `shapes.txt` nor `feed_info.txt`. It can improve hierarchy,
  source-local operational binding, zones and presentation, but cannot be shape-authoritative;
  retrieval provenance and snapshot validity must come from the external descriptor and calendar
  tables.

### Validation and caveats

- These are observations from local source snapshots, not yet provider-guaranteed contracts.
  Production adapters must retain raw fields, validate every new snapshot, and gain small golden
  fixtures before activation. The archives and generated analysis data were not committed.
- This was a documentation and read-only data-inspection change. The next safe implementation
  remains the JDF bundle importer; regional adapter code should start with a deliberately small
  PID slice only after canonical national imports exist.

## 2026-07-19 — Standalone JrUtil JDF conversion bundle implemented

### Delivered

- Added the standalone-fork `jdf-to-bundle` pipeline for extracted directories and safely
  validated ZIP batches. It requires a checksummed retrieval descriptor and explicit converter
  version, writes atomically, and returns nonzero on bundle failures.
- Added deterministic standard GTFS and separate Oběhy-extension directories, four narrow typed
  Snappy Parquet metadata sidecars, canonical JSON diagnostics, and a manifest containing snapshot/JDF
  metadata plus row counts, sizes and SHA-256 for every payload file.
- Added route-scoped source-zone identities and normalized stop-zone membership rows in
  `cz_stop_zones.txt` and Parquet. Standard `zone_id` is blank for plural membership and the
  non-standard `stop_times.stop_zone_ids` column is no longer serialized.
- Kept the project submodule pointer and CZPTT conversion unchanged.

### Validation evidence

- Native JDF bundle tests cover directory and ZIP inputs, checksum rejection, unsafe ZIP paths,
  atomic cleanup, exact GTFS/extension headers, Parquet schemas/readback, GTFS call joins and
  byte-identical repeated output.
- Standalone JrUtil solution tests passed: **19 passed** with
  `dotnet test jrutil-sln.sln --no-restore`; the two pre-existing warnings remain.
- Uncommitted real-data smoke bundles succeeded for `DP_JDF.zip`, one nested VLD batch and one
  nested dráhy batch. DP emitted 392 trips, 8,201 calls, 647 stops, 428 boarding points and 316
  public stop-zone memberships with no missing stop references. The VLD sample retained extended
  route type 701; the dráhy sample retained type 900.

### Remaining caveats and next handoff

- The implementation is an uncommitted standalone-fork working tree on top of `f5d8797`; pin its
  reviewed commit later. A DP inspection bundle is retained outside both repositories for manual
  review; source archives and generated bundles remain uncommitted.
- PostgreSQL import, downloader/snapshot storage, canonical stop continuity, IDS-system
  heuristics, CZPTT operational sidecars and Parquet import remain deferred.

## 2026-07-18 — Standalone JrUtil JDF extension contract implemented

### Delivered

- Extended the standalone `../jrutil` fork, not the `converters/jrutil` submodule, with optional
  typed `cz_routes.txt`, `cz_trips.txt`, and `cz_stops.txt` GTFS extension tables for JDF output.
- Preserved CIS line/trip identities, source IDs and provenance, passenger-facing `LinExt` line
  designations with a CIS-suffix fallback, deduplicated raw fare zones, and both JDF post forms:
  `Oznacniky` codes and text-only `Zasspoje` station numbers.
- Added the explicit `jdf-to-gtfs --stop-ids-cis` option. Local stop IDs remain the default;
  known authoritative national stop IDs must opt in.
- Added deterministic JDF route colors: bus `0076a3`, tram `7a0200`, trolleybus `80166f`, cable
  car `c8d021`, ferry `00b3cb`, and metro A/B/C `00b274`/`fbaf33`/`d31245`. Cable car, ferry,
  and metro B use dark `1c1745` text for contrast; the other colored routes use white. Unknown
  metro lines fall back to the general rail color `1c1745` with white text.
- Added a native JDF 1.11 golden fixture and deterministic parser/serializer tests. CZPTT leaves
  the new optional extension tables absent and remains otherwise unchanged.

### Validation evidence

- Standalone JrUtil solution tests passed: **17 passed** with
  `dotnet test jrutil-sln.sln --no-restore`.
- Six uncommitted real VLD/dráhy batches converted successfully and produced the expected public
  line mappings, including numeric overrides, leading-zero normalization and alphanumeric lines.
- The uncommitted ÚK `DP_JDF.zip` fixture converted with CIS stop mode after temporary extraction:
  428 derived stop/post children were emitted, 8,201 retained calls referenced them, and no
  `stop_times` row referenced a missing stop. Generated smoke-test files were removed afterward.
- Conversion and test logs retained the two pre-existing build warnings. Directly passing a JDF
  ZIP to `jdf-to-gtfs` still logs an error while returning exit code zero; directory input works.

### Remaining caveats and next handoff

- The project submodule pointer has not been advanced. Pin the standalone fork commit only after
  reviewing the independently scoped JrUtil changes.
- `ids_system_id`, CZPTT extensions, checksummed conversion manifests, Parquet sidecars and source
  snapshot provenance remain deferred.
- The next safe slice is importing one tiny extended JDF conversion into the canonical schema,
  followed separately by the CZPTT conversion contract.

## 2026-07-18 — Milestone 0 implemented

### Delivered

- Bootstrapped a Python 3.13 modular monolith managed by uv, with Ruff, strict Pyright, pytest,
  SQLAlchemy 2, Alembic, psycopg, and GeoAlchemy.
- Added a pinned PostgreSQL/PostGIS development service in `docker-compose.yml` and CI checks that
  validate the Compose configuration before starting the database.
- Added the initial Alembic schema for canonical entities, typed non-recycling ID sequences,
  lifecycle states, source bindings, identifier aliases, diagnostics, locations, routes,
  calendars, timetable variants, and calls.
- Implemented canonical allocation, redirects, tombstones, point-in-time source resolution,
  atomic ambiguity failure, separately committed ambiguity diagnostics, and typed DÚK alias
  normalization (`582588 -> 001588`).
- Implemented stop places with an unspecified boarding-point fallback, exact boarding points,
  operational points, passenger/operational call invariants, GTFS service-day time values beyond
  24:00, dated road-trip resolution, and full-train resolution from a PID call subsequence.
- Added fictional native JDF 1.11, CZPTT, PID GTFS, and DÚK fixtures with explicit normalized JSON
  projections. The native JDF files intentionally use Windows-1250 and CRLF as required by JDF.
- Documented that JDF stop continuity currently uses a mock authoritative shared ID. This is test
  scaffolding, not a claim that real national JDF-derived stop IDs are stable.

### Validation evidence

- Alembic revision `0001` was applied successfully to PostgreSQL/PostGIS 17/3.5.
- Full pytest suite passed against that database: **21 passed** (12 unit, 9 integration).
- Ruff lint and formatting checks passed.
- Strict Pyright passed with no errors or warnings.
- The uv lockfile passed an offline consistency check.
- Both native JDF fixtures were accepted and converted by the pinned JrUtil.
- The native CZPTT fixture was deserialized and merged by JrUtil. Its full GTFS conversion then
  reached JrUtil's unrelated live SŽ company-registry lookup; native-to-projection golden tests are
  still deferred.
- The JrUtil submodule source and pointer were not changed during Milestone 0.

### Current boundaries

- There is no production downloader, source snapshot store, national bundle importer, structural
  stop-continuity matcher, canonical GTFS exporter, realtime worker, API, or frontend. The JDF
  conversion bundle exists only in the standalone JrUtil fork and is not yet pinned here.
- Native fixtures and normalized projections coexist, but no automated adapter test yet proves
  their complete equivalence.
- The Compose credentials and exposed port are development-only.
- Flat Snappy Parquet is now the locked JDF bundle interchange format. CZPTT operational schemas
  and compatibility with the future Python importer remain unproven.

### Next handoff — JDF bundle import

After pinning the reviewed standalone fork commit, add a Python reader for bundle format v1,
validate its Parquet schemas independently, and import one tiny JDF bundle into PostgreSQL. Then
begin real two-export stop-continuity diagnostics. Keep CZPTT operational semantics as a separate
follow-up slice.
