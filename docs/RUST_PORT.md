# The realtime worker in Rust

The plan for moving Oběhy's realtime side from Python to Rust: the whole worker, from polling a
source to writing PostgreSQL and the GTFS-RT files, becomes one Rust program. The API, the
static pipeline, release loading, migrations and the retention jobs stay in Python. The two
meet only where they already meet today: the database, a few files, and the config.
`BASE_PLAN.md` sections 18–28 are the realtime design; this document is only about where it
runs. Nothing here changes behaviour: the Rust worker is done when it writes exactly what the
Python one wrote from the same input, and then the Python one goes.

## Why a separate program, not a Python extension

The first version of this plan ported only the core and called it from Python through PyO3.
That kept Python in the middle of every tick: facts converted into Rust every poll, effects
and a published snapshot converted back every tick, and the worker split across two languages
in one process. A separate program has none of that:

- **The boundary is one that exists.** The API reads `rt.*` and `history.*` and records lookup
  requests; it never calls the worker. The database schema is already the contract and is
  already tested from both ends.
- **State stays in one place.** `FeedState` lives in the worker's memory and is rebuilt by warm
  replay; nothing else needs to see it, so nothing has to cross a boundary per tick.
- **Shipping is simpler.** A static binary in its own image, no wheels, no Python ABI to match;
  the API image stays pure Python.
- **The switch is clean.** One program stops and the other starts.

The cost is porting more: about 16,000 of `realtime/`'s 17,700 lines instead of the core alone,
including egress, the archive, the lookup broker and the writer.

The port has two goals besides parity: a worker whose memory stays flat for weeks (the Python
one grows to about twice a freshly started process and pushes the server into swap), and a
worker that is a lot faster. Both are measured, and both are exit checks (see "Memory" and
"Speed").

## Why now, and what is ready

The 2026-11-20 cleanup (`PROGRESS.md`, phases 0–5) left the realtime side in the shape a port
needs:

- **One rule per concept.** Passed calls (`Instance.left_index`), lifecycle, the journey key
  (`[journey]` in policy), the published delay (`lateness_s`), vehicle identity: one
  implementation each. Thresholds live in `policy-v18.toml`, not in code.
- **A pure core behind a narrow boundary.** `core.py`, `timeline/`, `infer/`, `model.py` and
  `facts.py` touch no database, network or files (import-linter keeps it so). In: facts, a
  closed, versioned set (`facts.py`, schema 5; `model_json.py` reads and writes it). Out: eight
  effect types (`model.Effect`).
- **Connectors that only decode.** `sources/` turns a payload into facts; polling, egress and
  archiving are generic and driven by the manifests (`sources/*.toml`). Shared decoding
  helpers are in `sources/decoding.py`.
- **A reference to match.** The raw archive keeps every payload, replay runs from it without a
  database, the 106 real-journey goldens (`tests/golden/corpus`) pin each journey's outcome,
  and the index parity test pins the index's two loaders to one answer.
- **Time in one place.** `times.py` holds every conversion the worker makes; it is ported once.

It is not enough by itself: the goldens hold a summary of each journey, not everything the
worker writes, and two behaviours were deliberately left as they were. Step R0 closes both.

## What runs where

### Rust: everything that runs in `obehy rt worker`, `record`, `replay`, `warm` and `bench`

| Python today | Rust |
|---|---|
| `times.py`, `geo.py` | `time`, `geo` |
| `facts.py`, `model.py`, `model_json.py` | `model` (facts read and written in the same JSON) |
| `policy.py`, `manifest.py` | `config`: policy and manifests, read with serde from the same TOML |
| `sources/` | `sources`: format readers, declarative decoding, the named normalizers, the code decoders (see below) |
| `index.py`, `index_sql.py`, `index_files.py` | `index`, filled from `static.*` or a release's serving Parquet |
| `infer/`, `timeline/` | `infer`, `timeline` |
| `core.py`, `runs.py`, `platforms.py`, `runner.py` | `core`; the runner's invariants asserted in it |
| `emit/represent.py`, `emit/gtfs_rt.py`, `emit/alerts.py` (GTFS-RT part) | `publish`: the published state and the GTFS-RT bytes |
| `emit/vehicles.py`, `emit/pairing.py` | `vehicles`: the vehicle book |
| `emit/db.py`, `emit/storage.py`, `emit/stored.py`, `emit/compositions.py`, `emit/alerts.py` (writing) | `store`: writing in sets (COPY), partitions, reading stored observations back |
| `archive.py`, `record.py` | `archive`: the same layout, readable by both while both exist |
| `worker.py`, `runtime/` (scheduler, fetch, egress, demand, lookups), `alerts.py`, `compositions.py`, `phases.py`, `logs.py` | `worker`: the shell |
| `replay.py`, `warm.py`, `bench.py` | commands of the `obehy-rt` binary |

