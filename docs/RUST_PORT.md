# The realtime core in Rust

The plan for porting Oběhy's realtime core from Python to Rust: what moves, what stays, how the
two are kept equal while both exist, and in which order. `BASE_PLAN.md` sections 18–22 are the
core's design; this document is only about where it runs. Nothing here changes behaviour: the
Rust core is done when it answers exactly as the Python one did, and then the Python one goes.

## Why now, and what is ready

The 2026-11-20 cleanup (`PROGRESS.md`, phases 0–5) left the core in the shape a port needs:

- **One rule per concept.** Passed calls (`Instance.left_index`), lifecycle, the journey key
  (`[journey]` in policy), the published delay (`lateness_s`), vehicle identity: one
  implementation each. Thresholds live in `policy-v18.toml`, not in code.
- **A pure core behind a narrow boundary.** `core.py`, `timeline/`, `infer/`, `model.py` and
  `facts.py` touch no database, network or files (import-linter keeps it so). In: facts, a
  closed, versioned set (`facts.py`, schema 5; `model_json.py` reads and writes it). Out: eight
  effect types (`model.Effect`). Between ticks, the state (`FeedState`) only lives in memory and
  is rebuilt by warm replay, so it never has to be stored or read back.
- **A reference to match.** The 106 real-journey goldens (`tests/golden/corpus`, run in about 6 s)
  record each journey's history effects, bindings and per-tick lifecycle, delay and passed
  calls. Replay from the raw archive runs without a database, and the index parity test pins
  the index's two loaders to one answer.
- **Time in one place.** `times.py` holds every conversion (service dates, DST, local walls);
  it is ported once.

It is not enough by itself: the goldens hold a summary of each journey, not everything the core
produces, and two behaviours were deliberately left as they were. Step R0 closes both.

## What runs where

### Rust: the per-tick, pure, CPU-bound core (about 5,000 of `realtime/`'s 8,600 lines)

| Python today | Rust |
|---|---|
| `times.py` (the conversions the core uses) | `time`: `Instant`, `ServiceTime`, local walls, DST |
| `geo.py` | `geo`: distances and bearings |
| `facts.py`, `model.py` (core types, `FeedState`, `Instance`, effects) | `model` |
| `policy.py` (the core's parts) | `policy`: read from the same `policy-vNN.toml` with serde |
| `index.py` (`Index`, `IndexView`, runs, `calls_at_station`, `visit_numbers`) | `index`, built from columns handed over once per release |
| `infer/keyed.py`, `infer/keyless/` | `infer` |
| `timeline/` (path, plan, progress, stops, commit, estimate, runtimes, fusion) | `timeline` |
| `core.py` (`step`, `refresh`, `estimate_all`, lifecycle), `runs.py`, `platforms.py` | `core` |
| `emit/represent.py` (`publication`), `emit/gtfs_rt.py` | `publish`: the published snapshot and GTFS-RT bytes |
| the runner's invariants (committed progress never goes back, no re-dating while admissible) | asserted inside `core`, errors returned to Python |

### Python: I/O, glue, and everything that changes with the sources

- **Connectors and decoding (`sources/`).** They are I/O-bound, hold the quirk ledger, and change
  most often; they already hand the core clean facts.
- **The worker shell.** Scheduling, egress and the proxy pool, the raw archive, the lookup
  broker, alerts (`worker.py`, `runtime/`, `record.py`, `archive.py`).
- **Writing.** `emit/db.py` writes effects and the published snapshot to PostgreSQL in sets; SQL
  stays the place for sets.
- **Loading.** The index's rows come from SQL (`index_sql.py`) or release files
  (`index_files.py`) and are handed to Rust as columns. Manifests and the non-core parts of
  policy are read in Python too.
- **Commands and tools.** Replay, warm start, bench, corpus pinning, jobs; the API, release
  loading and the static pipeline are untouched.

### Undecided: vehicle identity (`emit/vehicles.py`, `emit/pairing.py`)

It runs every tick, but it is mostly dictionary work plus a spatial pairing. It starts in Python
and moves only if the bench shows it is the bottleneck once the core is native.

## The boundary

One Rust crate, `obehy-core`, built as a Python extension (PyO3, maturin) and imported as
`obehy_core`. The boundary is coarse on purpose: one crossing per tick, not one per function.

```text
Core(policy_toml: str, semantics: {channel: SourceSemantics}, feeds)
  .load_index(feed, columns)            once per release (and at the sweep/compaction times)
  .step(feed, observations) -> effects  every poll's observations, in order
  .tick(now) -> TickOutput              refresh, estimates, housekeeping
      effects                           what housekeeping recorded (a terminus arrival)
      publication                       the rows of rt.trip_state_current, trip_part_current,
                                        vehicle_state_current, per feed
      gtfs_rt                           the encoded feed per feed
```

- **Observations in** are the facts as `model_json.py` writes them today: a format that is
  already versioned and tested. It is the first thing to replace with typed objects if the
  bench shows that converting them costs too much; not before.
- **Effects out** are plain Python values the writer already knows: the shapes the goldens
  record (`tests/realtime/golden.py` `_effect`), made exact.
- **The publication** is the snapshot `emit/represent.publication` decides today, with the
  finished and stale state decided once. The writer and the vehicle book read it instead of
  walking `FeedState`, which Python no longer holds. This is the `trip_state_current.publication`
  step deferred from cleanup phase 3; doing it here is what lets the state stay inside Rust.

## R0: Before any Rust

1. **An exact comparison harness.** Serialise a core's whole output, not a summary: every
   effect of every step and tick, the publication, and the decoded GTFS-RT, in one canonical
   form (sorted keys, fixed float rounding, Prague times as ISO). A command,
   `obehy rt compare --from-files <window> --core python|rust`, writes it, and the comparison
   runs on the corpus goldens' 106 journeys and on the replay windows used in the cleanup (PID
   2026-10-15 07–08, DÚK/SŽ/Arriva 2026-10-06 07–08). The Python core's output becomes the
   pinned reference before anything is ported.
