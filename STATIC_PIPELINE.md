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
closed package trees. The release loader (`obehy release load`, `src/obehy/release/`) mirrors the
packages into PostgreSQL. MOTIS shape generation will be inserted between compilation and final
validation when it is built.

Memory budgets are soft admission/spill targets, with no process or .NET heap hard limit. See
`PROGRESS.md` for measured results, acceptance and the actual verification state.

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

Reviewed Czech data lives in the same checkout. `routes/transport-modes.csv` and
`routes/presentation.csv` go to `jdf-to-bundle` (`--transport-mode-rules`,
`--route-presentation-rules`); the presentation rules go to `regional-gtfs-overlay` as well, which
applies them last so they win over regional feed values. The overlay reads its policy override
CSVs from `overlay/` (`--overrides-root`), and the filtered JDF feed reads
`filtered-jdf/rules-v1.json`.

## Production package

JrUtil's normative contract is `jrutil/docs/PRODUCTION_CONTRACT.md` with
`contracts/serving-v5.json` (bundle version 3, serving schema 5.0), vendored as
`src/obehy/data/serving/serving-v5.json`. Oběhy accepts every minor of serving major 5 and
nothing else.

```text
package/
├── gtfs.zip
├── serving/
│   ├── agency.parquet               location.parquet          route.parquet
│   ├── service_calendar.parquet     service_exception.parquet
│   ├── trip.parquet                 trip_call.parquet
│   ├── route_stop.parquet           route_stop_zone.parquet   call_zone.parquet
│   ├── shape.parquet                shape_point.parquet       transfer.parquet
│   ├── service_note.parquet         assignment.parquet
│   ├── connection_claim.parquet     travel_restriction.parquet
│   └── source_key.parquet           call_key.parquet
├── manifest.json
└── diagnostics.json
```

`gtfs.zip` is a pure projection of the relations; transfer waiting limits live in `transfer`.
`trip_call` is one call sequence per trip, including CZPTT railway points passed without stopping
(`passenger_service = false`). Zones are codes: a route stop slot holds them in `route_stop_zone`
when all its calls agree, otherwise the calls carry them in `call_zone`. `route_stop` is the
merged, ordered stop list of each route direction (one slot per visit), and every
`trip_call.route_stop_id` points at its slot, so line timetables need no pattern merging in Oběhy.
`source_key` and `call_key` map source identifiers in documented namespaces (`cis:line_trip`,
`czptt:tr`, `pid:gtfs_trip_id`, …) to public IDs; realtime resolves through them. Each relation
has a fixed schema, Snappy compression, a unique primary key and resolving foreign keys; every ID
carries its feed prefix. Rows are in deterministic generation order and are not sorted. Rows keep
only `source_object_id`; source IDs and snapshot digests are in the manifest.

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