### Python: the API, the static side, and the tools that read what the worker leaves

- **The API**, unchanged. It records lookup requests (`rt.lookup_request`, `NOTIFY
  obehy_lookup`) as it does today; the Rust broker listens instead of the Python one.
- **Releases, migrations, the static pipeline.** Migrations stay in
  `src/obehy/release/migrations/` and are applied by Python; the worker refuses to start on a
  schema version it does not know.
- **Jobs** (`jobs.py`, `disk_guard.py`): retention and the disk guard are set-wise SQL and file
  deletions run by `obehy jobs`, not part of the worker.
- **Checks and analyses over output**: `gtfs_rt_check.py`, and the replay diagnostics
  (`quality.py`, `quality_checks.py`, `evaluate.py`, `keyless_report.py`) rewritten to read the
  replay's output stream (below) instead of hooking the core.
- **What the API and the release side read of the realtime config.** Today they import from
  `realtime/`: Prague time at the edge (`PRAGUE`, `local_date`, `instant`, `ServiceTime`),
  three policy values (`journey.namespaces`, `time.vehicle_day_gap_s`, `guard.free_gb`), the
  declared lookups and the sources' descriptions (`release/feed_list.py`). These move into a
  small `obehy/realtime_config.py` that reads the same TOML files, only those fields. Rust owns
  the full schema of both files; a test loads every manifest and policy file through both.

### The contract between them

| | Written by | Read by |
|---|---|---|
| `rt.observation`, `history.*`, `rt.trip_state_current`, `rt.trip_part_current`, `rt.vehicle_state_current`, `rt.alert`, compositions, `rt.lookup_fetch`, the physical-vehicle policy | Rust | the API, the jobs |
| `rt.lookup_request` + `NOTIFY obehy_lookup` | the API | Rust |
| `static.*`, `active.*`, `rt.alert_target` | Python (releases, SQL views) | Rust |
| the raw archive (`<root>/<source>/<channel>/<day>/`) | Rust | Rust replay, the jobs |
| `/srv/gtfs-rt/<feed>.pb` | Rust | nginx |
| `policy-vNN.toml`, `sources/*.toml`, `obehy.local.toml` | people | both |

## Config-driven sources

Porting the connectors is the moment to make them data where they can be. A manifest already
says how a channel is polled; it also says how its payload is read:

```toml
[channel.decode]
reader = "json"                       # gtfs-rt | json | geojson | dpmd-js | …
entities = "VehicleList"
skip = { State = 255 }
vehicle_key = "{ID}"
trip_key = { by = "CISLineID", cases = [
  { match = 0, feed = "czptt", namespace = "czptt:train_number", template = "{RouteID}" },  # DUK-Q8
  { feed = "jdf", namespace = "cis:line_trip", template = "{CISLineID:06}:{RouteID}" },      # DUK-Q9
] }
position = { lat = "Latitude", lon = "Longitude", crs = "EPSG:4326", time = "GPSPositionDT" }
time = { clock = "offset", none_before = "1971-01-01" }   # DUK-Q2; the skew comes from policy
bearing = { field = "Azimut", zero_is_none = true }        # DUK-Q13
state = { field = "State", pre_trip = [2, 3] }             # DUK-Q5
normalize = [{ quirk = "DUK-Q1", fn = "offset_applied_twice", against = "LastActivityDT", tolerance = "5m" }]
```

The shape above is a sketch; R2 fixes it against the eight sources.