2. **Decide the two behaviours left open**, or record that they are ported as they are:
   - Hop running times are learned from the arrival at the first call (`timeline/runtimes.py`)
     but applied from the departure. The corpus could not judge a change; a replay comparison
     can.
   - `platforms._boarding_point` is a stub kept as the hook for the board-platform work.
     Port it as a stub unless that work comes first.
3. **Measure the baseline.** `obehy rt bench` over the same windows, split by phase (step,
   refresh, estimate, state, GTFS-RT). It sets the number the port has to beat and says
   whether GTFS-RT or the vehicle book should move too.
4. **The publication snapshot in Python first.** Make the writer and the GTFS-RT emitter read
   `publication(state)` only, never `FeedState` directly. This is a refactor of the Python core,
   checked by the harness, and it fixes the boundary before Rust exists.

## R1–R7: The port, bottom up

Each step ports a layer together with its unit tests (from `tests/unit/realtime/`, rewritten as
`cargo test`), and is done when those pass. The layers are checked against Python one by one where
that is cheap (pure functions over the same inputs). The whole core is judged only by the harness
at R5, end to end.

| Step | Ports | Done when |
|---|---|---|
| R1 | crate skeleton, `time`, `policy`, `model`, facts' serde | the time scenario table (T1–T15) passes in Rust; every fact round-trips with `model_json.py` |
| R2 | `index` from columns | the index parity test's fixtures give the same trips, runs, visits and station calls as `Index` |
| R3 | `timeline`: path, plan, stops, progress, commit, estimate, runtimes | the timeline unit tests pass; a plan's and a projection's numbers match Python's on the corpus trips |
| R4 | `infer` (keyed, keyless) and fusion | the binding and keyless tests pass |
| R5 | `core`: step, refresh, estimate_all, lifecycle, runs, platforms, invariants | the harness: all 106 goldens and both replay windows identical to the Python core |
| R6 | `publish`: the publication and GTFS-RT | the harness, now including the state rows and the decoded feeds |
| R7 | the switch | replay, warm start, bench and the live worker run Rust; one live day on the dev database without a difference against a Python replay of the same archive; then the Python core and its tests are deleted |

AGENTS.md's rule is "no twin implementations": until R7 the Python core is the reference
under test only, never run live alongside Rust; at R7 live and replay switch together, and the
Python core is deleted rather than kept as a fallback.

## Carrying the rules over

- **Purity.** The `obehy-core` crate depends on no I/O crate: no database, network or files. Its
  `Cargo.toml` is the import-linter contract.
- **Determinism.** Output in sorted order (`BTreeMap`, or sorting before emitting), never hash
  order. Python's dicts keep insertion order: wherever the core relies on that today, Rust keeps
  it explicitly (an `IndexMap`), and the harness is what finds the places.
- **Floats.** Both cores use IEEE f64, but `sin`, `cos` and `atan2` may differ in the last bit
  between Python's libm and Rust's (`geo.py`, `timeline/path.py` and `timeline/progress.py`
  use them, about ten calls). The harness rounds at the edges (metres to 0.01, seconds to
  whole), and every place the core compares a float against a threshold is listed in R3, so a
  last-bit difference cannot flip a decision unseen.
- **Time zones.** `chrono-tz` and Python's `zoneinfo` must read the same tzdata. Pin both to
  one release and check it in the time tests.
- **Policy, not constants.** The crate reads `policy-vNN.toml`; a threshold in Rust code is the
  same defect it is in Python.
- **Quirks stay in connectors.** The core gets facts; nothing source-specific moves into Rust.

## Building and shipping

- **Locally:** the stable toolchain through `rustup`, `x86_64-pc-windows-msvc`, linking with
  the installed VS2026 MSVC (no compilers in WSL). The crate lives in `rust/obehy-core/`;
  `uv` builds it as a workspace member with maturin as its build backend, so `uv sync` gives
  the extension and `uv run pytest` tests it.
- **CI:** `cargo fmt --check`, `cargo clippy -D warnings` and `cargo test` next to the Python
  checks; the harness against the pinned corpus on every change to either core.
- **The image:** the `Dockerfile` gains a Rust build stage that produces the wheel. Changing the
  server stack is the user's to do.

## What to decide before starting

- The crate's place and name (`rust/obehy-core`, Python module `obehy_core`).
- Facts across the boundary as JSON first, typed objects only if the bench asks for them.
- Whether the vehicle book moves in R6 or stays in Python.
- Whether hop timing changes in R0 or is ported as it is.
