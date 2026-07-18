# Oběhy implementation progress

This file is the concise engineering handoff for completed work. `BASE_PLAN.md` remains the
authoritative roadmap and architecture document.

## 2026-07-18 — Milestone 0 implemented

### Delivered

- Bootstrapped a Python 3.13 modular monolith managed by uv, with Ruff, strict Pyright, pytest,
  SQLAlchemy 2, Alembic, psycopg, and GeoAlchemy.
- Added a pinned PostgreSQL/PostGIS development service in `docker-compose.yml` and CI checks that
  validate the Compose configuration before starting the database.
- Added the initial Alembic schema for canonical entities, typed non-recycling ID sequences,
  lifecycle states, source bindings, identifier aliases, diagnostics, locations, routes,
  calendars, timetable variants, and calls.
- Implemented canonical allocation, redirects, tombstones, point-in-time source resolution,
  atomic ambiguity failure, separately committed ambiguity diagnostics, and typed DÚK alias
  normalization (`582588 -> 001588`).
- Implemented stop places with an unspecified boarding-point fallback, exact boarding points,
  operational points, passenger/operational call invariants, GTFS service-day time values beyond
  24:00, dated road-trip resolution, and full-train resolution from a PID call subsequence.
- Added fictional native JDF 1.11, CZPTT, PID GTFS, and DÚK fixtures with explicit normalized JSON
  projections. The native JDF files intentionally use Windows-1250 and CRLF as required by JDF.
- Documented that JDF stop continuity currently uses a mock authoritative shared ID. This is test
  scaffolding, not a claim that real national JDF-derived stop IDs are stable.

### Validation evidence

- Alembic revision `0001` was applied successfully to PostgreSQL/PostGIS 17/3.5.
- Full pytest suite passed against that database: **21 passed** (12 unit, 9 integration).
- Ruff lint and formatting checks passed.
- Strict Pyright passed with no errors or warnings.
- The uv lockfile passed an offline consistency check.
- Both native JDF fixtures were accepted and converted by the pinned JrUtil.
- The native CZPTT fixture was deserialized and merged by JrUtil. Its full GTFS conversion then
  reached JrUtil's unrelated live SŽ company-registry lookup; native-to-projection golden tests are
  still deferred.
- The JrUtil submodule source and pointer were not changed during Milestone 0.

### Current boundaries

- There is no production downloader, source snapshot store, JrUtil conversion bundle, national
  importer, structural stop-continuity matcher, GTFS exporter, realtime worker, API, or frontend.
- Native fixtures and normalized projections coexist, but no automated adapter test yet proves
  their complete equivalence.
- The Compose credentials and exposed port are development-only.
- Parquet is a proposed bulk interchange format for typed JrUtil sidecars, not a locked domain
  requirement. Logical sidecar schemas must be agreed before choosing their physical encoding.

### Next handoff — JrUtil conversion contract

Before building the national importer, define and implement the JrUtil conversion bundle needed
by the canonical model:

1. Specify logical schemas for source stops, boarding points, routes, timetable variants,
   passenger calls, operational points/calls, zones, identifiers, and provenance.
2. Resolve Czech-data semantics explicitly: available stable identifiers by source, JDF route
   distinctions, post ID versus display name, zone multiplicity, CZPTT passenger activities,
   train identifiers, and service-day/passage times.
3. Choose the physical sidecar encoding after evaluating direct .NET Parquet output against a
   simpler typed intermediate representation.
4. Extend the pinned JrUtil fork in independently reviewable commits and add native-to-normalized
   golden tests with deterministic output checksums.
5. Only after that contract is proven, import a tiny national conversion into PostgreSQL and begin
   real two-export stop-continuity diagnostics.
