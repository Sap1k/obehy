# Oběhy

A Swiss-army knife for Czech public-transport operations and realtime data.

Oběhy builds two coordinated nationwide GTFS feeds (`jdf` for road and urban transport, `czptt`
for rail) with JrUtil, runs those builds on GitHub Actions, and will load each published release
into a PostgreSQL mirror on the application server, which also runs the realtime core: trip
inference from sources of very different quality, delay estimation, learned vehicle circulations
(*oběhy*), per-feed GTFS-RT and the project API. JrUtil is the only static compiler; stable
public IDs come from its identity rules and the reviewed registry in `jrunify-ext-geodata`.
PostgreSQL is never the static compiler.

See [BASE_PLAN.md](BASE_PLAN.md) for the architecture, [STATIC_PIPELINE.md](STATIC_PIPELINE.md)
for the static boundary, and [PROGRESS.md](PROGRESS.md) for the current state and next steps.

## Development

Requirements: Python 3.13 and [uv](https://docs.astral.sh/uv/). The shared OSM
builder requires the native `osmium-tool` command. On Windows it automatically uses `osmium`
from the default WSL distribution when no native executable is on `PATH`.

```powershell
uv sync
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run pyright
```

## Machine-local configuration and shared OSM

Copy `config/obehy.example.toml` to the gitignored `config/obehy.local.toml` and set absolute
paths for the work directory, active merged OSM PBF, JrUnify-Ext-GeoData checkout, and either a
JrUtil checkout or an executable command. Every national command accepts `--config PATH`;
there is no sibling-checkout or parent-directory fallback.

## Recording realtime payloads

`obehy rt record` polls the realtime sources (DÚK, SŽ, Arriva Express) and archives their
payloads, unprocessed, for later replay. Channels are defined in
`src/obehy/data/realtime/sources.toml`. Recording needs no `obehy.local.toml`:

```bash
uv run obehy rt record --once                      # one poll per channel, smoke test
nohup uv run obehy rt record --archive data/rt-raw > rt-record.log 2>&1 &
```

`--sources duk,sz-mapa` limits the sources and `--duration 2d` stops after a set time. The
recorder stops cleanly on SIGINT/SIGTERM. A restart appends to the same archive.

## Production feed pair

The main CLI freezes the current national and regional inputs, builds JDF and CZPTT sequentially,
applies PID and IDS JMK to JDF in one overlay pass, validates both JrUtil production packages, and
publishes the pair atomically:

```powershell
uv run obehy build
uv run obehy build --estimated-posts
uv run obehy build --refresh-osm
```

Two complete live builds have been published (2026-10-02 and 2026-10-03, about 40 minutes
each); GTFS validator and MOTIS acceptance checks are still to run. Memory budgets are soft
admission/spill targets; no process or .NET heap hard limit is configured. Measurements and
remaining limitations are recorded in `PROGRESS.md`.

Prepared OSM is the default. `--refresh-osm` updates it before source downloads. Every network
source (national JDF, CZPTT, PID and IDS JMK GTFS) is then fetched up front, with three attempts
each, into `workdir/runs/production/<run-id>/sources` (`fetch-log.json` records every attempt);
an unreachable source fails the run before any conversion. The command writes
the two consumer packages to `artifact_root/releases/<run-id>/jdf` and `czptt`. It also writes a
filtered JDF GTFS to `jdf-filtered/gtfs.zip`, following
[gtfs-processor](https://github.com/0xaa55h/gtfs-processor): it is the pre-overlay national JDF
without the lines of FlixBus, PMDP and DPMO (as operator or alternative operator) and of PID,
IDS JMK and IDZK (preferred `LinExt.txt` row), read from the merged national JDF, and without the
line-number prefixes in `jrunify-ext-geodata/filtered-jdf/rules-v1.json`. It also drops
calls at stops without coordinates. Customs stops (JDF fixed code `$`) are made non-boardable
earlier, by JrUtil, in every JDF output. Use
`--skip-filtered-jdf` to omit it. The command then switches
`artifact_root/current.json`. Failed runs leave the previous pointer unchanged and retain their
logs, source descriptors, detailed diagnostics and failure report under
`workdir/runs/production/<run-id>`.

The production PID + IDS JMK policy preserves the calibrated matching behavior and reports all
coverage metrics, with explicit zero floors for this milestone. Both regional snapshots are
required. The internal enrichment boundary currently passes packages through unchanged; future
shape generation belongs there, after regional shapes have been selected and before final package
validation.

Build the regional OSM snapshot explicitly:

```powershell
uv run obehy-osm build
uv run obehy-osm build --verify
```

The command tracks the Geofabrik MD5 sidecars for Czechia, Austria, Bavaria, Saxony, Slovakia,
Dolnośląskie, Opolskie, and Śląskie. Source extracts are cached under `workdir/osm/extracts`.
Their hashes, the Osmium identity, and the active output identity are recorded in the manifest
next to the configured `osm_file`; matching inputs and output safely skip regeneration. There
are no historical merged copies or hard-link publication. The same command uses native
`osmium tags-filter` to create a node-only railway-location PBF for CZPTT and a versioned
`jdf-transit-geometry.osm.pbf` for JDF. The latter contains stop/platform nodes, referenced nodes
for bus-usable road ways, and tram ways, including access and direction tags; it intentionally
excludes metro, funicular, ferry, and general railway geometry. The JDF extract does not need
municipality boundaries: JrUtil enriches its stop coordinates from its
separate bundled Czech municipality index. No Python code parses or transforms OSM objects. JDF
and CZPTT only consume and validate these artifacts; they never download, merge, or filter OSM.
Downloads, cache decisions, native merging/filtering, hashing, and publication all report progress.

## National JDF conversion bundle

The national raw-input builder uses the separately checked-out root-level JrUtil fork and pinned
external geodata. It downloads the current CIS JŘ VLD and municipal-dráhy archives, combines the
nested archives under deterministic `vld-`/`drahy-` staging
names, fixes the national batch set in one OSM/geodata pass, merges stops by name, and writes an
immutable GTFS-plus-Parquet bundle:

```powershell
uv run obehy-national-jdf build --output C:\data\obehy-national-jdf
```

The output path must not exist. Runtime paths come only from the machine-local configuration.
`--keep-work` retains staged source batches, fixed batch ZIPs, and merged intermediates after a successful build. Failed
runs always retain their staging directory, raw process logs, partial downloads and
`logs/failure.json` for diagnosis. `--progress auto` uses Rich on an interactive terminal and
periodic text when redirected; `rich`, `plain`, and `off` can be selected explicitly. Progress is
written to stderr.

Use `--jobs=auto|N` to configure both parallel JrUtil stages, with `--fix-jobs` and
`--merge-jobs` as optional stage overrides. `--memory-budget=auto|SIZE` controls the
adaptive admission budget. Auto aggressively oversubscribes logical CPUs but derives its memory
ceiling from current process use, available RAM, and a bounded evictable-memory allowance on hosts
with at most 20 GiB, with explicit operating-system headroom. Numeric values bound scheduler
admission; they are not process or .NET heap hard limits.
The fix-stage snapshot is taken after the persistent stop index is loaded. Live
worker/CPU/memory/backlog samples and observed
peak concurrency are shown in progress and recorded in `run-manifest.json`. Merged JDF packaging defaults to deterministic balanced
Deflate (`--zip-compression=balanced`); `fast` and `small` select levels 1 and 9.

The top-level `obehy build` forwards the same jobs and memory-budget settings to JDF, the regional
overlay, and CZPTT. The budget is a soft admission/spill target, not a hard .NET heap limit.

The builder writes fixed work batches as uncompressed ZIPs to reduce temporary file count.

With estimated posts, routed evidence is reused across runs from `workdir/cache/routing`
(override with `[paths] routing_cache_dir`; disable with `--no-routing-cache`). JrUtil keys each
routed context by its exact coordinates and reuses it only while the routing-graph tiles it read are
unchanged, so daily demand-clip changes and monthly OSM updates recompute only the affected contexts
and reused results are byte-identical to routing them again. Changing JrUtil's routing code
invalidates the whole cache; entries unused for 45 days are dropped. No manual pruning is needed.

Estimated posts remain default-off for national builds. Pass `--estimated-posts` to construct the
Osmium demand clip and enable directed road/tram routing. Oběhy passes the packaged learned scorer
(`src/obehy/data/post-inference/learned-v1.json`) unless `--post-inference-policy=FILE` names
another. Authored identities and labels remain unchanged. A decision is one physical candidate, the
most probable post of a confident area, or the parent centroid; coordinates are never averaged.
`JrutilPostCandidateEvidence.txt` and `JrutilRoutingDemands.txt` survive deterministic JDF round
trips and merge. The complete rollback is the default build or JrUtil's explicit
`--no-estimated-posts`.

For retraining, `obehy-national-jdf build --capture-post-inference-evidence` publishes a
policy-neutral evidence-v2 pack and run manifest instead of a bundle (Oběhy supplies JrUtil's
internal `--post-inference-evidence-only` switch). Capture cannot be combined with a policy.
`jrutil/scripts/post-scorer` turns region captures into training data and a new `learned-vN.json`.

JrUtil verifies the exact relation set, hashes, sizes, Parquet schemas and row counts, capture
ceilings, router and capture-tool versions, schema fingerprints, and the pack ID repeated in every
relation, plus canonical ordering, memberships, foreign keys, contiguous variants, sentinels,
numeric ranges, movement-family/block identity, and complete attachment coverage, before atomically
publishing the evidence directory. Oběhy trusts that command boundary and reads only the published
manifest; the run manifest records its hash and a summary with capture estimates, atomic-output
headroom, current and peak spill bytes, and the requested worker ceiling. The capture disk
preflight uses canonical deduplicated route-pattern contexts, not raw timetable-call count, and
charges one temporary evidence pack plus a fixed reserve; activation is a same-volume directory
rename rather than a second full copy.

## National CZPTT conversion bundle

The national railway builder snapshots the selected GVD annual CZPTT archive, every discovered
monthly change object, KADR dictionaries, and SR70 data; converts them with the separately
checked-out JrUtil fork; and atomically publishes a production package (GTFS plus operational and
IDS serving relations):

```powershell
uv run obehy-national-czptt build --output C:\data\obehy-national-czptt
```

Known, valid, unambiguous SŽ SR70 coordinates are authoritative. OSM fills only missing,
invalid, or conflicting SR70 identities; an OSM disagreement is diagnosed while SR70 remains
unchanged. CZPTT reads only tagged station/halt/stop nodes—never ways, relations, or station
geometry. Candidate lookup is indexed by PLC/object/name. Name matching is deliberately eager:
normalized exact names, railway suffix/qualifier-stripped names, and then close fuzzy names are
matched globally. Fuzzy matching may not discard distinguishing locality/direction tokens such as
`Ost`, `West`, `Nord`, `Süd`, `Mitte`, or their Czech/Slovak/Polish equivalents. An OSM country tag
ranks otherwise equivalent candidates but never excludes a name match. A candidate is rejected
only when every usable timetable occurrence makes it impossible at 150 km/h plus 2 km slack; the
converter then tries the next match method. Missing passenger locations are estimated from the
locally densest real-coordinate service occurrence. Pure timing points are never estimated: when
they have no SR70/OSM coordinate, they remain in operational Parquet but are omitted from GTFS.

By default, internal timing points appear only in the operational Parquet sidecars. Use
`--operational-points gtfs` (or `obehy build --czptt-operational-points gtfs`) to also emit those
with real coordinates as non-boardable/non-alightable GTFS rows. Synthesized fallback route labels use municipalities rather than
station/facility names. See [NATIONAL_CZPTT.md](NATIONAL_CZPTT.md) for source snapshots, GVD year
selection, bundle schemas, line changes, platform handling, IDS zones, and diagnostics.

## Serving database

There is no database code yet. JrUtil writes `jrutil-production` packages (bundle version 3,
serving schema version 4; see `STATIC_PIPELINE.md`). The release loader and realtime core described
in `BASE_PLAN.md` sections 16 and 18–23 are the next work. `JDF_SEMANTICS.md` records the JDF
preservation gaps that block calling GTFS plus the current sidecars a lossless semantic export.
