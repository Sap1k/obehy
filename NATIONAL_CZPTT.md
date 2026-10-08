# National CZPTT conversion bundle

`obehy-national-czptt` builds a reproducible national railway conversion snapshot without
importing it into the canonical database.

## Build

```powershell
uv run obehy-national-czptt build `
  --config C:\src\obehy\config\obehy.local.toml `
  --output C:\data\obehy-national-czptt `
  --timetable-year auto `
  --operational-points sidecar
```

The output path must not exist. `auto` changes to GVD year `Y` at midnight in
`Europe/Prague` on the second Sunday of December in `Y-1`; for example, GVD 2026 began on
14 December 2025. The default source is
`https://portal.cisjr.cz/pub/draha/celostatni/szdc`. Another HTTP(S) mirror or local test server
can be selected with `--source-base-url`. FTP URLs are rejected with guidance to use the official
HTTPS mirror because the FTP directory hierarchy is prohibitively slow to inventory.

Run `uv run obehy-osm build` separately before CZPTT. In addition to the shared regional PBF,
that command uses native `osmium tags-filter` to maintain a cached node-only extract of tagged
railway stations, halts, and stops under `workdir/osm`. Python never parses OSM objects. The
railway builder validates and records both artifacts but never downloads, merges, or filters OSM.

`--source-snapshot PATH` copies and validates an existing `sources/` snapshot and performs no
network requests. The snapshot must contain `sources.json`, the annual/monthly objects named by
that manifest, `kadr/catalog.json`, and `sr70/SR70.csv`. JrUtil, JrUnify-Ext-GeoData, the work directory, and the shared merged
OSM PBF are mandatory absolute paths in the selected TOML configuration. There is no
parent-directory fallback. `--sr70` selects a coordinate snapshot used only while creating a new live
snapshot.
`--jobs auto` means eight download workers.

Discovery reads monthly HTTP directory listings concurrently and makes no per-object metadata
requests. The downloaded bytes and their SHA-256 hashes form the authoritative snapshot. Downloads
stream through eight persistent HTTP connections with bounded in-flight work and file-count
progress. After each download pass, the build rechecks the inventory and downloads newly listed
objects. It stops when a recheck is stable, with at most five download passes total. The final
inventory and all downloaded objects are recorded in the snapshot. The build fails if the listing
still grows after the fifth pass, or if a discovered object disappears, cannot be downloaded
completely, has the wrong ZIP/gzip magic, contains a corrupt ZIP member, or decompresses to
malformed/unsupported XML.

## Operational timing points

`--operational-points sidecar` is the default (also in `obehy build`); see below.
`--operational-points gtfs` publishes non-passenger locations between the
first and last activity-`0001` call as ordinary GTFS stops with exact source times,
`pickup_type=1`, `drop_off_type=1`, and `timepoint=1`, but only when the location has a real SR70
or OSM coordinate. A purely timing/operational identity is never route-estimated. If it has no
real coordinate, its GTFS stop-time, child, parent, zone projection, and source-call projection
are omitted. The source point and call remain in operational Parquet. Locations before the first
passenger call or after the last one remain sidecar-only.

`--operational-points sidecar` keeps every non-passenger location out of GTFS. This is useful for
consumers that display all stops indiscriminately. A friendly-line change at an internal point
then moves to the following passenger call in GTFS and is recorded as an approximation.

Both modes retain the complete accepted PA route in operational Parquet. A PA without any
activity-`0001` call is never published. A PA is rejected in full if a departure precedes its
arrival, a later source event precedes the previous event, or `CZInconsistentTime=1` is present.
Times with source day offsets remain valid above 24:00.

Every primary location produces one `location_type=1` station parent. A location without
subsidiary data uses an `:unspecified` child; a subsidiary location uses a `:platform:<code>`
child. `stop_times.txt` always references a child. Parent and child names remain identical to the
source station name; only `platform_code` contains the non-empty passenger-facing subsidiary
name, falling back to the raw subsidiary code. Consecutive subsidiary-only duplicates are
collapsed in GTFS while their raw rows remain in Parquet.

Coordinates use a strict authority order:

