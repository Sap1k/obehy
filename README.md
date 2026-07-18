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
