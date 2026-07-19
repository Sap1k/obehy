# Oběhy — Czech Nationwide Public-Transport Data Platform

## Authoritative implementation plan

**Working goal:** build **Oběhy**, a nationwide Czech public-transport data platform that:

- publishes one stable nationwide GTFS Schedule feed;
- overlays higher-quality regional and operator data onto national JDF and CZPTT conversions;
- publishes a fused GTFS-Realtime feed;
- powers a public vehicle and departures map;
- preserves Czech-specific identifiers and metadata where useful;
- supports dynamic platforms/posts, alerts, vehicle details, train compositions and historical arrival/departure data;
- can initially run as a community project on one machine.

The system should be designed so that additional regions and providers can be added incrementally without rewriting the frontend or the core matching logic.

---

# 1. Core architectural decision

The canonical database is the source of truth.

GTFS Schedule, GTFS-Realtime, the map API, debugging endpoints and historical exports are all projections of the canonical model.

Source data must never directly define permanent public identity.

```text
STATIC SOURCES

National JDF ───────┐
National CZPTT ─────┼─> source adapters / JrUtil
Regional GTFS ──────┤
Operator GTFS ──────┘
                           |
                           v
                 canonical transit model
                           |
                 overlay and compilation
                           |
          +----------------+----------------+
          |                                 |
          v                                 v
      GTFS.zip                    source-ID mappings


REALTIME SOURCES

PID GTFS-RT ────────┐
DÚK custom API ─────┤
SŽ / rail APIs ─────┤
other IDS APIs ─────┘
                           |
                           v
                normalized realtime claims
                           |
                  validation and matching
                           |
               source arbitration / fusion
                           |
          +----------------+----------------+
          |                |                |
          v                v                v
      GTFS-RT          project API       history
```

This should initially be a **modular monolith**, not a distributed microservice system.

Use separate processes where operationally useful, but keep one repository, one canonical schema and one shared set of domain models.

Suggested runtime processes:

```text
static-compiler
realtime-worker
estimator-worker
api
web
```

---

# 2. Project scope

## Initial public proof of concept

The first meaningful release should contain:

- nationwide static data from JDF and CZPTT;
- a canonical stop, route and trip registry owned by the project;
- PID static data overlaid where it is better than the national conversion;
- PID GTFS-RT rewritten against the canonical feed;
- DÚK realtime matched against national static data;
- PID alerts preserved;
- at least one train correctly fused from multiple sources;
- at least one GPS-derived delay estimate;
- a basic map showing the resulting vehicles and departures.

## Later capabilities

After the proof of concept:

- additional regional static feeds;
- additional regional and operator realtime connectors;
- dynamic bus posts and train platforms;
- SŽ or other railway infrastructure observations;
- inferred arrivals, departures and pass-through events;
- vehicle registries and features;
- ČD train compositions;
- 55p.cz train compositions after explicit permission;
- optional paid Mapy.com routing for selected long-distance coach services;
- historical trip replay and punctuality data.

---

# 3. Non-negotiable design rules

1. Never treat source IDs as permanent canonical IDs.
2. Never recycle canonical IDs.
3. Never replace an entire trip with a regional trip that only covers part of it.
4. Overlay fields and journey segments, not ZIP files as opaque units.
5. Keep every realtime observation and claim with provenance, even when it loses arbitration.
6. Never interpret a coarse zero-minute delay as proof that a vehicle is exactly on time.
7. Never average contradictory vehicle positions blindly.
8. Never expose railway pass-through points as passenger stops.
9. Never publish a realtime platform/post assignment that cannot be mapped to a canonical boarding point.
10. Never broaden a regional alert beyond the area or journey segment it actually affects.
11. Activate a new static feed and its realtime ID mappings atomically.
12. Quarantine ambiguous matches instead of guessing.
13. Prefer a degraded but valid feed over publishing corrupted data.
14. Every build must be reproducible from stored input snapshots and configuration.
15. Every selected realtime value must be explainable by its source, timestamp and confidence.

---

# 4. Recommended implementation stack

## Backend and data processing

- **Python**
  - source downloading;
  - feed compilation;
  - ETL;
  - realtime connectors;
  - source arbitration;
  - estimators;
  - API.
- **PostgreSQL + PostGIS**
  - canonical registry;
  - source bindings;
  - geospatial matching;
  - current realtime state;
  - historical events;
  - configuration-backed mappings.
- **Polars and/or DuckDB**
  - bulk CSV and Parquet transformations;
  - diagnostics;
  - match reports;
  - large GTFS table processing.
- **FastAPI**
  - project API;
  - feed endpoints;
  - debugging endpoints.
- **Protocol Buffers**
  - GTFS-Realtime decode and encode.
- **Parquet**
  - immutable conversion sidecars;
  - cold historical observation storage.

## Frontend

- **React**
- **MapLibre GL JS**

## Deployment

Initially use:

- Docker Compose or systemd;
- one PostgreSQL instance;
- local filesystem/object-style directories for raw snapshots and immutable feed versions;
- reverse proxy for public endpoints.

Do not introduce Kafka, Kubernetes, Celery, Redis Streams or a dedicated stream-processing database until actual load proves that PostgreSQL and normal workers are insufficient.

---

# 5. Repository structure

```text
obehy/
├── apps/
│   ├── compiler/
│   ├── realtime/
│   ├── estimator/
│   ├── api/
│   └── web/
├── packages/
│   ├── canonical_model/
│   ├── identity/
│   ├── gtfs_io/
│   ├── realtime_model/
│   ├── source_registry/
│   ├── matching/
│   └── diagnostics/
├── connectors/
│   ├── pid_gtfsrt/
│   ├── duk/
│   ├── sz/
│   ├── cd_compositions/
│   └── fiftyfivep/
├── converters/
│   ├── jrutil/
│   └── pfaedle/
├── config/
│   ├── sources/
│   ├── aliases/
│   ├── precedence/
│   ├── overlays/
│   └── estimators/
├── tests/
│   ├── fixtures/
│   ├── golden/
│   ├── integration/
│   └── replay/
├── data/
│   ├── raw/
│   ├── converted/
│   ├── builds/
│   └── archive/
└── infra/
    └── compose.yaml
```

JrUtil and pfaedle can remain external repositories or Git submodules pinned to known commits. Their patches should be kept independently reviewable.

---

# 6. Canonical identity strategy

## 6.1 Own the canonical numbering

Czech national non-rail exports do not provide immutable stop IDs. IDs may change between exports, and some feeds do not provide CIS StopIDs or PostIDs at all.

The project therefore needs its own permanent numbering registry.

Example prefixes:

```text
S000000123    stop place or station
P000000456    passenger boarding point, post or platform
O000000789    operational or timing point
R000000123    canonical route
T000000456    scheduled trip
V000000789    vehicle
A000000123    canonical alert, if persistent alert identity is required
```

The exact format is not important. Required properties:

- opaque;
- project-owned;
- stable;
- never recycled;
- not derived from mutable source IDs;
- redirects supported after merges;
- tombstones retained after deletion.

Generated IDs should have an unmistakable project prefix if they are exposed outside the database.

## 6.2 Source bindings

Every source identifier is a time-bounded binding to a canonical entity.

```text
source_binding
    source_id
    entity_type
    source_object_id
    canonical_entity_id
    valid_from
    valid_to
    match_method
    match_confidence
    created_at
    reviewed_by
```

Examples:

```text
PID stop U123Z4                -> P000014842
CIS StopID 12345               -> S000003012
JDF stop 98142 in export A     -> S000003012
JDF stop 41287 in export B     -> S000003012
PID trip 775_80_251220         -> T000000456
```

## 6.3 Explicit identifier aliases

