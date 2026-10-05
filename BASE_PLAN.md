# Oběhy — Czech Nationwide Public-Transport Data Platform

## Authoritative architecture plan

**Working goal:** build **Oběhy**, a nationwide Czech public-transport data platform that:

- publishes two coordinated nationwide GTFS Schedule feeds: road and urban transport from JDF
  (`jdf`), and rail from CZPTT (`czptt`);
- overlays higher-quality regional and operator data onto the national conversions;
- publishes fused GTFS-Realtime feeds for each static feed;
- infers trips, delays and vehicle circulations (*oběhy*) from realtime sources of very
  different quality;
- powers a public vehicle and departures map;
- preserves Czech-specific identifiers and metadata where useful;
- supports dynamic platforms/posts, alerts, vehicle details, train compositions and historical
  arrival/departure data;
- runs as a community project on one application server.

Additional regions and providers must be addable incrementally without rewriting the frontend or
the core matching logic. Adding a realtime source should mean writing a connector that emits
facts, a manifest and fixtures, not a new matcher.

`PROGRESS.md` holds the current state and working backlog. The executable static contract is
JrUtil's `docs/PRODUCTION_CONTRACT.md` together with `contracts/production-v3.json` and
`contracts/serving-v4.json`. The target contract for the mirror and the realtime core is serving
schema 5.0 (`contracts/serving-v5.json`, draft). Serving versions are `major.minor`: a minor
version only adds relations, nullable fields, enumeration values or namespaces, and Oběhy accepts
every minor of its major.

---

# 1. Architecture and ownership

```text
GitHub Actions: obehy build ──► GitHub Release (release.json + jdf/ + czptt/ packages)
                                        │  pull, verify hashes
application server:                     ▼
  obehy release fetch → obehy release load ──► PostgreSQL + PostGIS ◄─────────────┐
                                                control / static / rt              │ observation log,
                                                  │        ▲ NOTIFY publication    │ events, current
                                                  │        │                       │ state projection
           upstream realtime APIs ──► obehy realtime (one asyncio process, in-memory state)
                                                  │
                                                  ├─► /gtfs-rt/{jdf,czptt}/*.pb (atomic files)
                                                  ▼
                                          obehy api (FastAPI) ──► /api/*, /gtfs/{feed}.zip
```

Ownership:

- **JrUtil** compiles every static build: national JDF and CZPTT conversion, regional overlays,
  stop and post identity, GTFS and the typed serving package. It is the only static compiler.
- **The GitHub Actions pipeline** runs `obehy build`, which acquires sources, drives JrUtil and
  publishes one immutable release. **Static feeds are never built on the application server.**
- **Oběhy on the application server** fetches releases, loads them into its PostgreSQL mirror,
  activates them atomically, runs the realtime core and serves the API and feeds. It never
  compiles, reconciles or arbitrates static source data.
- **Identity** is owned by JrUtil's deterministic ID rules plus the reviewed, append-only
  registry files in `jrunify-ext-geodata/registry/` (section 6). There is no identity service.
- **MOTIS** remains the connection-search engine and the source of the route-shapes companion
  (section 17).

Oběhy is a **modular monolith**: one Python package (`src/obehy/`) with a few processes. On the
application server:

```text
obehy release fetch|load|activate   one-shot, driven by a systemd timer
obehy realtime                      long-running realtime worker
obehy api                           FastAPI
web                                 later: React + MapLibre static assets
motis                               later: managed MOTIS instance
```

There is no job queue, message broker or stream processor. PostgreSQL, the filesystem and one
realtime process are enough for one machine. Revisit only when measurements show otherwise.

---

# 2. Project scope

## First public release

- nationwide static data from JDF and CZPTT as two GTFS feeds, with the PID and IDS JMK overlays;
- stable public stop, route and trip IDs from the JrUtil identity rules and reviewed registry;
- the static mirror loaded and activated through Oběhy;
- DÚK realtime (buses and trains) and SŽ rail realtime, fused per train;
- PID realtime from the Golemio APIs, and PID alerts from GTFS-RT;
- realtime for long-tail sources without trip IDs, starting with Arriva Express;
- delay derived from Oběhy's own GPS progress where that beats the source's delay;
- per-feed GTFS-RT and a project API with scheduled and realtime departures;
- a basic map of vehicles and departures.

## Later capabilities

- learned vehicle circulations (*oběhy*): trip forecasts, knock-on delays, expected vehicles;
- additional regional static feeds and realtime connectors;
- dynamic bus posts and train platforms;
- inferred arrivals, departures and pass-through events, and historical punctuality;
- vehicle registries and features;
- ČD train compositions, and 55p.cz compositions after explicit permission;
- external travel-time providers for long coach segments;
- historical trip replay.

---

# 3. Non-negotiable design rules

1. Public IDs come from JrUtil's identity rules and the reviewed registry, never directly from a
   mutable source-local ID.
2. Never recycle public IDs.
3. Never replace an entire trip with a regional trip that only covers part of it.
4. Overlay fields and journey segments, not ZIP files as opaque units.
5. Keep every realtime observation with provenance, even when it loses arbitration.
6. Never interpret a coarse zero-minute delay as proof that a vehicle is exactly on time.
7. Never average contradictory vehicle positions.
8. Never expose railway pass-through points as passenger stops.
9. Never publish a realtime platform/post assignment that cannot be mapped to a boarding point in
   the active static build.
10. Never broaden a regional alert beyond the area or journey segment it actually affects.
11. Activate a new static release and its realtime mappings atomically.
12. Quarantine ambiguous matches instead of guessing. An inferred match is accepted only when it
    is unique by a policy margin, and its explanation is retained.
13. Trip progress never jumps backwards or skips unvisited calls without evidence. A bad
    observation holds the state; it does not move it.
14. Prefer a degraded but valid feed over publishing corrupted data.
15. Every static build must be reproducible from stored input snapshots and configuration.
16. Every realtime output must be reproducible by replaying the archived source payloads.
17. Every selected realtime value must be explainable by its source, timestamp, method and
    confidence.
18. GTFS is not the semantic archive: every useful JDF 1.11 fixed code, note, restriction and
    connection claim must survive in a typed serving relation at its original scope.
19. Preserve each JDF code's actual closed- or open-world semantics. A useful GTFS approximation
    may coexist with, but never replace, the exact typed fact needed by Oběhy and NeTEx.
20. A NeTEx v2.0.0 export with zero unexplained semantic loss is the acceptance gate for the
    static serving model. See `JDF_SEMANTICS.md`.

---

# 4. Implementation stack

## Static build (GitHub Actions)

- **Python 3.13** — `obehy build`: source acquisition, GVD resolution, OSM snapshot, JrUtil
  orchestration, filtered feed, validation and publication.
- **F#/.NET and JrUtil** — national conversion, overlays, identity, GTFS and serving packages.
- **Parquet** — the typed serving relations.

## Application server

- **Python 3.13**, asyncio.
- **PostgreSQL + PostGIS** — the static mirror, the realtime log and projections, history.
- **psycopg 3** with raw SQL migrations; no ORM.
- **pyarrow** — reading serving Parquet for binary COPY.
- **FastAPI** — project API, feed endpoints and debugging endpoints.
- **gtfs-realtime-bindings / protobuf** — GTFS-RT decode and encode.
- **Parquet** — cold historical archive.

## Frontend

- **React** and **MapLibre GL JS**.

## Deployment

- systemd units (or Docker Compose) on one server; reverse proxy for public endpoints;
- one PostgreSQL server;
- local filesystem directories for releases, the raw realtime archive and checkpoints;
- GTFS-RT files written atomically and served directly by the reverse proxy.

---

# 5. Repository layout

```text
obehy/                         (this repository)
├── src/obehy/
│   ├── cli.py                 obehy build and the command entry points
│   ├── pipeline/              shared static-build plumbing
│   ├── national_jdf.py, national_czptt.py, regional_overlay.py, filtered_jdf.py, ...
│   ├── release/               fetch.py, load.py, activate.py, migrations/
│   ├── realtime/
│   │   ├── model.py clock.py archive.py worker.py replay.py evaluate.py policy.py
│   │   ├── connectors/        base.py, duk.py, sz_*.py, pid_*.py, arriva.py, ...
│   │   ├── infer/             facts.py, scorers/, engine.py, index.py, dates.py, runs.py, binding.py
│   │   ├── progress/          path.py, project.py, gps_delay.py, travel_time.py
│   │   ├── timeline/          intervals.py, semantics.py, engine.py, propagate.py, arbitration.py
│   │   ├── circulations/      build.py, model.py
│   │   └── emit/              gtfsrt.py, state_table.py
│   ├── api/                   app.py, static.py, realtime.py, debug.py
│   └── data/                  versioned rules, policies and connector manifests
├── config/                    obehy.example.toml, local config (not committed)
├── docs/sources/              one dossier per realtime source
├── tests/                     unit/, fixtures/, replay/
├── infra/                     compose.yaml, systemd units
└── converters/
    ├── jrutil/                pinned submodule
    └── jrunify-ext-geodata/   pinned submodule (geodata and identity registry)
```

JrUtil and JrUnify-Ext-GeoData are developed in their standalone checkouts and pinned here. The
MOTIS route-shapes companion will live in `converters/motis-route-shapes/`, pinned to MOTIS
release `v2.11.0` (`dc441d684099afbf4ce82d605f26e46504c70c28`) initially.

---

# 6. Identity

There is no identity service. Public identity consists of:

1. **Deterministic JrUtil ID rules** that derive IDs from stable source identities:

   ```text
   jdf:route:<cis line>                     regular line route
   jdf:route:<cis line>:detour              výluka timetable route
   jdf:trip:<line>:<yymmdd>[:det][:<hash>][:pN]:<cis trip>
   czptt:stop:<country>:<SR70>              railway primary location
   czptt:stop:<country>:<SR70>:platform:<n> railway platform
   czptt:route:…, czptt:trip:<PA id>:<part>, czptt:block:<PA id>
   ```

2. **The reviewed, append-only registry** in `jrunify-ext-geodata/registry/`, for identities
   that the sources do not keep stable:

   ```text
   stops.csv            jdf:stop:<N>  ← (town, district, nearby place, okres, country, coordinates)
   posts.csv            posts of a registered stop, including pinned inferred est:<k> posts
   overlay_places.csv   source-native stop places created by regional overlays
   ```

   - `merge-jdf --stop-registry` preloads registered stops, so `jdf:stop:N` does not depend on
     batch order. Same-named stops are split by reference coordinates; ambiguity is quarantined.
   - An unregistered stop gets a provisional number ≥ 1 000 000 000 hashed from name, okres and
     country, and it appears in the release's `stop-registry/` candidate CSVs.
   - `registry.py promote` adds reviewed rows. Rows are never deleted or renumbered; retirement
     is a status change. A future redirect or alias is another reviewed registry file applied
     by JrUtil.

Stability scope, stated honestly:

- stop, post and rail-location IDs are stable across builds;
- route IDs are stable while the CIS line exists;
- **trip IDs are stable for one timetable version** of a line. A new version (`yymmdd`) yields
  new trip IDs. Realtime never depends on trip-ID stability across releases because it resolves
  through timetable-stable keys (CIS line + CIS trip, train number, source bindings) against the
  active release.

Every package declares `identity_contract = "jrutil-identity-v1"` and the identifier namespaces
it uses. Oběhy stores every ID as unrestricted text.

Surface (JDF) and heavy-rail (CZPTT) identities never merge. Their namespaces are disjoint, which
is also what lets the two feeds share one database without stitching.

## 6.1 Identity claims versus source bindings

1. An **identity claim** is an explicit source assertion such as a CIS line, CIS trip, CIS stop
   or a trip ID documented to contain a CIS trip. JrUtil validates it against the national
   snapshot.
2. A **source binding** links a source-local object (a PID or IDS JMK GTFS trip, stop or route)
   to a public entity after identifiers, calendars and structure have been considered. Bindings
   are published in `source_key` (with the binding method) and, where call sequences differ,
   `call_key`, both with explicit identifier namespaces.

Never copy a guessed CIS identifier into normalized source data to make matching look uniform.
Store the original fact and the binding separately. Trivial transformations of identifiers by a
realtime API (prefixes, padding, formatting) are normalized by that source's connector and
documented in its dossier; they are not an identity mechanism. A source that gives only a public
line number supplies it as a `LineRef`, and the inference engine resolves it (section 19).

Treat a provider-supplied operational-to-static crosswalk as a candidate relation, not a unique
dictionary. IDS JMK `api.txt`, for example, maps `(source line code, course/train number)` to a
GTFS `trip_id`; the same key can map to several rows for different calendars. Resolve by
operating date and context; quarantine what stays ambiguous.

---

# 7. Stop and location model

A single generic "stop" entity is insufficient. The model distinguishes three classes.

## 7.1 Stop place

A rider-facing geographic place or station, such as `Praha, hlavní nádraží` or
`Teplice, Benešovo náměstí`. Used for search, map labels, nearby-departure grouping,
interchange grouping, parent-station relationships, and accessibility and place-level metadata.

## 7.2 Boarding point

A concrete place where passengers board or alight: a platform, track, post or direction-specific
pole. A stop place may have many boarding points. A stop place with boarding points also has an
**unspecified** child (`…:unspecified`); calls without a known post or platform use it until an
exact claim exists.

```text
czptt:stop:CZ:<SR70>               stop place
czptt:stop:CZ:<SR70>:unspecified   unspecified boarding point
czptt:stop:CZ:<SR70>:platform:1    platform 1
```

Realtime can reassign a call to a known boarding point of the same stop place (section 24).

## 7.3 Operational point

A location used for vehicle progress and timing, but not shown as a passenger stop: a railway
station passed without stopping, a junction, a block or timing point, a non-passenger CZPTT
location, potentially a bus timing checkpoint.

Operational points stay outside public `stop_times.txt`. A railway location keeps its SR70
identity whether a call is passenger-facing or operational-only. Operational points are
`location` rows of kind `operational_point`, and the calls at them are non-passenger rows of the
trip's `trip_call` sequence, used by the realtime core (section 19.4).

## 7.4 Disjoint transport domains and nearby presentation

Locations belong permanently to either the `surface` or `heavy_rail` domain. National JDF defines
the surface domain and national CZPTT defines heavy rail. They never resolve to the same stop
place, even when a railway station and bus stop share a name and coordinates.

Boarding points remain children of exactly one stop place in the same domain. Regional sources
that contain both domains, such as PID, classify each source object before matching.

The API and frontend may show nearby places of both domains together. That is presentation and
walking-transfer behaviour, not an identity merge.

---

# 8. Stop continuity across national exports

Czech national JDF exports do not provide immutable stop IDs. Continuity comes from the registry
(section 6):

1. registered stop by `(town, district, nearby place, okres, country)`, split by reference
   coordinates when names repeat;
2. stable identifiers such as PostID or ASW ID where genuinely supplied (overlays);
3. otherwise a provisional hashed number and a registry candidate for review.

Structural signals (coordinates, serving routes, neighbouring stops, mode, boarding points,
coordinate shift) are review evidence. They never override a conflicting registered identity
automatically.

Each build reports, in the release's `stop-registry/` output and diagnostics:

```text
Registered stops matched
Provisional (unregistered) stops
Ambiguous same-name stops (quarantined)
Large coordinate shifts against the registry
Registry candidates for review
```

A sudden increase in provisional stops should block publication of the release.

---

# 9. Route and trip identity

## 9.1 Road, tram and urban transit

The timetable-stable trip key is `CIS line + CIS trip`. The operating instance is
`CIS line + CIS trip + operating date`. These are identities of the national timetable, not
mandatory fields in every regional overlay: a regional GTFS trip reaches them through a source
binding without exposing the CIS identifiers itself. `source_key` publishes the key mapping with
validity ranges.

## 9.2 Rail

The timetable-stable key is the train number. The operating instance is
`train number + operating date`. `source_key` publishes the mapping. CZPTT may split one train
into several GTFS trips (`:1`, `:2`, rail-replacement parts); `trip.run_key` and `run_part` group
and order them, and the realtime core treats the parts of one train on one date as one **run**
(section 19.4).

## 9.3 Realtime instances

```text
TripInstance = (feed, trip_id, service_date)        road
RunInstance  = (train_number, operating_date)       rail; projects onto its trip parts
```

---

# 10. Trip calls

The serving model uses one ordered call sequence per trip (`trip_call`) with passenger flags,
scheduled arrival/departure/passage, an optional boarding point, the route stop slot, pickup and
drop-off types, the timepoint flag and shape distance. For CZPTT the sequence is complete:
it includes the timing points passed without stopping, with passage times and the CZPTT
subsidiary location and active line code.

```text
10  Praha hl.n.     passenger=true
20  Praha-Libeň     passenger=false
30  Český Brod      passenger=false
40  Kolín           passenger=true
```

The GTFS export publishes only passenger calls. The realtime core uses the full sequence, so
non-stop railway points anchor delay estimates without being exposed to riders as stops.

---

# 11. JrUtil workstream

JrUtil owns the complete production static pipeline. `obehy build` drives the JrUtil multitool:

```text
fix-jdf → merge-jdf → jdf-to-bundle            national JDF package
regional-gtfs-overlay                          PID + IDS JMK on top of it
czptt-to-bundle                                national rail package
validate-package                               for each result
```

Every package records the exact JrUtil commit as its compiler version. Sources reach JrUtil only
as checksum-pinned local snapshots; live URLs and credentials never enter its inputs.

The production package contains standard `gtfs.zip`, the typed Parquet serving relations with
fixed schemas, unique keys and resolving foreign keys, a manifest with sizes, SHA-256 hashes,
namespaces, source snapshots, feed version and identity contract, and bounded diagnostics. Bundle
v3 carries serving schema 4 (30 relations). Serving schema 5.0 (draft, 19 relations) is the
target: one call sequence per trip including rail pass-throughs, `source_key`/`call_key` lookups
with documented identifier encodings, closed enumerations, feed-prefixed IDs, and the typed
semantics in `service_note`, `assignment`, `connection_claim` and `travel_restriction`. `gtfs.zip`
is a pure projection of the relations. `JDF_SEMANTICS.md` is the normative preservation addendum:
GTFS is a projection of the typed facts, not their storage.

Resource rules:

- national-sized relations are streamed once per stage; the compiler may not retain or emit a
  second 17-million-row JDF call relation;
- stage duration, peak memory and row counts are recorded;
- after the first accepted production benchmark, unexplained performance regressions above
  15 percent fail the build gate;
- the build must fit a GitHub Actions runner. If it cannot, a self-hosted runner is the fallback,
  never the application server.

Contract items needed by the realtime core are resolved by serving schema 5: the JDF district
code is published separately from stop-name components (JrUtil `d26bf0b`), CZPTT operational
points are part of `trip_call`, and GTFS `stop_sequence` equals `trip_call.sequence` because GTFS
is projected from it.

