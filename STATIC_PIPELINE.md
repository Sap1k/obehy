# Oběhy static-pipeline contract

`BASE_PLAN.md` is authoritative. This document fixes the executable boundary between Oběhy,
JrUtil and the serving database.

## Current two-package production command

`obehy build` is the current production entry point. In production it runs on GitHub Actions,
never on the application server. It validates the configured OSM/geodata,
builds the national JDF package, freezes PID and IDS JMK GTFS snapshots, applies both overlays in
one JrUtil invocation, and builds CZPTT using the same resolved GVD year. It publishes exactly two
`jrutil-production` packages under one immutable release and atomically updates `current.json` only
after JrUtil validation and publication-eligibility checks pass.

Source snapshots, orchestration manifests, process logs and detailed diagnostics remain outside the
closed package trees. The release loader is the next step; the serving-v1 loader has been
removed. MOTIS shape generation will be inserted between compilation and final
validation when it is built.

Two complete live releases have been published (2026-10-02 and 2026-10-03); formal acceptance
(GTFS validator, MOTIS import check) is still to run. Memory budgets are soft admission/spill
targets, with no process or .NET heap hard limit. Cross-representation content validation remains
unfinished. See `PROGRESS.md` for measured results and the actual verification state.

## Ownership and build protocol

1. `obehy build` (GitHub Actions) downloads each configured static source and stores it immutably
   by SHA-256.
2. JrUtil validates and converts those snapshots, performs national compilation and
   regional/operator overlays, and writes deterministic GTFS plus the finalized serving package.
   Identity follows `BASE_PLAN.md` section 6: deterministic `jdf:`/`czptt:` IDs, JDF stops and
   posts pinned by the reviewed registry in `jrunify-ext-geodata/registry/`, and
   `identity_contract = "jrutil-identity-v1"`. There is no identity service.
3. The workflow publishes the release (`release.json` plus both packages) as a GitHub Release.
4. On the application server, `obehy release fetch` verifies every hash, and `obehy release load`
   streams the relations into isolated per-load partitions, validates them set-wise and attaches
   the complete partition set.
5. One `control.publication` transaction activates the GTFS artifacts, static mirror, source
   mappings and realtime resolver version together.

The compiler never reads or mutates Oběhy serving tables. The loader never performs identity
matching, trip collapse, overlay precedence, fuzzy matching, or static claim arbitration.

## Commands and build identity

`obehy build` drives the JrUtil multitool (the built `jrutil-multitool.dll` or a configured
command): `fix-jdf`, `merge-jdf` and `jdf-to-bundle` for the national JDF package,
`regional-gtfs-overlay` for PID + IDS JMK on top of it, `czptt-to-bundle` for rail, and
`validate-package` for each result. Every package records the exact JrUtil commit (plus a
working-tree digest when dirty) as its `compiler` version via `--converter-version`; the overlay
reuses the version recorded by its base package.

When `jrunify-ext-geodata/registry/stops.csv` exists, Oběhy passes the registry to `merge-jdf`,
`jdf-to-bundle` and `regional-gtfs-overlay` and keeps the review candidates in the release's
`stop-registry/` directory. Live source URLs and credentials never enter JrUtil inputs; sources
reach it as checksum-pinned snapshots with descriptors.

## Production package

JrUtil's normative contract is `jrutil/docs/PRODUCTION_CONTRACT.md` (bundle version 3, serving
schema version 4). Oběhy accepts nothing else.

```text
package/
├── gtfs.zip
├── serving/
│   ├── agency.parquet
│   ├── location.parquet
│   ├── route.parquet
│   ├── service_calendar.parquet
│   ├── service_exception.parquet
│   ├── shape.parquet
│   ├── shape_point.parquet
│   ├── trip.parquet
│   ├── trip_call.parquet
│   ├── transfer.parquet
│   ├── call_zone.parquet
│   ├── location_zone.parquet
│   ├── service_note.parquet
│   ├── service_note_assignment.parquet
│   ├── service_feature_assignment.parquet
│   ├── location_feature.parquet
│   ├── connection_claim.parquet
│   ├── travel_restriction_assignment.parquet
│   ├── operational_location.parquet
│   ├── operational_journey.parquet
│   ├── operational_call.parquet
│   ├── source_entity_map.parquet
│   ├── source_trip_map.parquet
│   ├── source_call_map.parquet
│   ├── source_trip_coverage.parquet
│   ├── road_route_key.parquet
│   ├── road_trip_key.parquet
│   ├── rail_trip_key.parquet
│   ├── route_stop.parquet
│   └── route_stop_zone.parquet
├── manifest.json
└── diagnostics.json
```

`gtfs.zip` is standard GTFS only; transfer waiting limits live in `transfer`. Zones are
codes: a route stop slot holds them in `route_stop_zone` when all its calls agree, otherwise the
calls carry them in `call_zone`; `location_zone` answers "zones of this stop" directly. `route_stop` is the
merged, ordered stop list of each route direction (one slot per visit), and every
`trip_call.route_stop_id` points at its slot, so line timetables need no pattern merging in Oběhy.
Each relation has a fixed schema, Snappy compression, a unique primary key and resolving foreign
keys. Rows are in deterministic generation order and are not sorted. Provenance is kept at trip and
route level (`source_trip_map`, `source_entity_map`); field-level provenance is not recorded.

The manifest inventories every payload with its size and SHA-256, declares every relation, and pins
the build specification digest, source snapshots, compiler version, feed version and identity
contract. Identical inputs must produce identical semantic output.

`JDF_SEMANTICS.md` is the normative preservation addendum. The current JrUtil GTFS conversion is
not lossless for JDF fixed codes: it collapses accessibility detail, can only approximate
order/conditional service in GTFS, and drops luggage,
reservation, stop facilities, and interchange hints. The production package therefore carries typed
service/call features, location features, notes, connection claims, restrictions, and operational
relations. GTFS is a projection of those facts, not their storage format.

Connections remain claims at the specificity supplied by `Navaznosti`. The future compiler may
parse only the note forms defined by JDF 1.11 and must record `target_derivation = "spec_note"`;
Oběhy never parses notes or invents targets. Only unique final resolution emits a routable
`transfer`, while unresolved claims are still retained for NeTEx and explanation APIs.

National-sized relations are streamed once per stage. The compiler may not retain or emit a second
17-million-row JDF call relation. After the first production benchmark, unexplained performance
regressions above 15 percent fail the build gate.

## Overlay policy

For every source, mode and coverage scope, capabilities are `disabled`, `fill_missing`, `preferred`
or `authoritative`, with explicit priority. Omission is not deletion. Equal-priority conflicts and
remaining mapping ambiguity are quarantined; optional-overlay failure removes only the affected
claim unless that source/scope is required.

The first fixture is a PID bus slice supplying exact posts only. National times, names and colours
remain selected. Every source entity/trip/call key names its identifier namespace explicitly, such
as `gtfs_trip_id`, `gtfs_stop_id`, or `gtfs_stop_sequence`; observation-source identity remains a
separate realtime concern. Runtime source-trip mappings may have multiple dated candidates.
Operating date and optional exact scheduled start/end, source route, direction, endpoints,
block ID, and call-pattern digest must reduce them to exactly one before realtime is
accepted. Missing optional context is unknown; supplied contradictory context rejects a candidate.
