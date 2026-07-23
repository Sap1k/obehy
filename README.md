# Oběhy

A Swiss-army knife for Czech public-transport data.

Milestone 0 defines the project-owned canonical identity, stop/location and scheduled-trip
contracts. It deliberately uses synthetic fixtures and does not yet publish GTFS or consume live
sources.

See [PROGRESS.md](PROGRESS.md) for the current engineering handoff and next implementation step.

## Development

Requirements: Python 3.13, [uv](https://docs.astral.sh/uv/), Docker with Compose.

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

## National JDF conversion bundle

The national raw-input builder uses the separately checked-out root-level JrUtil fork and pinned
external geodata. It downloads the current CIS JŘ VLD and municipal-dráhy archives plus the Czech
Geofabrik OSM extract, combines the nested archives under deterministic `vld-`/`drahy-` staging
names, fixes the national batch set in one OSM/geodata pass, merges stops by name, and writes an
immutable GTFS-plus-Parquet bundle:

```powershell
uv run obehy-national-jdf build --output C:\data\obehy-national-jdf
```

The output path must not exist. By default, JrUtil is resolved at `../jrutil` and geodata at
`../jrunify-ext-geodata/other`. Use `--jrutil-root` or `--geodata-root` to override those paths.
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
