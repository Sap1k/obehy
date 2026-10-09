# AGENTS.md

## Project identity

- The app and public-facing project name is **Oběhy**. Use the diacritic in prose and user-facing
  text; use ASCII `obehy` for repositories, packages, paths, identifiers and commands.
- `BASE_PLAN.md` is the long-term architecture document. Do not rewrite it as a side effect of
  other work; change it deliberately when an architectural decision changes.

## Current focus: the core runtime

- Read `PROGRESS.md` first. Its **Next steps** section is the working backlog: the realtime
  core in `BASE_PLAN.md` section 34 order, with static acceptance waiting on the first GitHub
  Actions build.
- Production static compilation and overlays belong to JrUtil. `obehy build` runs on GitHub
  Actions, never on the application server. On the server, Oběhy fetches, loads and activates
  releases and runs the realtime core. Do not recreate PostgreSQL source reconciliation.
- JrUtil work happens in the standalone checkout `E:/Git/obehy/jrutil`. `converters/jrutil` is a
  pinned submodule: do not edit it or advance its pointer unless the task explicitly asks.
- Stable JDF stop IDs come from the versioned stop-ID registry files applied by JrUtil. There is no
  identity service and none is planned (`BASE_PLAN.md` section 6).
- Keep generated data, source snapshots, build artifacts, credentials and local environment files
  out of version control.

## Working conventions

- Prefer small, focused changes; no infrastructure before the step that needs it.
- Preserve Czech text as UTF-8 with diacritics in public-facing names.
- Never silently guess an identity or coordinate match. Quarantine ambiguity and expose it in
  diagnostics.
- Add or update the closest tests with behavior changes, using small deterministic fixtures.
- Keep `PROGRESS.md` short. When work changes a capability, limitation, validation result or next
  step, update the matching status/next-step line and add at most a few lines to **Recent log**
  stating what changed, what was validated (including skipped checks) and what remains.

## Realtime code rules

These keep the realtime core small, testable and free of the midnight/DST bugs that sink Czech
realtime projects. `BASE_PLAN.md` sections 18–22 give the reasons.

- **Layering.** `realtime/core.py`, `infer/`, `timeline/` and `model.py` import no database,
  network or filesystem code; `sources/` (connectors) imports no `infer/` or `timeline/`. The
  import-linter contract enforces this.
- **No twin implementations.** Live operation and `obehy rt replay` run the same core. Analysis
  is an `obehy rt …` subcommand or a script that imports `obehy`; never re-implement core rules
  in a scratch script.
- **Time.** Only `realtime/times.py` converts times. Use `Instant` (aware UTC) and `ServiceTime`
  (service date + wall-clock seconds from local midnight). No naive datetimes. Key everything by service
  date, never by calendar date. An instance's date is chosen once and never re-derived.
- **IDs are opaque.** Never parse a public ID (for example the `yymmdd` in a trip ID); resolve
  through `source_key`. History is keyed by journey `(feed, key namespace, key, service date)`
  and never references `static.*`.
- **Keyed sources are literal.** No reinterpretation heuristics in the core: a key whose trip
  does not run is unmatched. Keyless inference stays in `infer/keyless/`.
- **SQL for sets, Python for the hot path.** Logic over many rows at once (index building,
  history aggregation, circulation learning, evaluation, API queries) is SQL in PostgreSQL. Only
  the per-vehicle state machine is Python.
- **Policy, not constants.** Thresholds, margins and tolerances live in versioned policy TOML
  under `src/obehy/data/`, not as module constants.
- **Connectors declare, the runtime executes.** A connector declares its channels and lookups
  and implements only `fetch`, `decode` and `plan`. Scheduling, rate limits, budgets and
  caching are the generic runtime's; the API never calls upstream.
- **Public IDs are journey keys and `vehicle_id`s**, never `trip_id`. Rail journeys are train
  number + service date, joined by `journey_link` where the number changes.
- **Core state is partitioned by feed**; nothing in memory crosses `jdf` and `czptt`.
- **Curated data is git-reviewed files + `obehy ref import`.** `ref.*` is a mirror. Private
  datasets (the DÚK register) come from a local path and are never committed.
- **Quirk ledger.** Every source quirk (a catch-22 of Czech data) gets an ID in its dossier's
  **Quirks** section (`DUK-Q3`), is normalized in the connector, and has a scenario test named
  after the ID.
- Frozen, slotted dataclasses; pyright strict for `realtime/`; modules stay under about 500
  lines; output is emitted in sorted, deterministic order; replay is golden-tested on pinned
  corpora.

## Documentation roles

- `BASE_PLAN.md`: decisions and architecture only, no status.
- `ARCHITECTURE.md`: the diagrams and cross-cutting contracts; keep it in step with
  `BASE_PLAN.md`.
- `PROGRESS.md`: status, backlog and a short recent log only.
- `docs/sources/<source>.md`: source facts, replay results and the quirk ledger.
- `docs/R1_SLICE.md`: the concrete contract of the first realtime slice (types, time scenario
  table, DDL, manifests, acceptance).
- `README.md`: how to install and run; point to the other documents instead of repeating them.
- `STATIC_PIPELINE.md`, `NATIONAL_CZPTT.md`, `JDF_SEMANTICS.md`: the static contracts.

## Database

- PostgreSQL + PostGIS through psycopg 3 and raw SQL migrations (`src/obehy/release/migrations/`,
  `obehy db migrate`); no ORM. `0003_static.sql` is generated from the vendored contract
  (`src/obehy/data/serving/serving-v5.json`) by `python -m obehy.release.ddl`; never edit it by
  hand, and add a new migration when the contract gains a minor.
- `obehy release load|activate|status` (`src/obehy/release/`, `BASE_PLAN.md` section 16).
  Consumers read the `active.*` views only.
- History tables carry `derivation` (core, policy, release) and are rebuildable by replay inside
  the raw-archive window; every realtime row carries `release_id`.
- DB tests live in `tests/db/` and run when `OBEHY_TEST_DATABASE_URL` names a database the user
  may create databases from (`compose.yaml` runs one); they are skipped otherwise.

## Validation

- Run the narrowest relevant checks first, then broader ones when practical:
  `uv run pytest tests/unit -q` (plus `tests/db` with `OBEHY_TEST_DATABASE_URL`),
  `uv run ruff check src tests`, `uv run ruff format --check src tests`, `uv run pyright`,
  `uv run lint-imports` (add each new pure realtime module to its contract in `pyproject.toml`).
- JrUtil: `dotnet test jrutil.tests/jrutil.tests.fsproj -c Release --no-restore` in the standalone
  checkout; report the exact command and result.
- Inspect JrUtil log output as well as the exit code: conversion commands may log entity-level
  errors while returning zero.
- For documentation-only changes, review `git diff --check` and `git diff`.
- Never run the full national feed (full `obehy build`, full `fix-jdf`/`merge-jdf`/bundle/overlay
  over all batches) for testing. Verify on a bounded subset of batches, lines or a region, and only
  where a real-data check is actually required.
- If a check cannot run, say so instead of claiming it passed.