Aliases normalize an identifier that a source claims is from a known external namespace but
encodes differently. They are not a way to invent a missing CISLineID from a public line number,
route name or arbitrary GTFS ID.

Manual aliases must be supported because some source systems transform identifiers. The initial
use case is a realtime API that is not keyed by the static GTFS identifiers:

Example:

```yaml
aliases:
  - source: duk
    entity: cis_line
    observed_id: "582588"
    canonical_value: "001588"
    valid_from: "2026-01-01"
    valid_to: null
    reason: "DÚK realtime API-specific encoding"
```

Aliases should be applied before canonical matching.

They should support validity ranges because upstream conventions may change.

## 6.4 Keep identity claims, aliases and source bindings separate

These mechanisms solve different problems:

1. An **identity claim** is an explicit source assertion such as
   `route_licence_number=260775`, `cis_stop_id=50619` or a trip ID documented to contain a
   CISTripID. Preserve its field-level provenance and validate its syntax and consistency against
   the applicable national snapshot.
2. An **identifier alias** deterministically rewrites one asserted external identifier into the
   same external namespace, such as a DÚK realtime API-specific line encoding into a CISLineID.
   Alias rules are explicit, versioned and validity-bounded. They never use fuzzy matching.
3. A **source binding** links an arbitrary source-local object such as a PID or DPMLJ GTFS trip to
   a canonical entity after exact identifiers, calendars and structural evidence have been
   considered. This includes a provider-supplied, snapshot-scoped crosswalk between that
   provider's realtime/operational key and its own static GTFS `trip_id`. A binding may exist even
   when the source never exposes a CISLineID.

Do not copy a guessed CISLineID into normalized source data merely to make downstream matching
look uniform. Store the original fact, the matching evidence and the resulting canonical binding
separately.

For realtime tied to a known static feed, prefer the static binding chain:

```text
PID GTFS-RT trip_id
 -> PID static GTFS trip_id
 -> active source trip binding
 -> canonical trip instance
```

No CISLineID remapping is needed in that path. CIS aliases are mainly for sources such as custom
realtime APIs that emit CIS-like identifiers but do not reference an imported static timetable.
They may also be used by a static adapter when that source explicitly publishes a transformed CIS
identifier, but not when the identifier is absent.

Treat a provider-supplied operational-to-static crosswalk as a candidate relation, not necessarily
as a unique dictionary. IDS JMK `api.txt`, for example, maps `(source line code, source
course/train number)` to its numeric GTFS `trip_id`; the same operational key can map to multiple
static rows for different calendars or timetable variants. Resolve it using the active snapshot,
operating date and, when still necessary, scheduled time or call context. If two active candidates
remain plausible, quarantine the realtime claim rather than selecting the first row.

## 6.5 Canonical redirects

If two canonical entities are later proven to be the same:

```text
S000004321 -> S000003012
```

The losing ID becomes a redirect.

Historical records remain unchanged and resolvable.

Do not bulk-renumber old history unless absolutely necessary.

---

# 7. Stop and location model

A single generic “stop” entity is insufficient.

The system should distinguish at least three classes.

## 7.1 Stop place

A rider-facing geographic place or station:

```text
Praha, hlavní nádraží
Teplice, Benešovo náměstí
Ústí nad Labem, hlavní nádraží
```

Used for:

- search;
- map labels;
- nearby-departure grouping;
- interchange grouping;
- parent-station relationships;
- accessibility and place-level metadata.

## 7.2 Boarding point

A concrete place where passengers board or alight:

```text
platform 3
track 2
post B
direction-specific bus pole
unspecified boarding point
```

A stop place may have many boarding points.

Each stop place should have an **unspecified boarding point** fallback where the timetable contains the place but no exact post/platform.

Example:

```text
S000003012  Ústí nad Labem, hl.n.
P000008921  unspecified boarding point
P000008922  track 1
P000008923  track 2
```

Static trips without a known platform use the unspecified child.

Realtime can reassign a call to a known child platform.

## 7.3 Operational point

A location used for vehicle progress and timing, but not shown as a passenger stop:

- a railway station passed without stopping;
- a junction;
- a block or timing point;
- a non-passenger CZPTT location;
- potentially a bus timing checkpoint.

Operational points must remain outside public passenger `stop_times.txt`.

They should exist in the canonical model and internal sidecar data.

## 7.4 Stop-place grouping for the frontend

The frontend concept previously called an “uzel” should become a canonical or derived stop-place grouping, not a source-feed grouping.

Suggested model:

```text
stop_place
    id
    name
    centroid
    grouping_method

stop_place_member
    stop_place_id
    boarding_point_id
    walking_distance
    confidence
```

Grouping evidence should be considered in this order:

1. explicit parent-station relationship;
2. shared authoritative Czech place identifier;
3. manual mapping;
4. compatible normalized name and close geography;
5. topology and interchange evidence;
6. manual review.

Proximity alone must not automatically merge locations.

Nearby opposite-direction platforms, rail and bus facilities, grade-separated stops and similarly named locations can be only metres apart while remaining distinct.

---

# 8. Reconciling mutable national stops

Every new national export needs continuity matching against the canonical registry.

## Matching order

1. Existing manual binding
2. Stable identifier such as CIS StopID, PostID or ASW ID
3. Existing source continuity that is still trustworthy
4. Strong structural match against the previous export
5. Review candidate
6. Allocate a new canonical ID

## Structural matching signals

Use a weighted combination of:

- normalized stop name;
- municipality;
- district or local part;
- coordinates;
- routes serving the location;
- neighbouring stops in trip sequences;
- trip-pattern topology;
- directionality;
- mode;
- historical source-object lineage;
- known boarding points;
- distance from prior coordinates.

Do not use only name and coordinates.

## Suggested confidence policy

```text
1.00    explicit manual mapping
0.99    stable authoritative identifier
0.95    strong structural continuation
0.85    probable continuation requiring review
<0.85   create a new entity or quarantine
```

Actual thresholds should be tuned using real exports.

## Build diagnostics

Each import should produce:

```text
Stops in previous export
Stops in new export
Exact stable-ID matches
Structural continuation matches
Manual matches
New canonical stops
Possible duplicates
Ambiguous matches
Retired source objects
Large coordinate shifts
```

A sudden increase in newly allocated stops should block automatic activation.

---

# 9. Route and trip identity

## 9.1 Road, tram and urban transit

The scheduled-trip identity anchor is:

```text
normalized CISLineID + CISTripID
```

The concrete operating instance is:

```text
CISLineID + CISTripID + operating date
```

These are identities of the national/canonical timetable, not mandatory fields in every regional
overlay. A regional GTFS trip may reach this identity through a source binding without ever
exposing either CIS identifier itself.

Match regional data at the operating-instance level first whenever calendars overlap:

```text
source trip + source service date
 -> national candidates active on that date
 -> route/operator/mode constraints
 -> ordered stop and time comparison
 -> canonical trip instance
```

Only collapse those results into one scheduled-trip binding when the same unique relationship is
valid across the relevant dates. If one regional GTFS trip represents multiple national
CISTripIDs on disjoint service dates, retain date-scoped instance bindings instead of guessing one
scheduled identity.

## 9.2 Rail

The scheduled-trip anchor is the train number.

The concrete operating instance is:

```text
train number + operating date
```

If the static source contains multiple timetable variants for one train number, the correct static variant should be resolved from:

- service calendar;
- call sequence;
- direction;
- validity period;
- source schedule metadata.

The train number remains the primary realtime anchor.

## 9.3 Canonical entities

```text
scheduled_trip
    canonical_trip_id
    mode
    canonical_route_id
    road_cis_line_id
    road_cis_trip_id
    train_number
    timetable_variant
    validity_range
```

```text
trip_instance
    canonical_trip_id
    operating_date
```