Maintain tiny golden fixtures for one JDF bus route, one JDF trip with multiple posts, one CZPTT
train with passenger and pass-through points, one overnight service, and one export where local
source IDs change.

---

# 12. Static source precedence and overlays

Static overlays apply to road and urban transport (the `jdf` feed) only. **No regional static rail
data is used**: rail static is national CZPTT alone, and regional sources contribute to rail only
through realtime (section 13).

Generic GTFS merging is not sufficient. JrUtil's overlay compiler overlays selected fields,
calls, journey segments and metadata. It does not replace entire trips merely because a matching
trip exists. Every imported notice, zone, connection and travel restriction is a positive source
claim; a regional feed that omits a field makes no deletion claim against national data.
Precedence is resolved during compilation; runtime requests never scan Parquet or arbitrate
static source claims.

## 12.1 Field-level precedence

For every source, mode, coverage scope and capability, the versioned policy sets an integer
priority and one of:

```text
disabled       ignore this capability
fill_missing   use it only when no selected value exists
preferred      beat lower-priority eligible claims
authoritative  must win inside proven coverage or produce a blocking conflict
```

Capabilities include schedules/calendars, names/coordinates, posts/platforms, route display,
shapes, accessibility, zones/fares, notices, restrictions and connections. Equal-priority
conflicts are quarantined. Permission to add unmatched stops, routes and trips is configured
independently per source. Do not implement an implicit "PID wins everything" rule.

## 12.2 Entity-specific deduplication

- **Trips:** road/MHD by CIS line + CIS trip.
- **Routes:** road/MHD by CIS line; source route IDs remain bindings.
- **Stops:** PostID, ASW ID or CIS stop ID; explicit crosswalk; registry continuity; structural
  candidate for review; reviewed manual mapping; otherwise a new provisional identity.

## 12.3 Regional GTFS adapter and matching contract

GTFS identifiers are source-local unless the provider documents another namespace. Every adapter
preserves the original rows and emits typed hints or identity claims; it never manufactures CIS
identifiers. Raw custom columns and companion files (such as IDS JMK `api.txt`) stay in snapshot
storage so adapter rules can be audited and replayed.

Static road/MHD matching order:

1. documented CIS line/trip claims, validated against the national snapshot;
2. an already reviewed source binding that remains structurally consistent;
3. candidate routes constrained by operator, mode, validity, public designation and geography;
4. operating-instance comparison by active service date, ordered stops and scheduled times;
5. a reviewed manual binding;
6. unresolved or ambiguous quarantine.

Names, public line numbers and zero-padding may generate candidates but never establish a CIS
identity on their own. Route, trip, stop and post resolution stay independent, so useful post or
shape data is not discarded because another entity is unresolved. When rows with different CIS
trips have identical calls and times, compare calendars; if ambiguity remains, trip-specific
replacement stays quarantined.

---

# 13. Rail: national static, regional realtime

Rail static data comes only from national CZPTT. Regional GTFS rail trips (PID, IDS JMK and
others) are never overlaid onto CZPTT and never published as separate trains.

Regional and operator sources still matter for rail in realtime, and they usually cover only part
of a train:

```text
CZPTT run:      Cheb -> Plzeň -> Beroun -> Praha -> Kolín -> Pardubice
SŽ:             ===================================================  (whole run)
PID realtime:                     ==================                 (inside PID's area)
DÚK realtime:   (none on this train)
```

So a rail run regularly **gains and loses sources en route**. The realtime core (section 19.4)
treats this as normal:

- each source's evidence applies to the calls it actually describes; a source never truncates
  or extends the run;
- when a source starts reporting mid-run, its first observations must agree with the run's
  current progress (section 20.4) before they move the state;
- when a source stops reporting at its area boundary, that is coverage ending, not a stale or
  failed train; the run continues on the remaining sources, or on propagation from the last
  anchor until a source picks it up again;
- the selected position and delay hand over between sources without jumps: a new source's
  values must be continuous with the current state within the plausibility rules, or they are
  held until consistent.

# 14. Static stop and post overlays

A regional source may provide exact posts where the national feed provides only the stop place.
The compiler resolves both to the same stop place, resolves or creates the post, binds the source
post ID, assigns that boarding point only to matching trip calls, and leaves other calls at the
stop place's unspecified boarding point.

```text
Trip 1 -> post B
Trip 2 -> post D
Trip 3 -> unspecified boarding point
```

## 14.1 Flat regional stop feeds

Do not assume a parentless GTFS `location_type=0` row is a complete stop place. Many feeds publish
one boarding post per GTFS stop without `parent_station`. Import such rows as source
boarding-point observations and resolve their grouping separately from identity, preferring:

1. a valid explicit `parent_station`;
2. a documented stop-place key such as PID `asw_node_id` or DÚK `cis_stop_id`/`duk_stop_id`;
3. reviewed provider-specific parsing of a source stop ID;
4. a grouping carried forward from an earlier snapshot;
5. structural candidates (name, coordinates, call structure, post labels);
6. otherwise a singleton source stop place.

Similar names and nearby coordinates may generate candidates but must not silently merge railway
facilities, grade-separated stops or similarly named nearby places. Uncertain groupings remain
unresolved rather than forcing a false merge.

## 14.2 Regional coverage never limits the national stop universe

The national baseline defines timetable completeness. A regional feed may add a stop place, posts
or exact call assignments, but its trip coverage never determines which trips may use that stop.
Creating or matching posts never assigns them to every call at the stop.

---

# 15. Static build and release pipeline

## 15.1 Build (GitHub Actions)

The `obehy build` workflow runs on GitHub Actions on a schedule and on demand:

1. resolve the GVD year and reference date;
2. restore caches (OSM snapshot, routing cache) and validate the OSM manifest;
3. download each static source, store the raw bytes by SHA-256 and record a source manifest
   (source, retrieval time, URL or method, checksum, declared version, licence);
4. build JrUtil at its pinned commit;
5. run the national JDF, regional overlay, filtered JDF and CZPTT stages;
6. validate both production packages and the official GTFS validator;
7. publish the release only when every configured gate passes.

## 15.2 Release

A release is one immutable, content-addressed directory, published as a GitHub Release:

```text
release/<run-id>/
├── release.json        run id, packages with manifest SHA-256 and feed_version, build inputs
├── jdf/                production package: gtfs.zip, serving/, manifest.json, diagnostics.json
├── czptt/              production package
├── jdf-filtered/       derived plain GTFS (gtfs.zip, filter and line reports)
└── stop-registry/      registry candidate CSVs for review
```

Source snapshots, orchestration logs and detailed diagnostics are retained as workflow artifacts
for the configured period.

## 15.3 Gates and last-known-good

Publication is blocked by manifest/hash/schema, referential-integrity, domain, identity,
required-mapping, configured count/drift and official GTFS validation errors. Warnings stay
advisory unless a versioned gate promotes them.

If a regional source fails validation, has catastrophic match-rate changes, contains ambiguous
trip mappings or loses required identity fields, its last-known-good snapshot is used only when
that source explicitly enables fallback and the snapshot is within its maximum age. Otherwise the
optional overlay is omitted and diagnosed. One broken upstream source must not destroy the
nationwide feeds.

---

# 16. Static publication and the Oběhy mirror

## 16.1 Fetch and load

On the application server:

1. `obehy release fetch` polls for new releases, downloads, verifies every hash against
   `release.json` and the package manifests, and unpacks into `data/releases/<run-id>`.
2. `obehy release load` verifies the contract versions (serving schema major 5), then:
   - creates fresh LIST partitions per package load in `static.*`;
   - streams every relation through binary COPY;
   - builds indexes after loading;
   - checks counts, keys and references set-wise;
   - derives non-semantic helpers: `service_date` (service × operating day over the service
     horizon), PostGIS geometry for locations and shapes, and the inference indexes;
   - records the load in `control`.
3. `obehy release activate` switches `control.publication` in one transaction (GTFS files, static
   mirror, source mappings and realtime resolver version together) and sends `NOTIFY`.
   `--rollback` reactivates the previous release.

A failed load leaves no attached partitions. The active release and its two most recent
predecessors keep their database payloads; older releases keep only metadata.

The loader never performs identity matching, trip collapse, overlay precedence, fuzzy matching or
static claim arbitration.

## 16.2 Database

```text
control   release, package, load, publication (+ history), source health, configuration digests
static    the serving relations per package load, plus derived helpers
rt        realtime observations, events, conflicts, alerts, assignments, current-state projection
history   later: archived events, vehicle day runs, circulation model
```

All public IDs are unrestricted text. Migrations are raw SQL, versioned in
`src/obehy/release/migrations/`. The static DDL is generated from `contracts/serving-v5.json` so
column types cannot drift from the contract.

## 16.3 Two feeds

| Feed | Static | Realtime |
|---|---|---|
| `jdf` | `/gtfs/jdf.zip` | `/gtfs-rt/jdf/{trip-updates,vehicle-positions,alerts}.pb` |
| `czptt` | `/gtfs/czptt.zip` | `/gtfs-rt/czptt/{trip-updates,vehicle-positions,alerts}.pb` |

`/gtfs/manifest.json` lists the active release, both `feed_version`s and hashes; versioned copies
are available under `/gtfs/versions/<run-id>/`. Each GTFS-RT feed names its static
`feed_version` and refers only to its own feed's IDs. The project API is feed-agnostic: a stop's
departures mix both feeds.

## 16.4 Typed semantic subset

