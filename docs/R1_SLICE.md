# R1 slice: what is fixed before the first realtime code

The decisions every realtime module or stored row depends on, settled before ticket 1
(`BASE_PLAN.md` section 34). Architecture and reasons stay in `BASE_PLAN.md`; this file is the
concrete contract for the first slice and is updated when the slice changes it.

**Scope.** DÚK buses only: the `jdf` feed, keyed by CIS line + trip number. DÚK trains and SŽ
fusion are the R2 slice. The slice writes history, so the history schema is proven by replay
before live rows accumulate. The worker has no checkpoint format: it restarts by warm replay.

Everything else (API, web, lookups, keyless inference, circulations, compositions, `ref`) is
designed when its milestone starts; `ARCHITECTURE.md` already fixes its boundaries.

## 1. Core types (`realtime/model.py`)

Frozen, slotted dataclasses. Facts are a closed union; each observation stores the version of
the fact schema it was decoded with.

```text
Observation   source, channel, feed, received_at: Instant, observed_at: Instant | None,
              raw_ref (sha256, index line), decoder_version, facts: tuple[Fact, ...]
Fact          VehicleKey(source_vehicle_id) | TripKey(namespace, key)
              | Position(lat, lon, bearing?) | Delay(seconds, reference: arrival|departure|unknown)
              | SourceState(code) | StopEvent(call_ref, kind, at) | NextStop(call_ref)
JourneyKey    feed, namespace, key, service_date          public and history identity
Binding       vehicle_key → JourneyKey, method, bound_at; service_date fixed once
Instance      JourneyKey, release_id, trip_id, lifecycle, flags {off_route, stale},
              progress (last call visit, fraction), timeline (per call: arrival and departure
              intervals, estimate, status, source_class)
VehicleState  vehicle_key, binding?, last observation, position?,
              state: running | positioning | layover | unmatched | not_in_service
Effect        EmitTripUpdate | EmitPosition | WriteEvent | SnapshotJourney | LogUnmatched | …
```

- `core.step(state, observation, ctx) -> (state, effects)` is the only entry. `ctx` is the
  release index, the policy and the clock. Effects are data; the worker and replay execute them.
- A new fact type is additive. A changed meaning bumps `fact_schema_version`; stored rows are
  re-decoded from raw inside the archive window.

## 2. Time scenario table

Written as tests before `realtime/times.py`; the module is whatever makes them pass. `ST(d, t)`
is a `ServiceTime`: wall-clock time `t` (may exceed 24:00) of service date `d`, as written by
JrUtil (`BASE_PLAN.md` 19.3). Values were checked with `zoneinfo`.

| # | Case | Input | Expected |
|---|---|---|---|
| T1 | `24:xx` time | `ST(10-08, 24:30)` | 2026-10-08T22:30Z |
| T2 | night trip belongs to yesterday | 00:20 local on 10-09, key of the 23:50 trip of 10-08 | binding service date 10-08 |
| T3 | waiting for tomorrow | 23:55 local on 10-08, key of a 00:05 trip running only on 10-09 | service date 10-09, `pre_trip` |
| T4 | rollover of a running trip | keyed binding at 23:58, observations after 00:00 | service date unchanged |
| T5 | yesterday's key in the morning (DUK-Q4) | 06:00 on 10-09, key of a 22:00 trip of 10-08 not running on 10-09 | `not_in_service`; the old journey is not extended |
| T6 | spring forward | `ST(03-29, 01:30)`, `ST(03-29, 02:30)` (skipped), `ST(03-29, 05:00)` | 00:30Z, 01:30Z (read as 03:30 CEST; accepted), 03:00Z |
| T7 | fall back | `ST(10-25, 02:30)` (repeated), `ST(10-25, 05:00)` | 00:30Z (first occurrence), 04:00Z |
| T8 | source local time in the repeated hour | bare `02:30` on 10-25, received 00:31Z / 01:31Z | 00:30Z / 01:30Z (the reading closest to `received_at`) |
| T9 | source local time in the missing hour | bare `02:30` on 03-29, received 00:31Z | rejected; the observation is untimed |
| T10 | DUK-Q1: Teplice UTC labelled with the local offset | `10:00:00+02:00` in summer, `10:00:00+01:00` in winter | 10:00Z both |
| T11 | ARRIVA-Q1: local time labelled UTC | `10:00:00+00:00` in summer | 08:00Z |
| T12 | SZ-Q2: bare `HH:mm` across midnight | `23:59`, received 00:01 local | 23:59 local of the previous day |
| T13 | `vehicle_day` across midnight | journeys 22:00–23:30 and 00:15–01:00 | one vehicle day dated by its first journey; split at gaps over 3 h |
| T14 | trip spanning the fall-back change | `ST(10-25, 01:00)` → `ST(10-25, 04:00)` | 23:00Z → 03:00Z: 4 h of real time, not 3 |
| T15 | clock lags the DST switch | correctly labelled DÚK bus still sends `+02:00` on 10-25 after 01:00Z, 1 h off `received_at` | beyond `max_clock_skew`: time dropped, observation untimed |

