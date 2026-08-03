# Oběhy static-pipeline contract

`BASE_PLAN.md` is authoritative. This document fixes the executable boundary between Oběhy,
JrUtil, the serving database, and the future public identity registry.

## Ownership and current build protocol

1. Oběhy downloads each configured static source, stores it immutably by SHA-256, and exports a
   versioned, secret-free build specification.
2. JrUtil validates/converts those snapshots, performs national compilation and regional/operator
   overlays, and writes deterministic GTFS plus the finalized serving package.
3. During the pre-registry phase JrUtil assigns opaque deterministic `v0:<kind>:<digest>` IDs from
   compiler-local normalized identity seeds. They are explicitly provisional and are guaranteed
   only to repeat for identical inputs.
4. Oběhy validates every package byte and relation before database work, streams sorted relations
   into isolated per-build tables, validates them set-wise, and attaches the complete partition set.
5. One `control.publication` transaction activates the GTFS artifact, static mirror, source
   mappings, and realtime resolver version together.

The compiler never reads or mutates Oběhy serving tables. The loader never performs identity
matching, trip collapse, overlay precedence, fuzzy matching, or static claim arbitration.

After one PID-overlay build and one PID realtime entity work end to end, the registry is built in a
separate repository. JrUtil then resumes the discovery/proposal/snapshot protocol described in
`IDENTITY_REGISTRY.md`, emits `identity_contract = "registry-v1"`, and makes the single declared
breaking transition away from provisional IDs. Oběhy needs no schema migration because every
public ID is unrestricted text.

## Commands and build identity

Current provisional compilation:

```text
jrutil-multitool static-compile <build-spec.json> --identity-mode provisional-v0 <output-root>
```

Later registry-backed compilation:

```text
jrutil-multitool static-discover <build-spec.json> <proposal-output>
jrutil-multitool static-compile <build-spec.json> <registry-snapshot> <output-root>
```

The build specification pins its schema, ordered source manifests, overlay-policy digest, JrUtil
identity, compiler options, resource limits and deterministic options. Registry fields are nullable
only for `provisional-v0`. Live source URLs and credentials never enter the specification.

## Serving-package v1

```text
build/
├── gtfs.zip
├── extensions/
├── serving/
│   ├── agency.parquet
│   ├── location.parquet
│   ├── route.parquet
│   ├── service_calendar.parquet
│   ├── service_exception.parquet
│   ├── trip.parquet
│   ├── trip_call.parquet
│   ├── shape.parquet
│   ├── shape_point.parquet
│   ├── route_segment.parquet
│   ├── transfer.parquet
│   ├── fare_system.parquet
│   ├── fare_zone.parquet
│   ├── location_zone.parquet
│   ├── call_zone.parquet
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
│   ├── identifier_alias.parquet
│   ├── road_route_key.parquet
│   ├── road_trip_key.parquet
│   ├── rail_trip_key.parquet
│   └── selected_field_provenance.parquet
├── diagnostics.json
├── validation/
└── manifest.json
```

`src/obehy/serving.py` is the executable schema contract. Each Parquet file has a fixed Arrow
schema/nullability contract, Snappy compression, `obehy.schema_version` and `obehy.relation`
metadata, and strictly increasing primary sort keys. The canonical manifest records each relation's
schema, sort key, row count, byte size and SHA-256 plus the aggregate serving digest.

The manifest also pins build-spec, ordered-source-set, overlay-policy, compiler, compiler-options,
GTFS, extensions, diagnostics and validation digests; compiler identity; feed version; identity
contract; and a nullable registry snapshot digest. Identical inputs must produce identical semantic
output.

`JDF_SEMANTICS.md` is the normative preservation addendum. The current JrUtil GTFS conversion is
not lossless for JDF fixed codes: it collapses accessibility detail, can only approximate
order/conditional service in GTFS, and drops luggage,
reservation, stop facilities, and interchange hints. Serving-package v1 therefore requires typed
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
block/run/duty IDs, and call-pattern digest must reduce them to exactly one before realtime is
accepted. Missing optional context is unknown; supplied contradictory context rejects a candidate.
