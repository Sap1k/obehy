# AGENTS.md

## Project identity

- The app and public-facing project name is **Oběhy**.
- Use the diacritic in prose and user-facing text. Use the ASCII form `obehy` for repository names, package names, paths, identifiers, and commands where portability matters.
- `BASE_PLAN.md` is the authoritative implementation plan. Keep architectural and roadmap changes consistent with it.

## Repository state

- Milestone 0 is implemented. Read `PROGRESS.md` before starting work for the current handoff,
  validation state, known limitations, and next intended milestone.
- The Python project, canonical domain model, initial PostgreSQL/PostGIS schema, synthetic fixtures,
  and Milestone 0 tests exist. Production source adapters, national imports, exports, and realtime
  processes do not exist yet.
- `converters/jrutil` is a pinned Git submodule. Do not edit submodule contents or advance its pointer unless the task explicitly calls for JrUtil work.
- Keep generated data, source snapshots, build artifacts, credentials, and local environment files out of version control.

## Working conventions

- Follow the vertical-slice order in `BASE_PLAN.md`; preserve canonical IDs, provenance, deterministic builds, and strict handling of ambiguous matches.
- Prefer small, focused changes. Do not introduce infrastructure or abstractions before the milestone that needs them.
- Preserve Czech text as UTF-8 and retain diacritics in public-facing names.
- Add or update the closest relevant tests and fixtures with behavior changes. Use small deterministic fixtures for data-conversion and matching work.
- Never silently guess an identity match. Quarantine ambiguity and expose it in diagnostics.
- Update `PROGRESS.md` whenever work materially changes repository capabilities, decisions, known
  limitations, validation results, or the recommended next step. Keep it factual and concise; do
  not use it as a speculative backlog or duplicate `BASE_PLAN.md`.
- A progress entry must state what changed, what was actually validated (including skipped or
  unavailable checks), any remaining caveats, and the next safe handoff point.

## Validation

- Run the narrowest relevant checks first, then broader checks when practical.
- For documentation-only changes, inspect the rendered structure and review `git diff --check` plus `git diff`.
- For JrUtil changes explicitly requested inside the submodule, run the relevant .NET tests from `converters/jrutil` and report the exact command and result.
- If a planned command or project structure has not been bootstrapped yet, say so instead of inventing a passing check.
- When validating native fixtures with JrUtil, inspect its log output as well as the process exit
  code: current conversion commands may log an entity-level error while returning exit code zero.
