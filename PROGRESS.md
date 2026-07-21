# Oběhy implementation progress

This file is the concise engineering handoff for completed work. `BASE_PLAN.md` remains the
authoritative roadmap and architecture document.

## 2026-07-21 — Referenced-stop pruning and refresh-first coordinate repair

### Delivered

- JrUtil now derives its stop set from the final emitted `stop_times`, retaining only called
  boarding points and required parents. The same set filters `cz_stops`, `cz_stop_zones` and
  `source_stop_metadata.parquet`, preventing dangling extension metadata and removing stops that
  occur only on filtered/pass-through calls or nowhere in the emitted service.
- Coordinate selection now preserves stop versus town precision and source provenance. Bundle
  schema v2 adds `coordinate_precision`, `coordinate_source` and `coordinates_missing`; invalid
  finite/range values still fail, while unresolved stops remain valid mandatory `0,0` GTFS rows
  with one aggregate warning and structured stop-ID diagnostics. GTFS stop places and their
  boarding-point/post children now append ` [APPROX]` exactly once when the selected coordinate is
  town-level; stop-precise and missing-coordinate names remain unchanged.
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
  merge. The merge phase reconciles still-missing final normalized stop identities against external
  geodata, preferring strict-okres candidates before boundary-tolerant alternatives and using a
  unique RUIAN town as the final conservative fallback.

### Validation and remaining live maintenance

- JrUtil passed **47 tests** and the multitool CLI built successfully. Geodata adapters/gap-fill
  include **37 tests**; the current review environment passed the five stdlib-only residual-plan
  tests but could not import the other modules because `lxml` was unavailable. Oběhy passed **26
  tests** with **10 expected skips**; Ruff, formatting and strict Pyright passed.
- The regenerated `JDF-final` initially contained 75 referenced stop-place identities without
  coordinates. A clean post-merge reconciliation against only the checked external/manual
  catalogue recovered all 75; the eight final aliases are now recorded in the geodata repository.
- A conversion of the already-merged national JDF (performed before the final provenance-table
  adjustment) reduced 37,960 stop places to the 37,708 actually referenced places: **zero**
  unreferenced boarding stops remained. Of these, 35,314 were stop-precise, 805 used town precision
  and 1,589 remained missing (`0,0`). Independent bundle verification passed and reported the
  missing coordinates once; its 3,178-row count includes both parent and boarding rows.
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
  refreshed-source recoveries**, leaving **zero unresolved work-list identities**. The Mapy key and
  raw responses were not retained. One manually reviewed school POI fallback is marked town-level;
  all other new accepted rows are stop-level except explicit Mapy town fallbacks. A final national
  conversion and the <=5% matcher benchmark remain deferred.

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
