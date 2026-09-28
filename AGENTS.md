# AGENTS.md

## Project identity

- The app and public-facing project name is **Oběhy**. Use the diacritic in prose and user-facing
  text; use ASCII `obehy` for repositories, packages, paths, identifiers and commands.
- `BASE_PLAN.md` is the long-term architecture document. It is partly out of date; do not rewrite
  it as a side effect of other work.

## Current focus: static feed readiness

- Read `PROGRESS.md` first. Its **Next steps** section is the working backlog: stop coordinates,
  the fixed JDF stop-ID registry, post-estimator speed and the `obehy build` outputs.
- Production static compilation and overlays belong to JrUtil. Oběhy acquires sources, supervises
  `obehy build`, and publishes/loads JrUtil output. Do not recreate PostgreSQL source
  reconciliation.
- JrUtil work happens in the standalone checkout `E:/Git/obehy/jrutil`. `converters/jrutil` is a
  pinned submodule: do not edit it or advance its pointer unless the task explicitly asks.
- Stable JDF stop IDs come from the versioned stop-ID registry file applied by JrUtil. The separate
  public identity-registry service in `IDENTITY_REGISTRY.md` remains a later milestone.
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

- The PostgreSQL database `obehy_test` is disposable and may be reset without approval. Before any
  destructive operation, verify the database is exactly `obehy_test` and the user is `obehy`.
- Generate Alembic migrations with `alembic revision --autogenerate` and review them. Hand-written
  migration code is only for behavior Alembic cannot infer (functions, triggers, extensions, seed
  data).

## Validation

- Run the narrowest relevant checks first, then broader ones when practical:
  `uv run pytest tests/unit -q`, `uv run ruff check src tests`, `uv run ruff format --check src
  tests`, `uv run pyright`.
- JrUtil: `dotnet test jrutil.tests/jrutil.tests.fsproj -c Release --no-restore` in the standalone
  checkout; report the exact command and result.
- Inspect JrUtil log output as well as the exit code: conversion commands may log entity-level
  errors while returning zero.
- For documentation-only changes, review `git diff --check` and `git diff`.
- If a check cannot run, say so instead of claiming it passed.