GTFS trip IDs should be stable projections of the canonical trip registry, not raw JrUtil or regional IDs.

---

# 10. Canonical trip calls

The internal model should use one ordered call sequence containing both passenger and operational locations.

```text
trip_call
    canonical_trip_id
    sequence
    location_id
    passenger_service
    scheduled_arrival
    scheduled_departure
    scheduled_passage
    scheduled_boarding_point_id
    pickup_allowed
    dropoff_allowed
```

Example:

```text
10  Praha hl.n.     passenger=true
20  Praha-Libeň     passenger=false
30  Český Brod      passenger=false
40  Kolín           passenger=true
```

The GTFS exporter publishes only passenger calls.

The realtime estimator uses the full call sequence.

This allows non-stop railway points to anchor delay calculations without exposing them to riders as stops.

---

# 11. JrUtil workstream

JrUtil should produce a **conversion bundle**, not only a ready-made final GTFS ZIP.

Suggested output:

```text
conversion/
├── gtfs-intermediate/
│   ├── agency.txt
│   ├── routes.txt
│   ├── trips.txt
│   ├── stops.txt
│   └── stop_times.txt
├── extensions/
│   ├── cz_routes.txt
│   ├── cz_trips.txt
│   ├── cz_stops.txt
│   └── cz_stop_zones.txt
├── source_route_metadata.parquet
├── source_stop_metadata.parquet
├── source_call_metadata.parquet
├── source_route_stop_zone_metadata.parquet
├── source_notice_metadata.parquet
├── source_transfer_metadata.parquet
├── source_travel_restriction_metadata.parquet
├── diagnostics.json
└── manifest.json
```

Bundle format version 1 is implemented for JDF in the standalone JrUtil fork.
It uses explicit flat Parquet schemas, Snappy compression, fixed row groups,
deterministic ordering and per-file SHA-256 metadata. Operational point/call
sidecars remain part of the later CZPTT slice rather than empty JDF files.

Standard GTFS plus the Oběhy extension tables are the primary normalized
representation. Parquet must not repeat fields that can be reconstructed from
those tables. Bundle v1 retains seven narrow enrichment relations:

```text
source_route_metadata
    gtfs_route_id, source_route_id, route_distinction,
    source_agency_id, source_agency_distinction, valid_from, valid_to

source_stop_metadata
    gtfs_stop_id, town, district, nearby_place, country,
    coordinates_missing

source_call_metadata
    gtfs_trip_id, stop_sequence, source_route_stop_id

source_route_stop_zone_metadata
    gtfs_route_id, source_route_stop_id, zone_id, zone_order

source_notice_metadata
    source_notice_id, notice_kind, gtfs_route_id?, gtfs_trip_id?,
    label?, text?, valid_from?, valid_to?, service_note_type?

source_transfer_metadata
    source_transfer_id, gtfs_trip_id, source_route_stop_id,
    source target identifiers, wait_minutes?, note?

source_travel_restriction_metadata
    assignment_scope, gtfs_route_id?, gtfs_trip_id?,
    source_route_stop_id, group_code
```

The call `stop_sequence` is the exact GTFS join key, not a separately numbered
sequence. Snapshot/source identity belongs in the manifest and Parquet file
metadata rather than on every row. Route/trip/stop/post identities, names,
coordinates, modes, public numbers, times, distances, pickup/drop-off rules and
zone catalogs remain solely in GTFS/extensions unless a future source exposes a
genuinely non-projectable value.

JDF zones are normalized as route-distinction-scoped source identities because
the raw token does not identify its IDS owner. Stop memberships are stored as
rows in `cz_stop_zones.txt`; `source_route_stop_zone_metadata.parquet` adds only
their route-stop scope and token order. `source_call_metadata.parquet` supplies
the join from emitted GTFS calls to those route stops, so zone membership is
not duplicated once per trip. Standard GTFS `stops.zone_id` is
populated only for a single source-zone identity and remains blank for plural
membership; `stop_times.txt` contains no custom zone column.

JDF `Udaje`, text-bearing or otherwise unhandled `Caskody`, `Mistenky` and
`Navaznosti` are retained as typed source notices or connection claims.
Calendar-only `Caskody` are omitted because their complete effect is already
represented by GTFS calendars. The `§`/`A`/`B`/`C` travel-exclusion codes retain
their original `Zaslinky` route-stop or `Zasspoje` trip-call scope rather than
being expanded over every trip. Bundle Parquet is an immutable import format;
the importer resolves it into indexed PostgreSQL source-claim relations before
runtime queries.

Required changes:

- extract IDS zones;
- expose friendly/public line numbers;
- preserve CISLineID;
- preserve CISTripID;
- preserve any available CIS StopIDs;
- preserve source identifiers and provenance;
- preserve train numbers;
- include locations trains pass through without stopping;
- include scheduled passage times;
- distinguish passenger and non-passenger calls;
- preserve any post/platform data available in the source;
- emit deterministic, testable conversion sidecars.

## JrUtil testing

Maintain tiny golden fixtures for:

- one JDF bus route;
- one JDF trip with multiple posts;
- one CZPTT train with passenger and pass-through points;
- one overnight service;
- one source export where local source IDs change.

Do not block project delivery on upstream acceptance. Pin the project to a known fork commit while submitting clean patches upstream independently.

---

# 12. Static source precedence and overlays

Generic GTFS merging is not sufficient for this project.

The required behaviour is a deterministic **GTFS compiler** operating on canonical entities.

Regional and operator feeds should overlay:

- selected fields;
- selected calls;
- selected journey segments;
- selected metadata.

They should not replace entire trips merely because a matching trip exists.
Every imported notice, zone, connection and travel restriction is a positive
source claim. A regional feed that omits the corresponding field makes no
deletion claim against national data. Precedence is resolved during static
compilation and materialized for the active build; runtime requests must not
scan Parquet or arbitrate source claims dynamically.

## 12.1 Source coverage

```text
source_trip_binding
    source_id
    source_trip_id
    canonical_trip_id
    coverage_from_sequence
    coverage_to_sequence
    valid_from
    valid_to
```

## 12.2 Field-level precedence

Example:

```yaml
pid:
  bus:
    covered_segment:
      stop_times: authoritative
      boarding_points: authoritative
      shape: authoritative
      headsign: authoritative
      route_colour: authoritative
      accessibility: authoritative

  train:
    covered_segment:
      stop_times: authoritative
      boarding_points: authoritative
      shape: preferred
    outside_covered_segment:
      schedule: national
```

The policy must be explicit, declarative and testable.

Do not implement an implicit “PID wins everything” rule.

## 12.3 Entity-specific deduplication

Trips, routes and stops must be deduplicated separately.

### Trips

- road/MHD: CISLineID + CISTripID;
- rail: train number, with timetable variant resolution where necessary.

### Routes

- primarily canonicalized by CISLineID for road/MHD;
- rail route grouping may require a project-specific service or line model;
- source route IDs remain bindings.

### Stops

Prefer:

1. PostID, ASW ID or CIS StopID;
2. explicit crosswalk;
3. existing canonical continuity;
4. structural candidate;
5. manual mapping;
6. new canonical allocation.

## 12.4 Regional GTFS adapter and matching contract

GTFS identifiers are source-local unless the provider explicitly documents another namespace.
Every static adapter should preserve the original GTFS row and emit typed hints or identity
claims; it should not manufacture canonical or CIS identifiers.

Normalize common custom attributes into namespaced facts, for example:

```text
duk_stop_id  -> source-local stop-place hint in the DÚK namespace
cis_stop_id  -> asserted CIS StopID
stop_post    -> source post designation
duk_zone     -> zone code in the DÚK fare-system namespace
```