- **Readers** turn bytes into entities: GTFS-RT protobuf, JSON, GeoJSON, and code readers for
  formats that are not data (DPMD's JavaScript literals, DPMD-Q2). A recorder filter (Arriva's
  fleet-wide feed, DPMD's parked vehicles) is a reader option, not a separate code path.
- **The mapping** says which field becomes which fact, with templates for keys, a time
  convention, a CRS (S-JTSK/Křovák through proj for SŽ), units and the "means none" values.
- **Normalizers** are the quirks that need logic: named Rust functions, each carrying its quirk
  ID from `docs/sources/`, parameterised from the manifest. The mapping itself chooses only by
  a field's value (`skip`, `cases`, "this value means none"); anything that compares, computes
  or looks at two fields at once is a normalizer.
  This is what keeps the manifest from turning into a language.
- **Code decoders** stay for what is not one entity per vehicle: ČD compositions (a lookup with
  its own `plan`), SŽ station boards, and alerts if their mapping does not fit. The manifest
  names the decoder.

A new source with a GTFS-RT or plain JSON feed then needs a manifest and no code. Which of
today's eight become fully declarative is decided per source in R2; the expectation is PID
positions, mpvnet and Arriva Express declarative, DÚK and SŽ map declarative with normalizers,
DPMD a code reader with a declarative mapping, and ČD and SŽ tabule code.

`DECODER_VERSION` stays per channel, in the manifest: changing a mapping is a decoder change
like any other, and replay names the version it decoded with.

## Memory

The Python worker's resident memory roughly doubled over a run compared with a freshly
started one, and the server (6 GB, about half of it for PostgreSQL) went into swap. The cause,
found in R0's first measurement (2026-11-21): a key names every variant of a trip in the
timetable year, and the index loaded them all. A restarted worker loads what its live journeys
need; within hours every key named in the last `keep_index_s` held its whole year again (on SŽ
at noon, 23,260 trips for 1,220 live journeys). The index now loads only trips running on its
loaded dates and the others when their date is loaded: 5,353 trips, Python heap 466 -> 212 MB,
output byte-identical. Ruled out on the way: `gc.freeze()` leaving the old runtime behind at
compaction (a dropped runtime holds no reference cycles).

What remains for the Python worker is the allocator keeping freed memory resident (pymalloc
returns an arena only when it is empty; glibc keeps an arena per thread), and the vehicle
book, which never forgets a (source, vehicle) it saw. Neither is measured on production yet.

Two parts:

1. **Fixed in Python first** (R0, the index part done): the soak test below, run against the
   Python worker, says whether anything else grows.
2. **Designed out in Rust:**
   - Every long-lived container has a stated rule for when an entry leaves, written next to it
     and tested: the index (keys not named for `keep_index_s`, dates before yesterday), plans,
     instances and vehicles (`forget_after_s`), hop runtimes, pairings (`hold_s`), the vehicle
     book (reports not seen for a policy number of days), GTFS-RT entity caches, alert caches.
     A container without a rule is a defect.
   - Compaction in place: the index sweeps, then shrinks its maps (`shrink_to_fit`), instead
     of a second index being built beside the first.
   - Compact data: IDs interned to integers per release, times as integer seconds, the index
     in flat vectors rather than one heap object per call.
   - An allocator that returns memory (mimalloc or jemalloc with decay), chosen by the soak
     test, not by taste.
   - The worker logs resident memory and every container's size in its periodic summary, as
     the Python one logs phases.

**The exit check** is a soak test: replay several days of archive at full speed through the
whole worker (index sweeps, compactions, release switches included) while sampling resident
memory per simulated hour. After the first day, the daily peak must not rise from day to day,
and the steady state must stay within 1.2× of the memory right after warm start.

## Speed

Known numbers from the Python worker (2026-10): about 43 µs of core per observation, the live
core near half a CPU before PID multiplied the observation rate, and a warm start of about
8 minutes over a 4-hour window (about 3,000 observations a second), most of it waiting on the
index's reads. R0 measures again per phase, with PID included; the targets are then fixed
against that baseline:

| Measure | Target |
|---|---|
| Core per observation (step, refresh and estimates, amortised) | ≥ 10× faster |
| Replay from files, observations a second | ≥ 10× faster |
| Warm start, to ready | under a minute |
| Live CPU, the whole worker | ≤ 1/5 of the Python worker's |
| Resident memory, steady state | ≤ 1/3 of the Python worker's after warm start |

Where the speed comes from, besides compiled code:

- **Feeds in parallel.** Each feed's state is independent (`FeedState` per feed); feeds step
  on separate threads, and their outputs are merged in sorted order, so parallelism cannot
  change the output. The vehicle book, which spans feeds, runs after them.
- **Decoding in parallel.** Payloads decode on a pool, ahead of the core, in replay as live.
- **Reads in bulk.** The index and warm start read with binary COPY and few round trips; warm
  replay decodes stored observations on a pool while the core runs.
- **Writes in bulk.** Binary COPY for history and observations, as sets, off the core's thread.
- **No per-tick rebuilding.** GTFS-RT entities and state rows are re-encoded only for what
  changed, as the Python emitter already does for GTFS-RT.

The harness decides correctness and the bench decides speed; a speed-up that changes the
harness output is a behaviour change and goes through the usual breakdown first.

## R0: Before any Rust

1. **An exact comparison harness, at two levels.**
   - *Facts:* every payload of a window decoded, written as facts JSON (`model_json.py`) in
     archive order, `PayloadError`s included. This checks the connectors alone.
   - *Output:* everything the worker would write from a window, in one canonical form (sorted
     keys, fixed float rounding, Prague times as ISO): every effect of every step and tick, the
     state rows per tick, the history rows, the alerts, and the decoded GTFS-RT. It is taken at
     the writer's interface (a recording writer), so it needs no database.

   `obehy rt compare --from-files <window> --level facts|output` writes it from Python; the
   `obehy-rt` binary writes the same. It runs on the corpus goldens' 106 journeys and on the
   replay windows used in the cleanup (PID 2026-10-15 07–08, DÚK/SŽ/Arriva 2026-10-06 07–08),
   plus a DPMD and an mpvnet window. Python's output becomes the pinned reference before any
   Rust exists.
2. **Decide the two behaviours left open**, or record that they are ported as they are:
   - Hop running times are learned from the arrival at the first call (`timeline/runtimes.py`)
     but applied from the departure. The corpus could not judge a change; the harness can.
   - `platforms._boarding_point` is a stub kept as the hook for the board-platform work.
     Port it as a stub unless that work comes first.
3. **Measure the baseline.** `obehy rt bench` over the same windows, split by phase (decode,
   step, refresh, estimate, state, GTFS-RT, writes), PID included, plus a warm start and a
   multi-day soak for memory. It fixes the speed targets' baseline.
4. **Fix the Python worker's memory growth** (see "Memory"): find which suspect it is with the
   soak, fix it, check the fix with the harness, and ship it on its own.
5. **The replay diagnostics read the output stream.** `quality`, `quality_checks`, `evaluate`
   and `keyless_report` move off the core's hooks onto the output stream (adding to the stream
   what they need), so they keep working when the core is no longer Python.
