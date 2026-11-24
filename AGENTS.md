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
- Production static compilation and overlays belong to JrUtil, implemented by the Rust compiler
  jrutil-rs: `obehy build` fetches the sources, writes a build specification and runs one
  `jrutil build` (`STATIC_PIPELINE.md`). `obehy build` runs on GitHub Actions, never on the
  application server. On the server, Oběhy fetches, loads and activates
  releases and runs the realtime core. Do not recreate PostgreSQL source reconciliation.
- Compiler work happens in the standalone checkout `E:/Git/obehy/jrutil-rs`; its build
  specification (`docs/switch.md`, `crates/jrutil/src/spec.rs`) is the interface Oběhy writes, and
  it also holds the route-shapes companion (`route-shapes/`). Nothing is read from the F# checkout
  `E:/Git/obehy/jrutil`; the gazetteers are in `jrunify-ext-geodata/gazetteer`.
  `converters/jrutil` is a pinned submodule: do not edit it or advance its pointer unless the task
  explicitly asks.
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
realtime projects. `BASE_PLAN.md` sections 18–22 give the reasons; `docs/RUST_PORT.md` says
where each part runs.

- **Where it lives.** The realtime worker is the Rust workspace in `rust/` (`obehy-core`,
  `obehy-sources`, `obehy-rt`; `rust/README.md`), shipped as the `worker` image target and
  run as `obehy-rt worker`. Python keeps the API, releases and migrations, `obehy jobs`, the
  GTFS-RT check and the replay diagnostics, which read the worker's output stream
  (`obehy rt report --from-stream`). The database is the boundary; the policy,
  the connector manifests (`src/obehy/data/realtime/sources/`) and the SQL the worker compiles
  in (`src/obehy/data/realtime/sql/`) are the only files both read. Python reads its few
  fields through `obehy/realtime_config.py`; Rust owns both schemas.
- **Layering.** `obehy-core` uses no I/O crate and `obehy-sources` neither I/O nor an async
  runtime; only `obehy-rt` has tokio, PostgreSQL and HTTP (`obehy-rt/tests/
  dependency_direction.rs` checks it). The core stays synchronous; the shell calls it.
- **No twin implementations.** Live operation, `obehy-rt replay` and `compare` run the same
  core; there is no Python worker or fallback. Analysis reads the output stream or the
  database (an `obehy rt …` subcommand, an `obehy-rt` command or a script); never re-implement
  core rules in a scratch script.
- **Time.** Only `obehy-core::time` converts times in the worker, and `realtime_config.py` at
  the Python edge. Use `Instant` (aware UTC) and `ServiceTime` (service date + wall-clock
  seconds from local midnight). No naive datetimes. Key everything by service date, never by
  calendar date. An instance's date is chosen once and never re-derived.
- **IDs are opaque.** Never parse a public ID (for example the `yymmdd` in a trip ID); resolve
  through `source_key`. History is keyed by journey `(feed, key namespace, key, service date)`
  and never references `static.*`.
- **Keyed sources are literal.** No reinterpretation heuristics in the core: a key whose trip
  does not run is unmatched. Keyless inference stays in `infer/keyless`.
- **SQL for sets, Rust for the hot path.** Logic over many rows at once (index building,
  history aggregation, circulation learning, evaluation, API queries) is SQL in PostgreSQL; the
  writer writes in sets. Only the per-vehicle state machine runs in the worker's loop.
- **Policy, not constants.** Thresholds, margins and tolerances live in versioned policy TOML
  under `src/obehy/data/`, not as constants, in Rust as in Python.
- **Connectors declare, the runtime executes.** A connector is its manifest: channels, lookups,
  `[channel.decode]` (or a named code decoder in `obehy-sources`). Scheduling, rate limits,
  budgets, egress and caching are the generic runtime's; the API never calls upstream.
- **The API refers to trips by the feed's `trip_id` + service date** and to vehicles by
  `vehicle_id`; source keys are lookup aliases only. History and realtime state are keyed by
  journey: rail journeys are train number + service date, joined by `journey_link` where the
  number changes.
- **Core state is partitioned by feed**; nothing in memory crosses `jdf` and `czptt`.
- **Curated data is git-reviewed files + `obehy ref import`.** `ref.*` is a mirror. The vehicle
  datasets (our own list, the DÚK register) live in `jrunify-ext-geodata/vehicles/`.
- **Quirk ledger.** Every source quirk (a catch-22 of Czech data) gets an ID in its dossier's
  **Quirks** section (`DUK-Q3`), is normalized in `obehy-sources` (every normalizer names its
  quirk), and has a test named after the ID.
- Output is emitted in sorted, deterministic order (`BTreeMap` or sorted before emitting, never
  hash order); modules stay small; replay is checked against the pinned streams
  (`tests/golden/corpus-stream/`, `tests/realtime/compare/`, written by `obehy-rt`). A
  deliberate change of behaviour rewrites the affected pinned streams in the same change and
  says why. The worker is plain, idempotent Rust: no emulation of Python's numbers, text or
  library quirks (it was removed after the switch).

## Documentation roles

- `BASE_PLAN.md`: decisions and architecture only, no status.
- `ARCHITECTURE.md`: the diagrams and cross-cutting contracts; keep it in step with
  `BASE_PLAN.md`.
- `PROGRESS.md`: status, backlog and a short recent log only.
- `docs/sources/<source>.md`: source facts, replay results and the quirk ledger.
- `docs/R1_SLICE.md`: the concrete contract of the first realtime slice (types, time scenario
  table, DDL, manifests, acceptance).
- `docs/R2_SLICE.md`: the rail slice on top of it (runs, SŽ and DÚK trains, fusion, platforms,
  proxied SŽ egress).
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
  `uv run lint-imports` (add each new pure module to its contract in `pyproject.toml`).
- The worker, in `rust/`: `cargo fmt --all --check`,
  `cargo clippy --workspace --all-targets -- -D warnings`, `cargo test --workspace` (its
  database tests run with `OBEHY_TEST_DATABASE_URL` or `[database] test_url`, through
  `rust/tools/store_db.py`, and drop what they make). Use your own `CARGO_TARGET_DIR` when
  another build may be running.
- jrutil-rs: `cargo fmt --all --check`, `cargo clippy --workspace --all-targets -- -D warnings`,
  `cargo test --workspace` in the standalone checkout; report the exact command and result.
- Inspect the compiler's log (`<run>/logs/compile.process.log`) as well as the exit code.
- For documentation-only changes, review `git diff --check` and `git diff`.
- Never run the full national feed (full `obehy build`, a full `jrutil build`) for testing. Verify on a bounded subset of batches, lines or a region, and only
  where a real-data check is actually required.
- If a check cannot run, say so instead of claiming it passed.
