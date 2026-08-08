# AGENTS.md

## Project identity

- The app and public-facing project name is **Oběhy**.
- Use the diacritic in prose and user-facing text. Use the ASCII form `obehy` for repository names, package names, paths, identifiers, and commands where portability matters.
- `BASE_PLAN.md` is the authoritative implementation plan. Keep architectural and roadmap changes consistent with it.

## Repository state

- Database v1, the finalized serving-package validator/loader, and the national conversion bundle
  builders are implemented. Read `PROGRESS.md`
  before starting work for the current handoff, validation state, known limitations, and next
  intended milestone.
- The PostgreSQL national compiler/importer has been removed. Do not recreate PostgreSQL source
  reconciliation. Production static compilation/overlays belong to JrUtil and Oběhy loads only
  JrUtil's finalized serving package.
- JrUtil uses explicitly provisional `v0:` IDs until one PID static-overlay build and one PID
  realtime entity work end to end. The permanent registry is then built in a separate repository;
  its launch is the sole planned public-ID break.
- Combined JrUtil static compilation, regional overlays, realtime processes, API, frontend and the
  public identity-registry service do not exist yet.
- `converters/jrutil` is a pinned Git submodule. Do not edit submodule contents or advance its pointer unless the task explicitly calls for JrUtil work.
- Keep generated data, source snapshots, build artifacts, credentials, and local environment files out of version control.
- The configured PostgreSQL database named `obehy_test` is disposable development/test state. It
  may be dropped, recreated, downgraded, or otherwise reset whenever implementation or validation
  requires it, without requesting additional approval. Before any destructive database operation,
  verify the connected database is exactly `obehy_test` and the user is `obehy`; this permission
  does not apply to any other database or inferred production environment.

## Working conventions

- Follow the vertical-slice order in `BASE_PLAN.md`; preserve opaque public IDs, provenance,
  deterministic builds, active-build mapping isolation, and strict handling of ambiguous matches.
- Prefer small, focused changes. Do not introduce infrastructure or abstractions before the milestone that needs them.
- Preserve Czech text as UTF-8 and retain diacritics in public-facing names.
- Add or update the closest relevant tests and fixtures with behavior changes. Use small deterministic fixtures for data-conversion and matching work.
- Never silently guess an identity match. Quarantine ambiguity and expose it in diagnostics.
- Update `PROGRESS.md` whenever work materially changes repository capabilities, decisions, known
  limitations, validation results, or the recommended next step. Keep it factual and concise; do
  not use it as a speculative backlog or duplicate `BASE_PLAN.md`.
- A progress entry must state what changed, what was actually validated (including skipped or
  unavailable checks), any remaining caveats, and the next safe handoff point.
- Generate Alembic schema migrations from SQLAlchemy metadata with `alembic revision
  --autogenerate`, then review the generated operations. Hand-written migration code is reserved
  for database behavior Alembic cannot infer, such as PostgreSQL functions, triggers, extensions,
  seed data, or a reviewed correction to generated DDL; do not hand-roll ordinary tables, columns,
  indexes, foreign keys, or constraints.

## Validation

- Run the narrowest relevant checks first, then broader checks when practical.
- For documentation-only changes, inspect the rendered structure and review `git diff --check` plus `git diff`.
- For JrUtil changes explicitly requested inside the submodule, run the relevant .NET tests from `converters/jrutil` and report the exact command and result.
- If a planned command or project structure has not been bootstrapped yet, say so instead of inventing a passing check.
- When validating native fixtures with JrUtil, inspect its log output as well as the process exit
  code: current conversion commands may log an entity-level error while returning exit code zero.