1. a valid, unambiguous SŽ SR70 coordinate for `(CZ, primary code)`;
2. exact OSM `ref:EU:PLC=<country><five-digit-code>`;
3. a reviewed source-identity-to-OSM-object alias;
4. a global normalized exact railway-location name;
5. a global match after removing operational suffixes and parenthesized qualifiers;
6. a close global fuzzy railway-location name;
7. a timetable-based coordinate estimate when every OSM method is absent or impossible.

SR70 is always retained when available. OSM never overwrites, averages, adjusts, or “improves”
it—even when OSM is closer to another candidate or has a different tag. An OSM/SR70 disagreement
records the OSM object, distance, and `retainedSource=sz_sr70`. OSM is considered only for an
absent, invalid, or internally conflicting SR70 identity. `uic_ref` and `railway:ref` are never
treated as Primary Location Codes. Neighboring timetable calls can veto a name match but cannot
override SR70. OSM candidates are actual `railway=station|halt|stop` node coordinates only; ways,
relations, unrelated public-transport stop positions, and geometry are discarded. Country tags
are ranking hints, not eligibility gates. A name candidate is accepted when at least one usable
timetable occurrence is plausible. It is vetoed only when every occurrence with timed selected
neighbors would require more than 150 km/h plus 2 km slack; this prevents one anomalous source
timing from suppressing an otherwise clear match. Rejection falls through to the next OSM method.
If none survives, only passenger-referenced identities are eligible for estimation. Every timed
occurrence is ranked deterministically: two-sided real passenger anchors first, then the fewest
source-call positions, shortest geographic anchor span, greatest active service-day count, and PA
identity/sequence. Only the winning occurrence is used; estimates are never averaged or reused as
anchors. Time interpolation is used between two anchors. If no passenger-anchor candidate exists,
real operational anchors are considered; an open route end retains the 300 m-per-call northward
fallback. Estimated station parents and children receive one ` [?]` suffix. Raw Parquet names,
headsigns, and route labels remain unmarked.

Fuzzy OSM matching retains distinguishing source-side direction/locality tokens. In particular,
`Bad Schandau Ost` cannot match an OSM candidate named only `Bad Schandau`; the same rule covers
`West/Nord/Süd/Mitte`, Czech/Slovak east-west-north-south/centre terms, and the corresponding
Polish tokens.

Location-level `CZPassengerServiceNumber` records describe the onward section. When any such
records exist in a PA, an absent or blank value clears the active line rather than carrying the
previous line to the terminus. A root-level value is used only for PAs with no location-level line
records. Both line activation and line expiry create linked GTFS segments at the shared boundary.
Activation remains at the location where the new line first appears. For `line → no line`, the
shared boundary is one source point earlier, because the train already ceases presenting that
line at the last lined call when its onward section leaves the line/IDS. If the raw expiry occurs
at an internal point omitted in `sidecar` mode, the boundary is the preceding passenger call and
the approximation is diagnosed.

## Bundle layout

```text
sources/
  inventory.json
  sources.json
  annual/JRYYYY.zip
  changes/YYYY-MM/*.xml.zip
  kadr/*.xml
  kadr/catalog.json
  sr70/SR70.csv
derived/messages.zip
bundle/
  gtfs.zip
  serving/<relation>.parquet
  diagnostics.json
  manifest.json
diagnostics-detail/
run-manifest.json
manifest.json
```

`derived/messages.zip` uses fixed entry names, timestamps, permissions, ordering, and Deflate
settings. Annual messages precede changes; changes are ordered by their source
`CZPTTCreation`/`CZPTTCancelation` timestamp, with additions before cancellations only when the
timestamps are equal. This ensures that a cancellation published later in the same month is
applied after its timetable instead of accidentally resurrecting the timetable. Exact duplicate
payloads are removed. Conflicting timetables with the same complete
object-type/company/core/variant/year PA identity fail the build.

`bundle/` is a JrUtil production package (bundle version 2, serving schema version 3).
`gtfs.zip` contains standard GTFS only and is what the official GTFS validator checks. The
converter's Czech tables (`cz_routes`, `cz_trips`, `cz_trip_stop_zones`) and source metadata are
compiler-internal: they are projected into the serving relations and are not written to the
package. Friendly KADR `Znacka` values such as `S1` and
`U32` are route short names. Route/operator/mode changes split one PA into linked trips sharing a
PA block; category changes do the same, and `transfers.txt` uses linked-trip `transfer_type=4`.
Every segment keeps the complete PA's final passenger call as its headsign.