The typed relations are not optional. `assignment` preserves route, trip and call features
(reservations, bicycle and luggage carriage, vehicle accessibility, on-request and conditional
operation), stop accessibility, facilities and interchange hints, and note links, each with its
original JDF code and source object. `service_note` preserves `Udaje`, `Caskody` and `Mistenky`
verbatim and typed. `connection_claim` preserves `m`/`M` at the supplied specificity, including
unresolved claims. `travel_restriction` retains its original scope.

CZPTT uses the same relations: notes stay lossless; codes `17`/`34` become wheelchair-capable
vehicle features; `22`/`26`–`29` positive bicycle features; `36` an authoritative bicycle
prohibition. Only whole-trip, all-date claims enter GTFS trip flags. Activity `0030` is both an
`on_request` call feature and GTFS pickup/drop-off type `3`. Rail-replacement boundaries use
stop-pair `transfer_type=2` rows with `min_transfer_time=0`. Only uniquely resolved connection
claims become `transfer` rows.

---

# 17. MOTIS route-shapes companion

Shape generation is part of the static build on GitHub Actions: it runs after overlays and before
final validation, and its output ships in the release. It is an internal build tool, not an Oběhy
API, and has no pfaedle fallback. The application server only loads the resulting shapes.

**Status: not implemented.** `obehy build` has an explicit no-op enrichment stage at this
position; packages currently carry only source shapes, if any. Until generation exists, the
realtime core uses stop-to-stop polylines (section 21.2).

```text
authoritative regional/operator shape
    else existing acceptable national shape
    else MOTIS-generated shape
    else no shape
```

A trip without a shape is preferable to a build failure. Source and retained national shapes are
never sent to or overwritten by the companion.

## 17.1 Companion boundary

A thin C++ CLI in `converters/motis-route-shapes/` against the pinned MOTIS, Nigiri and OSR
sources invokes MOTIS's import-time `route_shapes` implementation directly, without starting a
MOTIS server:

```text
obehy-motis-shapes --gtfs <candidate-projection.zip> --osm <snapshot.osm.pbf>
                   --work-dir <dir> --output <bundle-dir> --threads <auto|N>
```

The compiler creates a temporary GTFS projection of trips still lacking a shape, with one
synthetic service day, and invokes `route_shapes` in `all` mode. Every supported MOTIS routing
profile is enabled. Unsupported modes and routes with fewer than two distinct positioned stops
stay unshaped with diagnostics.

Cache keys cover the exact OSM hash, MOTIS and OSR versions, routing configuration, profile and
ordered stop coordinates. Cold and warm runs over identical inputs must be byte-identical.

Output: `shapes.txt`, `trip_shapes.csv` (trip → deterministic shape ID, profile, status,
provenance), `stop_shape_offsets.csv` (trip, stop sequence, point index, cumulative distance),
`diagnostics.json` and `manifest.json`. Shape IDs derive from the profile and canonicalized
geometry.

**Shapes with stop offsets are also the preferred path for the realtime progress engine
(section 21).** Until they exist, the realtime core uses stop-to-stop polylines.

## 17.2 Validation

Accept a generated shape only when it has at least two distinct valid points, every stop offset
exists and is monotonic, cumulative distances are monotonic, every call is within the mode's
stop-to-shape distance, and every reference joins to exactly one trip and shape. Beeline
fallbacks are explicit low-quality diagnostics. A rejected shape leaves the trip unshaped and
never fails the build.

Maintain golden fixtures for bus, coach, tram or rail, ferry, loops, branches, short turns,
repeated stops, unsupported modes, missing coordinates, routing failure and beeline fallback.
Before activation, benchmark against frozen pfaedle outputs and withheld authoritative Czech
shapes. Every MOTIS upgrade needs an explicit fixture, output and performance review.

---

# 18. Realtime core: layers, connectors and execution

The realtime core is the heart of Oběhy. It must cope with sources ranging from exact trip IDs
with per-stop predictions down to "line, trip, delay, untimed GPS".

## 18.1 Layers

```text
connector.fetch ─► raw archive ─► decode ─► Observation (source-shaped, unresolved)
   ─► Trip inference (trip/run instance + vehicle binding; method, confidence, explanation)
   ─► Evidence extraction (declared source semantics → interval constraints / events / predictions)
   ─► Timeline engine (per instance: actuals, progress, predictions, conflicts)
   ─► Arbitration (per call field and capability, by policy)
   ─► Projection: GTFS-RT per feed, rt.trip_state_current, rt.stop_event, debug output
```

Every layer after the connector is a pure function of its inputs, the active release and an
injected clock. Connectors are the only part that performs I/O. That is what makes deterministic
replay possible.

## 18.2 Connectors

**One connector per upstream system.** The boundary is a shared base URL, authentication, rate
limits, payload family and ID semantics. Inside it, **channels** are individual endpoints or
polls with their own intervals (for example SŽ train positions versus SŽ station departure
boards). Capabilities are a declared feature set of the connector, not separate connectors.

Each connector has a manifest (code plus TOML):

- **channels:** endpoint, poll interval, timeout, payload type;
- **capabilities per channel:** `vehicle_position`, `trip_progress` (last/current/next stop),
  `stop_event` (actual arrival/departure/passage), `delay`, `prediction` (per-call ETA/ETD),
  `platform`, `vehicle_assignment`, `vehicle_attributes`, `occupancy`, `trip_status`
  (cancelled/skipped/added/detour), `alert`, `composition`;
- **fact semantics:** which facts it supplies (section 19) and in which namespaces, whether it
  supplies an operating date, timezone;
- **time and delay semantics** (section 20.2): granularity, rounding, signedness, reference
  event, whether there is a source clock, typical latency, maximum staleness;
- **coverage:** modes and region.

Deployment configuration enables or disables channels and capabilities; a disabled capability's
channels are not polled. Policy (section 23) is a separate versioned file. Provenance records
`source_id` plus `channel`, and a policy may target either.

Connectors parse and normalize only. They never resolve trips, rank sources or know output IDs.
**Trivial source quirks are normalized in the connector**: line-number prefixes, plate
formatting, "Jede včas" and similar texts. There is no alias machinery in the core.

```text
Observation
    source_id, channel
    received_at
    observed_at          interval: source clock at its precision,
                         or [received_at − max_staleness, received_at] without a source clock
    vehicle_key          stable source vehicle identifier if any (RZ plate, vhc_id, fleet number)
    facts                open typed bag (section 19.1)
    payload              position, delay, events, predictions, platform, status, alerts
    raw_ref              pointer into the raw archive
```

Every source gets a dossier in `docs/sources/<source>.md` before its connector is written:
endpoints, terms and licence, identifier namespaces, delay rounding, sign and reference event,
timestamp semantics, update rates and captured edge cases (midnight, regressions, platform
changes). Its manifest semantics must be backed by captured examples.

## 18.3 Execution

- One asyncio process. Connector pollers push observations onto a queue; a single core loop
  applies them in `(received_at, source order)`, so state changes are deterministic.
- An emit tick (default 10 s) writes GTFS-RT `.pb` files per feed by atomic rename, upserts
  changed rows of the UNLOGGED `rt.trip_state_current` (joined by the API with static
  departures), and writes a state checkpoint.
- The inference indexes for the active release are double-buffered and swapped between cycles
  when `NOTIFY` announces a new publication. States are re-keyed by `(feed, trip_id, date)` where
  unchanged; the rest are rebuilt from fresh observations. Releases activate at night.
- Restart loads the checkpoint and today's `rt.stop_event`, then resumes polling.
- Scale target: about 20k vehicles updating every 10–30 s, roughly 1–2k observations per second,
  handled by one Python process with in-memory indexes. Shard per feed only if measurements
  require it.

## 18.4 Persistence, archive and replay

- `rt.observation`: normalized observations with their inference result. Daily partitions,
  batched COPY, retention by dropping partitions.
- `rt.vehicle_assignment`: vehicle → instance bindings with method, confidence and validity.
- `rt.stop_event`: accepted actual events (kept long term).
- `rt.conflict`, `rt.alert`, `rt.source_health`.
- Raw archive: `data/rt-raw/<source>/<channel>/<date>/…`, zstd-compressed and content-addressed,
  retained per source licence.
- `obehy rt record` archives sources without processing them, so replay corpora can be collected
  before a connector exists.
- `obehy rt replay --release R --from --to [--sources]` runs archived payloads through the same
  core with a simulated clock. Outputs are deterministic and golden-testable.
- `obehy rt evaluate` compares predictions against later actual events: MAE and p90 by lead time,
  source, method and mode. **Source priorities, inference thresholds and the choice between
  source delay and own GPS delay are set from these numbers.**

---

# 19. Trip inference

## 19.1 One extensible, fact-based engine

**The common case is easy.** Almost every source supplies one of:

- a train number;
- CIS line + CIS trip;
- PID/IDS GTFS route and trip IDs (on the overlaid parts).

These resolve through the `TripKey` index built from `source_key`. Only the operating date
still has to be inferred (section 19.3).

**The long tail is handled, not special-cased.** Some sources are much poorer:

- Arriva Express exposes line, destination, licence plate, last-position time, at-stop flag,
  "on time" text, next stop, product type and GPS — but no trip;
- DPKV exposes line, trip, delay and untimed GPS, and nothing else.

All of them go through one engine that takes whatever facts an observation carries and infers
the trip instance. A `TripKey` is just a very strong fact, so sane sources pay almost nothing for
the generality. Adding a source means emitting facts, not writing a matcher.

Facts are typed and versioned. A new kind is added by registering a fact type and its scorer.