Keep the raw custom columns in snapshot storage so an adapter rule can be audited and replayed.
Keep nonstandard companion files such as IDS JMK `api.txt` as well. Simple field mappings may be
declarative. Provider-specific parsing belongs in a small, versioned adapter with real-feed golden
fixtures.

Static road/MHD matching should proceed in this order:

1. documented CISLineID/CISTripID claims, validated against the national snapshot;
2. explicit, validity-bounded aliases for transformed identifiers;
3. an already reviewed source binding that remains structurally consistent;
4. candidate routes constrained by operator, mode, validity, public designation and geography;
5. operating-instance comparison using active service date, ordered canonicalized stops and
   scheduled times;
6. a reviewed manual binding;
7. unresolved or ambiguous quarantine.

Names, public line numbers, numeric suffixes and zero-padding may generate candidates, but must
not establish a CIS identity on their own. A source route can map to multiple CISLineIDs: PID, for
example, associates licences with `(route_id, sub_agency_id)`, so `route_id` alone is not always a
valid binding key. Route, trip, stop and post resolution remain independent so useful stop/post or
shape data is not discarded solely because another entity is unresolved.

Source capability is field-specific. The presence of a regional GTFS archive does not make that
source authoritative for shapes, posts or any other table it omits. For example, a feed without
`shapes.txt` can still contribute exact static/realtime crosswalks, stop hierarchy, zones, colours
and timetable evidence while national or generated geometry remains active.

When scheduled rows with different CISTripIDs have identical calls and times, compare service
calendars and operating dates. If ambiguity remains, applying an attribute to a proven common
route/segment may still be safe, but trip-specific timetable or post replacement must remain
quarantined.

---

# 13. Partial regional train feeds

PID may publish only the section of a train inside its area even though the train continues farther.

The project must preserve the complete national train.

Example:

```text
National CZPTT:
Cheb -> Plzeň -> Praha -> Kolín -> Pardubice

PID:
Beroun -> Praha -> Kolín
```

Compiled result:

```text
Cheb -> Plzeň       national schedule
Beroun -> Praha     PID fields where better
Praha -> Kolín      PID fields where better
Kolín -> Pardubice  national schedule
```

It remains one canonical trip.

PID source trip IDs bind to the covered segment of the complete canonical trip.

The same principle applies to realtime:

- PID positions can update the full canonical trip instance;
- PID stop updates apply only to the stop sequences they describe;
- the national schedule remains available outside PID coverage;
- another provider can continue supplying data after the train leaves PID;
- alerts retain their original scope.

---

# 14. Static stop and post overlays

A regional source may provide exact posts where the national feed provides only the stop place.

Example:

```text
National:
Teplice, Benešovo náměstí

Regional:
Teplice, Benešovo náměstí, post B
```

Compiler behaviour:

1. Resolve both records to the same stop place.
2. Resolve or create canonical post B.
3. Bind the regional PostID or source post ID.
4. Assign that boarding point only to matching trip calls.
5. Leave unmatched trips at the unspecified boarding point.

Result:

```text
Trip 1 -> post B
Trip 2 -> post D
Trip 3 -> unspecified boarding point
```

This preserves useful precision without pretending all sources know the same posts.

## 14.1 Flat regional stop feeds

Do not assume that a parentless GTFS `location_type=0` row represents a complete independent stop
place. Many otherwise useful feeds publish one boarding post as one GTFS stop and omit
`parent_station` entirely. Import such rows first as **source boarding-point observations** and
resolve their source-local grouping separately from canonical identity.

Prefer source-local grouping evidence in this order:

1. a valid explicit `parent_station`;
2. a documented stop-place key such as PID `asw_node_id`, DÚK `cis_stop_id` or DÚK
   `duk_stop_id`;
3. reviewed provider-specific parsing of a source stop ID;
4. a trustworthy grouping carried forward from an earlier source snapshot;
5. structural candidates using normalized name, coordinates, route/call structure and post labels;
6. otherwise a singleton source stop place.

Grouping rows within one source and binding that group to a canonical stop place are different
decisions. For example, a shared PID `asw_node_id` can establish that several PID rows are posts of
one PID stop without itself proving which national stop that group represents. Similar names and
nearby coordinates may generate candidates but must not silently merge railway facilities,
grade-separated stops or similarly named nearby places.

Preserve each post's own coordinates, labels and source identifiers after grouping. An uncertain
group or canonical match remains unresolved rather than blocking ingestion or forcing a false
merge.

## 14.2 Regional coverage never limits the national stop universe

The national JDF/CZPTT baseline defines timetable completeness. A regional feed may add a stop
place, posts or exact call assignments, but its trip coverage does not determine which canonical
trips are allowed to use that stop.

Example:

```text
PID U1Z1P (ASW node 1, post A) --\
                                      -> canonical Boletická
PID U1Z2P (ASW node 1, post B) --/

matched PID/national trip 1 -> Boletická, post A
matched PID/national trip 2 -> Boletická, post B
national-only trip 3        -> Boletická, unspecified boarding point
```

Creating or matching posts never assigns them to every call at the stop. Use an exact post only
for a matched source trip/call, another authoritative call-level claim, or an explicit reviewed
rule. Every other national call remains attached to the canonical stop's permanent unspecified
boarding point. This allows partial regional precision without deleting, duplicating or inventing
the rest of the national timetable.

---

# 15. Static compilation pipeline

Run the compiler in this order:

1. Download each source.
2. Store the raw source by checksum.
3. Record source metadata and retrieval time.
4. Validate source packaging and basic schema.
5. Convert JDF and CZPTT through JrUtil.
6. Reconcile source entities against the canonical registry.
7. Build complete national canonical routes, trips and calls.
8. Import regional and operator static feeds.
9. Match regional trips to canonical trips.
10. Apply source aliases.
11. Apply segment-level and field-level overlays.
12. Resolve exact boarding points.
13. Preserve regional shapes where authoritative.
14. Generate missing shapes where possible.
15. Validate canonical invariants.
16. Export GTFS Schedule and project extensions.
17. Run an official GTFS validator.
18. Produce machine-readable build diagnostics.
19. Atomically activate the new feed and mapping version.

## Raw source storage

```text
data/raw/<source>/<sha256>/...
```

A source manifest should include:

```text
source
downloaded_at
source_url or retrieval method
checksum
source-declared version
licence
conversion version
```

## Immutable build structure

```text
data/builds/2026-07-18T150000Z-4b913fa/
├── gtfs.zip
├── build.json
├── validation.json
├── source-manifest.json
├── source-bindings.parquet
├── operational-calls.parquet
└── diagnostics/
```

The active build should be selected by one atomic database update or symlink swap.

## Last-known-good behaviour

If a new regional feed:

- fails validation;
- has catastrophic match-rate changes;
- contains ambiguous trip mappings;
- loses required identity fields;

keep the last known good regional snapshot active.

One broken upstream source must not destroy the nationwide feed.

---

# 16. GTFS export

## Stable exported IDs

Example projections:

```text
route_id = R000000123
trip_id  = T000000456
stop_id  = S000000123 or P000000456
```

Canonical IDs can be used directly if their format is safe for public export.

## Project-specific schedule extensions

Suggested public extension files:

```text
cz_routes.txt
    route_id
    cis_line_id
    public_line_number
    source_provenance

cz_trips.txt
    trip_id
    cis_line_id
    cis_trip_id
    train_number
    source_trip_ids
    coverage_sources

cz_stops.txt
    stop_id
    stop_place_id
    cis_stop_id
    post_id
    asw_id
    source_ids

cz_stop_zones.txt
    stop_place_id
    zone_id
    zone_code
    route_id
    ids_system_id
    source_provenance
```

Operational points should generally remain internal sidecars rather than public GTFS stops.

