# AGENTS.md

## Project identity

- The app and public-facing project name is **Oběhy**. Use the diacritic in prose and user-facing
  text; use ASCII `obehy` for repositories, packages, paths, identifiers and commands.
- `BASE_PLAN.md` is the long-term architecture document. Do not rewrite it as a side effect of
  other work; change it deliberately when an architectural decision changes.

## Current focus: static readiness, then the core runtime

- Read `PROGRESS.md` first. Its **Next steps** section is the working backlog: static feed
  readiness, then the core runtime (release loader, realtime core) in `BASE_PLAN.md` order.
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

## Database

- There is currently no database code. The serving-v1 loader, ORM models and Alembic migration
  were removed. The release loader (`BASE_PLAN.md` section 16) reintroduces PostgreSQL with raw
  SQL migrations and psycopg 3, without an ORM.

## Validation

- Run the narrowest relevant checks first, then broader ones when practical:
  `uv run pytest tests/unit -q`, `uv run ruff check src tests`, `uv run ruff format --check src
  tests`, `uv run pyright`.
- JrUtil: `dotnet test jrutil.tests/jrutil.tests.fsproj -c Release --no-restore` in the standalone
  checkout; report the exact command and result.
- Inspect JrUtil log output as well as the exit code: conversion commands may log entity-level
  errors while returning zero.
- For documentation-only changes, review `git diff --check` and `git diff`.
- Never run the full national feed (full `obehy build`, full `fix-jdf`/`merge-jdf`/bundle/overlay
  over all batches) for testing. Verify on a bounded subset of batches, lines or a region, and only
  where a real-data check is actually required.
- If a check cannot run, say so instead of claiming it passed.