| Fact | Example | Evaluated as |
|---|---|---|
| `TripKey(namespace, key)` | `gtfs_trip_id` (PID), `cis_line_id`+`cis_trip_id`, `train_number` | index lookup → candidates |
| `LineRef(namespace, value)` | CIS line `580916`; public line + operator | candidate generator |
| `OperatingDate`, `ScheduledStart` | explicit date, first departure | hard filter |
| `Destination(name or ref)` | "Teplice,Celní" | hard filter: last passenger call or headsign |
| `Origin(name or ref)` | — | hard filter |
| `NextStop(name or ref)` | "Teplice,Pražská" | call exists at or after current progress |
| `LastStop`, `CurrentStop(at_stop)` | "v zastávce" | ordering constraint (section 20) |
| `Position(lon, lat, accuracy, bearing)` | GPS | distance to the candidate's path at the implied progress |
| `EventTime(interval)` | "19:30" → [19:30:00, 19:30:59] | anchors time-based scorers |
| `Delay(value, semantics)` | "Jede včas" → `coarse_on_time` | residual of the implied scheduled time |
| `Mode`, `Product`, `Operator` | "Express", Arriva | hard filter where mapped |
| `VehicleKey` | RZ `6SA3700`, DÚK `vhc_id` | binding continuity; circulation prior |
| `CirculationPrior` | learned successor of the vehicle's previous trip | soft prior (section 22) |

**Names are matched only against a candidate's own calls, never globally.** "Teplice,Pražská"
only needs to match a call name on the candidate trip (normalized JDF `Obec,Část,Místo` form),
with GPS proximity as tie-breaker. Ambiguous stop names across the country therefore do not
matter. Stop references in a known namespace are matched through `source_key`.

## 19.2 Procedure

1. **Candidate generation** from the strongest generator available: `TripKey` lookup; else
   `LineRef` → routes → trips; else, as a bounded last resort, `Operator + Mode` within a radius
   of `Position`. Candidates are expanded to instances by operating-date inference.
2. **Hard filters**: each fact may eliminate a candidate by contradiction. Missing facts are
   unknown, never evidence.
3. **Soft scoring**: each scorer adds a log-likelihood contribution (time residual under the
   declared delay semantics, GPS distance to the implied position between `LastStop` and
   `NextStop`, circulation prior).
4. **Decision**: accept the best candidate only if its score reaches the floor **and** beats the
   runner-up by the margin. Otherwise `ambiguous` (quarantined with all candidates) or
   `unmatched`.
5. **Explanation**: every decision stores each fact's contribution for the debug API.

Floors and margins are versioned policy tuned on replay. An inferred match may by default emit
positions and predictions, but its actual stop events become history-grade only once continuity
confirms the binding.

Worked example (Arriva Express): line `580916`, destination "Teplice,Celní", RZ `6SA3700`, last
position 19:30, "v zastávce", "Jede včas", next stop "Teplice,Pražská", product Express, GPS at
Praha, Holešovice.

- Candidates: trips of line 580916 active around 19:30.
- Destination removes the opposite direction; "Teplice,Pražská" must be a later call; at a stop
  with that next stop means the current call is the one before it; GPS inside that call's stop
  area confirms Praha, Holešovice.
- "On time" at 19:30 under the declared semantics puts `S_dep(Holešovice)` in about
  [19:28, 19:31]. One departure fits: accept. Two would be ambiguous.
- From then on the source's coarse "on time" is weak evidence; delay comes from GPS progress
  (section 21).

## 19.3 Operating date

Practically no provider sends the operating date, so date inference is core:

1. Candidate service dates are local today − 1, today and today + 1. Yesterday covers trips past
   midnight and `24:xx+` schedule times; tomorrow covers a vehicle already standing at the origin
   at 23:55 for a 00:05 trip.
2. Keep dates where the service is active (`service_date`) and the observation time falls in
   `[S_start(d) − pre(mode), S_end(d) + max_delay(mode)]`. Scheduled instants are computed from
   noon − 12 h in Europe/Prague, which handles DST days.
3. If several dates survive, the time scorers decide; otherwise `ambiguous`.
4. Once bound, an instance's date sticks. Continuity checks never re-guess the date, and the
   midnight rollover never moves a running trip to the next day's service.

## 19.4 Rail runs

`RunInstance = (train_number, operating_date)` is the ordered concatenation of the train's CZPTT
trip parts plus its operational points with passage times. All rail evidence — SŽ events at
operational points, DÚK and PID positions and delays — feeds one timeline per run. Output is
projected back onto each passenger trip part; operational-only points are never exported.

Sources cover runs partially and change en route (section 13). Each run tracks per source the
call range that source has described so far and its last observation. A source entering
mid-run is accepted once its evidence agrees with current progress. A source falling silent at
the edge of its declared coverage ends that source's coverage without marking the run stale.
Staleness is judged across all sources of the run. Selected values switch between sources only
through the continuity and plausibility rules of section 20.4, so a handover never makes progress
or delay jump. Policy coverage scopes (section 23) say where each source is expected, so silence
inside a source's area is distinguishable from leaving it.

## 19.5 Vehicle binding and continuity

- A `Vehicle` is `(source_id, source_vehicle_id)`. A `VehicleAssignment` binds it to an instance
  with method, confidence and validity. One run may have several vehicles (an SŽ train-number
  position and a DÚK `vhc_id`).
- Once bound, further observations of the vehicle get a **cheap consistency check** (position
  near the path at plausible progress, consistent next stop, not past the trip end plus grace)
  instead of full inference.
- Full re-inference runs on contradiction (section 20.4), at trip end, or when line or
  destination changes. At trip end the circulation prior or `block_key` proposes the next trip.

## 19.6 Untimed and stale data

- A connector without a source clock declares it; `observed_at` becomes a wide interval and every
  time-based scorer and constraint uses that width.
- The core detects frozen positions: identical coordinates across polls while the schedule or
  delay says the vehicle should move. Such fixes are downgraded and not used for progress.
- With a `TripKey` the match stays exact; only the GPS evidence gets weaker.

Unmatched observations stay in the log. The vehicle may appear in the API flagged as unmatched;
it never appears in a TripUpdate.

---

# 20. Evidence and the timeline engine

## 20.1 Event times as intervals

For each call `i` of an instance (passenger calls, and for rail also operational points) the core
knows scheduled instants `S_arr(i)` and `S_dep(i)` (a passage has arr = dep). The unknowns are
the actual instants `A_arr(i)` and `A_dep(i)`.

**Every piece of evidence becomes a constraint `A_x(i) ∈ [lo, hi]` or an ordering relation.**
That one mechanism serves sources reporting very different amounts of information:

| Source says (observed at t) | Constraint |
|---|---|
| actual departure from i at minute precision T | `A_dep(i) ∈ [T, T+59]` (per declared truncation) |
| passage of operational point i at T | `A_arr(i) = A_dep(i) ∈ [T, T+59]` |
| last stop k, not at a stop | `A_dep(k) ≤ t` and `A_arr(k+1) > t` |
| at stop k | `A_arr(k) ≤ t < A_dep(k)` |
| next stop k | `A_dep(k−1) ≤ t < A_arr(k)` |
| delay d, floor, non-negative, reference = last departure k | `A_dep(k) − S_dep(k) ∈ [60d, 60d+59]`; **d = 0 → (−∞, 59]** |
| delay d, rounded, signed | `∈ [60d−30, 60d+29]` |
| delay d, unknown rounding | `∈ [60d−59, 60d+59]` |
| delay d, reference = current position | anchored at the current progress gap |
| source prediction for call j | not a constraint: a prediction candidate (section 20.5) |
| GPS position | progress along the path (section 21) → `gps_derived` constraint |

Structural constraints always hold: `A_arr(i) ≤ A_dep(i) ≤ A_arr(i+1)`, optional minimum run and
dwell times per mode, and the no-early-departure rule where policy requires it (timepoints and
rail passenger stops: `A_dep(i) ≥ S_dep(i)`, unless precise signed evidence shows otherwise).

This is how **arrival versus departure** is handled: sources that know only departures (most
CIS-style delays), only passages (SŽ points) or only "between stops" each narrow the right event.
First and last calls with one scheduled time are handled naturally.

## 20.2 Delay semantics

Delay is never one integer internally. A coarse non-negative whole-minute delay is coarse
evidence: "3 minutes" is roughly 180–239 s depending on rounding; "0" is not proof of exact
on-time running and says nothing about early running. Each connector declares:

```text
granularity_seconds     e.g. 60
rounding                floor | round | ceil | unknown
signed                  whether early running can be reported
reference               last_departure | last_arrival | last_event | current_position | next_arrival
source_clock            whether the payload carries an event time; its precision
latency_seconds         typical lag between event and publication
text_values             e.g. "Jede včas" → coarse_on_time window
```

## 20.3 Propagation and point estimates

Lower bounds are pushed forward and upper bounds backward along the call chain, in O(n) per
update. Each event gets a feasible interval, a point estimate and a status:

```text
unknown      no evidence
bounded      interval narrowed, event not observed
tentative    derived and not yet confirmed (e.g. arrival without departure)
confirmed    source actual, or a derived event confirmed by later progress
```

The point estimate is the value preferred by the highest-ranked evidence, otherwise the interval
clipped toward schedule plus the last anchored delay.

## 20.4 Progress integrity and backtracking

**Trip progress never jumps around, and delay progression stays sane.** Two route shapes must
work:

- **A → B → A, stopping at A both times.** A is call `i` and call `k > i`; a fix or a source
  "last stop A" matches both.
- **A → B → A, stopping only the second time.** The bus passes A on the way out without a call
  there; A is only call `k` after B. Naively, passing A fires "arrived at A", jumps progress past
  B and produces an absurd early delay.