`cz_routes.txt` deliberately has no route-level IDS-system or zone union. A route can participate
in multiple systems and its zones vary by route stop, trip and call, so a singular system field or
comma-separated route union is ambiguous and not useful for compilation. Exact route-stop
membership remains in `cz_stop_zones.txt`; its per-membership `ids_system_id` can be populated
later when ownership is known.

In `cz_stops.txt`, `stop_id` is the exact GTFS row used by calls and may identify either a stop
place or a boarding post. `stop_place_id` is always the containing place-level GTFS row. The two
are equal on place rows and differ on child post rows, which also carry standard GTFS
`parent_station`. Post identifiers use the common `:post:` hierarchy: authoritative
`Oznacniky` values use `:post:id:<value>`, while textual `Zasspoje` values use bare
`:post:<value>`, so the two mechanisms cannot collide.

JrUtil-generated intermediate IDs use colon-separated namespaces consistently, including
`jdf:agency:…`, `jdf:route:…`, `jdf:trip:…`, `jdf:stop:…`, `jdf:zone:…` and
the enrichment namespaces. CIS-backed stop IDs use `cis:stop:…`; these remain source/intermediate
identities rather than permanent canonical Oběhy IDs. Deduplicated GTFS operating patterns use
the derived `gtfs:service:<weekday-bitmap>:<ordinal>` namespace.

## Version compatibility

Every static build gets a permanent `feed_version`.

The realtime feed must identify the matching static feed version.

The static feed, source bindings, trip-sequence mappings and realtime resolver must activate together.

---

# 17. pfaedle workstream

pfaedle should run after static overlays.

Shape selection order:

```text
authoritative regional/operator shape
    else existing acceptable national shape
    else pfaedle-generated shape
    else no shape
```

A trip without a shape is preferable to a build failure.

Required pfaedle work:

- support current OSM protobuf files;
- maintain one old known-working fixture;
- maintain one current fixture;
- prove equivalent graph extraction;
- separate protobuf compatibility changes from routing or matching algorithm changes;
- pin the patched commit;
- submit upstream independently.

Build diagnostics:

```text
Trips with source shape
Trips with retained national shape
Trips enriched by pfaedle
Trips still lacking shape
pfaedle failures by mode
Stops suspiciously far from shape
```

---

# 18. Realtime architecture

Realtime connectors must emit normalized **claims**, not final truth.

Claim types:

```text
PositionClaim
StopEventClaim
DelayClaim
PredictionClaim
PlatformClaim
VehicleIdentityClaim
AlertClaim
CompositionClaim
```

## Connector boundary

```python
class RealtimeConnector(Protocol):
    source_id: str

    async def fetch(self) -> NormalizedRealtimeBatch:
        ...
```

Connectors are responsible for:

- downloading or polling;
- source parsing;
- timestamp conversion;
- basic field normalization;
- source-specific identity extraction;
- returning raw references.

Connectors are not responsible for deciding which provider is trusted more.

## Common realtime pipeline

```text
fetch
 -> decode
 -> normalize source identifiers
 -> resolve active source-local static crosswalk
 -> apply aliases
 -> resolve canonical trip instance
 -> validate timestamps and geography
 -> store immutable claims
 -> arbitrate by capability
 -> update fused trip state
 -> emit GTFS-RT
 -> update map API
 -> append history
```

## Realtime claim metadata

Every claim should retain:

```text
source_id
source_entity_id
trip_instance_id
event_time
received_time
valid_from
valid_until
precision
granularity
uncertainty
geographical_scope
sequence_scope
raw_payload_reference
```

---

# 19. Multi-source realtime arbitration

A train may simultaneously have data from SŽ, PID and DÚK.

There must not be one global source ranking.

Trust must be:

- capability-specific;
- mode-specific;
- geographically scoped;
- sequence scoped;
- freshness limited.

## 19.1 Example capability preferences

| Information | Preferred evidence |
|---|---|
| Train platform | Fresh infrastructure assignment |
| Actual rail passage | Infrastructure event at an operational point |
| Vehicle identity | Operator or IDS vehicle registry |
| Current position | Freshest spatially plausible AVL/GPS observation |
| Per-stop ETA | Validated provider prediction or project estimator |
| Delay | Recent actual event or high-confidence GPS derivation |
| Coarse scalar delay | Fallback |
| Alerts | Preserve and deduplicate; do not select one universal winner |
| Composition | Most authoritative permitted composition source |

## 19.2 Policy configuration

```yaml
policies:
  - source: sz
    mode: train
    capability: platform_assignment
    scope: nationwide
    priority: 100
    max_age_seconds: 300

  - source: pid
    mode: train
    capability: vehicle_position
    scope: pid
    priority: 90
    max_age_seconds: 90

  - source: duk
    mode: train
    capability: vehicle_position
    scope: duk
    priority: 80
    max_age_seconds: 90
```

## 19.3 Eligibility gates

Before comparing priorities, reject or downgrade claims that fail:

- exact trip matching;
- plausible timestamp;
- freshness;
- source coverage;
- plausible speed;
- plausible movement from previous observations;
- proximity to expected path;
- consistent operating date;
- consistent sequence progression;
- acceptable source health.

Only eligible claims enter arbitration.

## 19.4 Conflicting positions

Never average two contradictory positions.

Prefer the claim that best satisfies:

- freshness;
- source capability policy;
- path plausibility;
- trajectory continuity;
- source accuracy;
- sequence scope.

Record the conflict for diagnostics.

The losing observation remains stored.

## 19.5 Selected-state provenance

Every selected value should expose internally:

```text
selected value
source
source timestamp
received timestamp
selection reason
confidence
competing claims
```

---

# 20. Delay model

Delay must not be represented internally as only one integer.

## Delay claim model

```text
delay_claim
    lower_bound_seconds
    upper_bound_seconds
    granularity_seconds
    supports_negative
    applies_from_sequence
    applies_to_sequence
    source_method
```

A source exposing only non-negative whole-minute delays is coarse evidence.

Example:

```text
reported 3 minutes
-> approximately 180 to 239 seconds, depending on rounding semantics

reported 0 minutes
-> not proof of exact on-time running
-> no reliable information about early running
```

## Preferred delay evidence

1. Explicit actual arrival, departure or passage event
2. High-confidence GPS progress estimate
3. Reliable per-stop prediction
4. Precise signed source delay
5. Coarse non-negative scalar delay
6. Static schedule

A lower-ranked source may still fill stops not covered by a better source.

Do not propagate one scalar delay unchanged through a long trip.

Maintain per-call predictions.

---

# 21. Trip-state estimator

Maintain one fused state per active trip instance.

```text
trip_state
    trip_instance_id
    current_path_distance
    current_call_sequence
    selected_position
    last_confirmed_event
    estimated_delay
    confidence
    selected_sources
    updated_at
```

Create a scheduled-time function over the expected path:

```text
scheduled time = f(distance along path)
```

For each position:

1. map-match it to the expected route or rail path;
2. determine distance along path;
3. determine expected scheduled time at that position;
4. compare observed time with expected time;
5. reject implausible jumps;
6. anchor against recent actual events;
7. calculate per-stop predictions.

## Railway estimation

Inputs:

- passenger stops;
- non-passenger operational points;
- scheduled passage times;
- railway geometry;
- SŽ or other infrastructure passage events;
- available GPS positions.

A recent actual passage event should generally anchor the state more strongly than an older position.

## Normal road estimation

Initial inputs:

- source or pfaedle shape;
- scheduled stop times;
- distance along shape;
- current GPS position;
- recent observed speed.

Later add historical segment travel times.

## Confidence

Estimator output should include confidence based on:

- age of last observation;
- map-match distance;
- number of recent observations;
- trajectory consistency;
- quality of scheduled timing points;
- source precision;
- availability of actual passage events.