Rules the table implies:

- Schedule `ServiceTime` → `Instant` reads wall clock: repeated hour → first occurrence,
  skipped hour → pre-change offset. DST nights are allowed to be somewhat wrong; no further
  special cases.
- Arithmetic is on UTC instants only: Python adds a `timedelta` to an aware non-UTC datetime in
  wall-clock time, which silently breaks durations across the change.
- Source local times that do not exist are rejected; source times beyond `max_clock_skew` from
  `received_at` are dropped. No offset guessing.
- The SQL helper `obehy_instant(service_date, seconds)` and `times.py` share the definition; a
  DB test checks that they agree on T1, T6, T7 and T14.
- `serving-v5.json` still describes times as "seconds after noon minus 12 h"; that contract
  text is corrected to wall-clock in JrUtil (the data already is).

## 3. Database (`0004_rt.sql`, `0005_history.sql`)

```text
rt.observation              received_at, source, channel, feed, raw_sha, raw_line,
                            decoder_version, fact_schema_version, facts jsonb, result jsonb,
                            release_id; daily partitions by received_at, dropped after the
                            policy window
rt.vehicle_state_current    one row per (feed, vehicle_key), upserted each emit tick; UNLOGGED
rt.trip_state_current       one row per journey of service dates today ± 1; the public per-call
                            model as jsonb; UNLOGGED
rt.source_health            source, channel, minute, polls, errors, rows, lag

history.derivation          id, core_version, policy_version, release_id
history.journey             PK (feed, key_namespace, key, service_date); first_bound_at,
                            latest_revision; monthly partitions by service_date
history.journey_schedule    journey, revision; release_id, trip_id, route and headsign text,
                            derivation_id
history.journey_call        journey, revision, ordinal; location_id, visit_n, passenger_service,
                            scheduled_arrival, scheduled_departure (ServiceTime seconds), name
history.actual_stop_event   journey, location_id, visit_n, event_type; revision, event_time,
                            interval_lo, interval_hi, method, confidence, source_ids, orphaned,
                            derivation_id
history.vehicle_assignment  feed, vehicle_key, journey, valid_from, valid_to, method,
                            derivation_id
history.vehicle_day         feed, vehicle_key, service_date, seq; journeys[], tour_id,
                            tour_match_share (written by the nightly SQL job)
```

- Journey key columns are repeated in every table instead of a surrogate key: partition pruning
  stays simple and rows read without joins.
- Replay rewrites a day by deleting and re-inserting `(service_date, derivation_id)`.
- `tour_id` and `tour_match_share` are reserved now and filled in R5 (`BASE_PLAN.md` section
  22), so the history schema needs no migration then.

## 4. Connector manifest and policy

Each connector has `realtime/sources/<source>.toml`; the recorder's single `sources.toml` is
split into these.

