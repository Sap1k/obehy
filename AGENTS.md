# AGENTS.md

## Project identity

- The app and public-facing project name is **Oběhy**.
- Use the diacritic in prose and user-facing text. Use the ASCII form `obehy` for repository names, package names, paths, identifiers, and commands where portability matters.
- `BASE_PLAN.md` is the authoritative implementation plan. Keep architectural and roadmap changes consistent with it.

## Repository state

- This repository is at the bootstrap stage; most of the planned application structure does not exist yet.
- `converters/jrutil` is a pinned Git submodule. Do not edit submodule contents or advance its pointer unless the task explicitly calls for JrUtil work.
- Keep generated data, source snapshots, build artifacts, credentials, and local environment files out of version control.

## Working conventions

- Follow the vertical-slice order in `BASE_PLAN.md`; preserve canonical IDs, provenance, deterministic builds, and strict handling of ambiguous matches.
- Prefer small, focused changes. Do not introduce infrastructure or abstractions before the milestone that needs them.
- Preserve Czech text as UTF-8 and retain diacritics in public-facing names.
- Add or update the closest relevant tests and fixtures with behavior changes. Use small deterministic fixtures for data-conversion and matching work.
- Never silently guess an identity match. Quarantine ambiguity and expose it in diagnostics.

## Validation

- Run the narrowest relevant checks first, then broader checks when practical.
- For documentation-only changes, inspect the rendered structure and review `git diff --check` plus `git diff`.
- For JrUtil changes explicitly requested inside the submodule, run the relevant .NET tests from `converters/jrutil` and report the exact command and result.
- If a planned command or project structure has not been bootstrapped yet, say so instead of inventing a passing check.