When no dated KADR line exists, services are grouped by operator, category, mode, and the
unordered pair of first/last passenger calls. Reverse directions and differing intermediate
patterns therefore share a route. Its `route_short_name` is a combined label such as
`RJ Praha – Břeclav`; `route_long_name` is empty. The display direction is the direction
with the greatest summed active service days, with full PA identity as the deterministic tie
break. Standard `trip_short_name` combines category and train number, such as `Os 3456`, while
the compiler-internal `cz_trips` table retains the raw train number.

Fallback identities and labels always use the first and last passenger calls of the complete PA,
including when only a prefix or suffix lacks a KADR line. Endpoint labels are derived from the
source municipality, not the station/facility name. Reviewed major-city families collapse their
district stations to the city root, while genuine compound municipalities remain intact. Terminal
railway qualifiers are removed conservatively, and each endpoint keeps the deterministic
20-character cap and municipality-safe abbreviations such as `…stadt → …st.`. Thus
`Os Karlovy Vary dol.n. – Johanngeorgenstadt` becomes
`Os Karlovy Vary – Johanngeorgenst.`. Raw GTFS stop names, trip headsigns, and KADR names remain
unchanged.

Routes use white text and one shared palette by passenger-facing service class:

- InterCity (`IC`, `EC`, `RJ`, `rj`, `LE`, `SC`, `AEx`): `b91c1c`;
- fast (`R`, `Ex`, `Rx`): `b45309`;
- regional (`Os`, `Sp`, `TL`, `TLX`, `LET`): `1c1745`;
- night (`NJ`, `EN`, `ES`): `4c1d95`;
- other/unknown rail: `475569`.

Generated IDs use the same colon-separated convention as the JDF feed:

- `czptt:agency:…`, `czptt:route:line:…`, and `czptt:route:fallback:…`;
- `czptt:trip:…`, `czptt:service:…`, and `czptt:block:…`;
- `czptt:stop:<country>:<primary-code>` with `:unspecified` or
  `:platform:<subsidiary-code>` children.

The compiler-internal CZPTT source metadata retains only source facts and typed projection
bridges:

- `operational_points`: source location identity/name plus optional coordinate, source object, and
  match method (`sz_sr70/country_primary_code`, `osm/ref_eu_plc`, or
  `osm/normalized_exact_name`);
- `operational_calls`: source PA/sequence/location, passenger flag, source arrival/departure
  seconds, subsidiary evidence, and active line code;
- `source_call_metadata`: typed GTFS trip/stop-sequence to source PA/sequence projection;
- `source_note_metadata`: lossless central, non-central, and note-calendar values, resolution state,
  validity bounds, and generated-trip projection;
- `source_feature_metadata`: typed trip/call features linked to their source note where applicable;
- `source_ids_coverage_metadata`: normalized accepted `CZIPTS`/`CZCalendarIPTS` source facts;
- `source_ids_coverage_trip_metadata`: typed coverage-to-overlapping-GTFS-trip projection.

PA/TR identities reach the package as `source_trip_map` bindings; there is no trip mirror
relation or pipe-delimited generated-trip list. Split boundaries produce multiple projection rows
and sidecar-only calls produce none.

The production package schema is unchanged: the finalizer maps the two note/feature bridges into
the existing `service_note`, `service_note_assignment`, and `service_feature_assignment`
relations. Central note codes `17` and `34` retain wheelchair capability plus their recommended or
required booking distinction. Codes `22` and `26`–`29` retain positive bicycle carriage,
carry-on/storage, and reservation distinctions; code `36` is explicit bicycle prohibition.
Activity `0030` remains standard GTFS request-stop pickup/drop-off type `3` and also becomes a
call-scoped `on_request` feature. Embark-only and disembark-only restrictions still take precedence
on the forbidden side of the call.