```toml
source = "duk"
manifest_version = 1

[[channel]]
name = "vehicles"
poll = { kind = "interval", seconds = 15 }
backoff = { initial_s = 15, max_s = 300 }
timeout_s = 20
request = { method = "GET", url = "https://tabule.portabo.cz/api/v1-tabule/cis/GetTraffic/0" }
feeds = ["jdf", "czptt"]            # decode routes each observation to exactly one feed
capabilities = ["vehicle_key", "trip_key", "position", "delay", "source_state"]

[channel.semantics]
clock = "source"                    # source | none (observed_at becomes an interval)
delay_reference = "unknown"         # DUK-Q6
key_namespaces = { jdf = "cis:line_trip", czptt = "czptt:train_number" }

# [[lookup]] sections arrive with the first lookup connector (R2 or later).
```

`src/obehy/data/realtime/policy-v1.toml`:

| Section | Values |
|---|---|
| `[time]` | date window `pre(mode)` and `max_delay(mode)`; vehicle-day gap 3 h; `max_clock_skew` (proposed 20 min) |
| `[lifecycle]` | stale after N s; off-route base 150 m urban / 400 m regional, k = 0.15, hold T = 180 s |
| `[delay]` | discard floor −30 min (DUK-Q6) |
| `[warm_replay]` | hours = 4 |

The file carries `policy_version`, which goes into `derivation`. Code holds no defaults for
policy values: a missing key is a load error.

## 5. Restart by warm replay

On start the worker loads the release index, then runs the core over the stored observations of
the last `warm_replay.hours` with the replay clock, discarding effects except the final state
rows. Polling starts after that. There is no checkpoint file and no second serialization format;
the replay path is exercised on every restart.

A binding made before the window starts is re-made from its key inside the window, and the
date rule (section 19.3: date chosen once from the admissible window) gives the same service date
because it depends on the schedule and the observation time only.

## 6. Acceptance (milestone R1 exit)

Pinned corpus: DÚK 2026-10-05/06 (the 25 h capture) against release
`20261006T194555Z-06c0829d89aa`. 2026-10-25 (DST fall-back) joins it once recorded, so the
recorder must run that day.

1. All T-table and DUK-Q scenario tests pass.
2. Replay is byte-identical across two runs: GTFS-RT snapshots at fixed ticks and a history
   dump.
3. Every emitted `trip_id` exists in the release; the GTFS-RT validator is clean on sampled
   ticks.
4. The keyed match rate is at least the current `obehy rt replay` resolve rate for the same
   buses; every unmatched vehicle carries a reason code.
5. Progress is monotonic per journey, and no journey's service date changes after binding.
6. Warm restart: stopping the worker mid-corpus and warm-replaying gives output equal to the
   uninterrupted run from the first tick after the window is caught up.
7. History rebuild: `replay --write-history` twice for one day gives the same rows; a mid-day
   release switch creates revision 2 with events re-attached or `orphaned`.

`resolve.py` and `episodes.py` are retired when item 4 passes.

## 7. Fixtures

- **Scenario tests, no database.** A small builder constructs the in-memory release index
  directly, for example
  `timetable().trip("582492:143", days=…, calls=[("A", "23:50"), ("B", "24:20")])`. The core
  never needs PostgreSQL in unit tests.
- **Index tests (`tests/db`).** The same builder fixture is loaded through the real loader; the
  test asserts that the index built from `active.*` equals the builder's index.
- **Pinned corpora.** Raw archive day directories plus `corpus.toml` (release id, sources, days,
  reason), stored outside git under `artifact_root/pinned/`. A digest file of the expected
  outputs is in git under `tests/golden/`, so golden checks need only the local corpus.
- **Quirk tests** are named after the quirk ID (`test_duk_q6_…`) and use the builder plus a
  hand-written payload snippet.

## 8. Oběh identity

Tours are plans keyed by trip keys; a vehicle is never part of a tour. A vehicle can run a
different oběh every day, and a mid-day swap changes only the live binding.

- `tour_id` is opaque and stable:
  - a roster or `block_key` tour keeps its own name: `roster:<dataset>:<name>`,
    `block:<key>`;
  - a learned chain is `learned:<feed>:<day_class>:<first journey key>`, renewed when the chain's
    first journey changes (for example at a timetable change).
- The nightly job sets `history.vehicle_day.tour_id` with `tour_match_share`, the share of the
  day's journeys that follow the tour. "Which vehicle ran oběh X each day" is then one query.
