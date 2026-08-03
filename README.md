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
`osmium tags-filter` to create two cached node-only PBFs under `workdir/osm`: railway locations
for CZPTT and the bus/tram/public-transport stop tags consumed by JrUtil's JDF matcher. The JDF
extract does not need municipality boundaries: JrUtil enriches its stop coordinates from its
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
memory-derived worker cap; the requested and resolved worker plans are shown in progress
and recorded in `run-manifest.json`. Merged JDF packaging defaults to deterministic balanced
Deflate (`--zip-compression=balanced`); `fast` and `small` select levels 1 and 9.

The builder writes fixed work batches as uncompressed ZIPs to reduce temporary file count.
The builder does not enable JrUtil's experimental persistent cache.

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

Internal timing points with real coordinates are included as non-boardable/non-alightable GTFS
rows by default. Use `--operational-points sidecar` for compatibility with consumers that display
such points as normal stops. Synthesized fallback route labels use municipalities rather than
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