The path also overlaps itself on the out-and-back road, so one fix projects onto both the
outbound and the inbound leg.

Rules, for GPS-derived and source-reported progress alike:

1. **Calls are identified by sequence and path offset, never by stop identity or coordinates
   alone.** A source stop reference resolves to the first matching call at or after current
   progress that is reachable under rule 3. A fix near A means "at call j" only if progress is
   inside call j's offset window.
2. **Windowed forward projection.** A fix is projected only onto
   `[progress − ε, progress + max_advance]`, where `max_advance = v_max(mode) · Δt` plus
   accuracy. The earliest admissible projection wins, not the globally nearest one. Where fixes
   or the source give a bearing, heading must agree with the path direction, which separates
   outbound and inbound legs.
3. **Sequential gating.** Progress cannot pass an unvisited intermediate call without evidence of
   visiting it: a fix inside its offset window, or a time-plausible run of fixes beyond it. In the
   second scenario, passing A on the way out lies in the path before B, not in call k's window,
   so nothing fires.
4. **Delay plausibility.** An update is rejected and logged as `jump` when it implies early
   running beyond the mode's bound (for example more than 3 minutes early for a bus) or a delay
   change larger than physically possible since the last accepted state
   (`|Δdelay| > Δt + slack`). This also catches a naive source reporting the second A early.
5. **Hold, don't jump.** A rejected update leaves progress and delay unchanged and widens the
   uncertainty with time. Only consistent evidence moves the state; a gap or one bad fix never
   produces a sawtooth delay.

Contradictions that survive these rules show up as an **empty interval**, for example a
high-ranked "last stop k−2" after an accepted "last stop k":

1. Rank the conflicting evidence by policy priority for the capability, method (source actual >
   confirmed derived > progress > coarse delay > tentative), precision and recency. The weaker
   side is rejected for this state, kept in the log and flagged `regressive` or `conflict`.
2. A confirmed event is never revoked by lower-ranked evidence; one regressive report never moves
   the vehicle backwards.
3. If the same or an equal-ranked source repeats the regression N times or for longer than Δt,
   first revoke tentative and derived events beyond the regression point; if the contradiction
   remains, mark the binding `suspect` and re-run inference (next trip in the block, opposite
   direction, different date).
4. Every conflict lands in `rt.conflict` and the debug output.

## 20.5 Predictions

Project propagation runs from the latest anchored event:

```text
pred(j) = S(j) + delay_anchor − recoverable_slack(anchor → j)
```

- Slack is the dwell slack (`S_dep − S_arr − min_dwell`) plus configured running-time recovery
  per mode (default 0; learned from history later).
- The no-early-departure clamp applies; arrivals may be early only with precise signed evidence.
- Uncertainty widens with lead time and with the anchor's interval width.
- Never propagate one scalar delay unchanged through a long trip; maintain per-call predictions.
- Across trips of one vehicle, the circulation anchor (section 22) gives the knock-on start of
  the next trip.

## 20.6 Per-field arbitration

Arbitration happens per call and field (arrival, departure), not per trip. Default order, which
policy may override per source and mode:

1. actual (source event, or confirmed progress event);
2. an external travel-time ETA where one is enabled (section 21.3), or a fresh source prediction
   from a policy-preferred source (typically rail);
3. project propagation from the best anchor (own GPS-derived delay on the road, infrastructure
   events on rail);
4. propagation of the source's scalar delay;
5. the schedule.

A lower-ranked source may fill calls a better one does not cover. A final monotone repair keeps
emitted times ordered, leaving the highest-confidence values fixed.

---

# 21. Own delay from GPS and travel-time providers

## 21.1 Own delay by default where GPS is good

Most sources compute their delay the same way Oběhy can, usually worse: coarse, rounded and
stale. **Where GPS is good, Oběhy derives the delay itself and the source's scalar delay becomes
weak supporting evidence.** The main exception is rail: infrastructure events (SŽ passages at
operational points) and operator train delays stay strong. Policy encodes this per source and
mode; `rt evaluate` confirms it on replay.

## 21.2 Progress along the path

Once a vehicle is bound:

- **Path:** the trip's shape with stop offsets when available (source shape, or MOTIS shapes),
  otherwise the stop-to-stop polyline of its calls. No router runs in the realtime core.
- **Progress:** each fix is projected under the windowed, gated rules of section 20.4, giving a
  distance along the path and the gap `(i, i+1)`. Fixes far from the path, frozen or implausible
  in speed are rejected for progress; repeated rejects mark the binding `suspect`.
- **Stop events from path offsets, not proximity:** the vehicle arrives at call j when progress
  enters j's offset window and dwells while it stays there. Events are tentative until the next
  accepted fix is past the call. They are the strongest road anchors.
- **Between stops:** `t − S(position)` is the current delay, where `S(position)` interpolates the
  schedule between `S_dep(i)` and `S_arr(i+1)` along the path. It enters the timeline as a
  `gps_derived` constraint with uncertainty from fix age and accuracy.

## 21.3 Remaining time

- Progress along the path plus last-stop delay propagation (section 20.5) is the default and is
  sufficient wherever stops are close together. That covers urban and regional traffic.
- A pluggable `TravelTimeProvider` interface exists **only for long gaps between stops** (coaches,
  motorway sections). Input: the instance, current progress and target calls. Output: ETAs with
  uncertainty, entered as prediction candidates.
- A segment is eligible when the distance or scheduled running time to the next call exceeds a
  policy threshold (for example 20 km or 20 minutes) and the trip or line is enabled for the
  provider.
- **The core is built ready for providers, but none is implemented yet.** External routing or
  traffic APIs such as Mapy.com come later with forced corridors (waypoints), caching, sparse
  calls (never per GPS update), monthly request and cost limits, automatic fallback, and a
  historical benchmark. A provider is enabled only on lines where replay shows it beats
  propagation. API failure never breaks realtime output.

## 21.4 Railway estimation

Rail runs use passenger stops, operational points with scheduled passage times, SŽ passage events
and available GPS. A recent infrastructure passage anchors the state more strongly than an older
position. GPS-inferred passages of operational points (crossing the point's path offset,
interpolated between fixes) are derived events with lower rank than infrastructure events.

## 21.5 Confidence

Confidence reflects the age of the last observation, distance to the path, number of recent
observations, trajectory consistency, quality of scheduled timing points, source precision and
availability of actual events.

---

# 22. Learned vehicle circulations (oběhy)

An **oběh** is the chain of trips one vehicle works in a day. Static data rarely contains it: JDF
has no blocks, and only CZPTT and some GTFS sources supply `block_key`. Oběhy learns circulations
from history wherever a source provides a stable vehicle identifier: DÚK `vhc_id`, Arriva
licence plates, PID vehicle IDs, later cross-source vehicles (section 27) and train sets
(section 28).

- **Observed runs.** A nightly `obehy rt circulations build` reads the day's vehicle assignments.
  It uses only confirmed, non-suspect assignments — a `TripKey` match, or an inferred match later
  confirmed by continuity — so weak inferences cannot feed their own prior. Per vehicle it
  writes an ordered `vehicle_day_run`: instances with actual start and end, layovers and gaps.
- **Timetable-stable keys.** Learning uses keys that survive timetable versions (CIS line + CIS
  trip, train number, source binding key), not trip IDs, together with a day class (weekday,
  Saturday, Sunday/holiday, school holiday) from the service calendar.
- **Model.** A successor graph rather than whole-sequence clustering:
  `circulation_edge(prev_key, next_key, day_class, support, trials, last_seen, layover stats)`,
  weighted toward recent days so it adapts to timetable and roster changes. Full patterns are
  derived from high-support chains for display. Edges whose trips no longer exist in the active
  release are retired.
- **Uses:**
  1. **Matching prior.** When a bound vehicle finishes trip A, a strong learned successor B
     enters inference as a `CirculationPrior` fact and is published as a forecast assignment
     (`predicted:circulation`) before B starts. It never overrides a `TripKey` or a
     contradiction; the explanation shows the edge's support.
  2. **Knock-on delay.** `pred_start(B) = max(S_dep(B, first), A_arr(A, last) + min_layover)`,
     with `min_layover` learned per edge. This is a cross-trip anchor for section 20.5.
  3. **Vehicle forecasts.** The API shows the expected vehicle and its features (low floor, air
     conditioning) for upcoming departures, flagged as a forecast with its confidence.
  4. **Static feedback (optional, later).** High-confidence patterns may be exported as a
     reviewed candidate block file for JrUtil. They never become identity.
- **Guardrails.** Circulation evidence is a prior and never produces actual stop events.
  Thresholds are versioned policy. `rt evaluate` measures the forecast hit rate and delay error
  with and without circulation anchors before the prior is enabled for a source.

---

# 23. Capability policy and arbitration

There is no global source ranking. Trust is capability-specific, mode-specific, geographically
scoped, sequence-scoped and freshness-limited.

## 23.1 Default capability preferences

| Information | Preferred evidence |
|---|---|
| Train platform | Fresh infrastructure assignment |
| Actual rail passage | Infrastructure event at an operational point |
| Vehicle identity | Operator or IDS vehicle registry |
| Current position | Freshest spatially plausible AVL/GPS observation |
| Road delay | Own GPS progress where GPS is good; else the best source delay |
| Rail delay | Infrastructure events and operator delays, then GPS |
| Per-stop ETA | Validated source prediction, own propagation, or an enabled travel-time provider |
| Coarse scalar delay | Fallback |
| Alerts | Preserve and deduplicate; no universal winner |
| Composition | Most authoritative permitted source |

## 23.2 Policy configuration