---

# 22. Long-distance coach routing

Per-stop delay propagation may be weak for long coach journeys with long motorway sections.

Implement an optional estimator plugin.

```yaml
trip_patterns:
  T000004201:
    estimator: mapy-routing
```

Use paid Mapy.com routing only for whitelisted services.

The routing result should estimate remaining travel time, not define canonical route identity.

Requirements:

- force the intended corridor using waypoints where needed;
- cache route geometry and reusable segments;
- call the API sparsely;
- do not call it on every GPS update;
- configure monthly request and cost limits;
- provide an automatic fallback;
- benchmark it against actual historical trips;
- enable it only where it measurably improves predictions.

A normal car routing estimate may not perfectly model coach operations, dwell, restrictions or service roads. Treat it as one predictor, not ground truth.

---

# 23. Dynamic posts and train platforms

Platform and post assignments are realtime claims.

```text
platform_claim
    trip_instance_id
    scheduled_call_sequence
    assigned_boarding_point_id
    source
    assigned_at
    valid_until
    confidence
```

Suggested arbitration order:

```text
fresh infrastructure assignment
 > fresh operator or IDS assignment
 > previous still-valid assignment
 > scheduled static boarding point
 > unspecified boarding point
```

## Static prerequisite

All known posts and platforms should exist in the static stop registry, even if only a small number of scheduled calls use them.

## Unknown realtime posts

If an API returns a post/platform that cannot yet be mapped:

- retain the raw value;
- expose it through debugging or a custom API where safe;
- do not invent an unstable GTFS stop ID;
- create a review/mapping task;
- include it in the next static build after canonicalization.

## GTFS-RT output

Where the assigned boarding point exists in static GTFS:

- identify the correct stop sequence;
- publish the assigned stop/platform against that sequence;
- keep the canonical stop-place relationship intact.

---

# 24. PID realtime on full train journeys

PID realtime is applied to the complete canonical train.

When PID covers only part of the train:

- accept PID positions while fresh and plausible;
- apply PID stop updates only to matching canonical call sequences;
- retain national schedule elsewhere;
- allow SŽ, DÚK or another provider to continue the journey state;
- use the estimator across gaps;
- do not terminate the canonical train at the PID boundary.

Realtime output should describe the complete canonical trip, not a duplicated PID-only partial train.

---

# 25. Alerts

Alerts should be preserved as independent claims and mapped to canonical scope.

```text
alert_scope
    canonical_trip_id
    canonical_route_id
    from_sequence
    to_sequence
    stop_place_id
    geographical_scope
```

## Mapping rules

### Whole-trip incident

Map to the whole canonical trip.

### Stop-specific incident

Map to the canonical stop place or boarding point.

### Segment-only incident

Retain the affected sequence range.

Do not expose a PID-only segment disruption as a nationwide route disruption.

## Alert deduplication

Use:

- normalized text;
- active period;
- canonical scope;
- cause;
- effect;
- source references.

Do not merge alerts only because they mention the same line.

## Standard versus custom representation

Where GTFS-RT cannot represent the exact segment scope without becoming misleading:

- publish the closest safe standard selector;
- retain the precise scope in the project API;
- expose provenance.

---

# 26. PID connector

PID is the first GTFS-Realtime connector.

Do not proxy PID protobuf unchanged.

Decode and rewrite:

```text
PID trip_id      -> canonical trip_id
PID route_id     -> canonical route_id
PID stop_id      -> canonical boarding point or stop
PID vehicle_id   -> source binding / canonical vehicle
```

Unmatched or ambiguous entities go to diagnostics.

Capabilities should be handled independently:

- vehicle positions;
- trip updates;
- alerts;
- occupancy;
- vehicle identity.

PID can be authoritative for some capabilities without owning all fields.

---

# 27. DÚK connector

The DÚK connector should:

- call the custom API;
- parse observations;
- extract `vhc_id`;
- extract CISLineID and CISTripID;
- normalize timestamps and coordinates;
- emit claims.

It must not know GTFS output IDs or database internals.

Common matching performs:

```text
582588
 -> source alias
 -> 001588
 -> CISLineID + CISTripID + operating date
 -> canonical trip instance
```

DÚK can initially provide:

- vehicle positions;
- vehicle identity;
- source delay;
- current/next stop information;
- later dynamic posts if available.

---

# 28. SŽ and other rail sources

A railway infrastructure connector should focus on the capabilities it is best at:

- actual passage events;
- operational-point occupancy or progress;
- platform assignments;
- train identity;
- infrastructure-origin delay claims.

It should not automatically override a fresher and more spatially precise GPS source for current position.

Its events should strongly anchor the railway estimator.

---

# 29. Vehicle registry

A source vehicle ID should bind to a canonical vehicle.

```text
vehicle
    canonical_vehicle_id
    operator_id
    fleet_number
    public_label

vehicle_source_binding
    source_id
    source_vehicle_id
    canonical_vehicle_id
    valid_from
    valid_to

vehicle_attribute
    canonical_vehicle_id
    attribute
    value
    source_id
    valid_from
    valid_to
```

DÚK `vhc_id` observations resolve to canonical vehicles.

A separate fleet dataset can provide:

- model;
- manufacturing year;
- low-floor status;
- air conditioning;
- USB;
- Wi-Fi;
- other features.

Keep provenance per attribute because sources may disagree.

The public vehicle API should indicate the source of:

- current position;
- current trip;
- public label;
- model;
- features.

---

# 30. Train compositions

Compositions are attached to:

```text
train number + operating date
```

Suggested model:

```text
train_composition
    train_number
    operating_date
    observed_at
    source_id

train_composition_vehicle
    sequence
    vehicle_number
    vehicle_type
    passenger_label
    features
```

Implementation order:

1. ČD source already available.
2. 55p.cz only after explicit permission covering:
   - retrieval;
   - storage;
   - display;
   - redistribution;
   - caching duration.

The project API should remain the rich source of truth.

GTFS-Realtime carriage details can be populated where suitable, but the canonical model should not be limited to what standard GTFS-RT can express.

---

# 31. Historical observations and stop events

Begin retaining normalized realtime observations as soon as the first connector works.

Keep these concepts separate:

```text
scheduled event
source prediction
project prediction
source-reported actual event
GPS-inferred actual event
```

## Tables

```text
vehicle_observation
stop_event_claim
platform_claim
prediction_snapshot
resolved_trip_state
actual_stop_event
```

## Actual event model

```text
actual_stop_event
    trip_instance_id
    call_sequence
    event_type
    event_time
    method
    confidence
    source_ids
```

Event types:

```text
arrival
departure
passage
```

Methods:

```text
source
gps_geofence
map_match
interpolated_crossing
```

## Passenger-stop arrival inference

1. Vehicle is matched to a trip instance.
2. It approaches the expected call in sequence.
3. It enters the stop or platform geofence.
4. The position is consistent with the trip path.
5. Speed or source status indicates arrival.
6. Departure is recorded after leaving toward the next call.

## Railway passage inference

1. Map-match observations to the railway path.
2. Detect crossing of an operational-point distance.
3. Interpolate the crossing time between observations.
4. Compare it with the scheduled passage time.
5. Feed the event back into the trip-state estimator.

## Retention

For one machine:

- partition high-volume observation tables by date;
- keep high-resolution positions for a limited period;
- retain derived actual events long term;
- downsample or export old trajectories to Parquet;
- retain raw source payloads only for a defined debugging and licensing period.

---

# 32. Map and public API

The frontend must consume only project-owned contracts.

It must not directly know whether a vehicle came from PID, DÚK, SŽ or another provider.

Initial endpoints:

