# Oběhy realtime worker in Rust

The port of `src/obehy/realtime/` to a standalone binary (`docs/RUST_PORT.md`). Python stays the
reference until the switch.

| Crate | What | May depend on |
|---|---|---|
| `obehy-core` | time, geo, model + facts JSON, config (policy, manifests), `py` (Python semantics), index; later infer, timeline, core, publish, vehicles | no I/O crate |
| `obehy-sources` | connectors: bytes in, facts out (R2) | `obehy-core`; no async, no I/O |
| `obehy-rt` | the `obehy-rt` binary: shell, store, archive, commands | everything |

One file per Python module; stubs name the module they will port.

## Build and check

Stable MSVC toolchain (`rust-toolchain.toml`), linking with the installed VS2026 MSVC. In `rust/`:

```
cargo fmt --check
cargo clippy --all-targets -- -D warnings
cargo test
```

The dependency direction is a test: `obehy-rt/tests/dependency_direction.rs` walks Cargo.lock
and fails if `obehy-core` reaches an I/O crate (tokio, postgres, reqwest, hyper, ...) or
`obehy-sources` reaches one or an async runtime.

## Python parity fixtures

`obehy-core/tests/fixtures/` is written by the Python side itself:

```
uv run python rust/tools/gen_fixtures.py
```

It covers `float.__repr__` and `json` float text, `round`/`hypot`/float `%` and `//`, the
Prague zone (tzdata release, TZif bytes, every transition 1970-2100, schedule times and local
readings), `fromisoformat`, `timedelta` rounding, the source-time readers, haversine, the loaded
policy and manifests, and facts JSON (hand-made plus real payloads decoded from the archives
under `data/rt-raw-prod`, `../rt-data/rt-raw` and `data/rt-raw`). Regenerate it when the
Python side changes, and on a tzdata bump after copying the new TZif (see
`obehy-core/src/time/zone.rs`).

## Connectors (obehy-sources) and the facts harness

`uv run python rust/tools/gen_tables.py` writes the Python tables read as code (Unicode digit
and space classes for `obehy_core::py::text`, the HTML5 entities for the board parser);
`uv run python rust/tools/gen_sources_fixtures.py` writes `obehy-sources/tests/fixtures/` (every
decoder call of the Python connector tests with its result, JSON error texts, pyproj Křovák
outputs). The facts level of the harness:

```
obehy-rt compare --level facts --archive <root> --sources <ids> --from <day> --to <day> \
    [--since <local>] [--until <local>] --out <stream.jsonl[.gz]> [--check <python stream>]
```

writes the stream `obehy rt compare --level facts` writes, from the same arguments.

## The tracker against Python (R4)

`uv run python rust/tools/gen_progress_fixtures.py` steps the corpus journeys
(`tests/realtime/corpus`) and the unit scenarios of `test_progress.py`, `test_stops.py` and
`test_timeline.py` through the Python core and records every call into `timeline/plan`,
`stops`, `commit` and `progress` with its inputs and output, plus every plan those calls read
(`obehy-core/tests/fixtures/progress.json.gz`); `tests/timeline_progress.rs` replays them bit for
bit. Plans are cached in `timeline::plan::PlanCache` (`plan(trip, day, &index, &policy)`).

`uv run python rust/tools/gen_estimate_fixtures.py` does the same for `timeline.advance`, fusion,
the estimates and platforms: every `advance`, `apply_point`, `with_lead`, per-journey
`estimate_all` (with its intermediates) and `apply_platform` call over the corpus and the unit
scenarios of `test_timeline.py`, `test_rail_fusion.py`, `test_cancellation.py`, `test_boards.py`
and `test_hot_path.py`, instances in and out (`obehy-core/tests/fixtures/estimate.json.gz`);
`tests/timeline_estimate.rs` replays them and compares the instances and effects exactly.

## Reading policy, manifests and facts

```rust
use obehy_core::config::{Policy, manifest};
let policy = Policy::load(&data_dir.join(obehy_core::config::policy::POLICY_FILE))?;
let skew = policy.time.max_clock_skew();
let rail_delay = policy.time.max_delay_s.get("rail");        // ByMode values are f64
let sources = manifest::load_sources(&sources_dir, &manifest::RECORDER_FILTERS)?;

use obehy_core::model::json::{facts_from_json_text, facts_json_text};
let facts = facts_from_json_text(stored)?;                    // rt.observation.facts
assert_eq!(facts_json_text(&facts), stored);                  // byte-identical to Python
```

Python semantics the port must keep (half-even `timedelta` arithmetic, `round`, `min`/`max`
ties, floored `%`, CPython `hypot`, float repr) are in `obehy_core::py` and
`obehy_core::time::TimeDelta`; `docs/PORTING_HAZARDS.md` lists where each is needed.

## The index against Python (R3)