```toml
[[policy]]
source = "sz"
mode = "rail"
capability = "platform"
scope = "nationwide"
priority = 100
max_age_seconds = 300

[[policy]]
source = "duk"
mode = "bus"
capability = "delay"
scope = "duk"
priority = 40              # below own GPS-derived delay
max_age_seconds = 90

[[policy]]
source = "arriva-express"
capability = "inference"
score_floor = 0.0          # tuned on replay
margin = 2.0
```

Priorities and thresholds are versioned and set from `rt evaluate` results, not guessed.

## 23.3 Eligibility gates

Before comparison, reject or downgrade evidence that fails: a binding of sufficient confidence,
plausible timestamp, freshness, source coverage, plausible speed and movement, proximity to the
expected path, consistent operating date, consistent sequence progression (section 20.4) and
acceptable source health.

## 23.4 Conflicting positions

Never average contradictory positions. Prefer the claim that best satisfies freshness, policy,
path plausibility, trajectory continuity, source accuracy and sequence scope. Record the conflict;
the losing observation stays stored.

## 23.5 Selected-state provenance

Every selected value exposes internally: value, interval, source and channel, source and received
timestamps, method, selection reason, confidence, and the competing evidence.

---

# 24. Dynamic posts and train platforms

Platform and post assignments are realtime evidence:

```text
platform_evidence
    instance, call sequence, raw platform/track value, boarding_point_id (if mapped),
    source, assigned_at, valid_until, confidence
```

Arbitration order: fresh infrastructure assignment > fresh operator or IDS assignment > previous
still-valid assignment > scheduled static boarding point > unspecified boarding point.

All known posts and platforms should exist in the static build, even if few scheduled calls use
them. An unmappable realtime post or platform keeps its raw value, is exposed in the API and
debug output, never becomes an invented GTFS stop ID, and becomes a review item for the next
static build. Where the assigned boarding point exists in static GTFS, publish it against the
correct stop sequence and keep the stop-place relationship intact.

---

# 25. Alerts

Alerts are independent evidence mapped to precise scope:

```text
alert_scope
    trip_id or run, route_id, from_sequence, to_sequence, stop place or boarding point,
    geographical scope
```

- Whole-trip incident: the whole trip or run.
- Stop-specific incident: the stop place or boarding point.
- Segment-only incident: the affected sequence range. Do not expose a PID-only segment disruption
  as a nationwide route disruption.

Deduplicate by normalized text, active period, scope, cause, effect and source references. Never
merge alerts only because they mention the same line. Where GTFS-RT cannot represent the exact
scope without becoming misleading, publish the closest safe selector, keep the precise scope in
the project API and expose provenance. Alerts are emitted per feed, with entities of that feed
only.

---

# 26. Realtime sources

Order of implementation: **DÚK and SŽ first, then PID, then Arriva Express and further long-tail
sources.** Every source starts with a dossier and recorded payloads (section 18.2). Details below
are expectations to be confirmed by the dossier.

## 26.1 DÚK

Dossier: `docs/sources/duk.md` (draft from a sample payload, checked against a release). The
vehicle list gives per vehicle (3–4 digit DÚK `ID`s and `40`-prefixed Teplice city buses; `20xxx`
train entries are dropped):

- the CIS line (as an integer without leading zeros, zero-padded by the connector) and the CIS
  trip number → `TripKey(cis_line_id, cis_trip_id)`. In the sample, 148 of 149 vehicles resolve
  to exactly one `jdf` line and trip; the miss is a line absent from the CIS export and falls back
  to `LineRef` inference;
- the public line, a predicted delay in whole minutes, the actual arrival at the last stop and
  its timetabled departure (which identifies the call without stop mapping), and a vehicle
  state: off, running, at a stop, waiting before the trip, or running to the trip's first stop.
  The two pre-trip states give forecast assignments only and never advance the trip;
- GPS position, bearing and fix time, last activity time, low-floor flag.

This is a timed source with explicit arrival evidence. Its delay is rounded, never negative and
mixes arrival- and departure-based values, so it jumps by the dwell slack: it is weak evidence
spanning both events, and own GPS-derived delay is DÚK's delay source. DÚK stop node/post IDs
are not used: trips match by key and progress comes from GPS. Vehicle `ID`s are the fleet
numbers printed on the buses, stable across trips and days, so DÚK history seeds the
circulation model. DÚK trains are not used; rail realtime comes from SŽ.

## 26.2 SŽ

Dossier: `docs/sources/sz.md` (one sample payload checked against a release, plus the upstream
JrUtil scraper `SzMapa.fs`). The SŽ train map (`mapy.spravazeleznic.cz`, layer `OsVlaky`)
returns all trains as GeoJSON with a response timestamp. Per train:

- an ID `TR/<company>/<core>/<variant>/<year>/<date>`: the CZPTT TR identity and the operating
  date → `TripKey(czptt_tr_id)` + `OperatingDate`. With binding and trip calendars applied,
  468 of 469 sample trains resolve to exactly one CZPTT timetable; the remaining one is
  quarantined unless position or next-stop facts separate its candidates;
- position in S-JTSK/Křovák (EPSG:5514, transformed to WGS84) and bearing;
- train category, number and name, origin and destination;
- the last point by name, with timetabled and actual `HH:mm` times and a "standing there" flag →
  actual arrival, departure or passage at passenger and operational points;
- signed current delay in minutes, a predicted delay, the next point (SR70 with check digit) and
  the next passenger stop (5-digit SR70) with timetabled and predicted times;
- operator, rail-replacement and diversion flags.

Times carry no date and are recovered from the response timestamp. Last-point names map to SR70
through the SR70 catalogue, then to the run's calls at or after current progress. SŽ anchors rail
runs strongly but does not override a fresher, spatially more precise GPS position. Platforms
come from station departure boards, a separate channel still to be investigated.

## 26.3 PID

Golemio APIs for vehicle positions, trip progress and departures (`TripKey` via `gtfs_trip_id`
keys in `source_key` on the overlaid parts, train numbers for trains), and PID GTFS-RT
**for alerts only**. PID protobuf is never proxied unchanged; every ID is rewritten to the active
release, and alerts are mapped to entities per feed. PID trains apply to the complete CZPTT run
(sections 13 and 19.4). Capabilities are handled independently: positions, progress, predictions, alerts,
occupancy and vehicle identity.

## 26.4 Arriva Express and long-tail sources

Dossier: `docs/sources/arriva-express.md`. Arriva's fleet-wide location feed is filtered to
Arriva Express only. Per vehicle it gives the CIS line, destination and last stop names, an
at-stop flag, a signed delay in minutes, GPS with bearing, the licence plate and a report time
(apparently local time mislabelled as UTC); no trip number, next stop or date. It is the
reference long-tail source: inferred matching by line, destination, last stop, time and GPS
(section 19.2; both express vehicles in the sample resolve to exactly one trip), continuity by
plate, own GPS delay (section 21), and later a travel-time provider on long motorway segments. DPKV-style sources (line, trip, delay, untimed GPS) use the exact `TripKey` path with
untimed-GPS handling (section 19.6). Each further source is a connector, a manifest, a dossier and
fixtures.

---

# 27. Vehicle registry

A source vehicle ID binds to a vehicle:

```text
vehicle                    vehicle_id, operator_id, fleet_number, public_label
vehicle_source_binding     source_id, source_vehicle_id, vehicle_id, valid_from, valid_to
vehicle_attribute          vehicle_id, attribute, value, source_id, valid_from, valid_to
```

A fleet dataset can supply model, manufacturing year, low-floor status, air conditioning, USB,
Wi-Fi and other features. Keep provenance per attribute because sources disagree. The vehicle API
indicates the source of current position, current trip, public label, model and features.

---

# 28. Train compositions

Compositions attach to `train number + operating date`:

```text
train_composition           train_number, operating_date, observed_at, source_id
train_composition_vehicle   sequence, vehicle_number, vehicle_type, passenger_label, features
```

Order: the ČD source first; 55p.cz only after explicit permission covering retrieval, storage,
display, redistribution and caching duration. The project API is the rich source of truth;
GTFS-RT carriage details are populated only where suitable.

---

# 29. History

Keep distinct: scheduled event, source prediction, project prediction, source-reported actual
event, GPS-inferred actual event.

```text
actual_stop_event
    instance, call_sequence, event_type (arrival | departure | passage),
    event_time, interval, method (source | progress | interpolated_crossing),
    confidence, source_ids
```

Retention on one machine:

- partition high-volume tables by date;
- keep high-resolution observations for a limited period;
- keep derived actual events, vehicle day runs and the circulation model long term;
- downsample or export old trajectories to Parquet;
- keep raw payloads only for each source's debugging and licensing period.

---

# 30. Project API and map

The frontend consumes only project-owned contracts. It never knows whether a vehicle came from
PID, DÚK, SŽ or another provider.

```text
/gtfs/jdf.zip, /gtfs/czptt.zip, /gtfs/manifest.json, /gtfs/versions/<run-id>/…
/gtfs-rt/{jdf,czptt}/{vehicle-positions,trip-updates,alerts}.pb

/api/stops?bbox=…
/api/stop-places/<id>/departures         both feeds; scheduled + realtime + forecasts
/api/trips/<id>?date=…                   calls, typed features, notes, realtime state
/api/vehicles?bbox=…                     includes flagged unmatched vehicles
/api/vehicles/<id>
/api/alerts

/api/debug/realtime/instances/<instance> evidence, intervals, inference explanation
/api/debug/realtime/sources              health per source and channel
/api/debug/realtime/unmatched            unmatched and ambiguous observations with candidates
```