6. **Freeze the Python worker** for the length of the port: fixes only, each one added to the
   reference and to the Rust side in the same change.

## R1–R8: The port, bottom up

Each step ports a layer with its unit tests (from `tests/unit/realtime/`, rewritten as `cargo
test`) and is done when those pass and its check holds. Layers are compared with Python where
that is cheap; the whole worker is judged by the harness.

| Step | Ports | Done when |
|---|---|---|
| R1 | workspace, `time`, `geo`, `config` (policy, manifests), `model` and the facts' serde | the time scenario table (T1–T15) passes; every fact round-trips with `model_json.py`; every manifest and policy file loads the same in both |
| R2 | `sources`: readers, the declarative mapping, normalizers, code decoders; manifests gain `[channel.decode]` | the facts harness: every payload in the windows and the corpus decodes to identical facts and identical errors |
| R3 | `index`, from `static.*` and from serving Parquet | the index parity test's fixtures give the same trips, runs, visits and station calls |
| R4 | `timeline`: path, plan, stops, progress, commit, estimate, runtimes | the timeline unit tests pass; plans and projections match Python's on the corpus trips |
| R5 | `infer` (keyed, keyless) and fusion | the binding and keyless tests pass |
| R6 | `core` and the runner; `obehy-rt replay --from-files` | the output harness, effects only: all 106 goldens and every window identical; core and replay speed at target |
| R7 | `publish`, `vehicles`, `store`; warm start from `rt.observation` | the output harness in full; a window replayed into the test database by each gives the same rows; warm start rebuilds the same state, under a minute; the memory soak passes |
| R8 | `archive`, `worker`: scheduler, fetch, egress, demand polling, the lookup broker, alerts, phase timing | fetching and egress tested against a local HTTP server, including SŽ's fail-closed rule and the proxy list's reload; one live day on the dev database without a difference against a Python replay of the same archive; live CPU and memory at target |

