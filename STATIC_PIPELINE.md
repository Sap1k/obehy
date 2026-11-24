# Oběhy static-pipeline contract

`BASE_PLAN.md` is authoritative. This document fixes the executable boundary between Oběhy,
the jrutil-rs compiler and the serving database.

## Current two-package production command

`obehy build` is the current production entry point. In production it runs on GitHub Actions,
never on the application server. It validates the configured OSM snapshot, its extracts and the
geodata, fetches every source (national JDF, CZPTT, PID and IDS JMK GTFS), builds the compiler,
prepares the route-shapes graph, and runs one `jrutil build` that compiles both packages with the
same resolved GVD year, the overlays and the filtered road feed. It publishes exactly two
`jrutil-production` packages under one immutable release and atomically updates `current.json` only
after the compiler's validation, Oběhy's GTFS stop checks and the publication-eligibility check
pass.

Source snapshots, the build specification, process logs and detailed diagnostics remain outside
the closed package trees. The release loader (`obehy release load`, `src/obehy/release/`) mirrors
the packages into PostgreSQL. Route-shape generation is a phase of the compilation (see "Route
shapes" below). `--jobs N` sets the compiler's thread count (`RAYON_NUM_THREADS`).

See `PROGRESS.md` for measured results, acceptance and the actual verification state.

## Ownership and build protocol

1. `obehy build` (GitHub Actions) downloads each configured static source and stores it immutably
   by SHA-256.
2. The compiler validates and converts those snapshots, performs national compilation and
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

`obehy build` builds the compiler from the `[jrutil] directory` checkout
(`cargo build --release --locked -p jrutil`, or runs a configured `command`), writes the build
specification to `<run>/build.toml` and runs `jrutil build --spec <run>/build.toml
--progress-events` (`src/obehy/static_build.py`). The specification is the compiler's interface
(`docs/switch.md` and `crates/jrutil/src/spec.rs` in jrutil-rs) and names every input with an
absolute path:

- `[czptt]`: the CZPTT source snapshot as fetched (`sources.json`, `annual/`, `changes/`, `kadr/`,
  `sr70/`; the compiler orders the messages itself), the railway-location OSM extract, the GVD
  year and the output package;
- `[jdf]`: the stop catalogues (`jrunify-ext-geodata/other`), the gazetteers
  (`jrunify-ext-geodata/gazetteer`, or `[paths] gazetteer_dir`), the post-candidate OSM extract, the registry, the route rules, the GVD
  year, the reference date and the output package; `[[jdf.exports]]` the two national exports as
  downloaded;
- `[jdf.posts]` (with `--estimated-posts`): the transit-routing base extract, the post-inference
  policy, the review reports and the routing cache (omitted with `--no-routing-cache`);
- `[jdf.overlay]`: PID (with `overlay/pid-stop-overrides.csv`) and IDS JMK by the source profiles
  that ship with the compiler, and the review reports;
- `[czptt.shapes]` and `[jdf.shapes]`: the route-shapes companion, graph and cache;
- `[jdf.filtered]` (unless `--skip-filtered-jdf`): `filtered-jdf/rules-v1.json` and the
  `jdf-filtered/` output.

The compiler validates every package it writes and leaves no invalid one. Each package's manifest
names the compiler and its version and every source by ID and SHA-256; `release.json` records the
compiler checkout (`compiler`: commit, dirty state) and the retrieval times, URLs and licences.

When `jrunify-ext-geodata/registry/stops.csv` exists, the specification names the registry and the
release's `stop-registry/` directory keeps the review candidates: `stop-candidates.csv` from the
merge, `post-candidates.csv` from the post reports and `place-candidates.csv` from the overlay
reports. Live source URLs and credentials never enter compiler inputs; sources reach it as
checksum-pinned snapshots.

## Production package

The normative contract is jrutil-rs's `contracts/production-v3.json` with
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

`JDF_SEMANTICS.md` is the normative preservation addendum. The GTFS projection is
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

## Route shapes

See `BASE_PLAN.md` section 17. Route shapes are an optional phase of the compiler's
package writer, between the route-stop order and the `gtfs.zip` projection, so nothing is
repackaged:

1. Before compiling, `obehy build` filters the OSM snapshot to a routing PBF: bus-usable
   `highway` ways, every railway osr's railway profile uses, `route=ferry`, barriers and turn
   restrictions. It is cached and manifest-validated like the other OSM filters.
2. `obehy-motis-shapes extract` builds the osr graph from it (cached by PBF hash and osr commit).
3. The specification's `[czptt.shapes]` and `[jdf.shapes]` name the companion, the graph and the
   results cache. The compiler collapses trips into match-point patterns, runs
   `obehy-motis-shapes route`, and writes every shape it produced (`generation_method = motis`,
   per-call offsets in metres) into the relations of the final `jdf` (after the overlay) and
   `czptt` packages.

Every shape is generated: the overlay source profiles leave the regional feeds' shapes unused.
Shapes are best effort: a pattern osr could not route or an
unsupported mode leaves its trips unshaped with a diagnostic; a missing companion binary means
the sections are not written. Neither fails the build. Counts (patterns; shaped and unshaped by
reason; trips shaped)
are in each package's `diagnostics.json` (`route_shapes`) and copied into `release.json`.
`jdf-filtered` keeps no generated shapes for now.

The companion lives in the JrUtil repository (`route-shapes/`). JrUtil's `route-shapes.yml`
builds it and publishes a release `route-shapes-<source hash>`; the nightly build downloads the
one matching its JrUtil checkout and never compiles C++.

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