Initial frontend: nationwide stops and routes, current vehicles, scheduled and realtime
departures, vehicle details, alerts, stale-data state, and a source/confidence indicator in debug
views. Later: dynamic platforms and posts, circulations and vehicle forecasts, historical replay,
compositions, disagreement diagnostics, nearby grouped departures, quality metrics.

Use viewport-based queries. Polling every few seconds is acceptable; WebSockets are not an early
milestone.

---

# 31. Observability

## Technical metrics

Source download success, parse time, connector response time, source age, core-loop lag, load
duration, active instance count, database write latency, API latency, GTFS-RT generation time.

## Data-quality metrics

Inference outcome rates per source (exact, inferred, ambiguous, unmatched), date-ambiguity rate,
rejected jumps and regressions, suspect bindings, source conflicts, position rejection rate,
frozen positions, stale evidence, platform mapping failures, delay disagreement (source versus
own), prediction error by lead time, circulation forecast hit rate, feed entity counts, alert
mapping failures.

## Source health

```text
healthy | degraded | stale | invalid | disabled | using_last_known_good
```

## Realtime instance state

```text
fresh | stale | unmatched | ambiguous | suspect | suppressed_by_better_source
```

---

# 32. Testing strategy

## Unit tests

- fact scorers and the inference decision rule;
- operating-date inference (past midnight, `24:xx+`, waiting before midnight, DST, rollover);
- delay semantics → intervals;
- interval propagation and the timeline engine;
- progress gating: A → B → A both variants, out-and-back roads, naive source reports, loops;
- rail source handover: a source entering mid-run, leaving at its coverage edge, and silence
  inside its coverage;
- arbitration and monotone repair;
- circulation edge learning.

Scenario tables with small synthetic timetables are the main form.

## Static tests

JrUtil golden conversion tests (JDF, CZPTT, operational points, posts, zones, overlays). Oběhy:
release verification, contract-to-DDL generation, load/activate/rollback on tiny fixture
packages, failed-load cleanup.

## Replay tests

Recorded payloads replayed deterministically against a fixed release, for connector parsing,
inference, arbitration, alert mapping, arrival inference and output stability. Replay output is
golden-tested.

## Invariants

```text
One observation resolves to at most one instance.
One instance emits at most one vehicle position per feed.
Every exported realtime trip ID exists in the active release of its feed.
Every assigned platform ID exists in the active release.
Every passenger stop_time references a passenger location.
Operational points never leak into passenger stop_times or TripUpdates.
Progress of an instance is monotonic in emitted output.
```

Real-data checks run on bounded subsets; full national builds are not a test tool.

---

# 33. Roadmap

## Done or obsolete

- National JDF and CZPTT compilation, PID + IDS JMK overlays, typed serving package (bundle v3,
  serving schema v4), `obehy build` with atomic release publication.
- The provisional `v0` identity phase and the identity-registry service are dropped; identity is
  section 6.

## Milestone S — Static release acceptance

One complete live release built by the GitHub Actions pipeline, published, and accepted (package
validation, GTFS validator, MOTIS import sanity check). This is the single permitted full run.

## Milestone C1 — Mirror and API foundation

PostgreSQL schemas, generated static DDL, `obehy release fetch|load|activate`, derived helpers,
rollback; static stops, departures and trips in the API; per-feed GTFS downloads.

Exit: fixture packages load, activate and roll back; a failed load leaves nothing attached; the
real CZPTT package loads in a bounded check.

## Milestone R1 — Realtime core

Recorder, raw archive, replay and evaluate; observation model and core loop; inference engine
with exact and inferred paths and date inference; rail runs; timeline engine with progress
integrity; arbitration; per-feed GTFS-RT emitters; debug endpoints.

Exit: all scenario tables pass; replay is byte-deterministic.

## Milestone R2 — DÚK and SŽ

DÚK buses and trains, SŽ positions, operational-point events and platforms; one train fused from
DÚK and SŽ with provenance.

Exit: replays of recorded days pass the GTFS-RT validator against the matching static feed;
every emitted ID exists; inference rates and accuracy per source are reported.

## Milestone R3 — PID

Golemio APIs and GTFS-RT alerts; PID trains on CZPTT runs.

Exit: PID and DÚK coexist without duplicate positions per instance; alerts keep their scope.

## Milestone R4 — Long tail and own delay

Arriva Express with inferred matching and own GPS delay; the travel-time provider interface with
segment eligibility (no provider yet); DPKV-style untimed sources.

Exit: inference and ambiguity rates are reported; own delay versus source delay is measured per
source, and policy prefers own delay only where it wins.

## Milestone R5 — Circulations

Nightly circulation build, successor graph, forecast assignments, knock-on delays, expected
vehicles in the API.

Exit: on a held-out week, forecast hit rate is reported and next-trip delay error improves over
propagation without circulations.

## Later milestones

- MOTIS route-shapes companion as a static build stage (gives section 21 real paths through the
  release);
- history archive, punctuality and trip replay;
- vehicle registry and attributes;
- train compositions;
- external travel-time providers for whitelisted coach lines;
- public frontend and managed MOTIS (blue-green loading, health checks, activation and rollback
  always naming the same release as GTFS, mappings and realtime);
- additional regions and providers. Each new source requires configuration, licence metadata, a
  dossier, a connector manifest, fixtures and a conformance report (download, parsing, licence,
  inference rate, freshness, duplicates, vehicle identity stability, static/realtime
  compatibility).

---

# 34. Next implementation tickets

1. `obehy rt record` and source dossiers for DÚK, SŽ and Arriva Express; record several days.
2. Serving schema 5 in JrUtil: writer, validation, GTFS projection; Oběhy accepts only v5.
3. Database foundation: compose file, generated static DDL, `control` schema, migration runner.
4. `obehy release fetch|load|activate --rollback` with derived helpers.
5. Realtime skeleton: model, clock, archive, core loop, `rt` migrations, replay.
6. Inference engine: facts, scorers, decision rule, explanations, date inference, vehicle
   binding, rail runs.
7. Timeline engine: intervals, delay semantics, propagation, progress integrity, conflicts,
   predictions, arbitration, monotone repair.
8. DÚK connector and per-feed GTFS-RT emitters with debug endpoints.
9. SŽ connectors and rail fusion.
10. Project API with realtime departures and vehicles.
11. PID: Golemio APIs and GTFS-RT alerts.
12. Arriva Express: inferred matching, own GPS delay, travel-time provider interface.
13. Circulation learning v1.

The first end-to-end success is:

```text
one DÚK bus and one DÚK/SŽ train
 -> resolved against the active release of their feeds
 -> per-call actuals and predictions with explained evidence
 -> emitted in valid per-feed GTFS-RT and the project API
```

After that, adding regions and providers is controlled repetition rather than architecture
discovery.

---

# 35. Example realtime debugging output

```json
{
  "instance": "czptt:run:6608/2026-10-05",
  "inference": {
    "method": "trip_key",
    "facts": [
      {"fact": "TripKey", "namespace": "train_number", "value": "6608", "effect": "candidates=1"},
      {"fact": "OperatingDate", "inferred": "2026-10-05", "rejected": ["2026-10-04"]}
    ]
  },
  "selected": {
    "position": {
      "source": "duk", "observed_at": "2026-10-05T17:31:04+02:00",
      "reason": "freshest eligible path-consistent position", "confidence": 0.96
    },
    "call:Ústí nad Labem hl.n.:departure": {
      "interval": ["17:42:00", "17:42:59"], "estimate": "17:42:30",
      "status": "bounded", "method": "propagation from SŽ passage at Ústí n.L.-Střekov",
      "confidence": 0.88
    },
    "platform": {
      "source": "sz", "boarding_point_id": "czptt:stop:CZ:<SR70>:platform:3",
      "reason": "fresh infrastructure assignment", "confidence": 0.99
    }
  },
  "suppressed": [
    {"source": "duk", "capability": "delay", "reason": "coarse non-negative whole-minute value"},
    {"source": "duk", "capability": "trip_progress", "reason": "regressive: last stop behind accepted progress"}
  ]
}
```

---

# 36. Decisions intentionally deferred

- WebSockets versus polling;
- scaling beyond one realtime process;
- the permanent high-resolution position retention period;
- exact source priorities and inference thresholds before replay benchmarking;
- whether operational sidecars are publicly downloadable;
- whether compositions are projected into experimental GTFS-RT fields;
- which external travel-time provider, if any, is worth its cost;
- the release transport beyond GitHub Releases if size or retention requires it.

These are decided when the relevant milestone produces real evidence.

---

# 37. Definition of project success

- national source ID churn does not break public stop, route or rail IDs;
- regional static feeds improve national data without duplicating or truncating journeys;
- exact posts and platforms are preserved where known;
- realtime from several providers coexists without silent corruption;
- poor sources (no trip IDs, no dates, untimed GPS) still produce correct, explained matches or
  are visibly quarantined;
- trip progress never jumps, and delays never sawtooth on loops or out-and-back routes;
- own GPS-derived delays replace coarse provider delays wherever they measurably win;
- non-passenger railway points improve predictions without appearing as public stops;
- learned circulations forecast vehicles and knock-on delays measurably;
- alerts retain correct geographic and sequence scope;
- the frontend remains backend-source agnostic;
- historical events can be replayed and explained;
- adding another provider is mostly a connector, manifest, dossier and fixture task;
- the system stays operable on one community-hosted application server, with builds on GitHub
  Actions.

Build in usable vertical slices: prove the mirror, then one exact-key source end to end, then rail
fusion, then the long tail, then circulations. Then expand coverage.