Then **the switch**: the image runs `obehy-rt worker`; live and replay switch together; the
Python worker, its connectors and its tests are deleted, with `obehy rt compare` and the Python
facts decoders going last. AGENTS.md's rule is "no twin implementations": until the switch the
Python worker is the reference under test only and is never run live alongside Rust; after it,
it is not kept as a fallback.

## Carrying the rules over

- **Purity, by crate.** A Cargo workspace in `rust/`:
  - `obehy-core`: `time`, `geo`, `model`, `config` types, `index`, `infer`, `timeline`, `core`,
    `publish`, `vehicles`; no I/O crate.
  - `obehy-sources`: readers, mapping, normalizers, decoders; bytes in, facts out, no I/O.
  - `obehy-rt`: the binary; the shell, `store`, `archive`, the commands; the only crate with
    tokio, a database driver or HTTP.

  The dependency graph between them is the import-linter contract.
- **The core stays synchronous.** The shell is async (tokio); the core and the decoders are
  plain functions it calls.
- **Determinism.** Output in sorted order (`BTreeMap`, or sorting before emitting), never hash
  order. Where the Python worker relies on dict insertion order, Rust keeps it explicitly
  (`IndexMap`); the harness finds the places.
- **Floats.** `sin`, `cos` and `atan2` may differ in the last bit between Python's libm and
  Rust's (`geo.py`, `timeline/path.py`, `timeline/progress.py`, about ten calls). The harness
  rounds at the edges (metres to 0.01, seconds to whole), and every place the core compares a
  float against a threshold is listed in R4, so a last-bit difference cannot flip a decision
  unseen. Křovák goes through proj in both.
- **Time zones.** `chrono-tz` and `zoneinfo` must read the same tzdata: pin both to one release
  and check it in the time tests.
- **Policy, not constants**, in Rust as in Python; so are the manifests' mappings.
- **Quirks stay in sources.** The core gets facts; every normalizer names its quirk.
- **SQL stays the place for sets.** The writer writes in sets and the views stay in SQL; the
  port moves no SQL into Rust loops.
- **Secrets** stay where they are: `obehy.local.toml` and the proxy list file named by
  `OBEHY_EGRESS_PROXY_LIST_FILE`, read by the binary the same way.

## Building and shipping

- **Locally:** the stable toolchain through `rustup`, `x86_64-pc-windows-msvc`, linking with the
  installed VS2026 MSVC (no compilers in WSL). `cargo build` in `rust/`; the Python harness
  calls the built `obehy-rt` as a subprocess.
- **CI:** `cargo fmt --check`, `cargo clippy -D warnings` and `cargo test` next to the Python
  checks; both harness levels against the pinned reference on every change to `rust/` or
  `realtime/`.
- **The image:** the `Dockerfile` gains a Rust build stage producing `obehy-rt`, in a worker
  image of its own; the API image loses the worker. Changing compose, the server and nginx is
  the user's to do; the GTFS-RT files keep their paths.

## What to decide before starting

- The workspace's layout and names (`rust/`, `obehy-core`, `obehy-sources`, `obehy-rt`).
- The `[channel.decode]` shape, settled in R2 on the eight sources.
- Whether hop timing changes in R0 or is ported as it is.
- Whether any replay diagnostic is worth porting into Rust instead of reading the stream.