`obehy_core::index` is the index and its loader, written against the `Rows` trait;
`obehy_rt::index` reads the rows from PostgreSQL (`SqlRows`) or a release's serving Parquet
(`ServingRows`). `rust/tools/index_crosscheck.py` turns an archive window into a load plan, runs it
with the Python loader and dumps the index; the `index_check` example runs the same plan in Rust
and compares (`index_parity` compares every `Rows` method between Parquet and PostgreSQL):

```
uv run python rust/tools/index_crosscheck.py plan --release data/releases/<run> --source sz-mapa \
    --channel trains --since 2026-10-10T01:30:00+00:00 --until 2026-10-10T10:00:00+00:00 --out OUT
cargo run --release -p obehy-rt --example index_check -- --plan OUT/plan.json --expect OUT/expect.json
uv run python rust/tools/index_crosscheck.py sql --plan OUT/plan.json --expect OUT/expect.json --subset --parity
```

## Publishing and the vehicle book against Python (R7)

`obehy_core::publish` (`FeedEmitter`, `represent`, alert entities) and `obehy_core::vehicles`
(`VehicleBook`, pairings, the registry and precedence it decides with) are checked by
`obehy-core/tests/publish.rs` against `obehy-core/tests/fixtures/publish/`, which
`uv run python rust/tools/gen_publish_fixtures.py` writes (about 2 minutes): two real windows
(PID 2026-10-15, DÚK/SŽ map/Arriva 2026-10-06, 07:00-07:15 Prague) run through the Python runner
as `obehy rt compare` runs them, every call the writer made to its vehicle book with its result,
and on two snapshots each the whole `FeedState` (read back by `obehy_core::model::state_json`)
with Python's GTFS-RT bytes; the GTFS-RT unit tests' serializations; a scripted vehicle book
(registry keys, pairing, a train, a reload); hand-made alert rows.

## The store, warm start and the lookup broker (R7/R8)

`obehy_rt::store` is the writer (`Writer`: effects and state as rows), the `Store` trait with
`PgStore` (PostgreSQL; prepared statements, pipelined rows, binary COPY for sets) and
`RecordingStore` (the harness's row records), and warm start's reader (`store::stored`, binary
COPY out on a thread of its own); `obehy_rt::warm` steps the runner through it,
`obehy_rt::lookups` is the lookup broker. Checks against Python (test database from
`[database] test_url`; every database made is dropped):

```
uv run python rust/tools/store_db.py parity tests/realtime/compare/dpmd-output.jsonl.gz
cargo run --release -p obehy-rt --example writer_parity -- --stream <output stream>
cargo test -p obehy-rt --test store_db
```

`parity` sends a stream's row records through Python's `SqlStore` and the Rust store into two
fresh databases and compares every `rt.*` and `history.*` table; `writer_parity` drives the Rust
writer with a stream's effects and compares its rows with Python's.

## The core, the runner and the output harness (R6)

`obehy_core::core` is `core.py` (`step`, `refresh`, `estimate_all`, `rebase`, `resolve`) over a
`Context` (index, plans, per-source progress policies, policy, semantics);
`obehy_core::runner` is `runner.py` without I/O: `Runner<L: Loads, P: Phases>` (`process`,
`tick`, `sweep`, `compact`, `switch_release`, invariants) and `Ticks`. Effects go back to the
caller, which writes them. `obehy-rt` drives it from the archive (`replay.rs`: decoding on a
pool, `drive`, `Clock`, `finish`):

```
obehy-rt compare --level output --from-files <release> ... --out S.jsonl.gz [--check REF] \
    [--diagnostics] [--quality] [--threads 8]
obehy-rt replay --from-files <release> ... [--gtfs-rt-every 60 --out DIR] [--threads 8]
obehy-rt corpus-stream --out DIR [--check] [names...]   # tests/golden/corpus-stream
```

`compare` takes the arguments `uv run python -m tests.realtime.compare_windows args NAME output`
prints; `cargo test -p obehy-rt --test corpus_stream` checks the 106 corpus journeys.

## The worker (R8)

`obehy-rt worker` takes `obehy realtime`'s flags (`--archive`, `--gtfs-rt`, `--sources`,
`--feeds`, `--manifest`, `--config`, `--database-url`). `obehy_rt::worker` is `worker.py`: a
core thread owns the runner, the writer and the emitters and serves polls, ticks (round
multiples of `emit.tick_s`, each before the first poll after it, as replay ticks), release
switches and registry reloads in arrival order; pollers, the tick timer, publications, the
lookup broker and the summaries (with resident memory and container sizes) are tokio tasks. It
refuses a database whose last migration is not the one it knows (`worker::schema`). Live and
replay share decoding (`replay::decode_payload`), the runner and the output
(`worker::pipeline::Output`, `ReplaySink`):

```
cargo test -p obehy-rt --test worker_db
```

runs the worker against a local stub serving archived DÚK payloads on a simulated clock, into a
test database with a bounded subset release (`store_db.py release`), and compares every `rt.*`
and `history.*` row and the GTFS-RT file with replay of the worker's archive; it also checks the
schema refusal and a mid-run release switch.