Train-equipment note semantics are not projected onto generated rail-replacement (`NAD`) trips;
the raw notes remain attached to their CZPTT source and explicit call activities such as `0030`
still apply. Because the pinned MOTIS/Nigiri importer ignores trip specificity for non-stay-seated
transfers and otherwise applies
its configured default transfer time, each boundary is emitted as a deduplicated stop-pair
`transfer_type=2` row with a zero minimum time. This explicitly overrides that default without
adding time. The stop-scoped rule remains bounded by the synthetic `BUS` boarding point, which is
served only by replacement transport. Such a transfer is emitted only when both public endpoints
have the same country and primary location code. If removing an operational boundary point leaves
rail and NAD trips at different public stations, both trips remain available but no walking
transfer is invented; the omitted cross-station link is recorded in diagnostics.

GTFS-only split edges without source timing first inherit a timed counterpart at the same primary
location and otherwise use sequence-linear interpolation only when bracketed by known PA times.
Inferred edges carry `timepoint=0`; `operational_calls` retains the original nullable source times.
A generated journey whose edge remains untimed is omitted and diagnosed. Public agency display
names omit catalog qualifiers beginning with a spaced dash (` - `), without changing agency IDs or
hyphenated legal names.

GTFS trip flags are deliberately conservative. Wheelchair capability is emitted as
`wheelchair_accessible=1`, positive bicycle carriage as `bikes_allowed=1`, and code `36` as
`bikes_allowed=2` only when the corresponding note covers the complete generated trip on every
active service date. Partial, calendar-limited, unresolved, or contradictory positive/negative
bicycle claims remain unknown and are retained in the semantic relations and diagnostics. Missing
accessibility never becomes `wheelchair_accessible=2`; code `17` is not presented as a separate
low-floor guarantee because its source meaning also permits a lift-equipped vehicle.

Every KADR IDS entry remains represented as coverage metadata. Entries whose catalog note
explicitly identifies a fare band (for example `PID pásmo P`) additionally produce catalog-resolved
trip/stop zone memberships in the serving zone relations. Standard `stops.zone_id` is populated only
where all generated memberships for that stop agree on one zone.

`diagnostics.json` schema v1 records rejected journeys, unresolved IDS intervals, unknown
cancellation targets, sidecar boundary approximations, resolved coordinate counts, unresolved
points grouped by country, authoritative SR70 resolutions, OSM gap fills, SR70/OSM disagreements,
invalid/conflicting SR70 identities, route estimates, ambiguous candidates, and speed/corridor
vetoes.
`run-manifest.json` records the selected GVD, operational-point mode, flattened-message digest,
source-manifest digest, configured JrUtil provenance, shared OSM source key, and individual SR70
and `Název 20` digests.
`manifest.json` hashes every published file. Failed builds retain their staging directory and
`failure.json`; successful builds publish atomically.

The synthetic feeds for both operational-point modes are checked locally with MobilityData GTFS
Validator `v8.0.1` (CLI JAR SHA-256
`19293ddd9b6f954f216d4f12054bd8a3232921751c4484339e339764a91000e2`).

The 2026-08-01 full GTFS-mode replay (`czptt-aug-v1`) processed 135,433 messages and
resolved 3,483 locations: 3,415 from authoritative SR70 and 68 from OSM. It produced no route
estimates because every passenger-referenced location had a real coordinate. Four unresolved
timing-only identities (`DE:10535`, `DE:25606`, `PL:07397`, and `PL:63752`) remain in operational
Parquet and diagnostics but have no row in `stops.txt` or `stop_times.txt`. Bundle foreign-key,
foreign-passenger-coordinate, and exact SR70-coordinate checks passed during atomic publication;
no known SR70 coordinate was replaced by OSM. A final direct regeneration with the same frozen
messages/SR70/OSM inputs passed MobilityData v8.0.1 with all 40 agencies, 9,178 stops, 47,098
trips, and 952,854 stop times parsed and zero `ERROR` notices. Negative source-day offsets are
shifted by whole days only for GTFS, schemeless KADR operator URLs are normalized, and source
calls without times use `timepoint=0`; raw operational timing values remain unchanged.

Bundles produced before this CZPTT v1 contract are unsupported inspection artifacts; readers do
not receive an implicit compatibility path for the provisional mirror schemas.
