# The realtime worker in Rust

The plan for moving Oběhy's realtime side from Python to Rust: the whole worker, from polling a
source to writing PostgreSQL and the GTFS-RT files, becomes one Rust program. The API, the
static pipeline, release loading, migrations and the retention jobs stay in Python. The two
meet only where they already meet today: the database, a few files, and the config.
`BASE_PLAN.md` sections 18–28 are the realtime design; this document is only about where it
runs. Nothing here changes behaviour: the Rust worker is done when it writes exactly what the
Python one wrote from the same input, and then the Python one goes.

**Status: done (2026-11-21).** R0–R8 are in, byte-identical to the Python worker on every
harness, and the switch is made: the image runs `obehy-rt worker`, and the Python worker, its
connectors, its tests and the Python harness are deleted. What follows is the plan as it was
carried out, kept for its reasons; "Python today" and "until the switch" read as history.

**After the switch (2026-11-21): parity emulation removed.** With the Python worker gone,
byte-identical output stopped being a goal, and the code that only reproduced Python's exact
behaviour went: `obehy_core::py` (Python's Unicode tables, `json.dumps`, float repr, `int()`
and `float()` of text), `timedelta`'s half-even rounding, CPython's `hypot`, the UCRT `pow`
workaround, GEOS's Douglas-Peucker details, PROJ's per-point choice of the Křovák operation
(one operation now, EPSG:5239), the vendored tzdata (jiff's bundled database instead), a
hand-written `json.loads` and CPython's `html.parser` (serde_json and scraper instead), and
the Python-pinned parity fixtures and examples. The rules are unchanged; the pinned streams were
rewritten from Rust once, the differences being rounding (a microsecond, a second at a half),
last float digits and Křovák positions outside Czechia's area (about a metre).

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
  (`replay_report.py`, `quality.py`, `quality_checks.py`, `evaluate.py`, `keyless_report.py`),
  which read the replay's output stream (below), not the core: `obehy rt report --from-stream`
  makes `report.json` from a stream the Rust binary writes with `--diagnostics` (`--quality`).
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

**Rust (R7, 2026-11-21)**: `obehy-rt bench --warm-hours 6` over SŽ's map 2026-10-09..15
(6.2 M observations, 6 h warm start, two releases with the switch on 10-15, Windows, system
allocator). The serving files stand in for PostgreSQL and hold the whole release in memory
(295 MB for czptt, 1.55 GB for both feeds), read once and reported apart. The vehicle book was
the one container without a rule: on SŽ it gained about 8,800 reports a day, and the daily peak
rose with it (537, 548, 558 MB on 10-12..14). With `[emit] forget_reports_s` (two days; Rust
only, Python ignores the key) it levels at about 18,500 reports and the weekday peaks are
516, 519, 519, 512 MB. At 10-14 13:00 the 4.5-day process holds 503 MB, a process warm-started
for the same moment 454 MB (1.11×; 1.32× without the files' 295 MB, most of it the book's two
days of reports). A release switch opens a new writer, so the live worker's book also starts
over with each release. Every other container follows the live state (index, plans, hops,
state rows, GTFS-RT entities). Allocator: the system one; mimalloc kept 330 MB more after
reading the files and doubled at the release switch (1.09 GB), with the same day-to-day
growth; jemalloc does not build for the MSVC target. The soak ran on Windows: repeat it in the
Linux image (glibc) before the switch; `--features mimalloc` stays available.

## Speed

Known numbers from the Python worker (2026-10): about 43 µs of core per observation, the live
core near half a CPU before PID multiplied the observation rate, and a warm start of about
8 minutes over a 4-hour window (about 3,000 observations a second), most of it waiting on the
index's reads.

**Baseline (R0, 2026-11-21)**: `obehy rt bench --from-files` (the worker's path: archive,
decode, core, ticks with estimates, state tables and GTFS-RT; rows made and dropped, not sent;
the index read from the release's serving files, not PostgreSQL), Python 3.13 on a Ryzen AI 9
465, one run with other jobs on the machine. CPU seconds per phase over the hour, µs per
observation in brackets:

| Hour | Observations | decode | step | refresh | estimate | state | GTFS-RT | writes | index | obs/s | peak RSS |
|---|---|---|---|---|---|---|---|---|---|---|---|
| PID 2026-10-15 07–08 | 480,778 | 72 (149) | 181 (376) | 8 (16) | 292 (607) | 170 (353) | 96 (199) | 32 (67) | 127 (265) | 475 | 3.5 GB |
| DÚK+SŽ+Arriva 2026-10-06 07–08 | 180,744 | 9 (50) | 36 (199) | 2 (8) | 42 (231) | 44 (242) | 25 (136) | 9 (48) | 80 (442) | 712 | 3.0 GB |

Core (step + refresh + estimate) is 1.0 ms per PID observation and 0.44 ms per DÚK+SŽ+Arriva
observation, far above October's 43 µs (the step alone, on DÚK, before PID); on PID, estimates
are the largest phase. Warm start (`warm.warm_start`, no writer) over 4 hours from files,
decoding standing in for the read: DÚK+SŽ+Arriva 2026-10-06 05–09, 670,353 observations in
643 s (1,042/s; index 260 s, estimates 190 s, core 118 s, decode 52 s, peak 3.1 GB); PID
2026-10-15 05–09, 1,498,899 observations in 3,094 s (485/s; estimates 1,668 s, core 512 s,
index 475 s, decode 312 s, peak 3.8 GB). Both are the whole window (`mode = "window"`); the
worker's `vehicles` mode reads fewer. Neither ran on PostgreSQL (the test database's disk was
too full to load a release), so the index's database reads and reading `rt.observation` are
not in them. The targets are fixed against this baseline:

| Measure | Target |
|---|---|
| Core per observation (step, refresh and estimates, amortised) | ≥ 10× faster |
| Replay from files, observations a second | ≥ 10× faster |
| Warm start, to ready | under a minute |
| Live CPU, the whole worker | ≤ 1/5 of the Python worker's |
| Resident memory, steady state | ≤ 1/3 of the Python worker's after warm start |

**Rust (R7, 2026-11-21)**: `obehy-rt bench --from-files`, the same hours, same machine (decoding
on 6 threads, CPU summed; the serving tables read before the clock starts, so `index` is the
lookups only):

| Hour | decode | step | refresh | estimate | state | GTFS-RT | writes | index | obs/s | peak RSS (files) |
|---|---|---|---|---|---|---|---|---|---|---|
| PID 2026-10-15 07–08 | 25 (52) | 10.7 (22) | 1.0 (2) | 15.5 (32) | 57 (119) | 10.4 (22) | 3.6 (8) | 0.8 | 4,765 | 2.14 GB (1.59) |
| DÚK+SŽ+Arriva 2026-10-06 07–08 | 2.1 (12) | 2.5 (14) | 0.3 (2) | 2.7 (15) | 12.7 (70) | 3.3 (18) | 1.0 (6) | 0.4 | 7,777 | 1.75 GB (1.55) |

Core is 18× faster on PID and 15× on DÚK+SŽ+Arriva; replay 10× and 11× in observations a
second; the whole path's CPU 1/8 and 1/10 of Python's. The state tables are now the largest
phase (only 3× faster; R7 shared each journey's calls JSON between its row and the writer's
cache, which took them from 76 to 19 s on two DÚK+SŽ+Arriva hours): the next speed item. Warm
start over 4 hours from files: DÚK+SŽ+Arriva 27 s (23×), PID 113 s (27×; the core thread,
estimates 61 s, is the limit, so PID in `window` mode misses the minute). Reading
`rt.observation` with binary COPY runs at about 100,000 observations a second, beside the
core, so the database adds no time (checked on a 20-minute window; a 4-hour one did not fit
the test database's disk).

**Speed pass (2026-11-21)**: the same bench, same hours (estimates on one thread here, so each
phase is still one core's CPU):

| Hour | decode | step | refresh | estimate | state | GTFS-RT | writes | index | obs/s | peak RSS |
|---|---|---|---|---|---|---|---|---|---|---|
| PID 2026-10-15 07–08 | 25 (52) | 6.8 (14) | 0.7 (2) | 9.0 (19) | 23.3 (49) | 10.4 (22) | 2.9 (6) | 0.5 | 8,719 | 2.07 GB |
| DÚK+SŽ+Arriva 2026-10-06 07–08 | 2.2 (12) | 1.9 (11) | 0.3 (1) | 1.7 (9) | 4.7 (26) | 3.1 (17) | 0.7 (4) | 0.2 | 13,998 | 1.72 GB |

Warm start over 4 hours from files (`obehy-rt warm --from-archive --mode window --threads 6`):
PID 50 s (was 109–113; 62×), DÚK+SŽ+Arriva 15 s (was 27; 42×). Found by timing the phases'
parts (no sampling profiler without elevation on the dev machine), and changed:

- **Estimates in parallel.** A journey's estimate reads only its own instance, trip, plan,
  the index, policy and hops, and writes only its instance; `estimate_all_with` estimates
  the due journeys on up to `Runner::estimate_threads` threads (the core thread among them;
  cached plans read there, missing ones built after in journey order) and puts each instance
  back in its own slot, so the result does not depend on the thread count. Warm start,
  replay, compare and history use `--threads`, the worker up to 4; bench keeps 1. PID is one
  feed, so feeds in parallel would not have helped it; that lever is still open for
  multi-feed sets.
- **Estimates without waste.** Each hop's ratio is computed once per journey for both the
  running extras and the least drives, looked up without building its key, and memoized per
  tick across journeys (`RatioMemo`); estimates and the monotone repair work on the four
  estimated fields and build new calls only when either step would have (the calls' pointer
  identity, which the state writer reads, is unchanged).
- **Projections.** `math.hypot` (`py_hypot`) no longer allocates; the nearest projection
  (the lagging-fix check) starts from the nearest run of 16 segments and skips segments and
  runs that cannot be nearer, by bounds whose rounding is far inside their margin; candidate
  segments clearly out of reach skip the exact norm.
- **Hashing.** `FeedState`'s maps, the index's, the plan cache's and the serving tables' use
  FxHash (insertion order or no order is iterated, never the hash).
- **State tables in place.** The writer keeps each feed's rows by vehicle and journey and
  rebuilds a trip's rows only when its calls or what else they read changed, sending the same
  deletes and upserts in the same key order. JSON keys and enum texts are borrowed (`Text`),
  and ISO times are written without `format!`.
- **Serving tables** decode their row groups on 6 threads (files only; the worker reads
  PostgreSQL).

Left on the PID core thread over the 4 hours: step 23 s (the tracker's update and the rest of
`step`), the estimates' share 13 s, the index 4 s (mostly reading the files), refresh 3.5 s.
GTFS-RT (10 s an hour on PID) was not touched. Resident memory after the 4-hour PID warm
start is higher with 6 estimating threads (2.46 GB against 2.19, Windows heap; 1.97 GB with
one thread), so check it in the Linux image with the soak.

Where the speed comes from, besides compiled code:

- **Feeds in parallel.** Each feed's state is independent (`FeedState` per feed); feeds step
  on separate threads, and their outputs are merged in sorted order, so parallelism cannot
  change the output. The vehicle book, which spans feeds, runs after them.
- **Estimates in parallel.** Within a feed, the due journeys' estimates are independent and go
  back to their own slots (done; see the speed pass above).
- **Decoding in parallel.** Payloads decode on a pool, ahead of the core, in replay as live.
- **Reads in bulk.** The index and warm start read with binary COPY and few round trips; warm
  replay decodes stored observations on a pool while the core runs.
- **Writes in bulk.** Binary COPY for history and observations, as sets, off the core's thread.
- **No per-tick rebuilding.** GTFS-RT entities and state rows are re-encoded only for what
  changed, as the Python emitter already does for GTFS-RT (state rows: done in the speed
  pass).

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
   what they need), so they keep working when the core is no longer Python. Done: the stream
   gains, only when asked, `poll` and `predictor_sample` records (`--diagnostics`) and
   `journey_sample` (`--quality`; floats exact), specified in `compare.py` and emitted by
   `emit/samples.py`; an import-linter contract keeps the diagnostics off the core.
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

Then **the switch** (done 2026-11-21): the image runs `obehy-rt worker`; live and replay
switch together; the Python worker, its connectors and its tests are deleted, with `obehy rt
compare` and the Python facts decoders going last. AGENTS.md's rule is "no twin implementations": until the switch the
Python worker is the reference under test only and is never run live alongside Rust; after it,
it is not kept as a fallback.

What stays and what goes, prepared so that the switch only deletes (the import-linter contract
"What survives the switch …" in `pyproject.toml` keeps it true):

- **Stays Python:** `obehy/realtime_config.py` (Prague time at the edge, the policy values and
  manifest fields the API, releases and jobs read; `realtime/times.py`, `policy.py` and
  `manifest.py` build on it until then); in `realtime/`: `__init__`, `jobs.py` (retention, the
  disk guard, and what they read of the archive layout and the partition names),
  `disk_guard.py`, `gtfs_rt_check.py`, the diagnostics `replay_report.py`, `quality.py`,
  `quality_checks.py`, `evaluate.py`, `keyless_report.py`, `journey_view.py`, and
  `compare_check.py`, `jsonl.py`, `names.py`; `commands.py` cut down to `obehy jobs`,
  `obehy rt report --from-stream`, `check-gtfs-rt`, `compare-diff` and `corpus pin`
  (`compare_check.py` and `compare-diff` went after the switch: `obehy-rt compare --check`
  reports where two streams part).
- **Goes:** `alerts`, `archive`, `bench`, `compositions`, `core`, `facts`, `geo`, `index`,
  `index_files`, `index_sql`, `logs`, `manifest`, `model`, `model_json`, `phases`,
  `platforms`, `policy`, `record`, `replay`, `runner`, `runs`, `times`, `warm`, `worker`, and
  `emit/`, `infer/`, `runtime/`, `timeline/`, the connectors `sources/*.py`; last, `compare`,
  `canonical` (it only turns Python core objects into the stream) and the facts decoders. With
  them `tests/unit/realtime/`, `tests/realtime/`, the realtime tests in `tests/db/`, and the
  fixture generators in `rust/tools/`. `tests/db/test_api.py` writes its realtime rows through
  the Python writer: it is rewritten to insert them with SQL first. The diagnostics' unit tests
  write their streams with the Python replay; they move to hand-written stream records.
- **Files both read, which must survive:** `src/obehy/data/realtime/` (policy, SR70 catalogue)
  and `src/obehy/data/vehicles/attributes.toml` stay where they are. The manifests
  (`src/obehy/realtime/sources/*.toml`) and the SQL compiled into `obehy-rt`
  (`src/obehy/realtime/sql/`) lived in directories emptied of Python; they moved, in the
  switch, to `src/obehy/data/realtime/sources/` and `src/obehy/data/realtime/sql/`, with
  `realtime_config.SOURCES`, `obehy-rt`'s `Paths` (and the repository root it finds by them),
  its `include_str!` paths and the `Dockerfile`'s `COPY` lines.
- **The image:** the `Dockerfile`'s `worker` target is `obehy-rt worker` in Debian trixie
  (glibc) with those files under `/app` as in the repository; compose's `realtime` service
  takes it (`ghcr.io/sap1k/obehy-worker`, published by CI next to the server image) in place of
  `command: ["realtime"]`.
- **How the switch came out:** `obehy rt corpus pin` stays (it only copies archive days).
  The Python harness script `tests/realtime/compare_windows.py` went (no Rust test used it);
  its four windows, which the pinned `tests/realtime/compare/` streams were written from, are
  listed in `rust/README.md`. The fixture generators in `rust/tools/` went; their outputs are
  frozen references. `rust/tools/store_db.py` stays, cut to what the Rust database tests need
  of Python (create and migrate a test database, load a subset release from a plan the Rust
  test makes, the broker fixture, a dump). `tests/db/test_api.py` and the jobs tests write the
  worker's rows with SQL (`tests/db/worker_rows.py`); the diagnostics' tests use hand-written
  stream records. The stream contract that was `compare.py`'s docstring is the module doc of
  `rust/obehy-rt/src/compare.rs`.

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
  order. (During the port, Python's dict insertion order was kept with `IndexMap`; after the
  switch the core state is ordered by key.)
- **Floats.** Plain `f64` and the platform libm; the canonical stream rounds at the edges
  (metres to 0.01, seconds to 0.001) and the corpus test allows a last rounded digit.
- **Time zones.** Europe/Prague from jiff's bundled IANA database (during the port: Python's
  own TZif file, so both read one release).
- **Policy, not constants**, in Rust as in Python; so are the manifests' mappings.
- **Quirks stay in sources.** The core gets facts; every normalizer names its quirk.
- **SQL stays the place for sets.** The writer writes in sets and the views stay in SQL; the
  port moves no SQL into Rust loops.
- **Secrets** stay where they are: `obehy.local.toml` and the proxy list file named by
  `OBEHY_EGRESS_PROXY_LIST_FILE`, read by the binary the same way.

## Building and shipping

- **Locally:** the stable toolchain through `rustup`, `x86_64-pc-windows-msvc`, linking with the
  installed VS2026 MSVC (no compilers in WSL). `cargo build` in `rust/`.
- **CI:** `cargo fmt --check`, `cargo clippy -D warnings`, `cargo test` and a release build on
  Linux next to the Python checks (the tests ran on Windows while they were pinned to UCRT's
  libm); the corpus streams are a cargo test, the compare windows need the local archives and
  are checked by hand (`rust/README.md`).
- **The image:** the `Dockerfile` has a Rust build stage producing `obehy-rt`, in a worker image
  of its own (`worker` target, `ghcr.io/sap1k/obehy-worker`); the server image has no worker.
  Pulling the new images and restarting compose on the server is the user's to do; the GTFS-RT
  files keep their paths.

## What to decide before starting

- The workspace's layout and names (`rust/`, `obehy-core`, `obehy-sources`, `obehy-rt`).
- The `[channel.decode]` shape, settled in R2 on the eight sources.
- Whether hop timing changes in R0 or is ported as it is.
- Replay diagnostics in Rust: none (decided in R0 item 5). Rust writes the diagnostics
  records; the Python diagnostics read them.
