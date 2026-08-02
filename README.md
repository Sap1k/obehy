# Oběhy

A Swiss-army knife for Czech public-transport data.

Milestone 0 defines the project-owned canonical identity, stop/location and scheduled-trip
contracts. It deliberately uses synthetic fixtures and does not yet publish GTFS or consume live
sources.

See [PROGRESS.md](PROGRESS.md) for the current engineering handoff and next implementation step.

## Development

Requirements: Python 3.13, [uv](https://docs.astral.sh/uv/), Docker with Compose. The shared OSM
builder requires the native `osmium-tool` command. On Windows it automatically uses `osmium`
from the default WSL distribution when no native executable is on `PATH`.

```powershell
uv sync
docker compose up -d --wait db
$env:OBEHY_DATABASE_URL = "postgresql+psycopg://obehy:obehy-m0-local-only@localhost:45873/obehy_test"
$env:OBEHY_TEST_DATABASE_URL = $env:OBEHY_DATABASE_URL
uv run alembic upgrade head
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run pyright
```

The database is exposed on port `45873` to avoid colliding with a local PostgreSQL installation.
The default credentials are development-only.

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
