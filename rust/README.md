# Oběhy realtime worker in Rust

The realtime worker, `obehy-rt`: one program from polling a source to writing PostgreSQL, the
raw archive and the GTFS-RT files (`docs/RUST_PORT.md`). It replaced the Python worker on
2026-11-21; Python keeps the API, releases, migrations, `obehy jobs` and the diagnostics that
read this program's output stream.

| Crate | What | May depend on |
|---|---|---|
| `obehy-core` | time, geo, model + facts JSON, config (policy, manifests), index, infer, timeline, core, runner, publish, vehicles | no I/O crate |
| `obehy-sources` | connectors: readers, declarative decoding, normalizers, code decoders; bytes in, facts out | `obehy-core`; no async, no I/O |
| `obehy-rt` | the `obehy-rt` binary: worker, record, replay, warm, bench, compare, corpus-stream; store, archive, scheduler, fetch, egress, lookup broker | everything |

## Build and check

Stable MSVC toolchain (`rust-toolchain.toml`), linking with the installed VS2026 MSVC. In `rust/`:

```
cargo fmt --all --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace
cargo build --release -p obehy-rt
```

The dependency direction is a test: `obehy-rt/tests/dependency_direction.rs` walks Cargo.lock
and fails if `obehy-core` reaches an I/O crate (tokio, postgres, reqwest, hyper, ...) or
`obehy-sources` reaches one or an async runtime.

`obehy-rt` finds the repository (or the image's `/app`) by `src/obehy/data/realtime/sources/`
above the working directory or the executable, and reads the policy, the manifests, the SR70
catalogue and the vehicle vocabulary from there; the SQL it shares with Python
(`src/obehy/data/realtime/sql/`) is compiled in. Settings come from the environment
(`OBEHY_DATABASE_URL`, `OBEHY_EGRESS_PROXY_LIST_FILE`, `OBEHY_SECRET_<NAME>`) before
`config/obehy.local.toml`.

## Commands

```
obehy-rt worker [--archive data/rt-raw] [--gtfs-rt data/gtfs-rt] [--sources S] [--feeds jdf,czptt]
obehy-rt record [--once] [--duration 2d] [--sources S] [--archive data/rt-raw]
obehy-rt replay --from-files <release> --from D --to D [--since L --until L] [--sources S] \
    [--gtfs-rt-every 60 --out DIR] [--write-history [--url-env VAR]] [--threads 8]
obehy-rt warm --from-files <release> --until <local> [--hours H] [--mode window|vehicles] \
    [--url-env VAR | --from-archive --archive A --sources S] [--then <local> --out S.jsonl.gz]
obehy-rt bench --from-files <release> ... --out DIR [--warm-hours 6] [--sample-every 3600] \
    [--switch-release <release>@<instant>] [--forget-reports S] [--keep-writer]
obehy-rt compare --level facts|output [--from-files <release>] ... --out S.jsonl.gz \
    [--check REF] [--diagnostics] [--quality] [--threads 8]
obehy-rt corpus-stream [--out DIR] [--check] [names...]
```

`worker` needs an active release and a database whose last migration it knows
(`worker::schema`); a core thread owns the runner, the writer and the emitters and serves
polls, ticks, release switches and registry reloads in arrival order, while pollers, the tick
timer, publications, the lookup broker and the summaries are tokio tasks. Live and replay share
decoding, the runner and the output (`worker::pipeline`). `compare` writes the exact record of a
window (the stream contract is the module doc of `obehy-rt/src/compare.rs`) and with `--check
REF` tells where it parts from a reference; `uv run obehy rt report --from-stream` makes the
replay report of one written with `--diagnostics`.

## Pinned references

Written by `obehy-rt` itself (rewritten from Rust once after the switch, when the Python parity
emulation went). A deliberate change of behaviour rewrites the references it moves, in the same
change, and says why.

- `obehy-sources/tests/fixtures/cases.json`: the connector cases (from the Python worker's
  connector tests, plus extra payloads), compared as JSON values; a new case is added by hand.
- `tests/golden/corpus-stream/`: the output stream of each of the 106 corpus journeys
  (`tests/realtime/corpus/`); `cargo test -p obehy-rt --test corpus_stream` checks them (a last
  rounded digit, or a second or count off by one at a rounding boundary, is allowed: the
  platform's libm) and `obehy-rt corpus-stream --out tests/golden/corpus-stream` rewrites them.
- `tests/realtime/compare/`: the first minutes of four real windows, both levels. The archives
  and releases are local (gitignored); the windows, all `--since` 07:00 Prague:

  | Name | `--sources` | `--archive` | Release | Day | `--until` (full hour) |
  |---|---|---|---|---|---|
  | `pid` | `pid` | `data/rt-raw-prod` | `20261015T014009Z-f4453d48b8d2` | 2026-10-15 | 07:01 (08:00) |
  | `duk-sz-arriva` | `duk,sz-mapa,arriva-express` | `../rt-data/rt-raw` | `20261014T065758Z-d732b1d05827` | 2026-10-06 | 07:03 (08:00) |
  | `dpmd` | `dpmd` | `data/rt-raw` | `20261014T065758Z-d732b1d05827` | 2026-10-12 | 07:30 (08:00) |
  | `mpvnet` | `mpvnet` | `data/rt-raw` | `20261014T065758Z-d732b1d05827` | 2026-10-12 | 07:02 (08:00) |

  ```
  obehy-rt compare --level output --from-files data/releases/<release> --archive <archive> \
      --sources <sources> --from <day> --to <day> --since <day>T07:00 --until <day>T<until> \
      --out OUT.jsonl.gz --check tests/realtime/compare/<name>-output.jsonl.gz
  ```

  (`--level facts` takes no release; `--out` the reference itself rewrites it.) Full hours were
  kept in `data/compare/` (gitignored).

## Database tests

`cargo test -p obehy-rt --test store_db` (the store, warm start's reader, the lookup broker,
alerts) and `--test worker_db` (the worker end to end against a local stub serving archived DÚK
payloads on a simulated clock, compared row for row with replay of its own archive; a schema it
does not know; a release switched to mid-run) run with `OBEHY_TEST_DATABASE_URL` or `[database]
test_url` and pass without running otherwise. They make small databases through
`uv run python rust/tools/store_db.py` (create and migrate, a bounded subset release from the
keys `worker_db.rs` collects, the broker fixture, a dump to compare two databases) and drop
them. `worker_db` also needs `data/releases/20261014T065758Z-d732b1d05827` and
`../rt-data/rt-raw/duk`. No request leaves 127.0.0.1.

Examples: `live_replay` (a live worker's archive replayed into a database, then `store_db.py
dump` on both), `warm_read` (warm start's read rate).