```text
/gtfs/gtfs.zip
/gtfs/versions/<version>/gtfs.zip
/gtfs/manifest.json

/gtfs-rt/vehicle-positions.pb
/gtfs-rt/trip-updates.pb
/gtfs-rt/alerts.pb

/api/vehicles?bbox=...
/api/stops?bbox=...
/api/stop-places/<id>/departures
/api/trips/<id>
/api/vehicles/<id>
/api/alerts
/api/debug/realtime
```

## Frontend capabilities

Initial:

- nationwide stops and routes;
- current vehicles;
- scheduled departures;
- realtime predictions;
- vehicle details;
- alerts;
- stale-data state;
- source/confidence indicator in debugging views.

Later:

- dynamic platform/post display;
- historical trip replay;
- train composition;
- disagreement diagnostics;
- nearby grouped departures;
- operational quality metrics.

Use viewport-based queries.

Polling every few seconds is acceptable initially.

WebSockets are not an early milestone.

---

# 33. Logging and observability

The platform needs both technical logs and data-quality diagnostics.

## Technical metrics

- source download success;
- source parse time;
- connector response time;
- source age;
- worker lag;
- build duration;
- active trip count;
- database write latency;
- API latency;
- GTFS-RT generation time.

## Data-quality metrics

- static trip match rate;
- stop continuity match rate;
- ambiguous matches;
- newly allocated stops;
- unmatched realtime entities;
- source conflicts;
- position rejection rate;
- stale claims;
- platform mapping failures;
- delay disagreement;
- estimator confidence;
- feed entity counts;
- alert mapping failures.

## Source health state

```text
healthy
degraded
stale
invalid
disabled
using_last_known_good
```

## Realtime entity state

```text
fresh
stale
invalid
unmatched
ambiguous
suppressed_by_better_source
```

---

# 34. Testing strategy

## Unit tests

- identity normalization;
- manual aliasing;
- canonical ID allocation;
- stop structural scoring;
- trip-key matching;
- source precedence;
- delay intervals;
- platform arbitration;
- sequence-scope mapping.

## Golden conversion tests

- JrUtil JDF conversion;
- JrUtil CZPTT conversion;
- operational points;
- posts;
- friendly line numbers;
- IDS zones.

## Static integration tests

- national-only build;
- one PID bus overlay;
- one truncated PID train overlay;
- changing national stop IDs;
- new and retired stops;
- ambiguous regional match;
- last-known-good fallback.

## Realtime integration tests

- PID ID rewriting;
- DÚK alias mapping;
- duplicate observations;
- contradictory positions;
- stale provider;
- dynamic platform assignment;
- partial stop updates;
- source failover.

## Replay tests

Record real source payloads and replay them deterministically.

Use them to test:

- estimator changes;
- source arbitration;
- alert mapping;
- arrival inference;
- feed output stability.

## Invariants

Examples:

```text
One source trip cannot resolve to multiple canonical trips.
One canonical trip instance cannot emit duplicate competing vehicle positions.
Every exported realtime trip ID exists in the active static feed.
Every assigned platform ID exists in the active static feed.
Every passenger stop_time references a passenger boarding point.
Operational points never leak into passenger stop_times.
```

---

# 35. Delivery roadmap

## Milestone 0 — Domain contracts and fixtures

Build:

- canonical ID allocator;
- source binding model;
- manual aliases;
- stop place / boarding point / operational point model;
- route and trip identity model;
- trip-instance model;
- tiny JDF, CZPTT, PID and DÚK fixtures.

Exit criteria:

- two different JDF export IDs resolve to one stable canonical stop;
- `582588` resolves to `001588`;
- a PID train segment resolves to one complete canonical train;
- a source identifier cannot map ambiguously without causing failure.

---

## Milestone 1 — National conversion and registry

Build:

- raw-source downloader;
- source checksum storage;
- JrUtil conversion bundle;
- IDS zones;
- friendly line numbers;
- operational pass-through points;
- canonical stop continuity matcher;
- canonical route/trip import.

Exit criteria:

- a second national export imports without wholesale ID churn;
- operational train points are preserved internally;
- all entities contain provenance.

---

## Milestone 2 — First valid nationwide GTFS

Build:

- GTFS exporter;
- stable canonical IDs;
- stop-place and boarding-point hierarchy;
- unspecified boarding-point fallback;
- Czech extension files;
- validation;
- immutable build directory;
- build diagnostics.

Exit criteria:

- the database and generated files can be deleted;
- one command reconstructs the nationwide feed from stored snapshots;
- the output validates;
- feed versioning works.

This is the first publishable static artifact.

---

## Milestone 3 — Small PID static overlay

Use a deliberately small PID slice.

Build:

- PID stop and post matching;
- route and trip bindings;
- field precedence;
- segment coverage;
- one road trip overlay;
- one partial train overlay.

Exit criteria:

- the final feed contains one canonical trip, not duplicate national and PID trips;
- PID passenger-facing data is used inside coverage;
- the full national train continues outside coverage;
- national metadata survives where PID lacks it;
- exact post information is preserved;
- a machine-readable substitution report is generated.

---

## Milestone 4 — Complete PID static overlay

Build:

- complete PID import;
- hard match thresholds;
- ambiguity quarantine;
- last-known-good fallback;
- full stop/post overlay;
- complete diagnostics.

Exit criteria:

- ambiguous trip matches block activation;
- PID trips are correctly overlaid;
- source IDs map back to canonical IDs;
- unexpected count changes require explicit acceptance.

---

## Milestone 5 — Shapes and static production pipeline

Build:

- pfaedle protobuf update;
- post-overlay shape enrichment;
- shape quality checks;
- automatic build scheduling;
- atomic activation;
- active feed manifest.

Exit criteria:

- shape generation failure does not invalidate the feed;
- regional shapes remain preferred;
- active static data and mapping tables switch together.

---

## Milestone 6 — PID realtime vertical slice

Build:

- normalized realtime claims;
- PID GTFS-RT connector;
- ID rewriting;
- current trip-state storage;
- GTFS-RT output;
- PID alerts;
- minimal map.

Exit criteria:

- PID realtime entities resolve against the project static feed;
- unmatched entities are visible in diagnostics;
- partial PID train updates apply to full canonical trains;
- the map shows project-owned IDs only.

---

## Milestone 7 — DÚK realtime

Build:

- DÚK connector;
- CIS line aliasing;
- trip-instance matching;
- DÚK vehicle positions;
- DÚK vehicle IDs;
- basic source arbitration.

Exit criteria:

- PID and DÚK coexist in one GTFS-RT feed;
- no duplicate vehicle positions are emitted for one trip instance;
- DÚK can be disabled without affecting PID or static publication;
- unmatched DÚK trips are measurable.

This completes the main proof of concept:

```text
nationwide static
+ PID static overlay
+ PID realtime and alerts
+ DÚK realtime against national static
```

---

## Milestone 8 — Rail fusion and dynamic platforms

Build:

- SŽ or other infrastructure connector;
- capability-specific source policies;
- railway passage events;
- dynamic platform claims;
- multi-source train arbitration;
- conflict diagnostics.

Exit criteria:

One train can correctly use:

```text
position from PID
passage event from SŽ
vehicle metadata from DÚK
platform assignment from infrastructure data
```

Every selected value retains provenance.

---

## Milestone 9 — GPS and operational-point estimator

Build:

- path map matching;
- distance-along-path state;
- scheduled-time interpolation;
- operational-point delay derivation;
- per-stop predictions;
- precision-aware delay handling;
- confidence scoring;
- replay evaluation.

Exit criteria:

- estimator accuracy can be compared with each provider;
- coarse non-negative source delays no longer override better GPS evidence;
- pass-through railway points improve prediction quality;
- source policy is based on measured performance.

---

## Milestone 10 — Long-distance coach estimator

Build:

- estimator plugin interface;
- Mapy.com routing connector;
- whitelist;
- waypoint configuration;
- cache;
- spending limits;
- fallback;
- historical benchmark.

Exit criteria:

- paid routing is enabled only on lines where it beats simpler estimation;
- API failure does not break realtime output;
- cost is bounded.

---

## Milestone 11 — Historical arrivals and vehicles

Build:

- immutable observation storage;
- inferred arrival/departure events;
- inferred railway pass events;
- DÚK vehicle registry;
- vehicle detail API;
- Parquet archival.

Exit criteria:

- a trip can be replayed;
- scheduled, predicted and actual events are distinct;
- vehicle details are shown with provenance.

---

## Milestone 12 — Train compositions

Build:

- ČD composition import;
- canonical composition model;
- project API;
- optional GTFS-RT carriage projection;
- 55p connector after permission.

Exit criteria:

- composition is matched by train number and date;
- ordered vehicles are preserved;
- source licence and permission state are recorded.

---

## Milestone 13 — Full frontend

Build:

- MapLibre nationwide map;
- stop-place grouping;
- nearby departures;
- exact posts/platforms;
- alert display;
- vehicle features;
- train compositions;
- historical trip view;
- stale/conflicting data presentation.

Exit criteria:

- removing or disabling a connector changes coverage but requires no frontend code change;
- source-feed grouping is no longer visible to users;
- nearby relevant stops appear together without being falsely merged.

---

## Milestone 14 — Additional regions and providers

For every new source, require:

```text
source configuration
licence metadata
static adapter, if needed
realtime connector, if available
identity extraction
coverage declaration
precedence policy
fixtures
conformance report
```

Source conformance checks:

- download works;
- parser works;
- licence is understood;
- trip matching rate is acceptable;
- stop matching rate is acceptable;
- realtime freshness is acceptable;
- duplicate rate is acceptable;
- vehicle identity is stable enough;
- static and realtime versions are compatible.

---

# 36. First implementation tickets

Start with these tickets in this order.

1. Create repository and Python project structure.
2. Define canonical ID types and allocator.
3. Define source registry schema.
4. Define source binding and alias schema.
5. Define stop place, boarding point and operational point entities.
6. Define scheduled trip and trip-instance entities.
7. Define passenger and operational trip calls.
8. Create tiny synthetic JDF, CZPTT, PID and DÚK fixtures.
9. Build raw-source snapshot downloader with checksums.
10. Containerize and pin JrUtil.
11. Add JrUtil golden tests.
12. Add IDS zone extraction.
13. Add friendly line number extraction.
14. Add CZPTT pass-through operational points.
15. Import one tiny national conversion into PostgreSQL.
16. Implement canonical stop allocation.
17. Implement second-export stop continuity matching.
18. Export one deterministic GTFS sample.
19. Add GTFS validation.
20. Compile a complete national baseline.
21. Import one tiny PID static slice.
22. Match exactly one PID bus trip.
23. Match exactly one partial PID train.
24. Overlay one exact stop post.
25. Generate source-substitution diagnostics.
26. Decode PID GTFS-RT.
27. Rewrite one PID realtime trip to a canonical trip ID.
28. Display one canonical moving vehicle on a basic map.
29. Implement the DÚK `582588 -> 001588` alias.
30. Match and display one DÚK vehicle.

The first end-to-end success should be:

```text
one national trip
 -> overlaid by one regional trip
 -> exported with a stable canonical trip ID
 -> regional realtime rewritten to that ID
 -> displayed as one moving vehicle
```

After that works, nationwide expansion becomes controlled repetition rather than architecture discovery.

---

# 37. Suggested source configuration

```yaml
sources:
  national-jdf:
    type: jdf
    static_priority: 10
    coverage: nationwide-road
    required: true

  national-czptt:
    type: czptt
    static_priority: 10
    coverage: nationwide-rail
    required: true

  pid:
    type: gtfs
    static_priority: 100
    coverage: pid
    realtime:
      vehicle_positions_priority: 100
      trip_updates_priority: 100
      alerts_priority: 100

  duk:
    type: custom-api
    coverage: duk
    realtime:
      vehicle_positions_priority: 80
      trip_updates_priority: 70
      vehicle_identity_priority: 100

  sz:
    type: custom-api
    coverage: nationwide-rail
    realtime:
      platform_assignment_priority: 100
      passage_event_priority: 100
```

Priorities are capability-specific defaults, not unconditional truth.

---

# 38. Example build report

```text
Build version: 2026-07-18T150000Z-4b913fa

National JDF trips imported:          142,381
National CZPTT trips imported:         18,921
PID source trips imported:             31,442

PID exact canonical matches:           30,981
PID partial train matches:                211
PID trips added as new:                   302
PID missing identity:                     148
PID ambiguous matches:                      0

Stops matched by stable ID:             7,402
Stops matched structurally:            24,118
Stops matched manually:                    14
New canonical stops:                      106
Ambiguous stop candidates:                  3

Trips with source shapes:              82,103
Trips enriched by pfaedle:             61,420
Trips lacking shapes:                   1,779

Validation errors:                           0
Validation warnings:                       182
```

Any threshold that affects automatic activation should be configurable and version-controlled.

---

# 39. Example realtime debugging output

```json
{
  "trip_instance": "T000004201/2026-07-18",
  "selected": {
    "position": {
      "source": "pid",
      "observed_at": "2026-07-18T17:31:04+02:00",
      "reason": "freshest eligible path-consistent position",
      "confidence": 0.96
    },
    "delay": {
      "source": "project-estimator",
      "seconds": 214,
      "reason": "GPS progress anchored by SŽ passage event",
      "confidence": 0.88
    },
    "platform": {
      "source": "sz",
      "boarding_point_id": "P000008923",
      "reason": "fresh infrastructure assignment",
      "confidence": 0.99
    }
  },
  "suppressed_claims": [
    {
      "source": "duk",
      "capability": "position",
      "reason": "older observation"
    },
    {
      "source": "pid",
      "capability": "delay",
      "reason": "coarse non-negative whole-minute value"
    }
  ]
}
```

This kind of endpoint will be essential while tuning the fusion logic.

---

# 40. Decisions intentionally deferred

Do not decide these prematurely:

- exact canonical ID prefix format;
- whether every canonical registry table uses integer or UUID primary keys internally;
- websocket versus polling;
- advanced scaling architecture;
- permanent high-resolution position retention period;
- exact source priorities before replay benchmarking;
- whether operational sidecars are publicly downloadable;
- whether stop-place grouping becomes fully canonical or remains a derived layer;
- whether train composition is projected into experimental GTFS-RT fields;
- whether Mapy.com routing is worth the cost.

These decisions should be made after the relevant milestone produces real evidence.

---

# 41. Definition of project success

The architecture is successful when:

- national source ID churn does not break public IDs;
- regional static feeds improve national data without duplicating or truncating journeys;
- exact posts/platforms are preserved where known;
- realtime from several providers can coexist without silent corruption;
- source quality is evaluated per capability rather than by one global ranking;
- coarse provider delays can be replaced by better GPS or infrastructure evidence;
- non-passenger railway points improve predictions without appearing as public stops;
- PID partial trains remain full national journeys;
- alerts retain correct geographic and sequence scope;
- the frontend remains backend-source agnostic;
- historical events can be replayed and explained;
- adding another regional provider is mostly a connector, configuration and fixture task;
- the initial system remains operable on one community-hosted machine.

The project should be built as a sequence of usable vertical slices.

Do not begin the polished frontend, nationwide realtime fusion, pfaedle internals, arrival inference, vehicle registries and train-composition negotiations at the same time.

Prove the identity and overlay model first.

Then prove one static regional overlay.

Then prove one rewritten realtime vehicle.

Then expand coverage.
