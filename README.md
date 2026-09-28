# Oběhy

A Swiss-army knife for Czech public-transport operations and realtime data.

Oběhy orchestrates immutable static snapshots, stores the finalized serving mirror and owns the
operational/realtime platform. JrUtil compiles the unified nationwide GTFS and static overlays. A
future standalone public registry will own permanent IDs. Until the first static-overlay and PID
realtime vertical slices are stable, JrUtil emits explicitly provisional `v0:` IDs. PostgreSQL is
never the static compiler.

See [STATIC_PIPELINE.md](STATIC_PIPELINE.md) and [IDENTITY_REGISTRY.md](IDENTITY_REGISTRY.md) for
the executable boundaries. The former PostgreSQL national compiler/importer has been removed.

See [PROGRESS.md](PROGRESS.md) for the current engineering handoff and next implementation step.

## Development

Requirements: Python 3.13, [uv](https://docs.astral.sh/uv/), Docker with Compose. The shared OSM
builder requires the native `osmium-tool` command. On Windows it automatically uses `osmium`
from the default WSL distribution when no native executable is on `PATH`.

```powershell
uv sync
docker compose up -d --wait db
$env:OBEHY_DATABASE_URL = "postgresql+psycopg://obehy:password@host:45873/obehy_test"
$env:OBEHY_TEST_DATABASE_URL = $env:OBEHY_DATABASE_URL
uv run alembic upgrade head
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run pyright
```

The Compose database is exposed on port `45873` to avoid colliding with a local PostgreSQL
installation. A repository-local `.env` may instead point at another development server and hold `OBEHY_DATABASE_URL` and
`OBEHY_TEST_DATABASE_URL`; it is ignored by Git. The `obehy_test` database is disposable and the
database-v1 baseline requires recreating any earlier Milestone 0 database.

Alembic migrations are generated from ORM metadata and then reviewed. PostgreSQL extensions,
functions, triggers and seed rows are the only hand-written migration portions. MobilityData GTFS
Validator results are retained as advisory diagnostics and do not independently block activation.

Fixture boundaries and the temporary mock CIS stop-identity assumption are documented in
`tests/fixtures/README.md`.

## Machine-local configuration and shared OSM

Copy `config/obehy.example.toml` to the gitignored `config/obehy.local.toml` and set absolute
paths for the work directory, active merged OSM PBF, JrUnify-Ext-GeoData checkout, and either a
JrUtil checkout or an executable command. Every national command accepts `--config PATH`;
there is no sibling-checkout or parent-directory fallback.

## Production feed pair

The main CLI freezes the current national and regional inputs, builds JDF and CZPTT sequentially,
applies PID and IDS JMK to JDF in one overlay pass, validates both JrUtil production packages, and
publishes the pair atomically:

```powershell
uv run obehy build
uv run obehy build --estimated-posts
uv run obehy build --refresh-osm
```

National release acceptance is pending. JDF calls now use disk-backed storage and typed output
sinks, while the regional finalizer releases completed compiler, source-call, provenance and
validation phases instead of retaining them together. A replay of the previously failing national
overlay finalizer peaked at 4,373,061,632 private bytes, down from 14,130,675,712 bytes. This is
soft planning and phase reclamation only; no process or .NET heap hard limit is configured.
Measurements and remaining limitations are recorded in `PROGRESS.md`.

Prepared OSM is the default. `--refresh-osm` updates it before source downloads. The command writes
the two consumer packages to `artifact_root/releases/<run-id>/jdf` and `czptt`. It also writes a
filtered JDF GTFS to `jdf-filtered/gtfs.zip`, following
[gtfs-processor](https://github.com/0xaa55h/gtfs-processor): it is the pre-overlay national JDF
without the lines portal.radekpapez.cz lists for FlixBus, PMDP, DPMO, PID, IDS JMK and IDZK, and
without the line-number prefixes in `src/obehy/data/filtered-jdf/rules-v1.json`. It also drops
calls at stops without coordinates. Customs stops (JDF fixed code `$`) are made non-boardable
earlier, by JrUtil, in every JDF output. Use
`--skip-filtered-jdf` to omit it, or `--line-filter-snapshot PATH` to replay a saved portal
snapshot. The command then switches
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
The builder does not enable JrUtil's experimental persistent cache.

Estimated posts remain default-off for national builds. Pass `--estimated-posts` to construct the
Osmium demand clip and enable conservative directed road/tram routing; add
`--diagnostic-post-labels` independently to expose `O1`/`O-N`/`?` only through GTFS
`platform_code`. Authored identities and labels remain unchanged. A decision is one physical
candidate, a predefined compact side group represented by a real medoid, or the parent centroid;
coordinates are never averaged. `JrutilPostCandidateEvidence.txt` and
`JrutilRoutingDemands.txt` survive deterministic JDF round trips and merge. Parquet schema v7 adds
candidate evidence, side groups and distinct pattern/candidate score relations. The complete
rollback is the default build or JrUtil's explicit `--no-estimated-posts`.

For tuning, JrUtil can capture a versioned, policy-neutral evidence directory once, replay JSON
policies and deterministic grids without OSM/A*, and generate a final bundle from the selected
evidence/policy pair. The national command accepts `--post-inference-policy=policy.json` for a live
build, `--post-inference-evidence=DIR` for an evidence-backed replay, and
`--capture-post-inference-evidence` to publish an evidence-v2 pack and run manifest without writing
a bundle. Oběhy supplies JrUtil's internal `--post-inference-evidence-only` switch for that capture.
Capture is policy-neutral and cannot be combined with a policy or evidence reuse. When no policy
file is supplied, JrUtil uses the selected
`conservative-routed-v4|tuned-safe-v3|kostany-diagnostic-best-safe` policy as its compiled default.

Evidence-backed publication is the score-free replay path: Oběhy passes
`--no-post-inference-scores`, so the selected policy produces the final GTFS and assignments without
materializing diagnostic score rows (the stable score relation remains present with zero rows).
Diagnostic GTFS platform labels use final assignments and remain compatible with that score-free
path. `--post-review-stops=FILE` and the standalone review commands retain score rows for tuning and
inspection. Evidence reuse validates the merged-JDF identity and bypasses graph construction and A*.

JrUtil verifies the exact relation set, hashes, sizes, Parquet schemas and row counts, capture
ceilings, router and capture-tool versions, schema fingerprints, and the pack ID repeated in every
relation. It also validates canonical ordering, memberships, foreign keys, contiguous variants,
sentinels, numeric ranges, movement-family/block identity, and complete attachment coverage before
atomically publishing the evidence directory. Oběhy trusts that successful command boundary and reads
only the published manifest to record the evidence lock; it does not decode the national Parquet pack
a second time or reproduce policy decisions. The run manifest records capture estimates,
atomic-output headroom, current and peak spill bytes, the requested worker ceiling, and a
policy-independent lock containing the manifest hash, pack/tool/JDF/PBF identities, routing and
capture versions/ceilings, and every relation's hash, row count, byte count, and schema fingerprint.
Replay carries the same lock forward from its input pack. The capture disk preflight uses canonical
deduplicated route-pattern contexts, not raw timetable-call count, and charges one temporary evidence
pack plus a fixed reserve; activation is a same-volume directory rename rather than a second full copy.

## National CZPTT conversion bundle

The national railway builder snapshots the selected GVD annual CZPTT archive, every discovered
monthly change object, KADR dictionaries, and paired SR70/`Název 20` data; converts them with the
separately checked-out JrUtil fork; and atomically publishes GTFS plus operational/IDS Parquet
sidecars:

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
station/facility names; `SR70_Nazev20.csv` remains a checksummed provenance input but does not
affect conversion output. See [NATIONAL_CZPTT.md](NATIONAL_CZPTT.md) for source snapshots, GVD year
selection, bundle schemas, line changes, platform handling, IDS zones, and diagnostics.

## Finalized static serving database

JrUtil will write one manifested build containing GTFS, extensions, diagnostics, validations, and
33 sorted typed Parquet relations under `serving/`. `obehy.serving.validate_serving_package` verifies
the complete manifest, hashes, Arrow schemas, metadata, row counts, ordering, and aggregate digest
before database work begins.

`JDF_SEMANTICS.md` records the current JrUtil preservation gaps and the typed sidecar contract for
JDF 1.11 fixed codes, notes, connection claims, restrictions, and stop facilities. Until JrUtil
emits that contract and the NeTEx gate passes, GTFS plus the current conversion sidecars must not be
described as a lossless semantic export.

The loader streams the relations into isolated per-build tables, validates passenger/operational
calls, location hierarchy, coverage endpoints and route segments set-wise, then attaches every
`static` partition atomically. `control.publication` selects the matching static data, source
mappings, GTFS artifact, and realtime resolver version with one build ID. The active build and two
predecessors are retained for rollback.

Source-native mappings include explicit identifier namespaces and optional route, direction,
endpoint, timing, block/run/duty, and call-pattern context. This allows realtime APIs to reference
their regional GTFS identifiers even when CISLineID/CISTripID is absent, while preserving the API
that observed the claim separately from the static feed that owns the identifier.

Database v1 contains only the `control` and `static` schemas. Realtime claims and history receive
their own migrations when the PID realtime vertical slice is implemented. Database bytes are
disposable development state; immutable source and build artifacts remain on the configured
filesystem/object-style store.
