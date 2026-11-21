# Porting hazards: Python realtime core → Rust (byte-identical output)

Survey of `src/obehy/realtime` (2026-11-21). Line numbers are as of commit 422870d. "half-even"
= Python's exact round-half-to-even microsecond rounding.

## Cross-cutting rules
- **timedelta arithmetic**: store durations as i64 microseconds. `td * float` / `td / float` are
  exact (as_integer_ratio then half-even; `times.py:115-124` mirrors this) — needs i128 (and for
  tiny factors with denominator > 2^127 a shift with sticky bits); the `q % 2 == 1` test with q
  negative (e.g. `lateness * early_keep`) needs rem_euclid. `td / int` is also half-even, not
  truncation: `Interval.mid` = `lo + (hi-lo)/2` (`facts.py:33`). `timedelta(seconds=<float>)`
  (every ByMode value is a float, `policy.py:250-254`) = CPython algorithm: `secs*1e6` in f64,
  then round the fraction half-even. `td/td` → f64 via correctly-rounded int/int division.
- `total_seconds()` = `micros as f64 / 1e6`.
- Python `min`/`max`/`sorted`: the first element wins ties; with -0.0 vs 0.0 the first argument
  is returned. `py_max(a,b) = if b > a {b} else {a}`; never `f64::max/min`.
- Sorting with float keys: stable sort with `partial_cmp`, never `total_cmp` (-0.0 vs 0.0, e.g.
  `-h.log_p` with log_p == 0.0, `progress.py:423`).
- `round()` = `round_ties_even`. `int(x.timestamp())` truncates = `micros.div_euclid(1_000_000)`.
  `//` and `divmod` are floor → `div_euclid`.
- `math.hypot` is CPython's own `vector_norm` (3.8+), not libm hypot — port it.
  Used `path.py:72`, `progress.py:224,466`.
- libm: port formulas op for op, keep evaluation order; verify libm parity on the target OS for
  sin, cos, asin, sqrt, atan2, exp, log; `x**2` is libm pow, NOT `x*x` (UCRT differs in ~1 in 2,000: use `geo::square`);
  radians/degrees = `x*(pi/180)` / `x*(180/pi)`.
- Float `%` is floored: `degrees(atan2(-0.0,+y)) % 360.0` → +0.0 in Python, -0.0 with Rust
  rem_euclid. Reaches GTFS-RT bearing bytes via `heading_at` (`path.py:111,149`,
  `progress.py:478`).
- Division by zero: Python raises; Rust gives inf/NaN. `runtimes.py:92` (`total/weight` when
  `runtime_prior_weight`=0 and no traversal), `runtimes.py:168` (`/speed_mps`),
  `progress.py:246,476,480,508` (sigma/spread). Mirror as panics/errors.
- Dict order: `FeedState.instances` / `vehicles` need IndexMap with `shift_remove` (never
  `swap_remove`): Python keeps position on reassign, moves to end on delete+reinsert.
- String order: Python compares code points = Rust UTF-8 byte order. Never locale collation.
- Derived Ord: `JourneyKey` (feed, namespace, key, service_date) `model.py:46`; `VehicleId`
  (source, source_vehicle_id).
- `ServiceTime.instant` uses fold=0 (`times.py:51-53`): a skipped spring time uses the
  pre-change offset; a repeated hour its first occurrence. Map the gap/fold cases
  explicitly (in Rust: jiff Gap/Fold, `time::zone::fold0`; tzdata is Python's own TZif copied into `obehy-core/tzdata`).

## core.py
- `:337-338` due is a set then sorted (ok). `:351` `round(lateness.total_seconds())` half-even.
- `:424-458` refresh iterates `state.instances` in insertion order; `effects.append(event)`
  `:456` follows it → IndexMap order.
- `:423,442-443,395` `timedelta(seconds=float ByMode)`. `:395,405` `fresh <= 0` float.
- `:152` `previous.facts == observation.facts` IEEE equality on Position floats; `:712` exact
  `(lat,lon)==`.
- `:247` `instance.calls is not calls_before` identity → "calls changed" is equivalent (record is
  idempotent for equal calls).
- `:310-315` first key wins; later keys only while AMBIGUOUS. `:318` facts in order.
- `:752,780` rebase sorts; `:757` in `old` dict the last duplicate (location, visit) wins.
- `:661-662` `local_wall(...)` compares `.date()/.time()` on the Prague wall clock.
- Policy: journey.shared, keyless.*, prediction.floor_fresh_s(mode), dwell_credit_s(mode),
  rail.predictor, rail.position_fresh_s, progress.max_speed_mps(mode), progress.*,
  lifecycle.forget_after_s, stale_after_s(mode), predict_without_data_s(mode),
  terminal_radius_m.

## runner.py
- `:184-190` per-feed stable sort by (received_at, source, raw.sha256, raw.item) (docstring says
  (received_at, source, item); code includes sha256). Full ties keep input order.
- `:276,291,336,354` `sorted(runtimes.items())`; `:288` passes runtimes in insertion (= feeds)
  order to write_state.
- `:241-253` InvariantError is fatal. `:259` Ticks / `times.next_multiple`:
  `int(at.timestamp())//every_s`. `:324` min over dates.
- Policy: emit_tick_s, sweep_index_every_s, keep_index_s, compact_index_at_s, lifecycle, time.*.

## runs.py
- `:104` `_opaque` lru_cache(8192), pure: `f"{feed}|{ns}|{key}|{date}"` (ISO date) → sha256 hex
  `[:16]`. `:125-128` `next(...)` first part. `:51` merges consecutive equal train numbers.

## platforms.py
- `:42` exact Instant equality with `ServiceTime.instant()`. `:73` `fact.scheduled.isoformat()`
  ("+00:00", microseconds only if nonzero) — only in the diagnostic Unresolved name.
  `:85` compares (value,label).

## index.py
- `:290-327` sweep rebuilds dicts iterating sets; lookups only after → HashMap ok, but no core
  code may iterate `trips`/`key_entries` unsorted.
- `:337-343` run parts in `runs[run_key]` (loader) order; duplicate check by set;
  `run_id = ','.join(str(run_part))`. `:85` `Call.time` raises IndexMiss — fatal (no except).
  `:337` `or [trip]` fallback.

## infer/keyed.py
- `:42,47` lru_cache on pure fns. `:52-53` `timedelta(seconds=float pre_trip_s/max_delay_s)`.
- `:109-117` run parts deduped by journey, first seen. `:113` AMBIGUOUS at the first ambiguous
  day in yesterday/today/tomorrow order. `:126-128` stable sort (distance, service_date); equal
  distance → AMBIGUOUS regardless of date.
- Policy: time.pre_trip_s(mode), time.max_delay_s(mode).

## infer/keyless
- `__init__.py:43-48` `_median` float sort + (a+b)/2. `:86` stable sort by (residual, journey);
  `candidates.py:57` sorts by journey only and one key on one day can give two trips → on equal
  residual the first in loop order wins (day, then line_keys order, then entries order) and that
  changes `match.trip`. `:90` `best > accept`; `runner_up - best < margin_s`.
- `episode.py:39` `str.casefold()` = full Unicode case folding (ß→ss), not to_lowercase.
  `:78` `[-MAX_DEPARTURES:]` (40). `:60` `>` gap vs int seconds.
- `score.py:67` first matching name; `:69` `name in names[:k]`; `:115` ±inf bounds;
  `:129-130` abs then min; `:96` `gap > departure_radius_m`.
- Policy: keyless.accept_s, gps_accept_s, margin_s, confirm_departures, candidate_window_s,
  episode_gap_s, departure_radius_m, max_path_distance_m; lifecycle.arrival_radius_m
  (`score.py:117`).

## timeline/__init__.py
- `:159,199` `min(project_all, key=lateral_m)` earliest segment wins ties. `:106`
  `sigma_m <= gps_sigma_m` → precise. `:169` `total_seconds() > fresh`. `:194` max(along, held).
  `:205` `along < held - jitter_m`. `:211` cached `_source_progress` is pure. `:76`
  `observation.at <= seen` → repeated. `:228` `delay.seconds <= delay_discard_below_s`.
- Policy: rail.position_sigma_m(source), rail.position_fresh_s, progress.gps_sigma_m, jitter_m,
  prediction.dwell_credit_s, lifecycle.departure_margin_m, terminal_radius_m,
  delay_discard_below_s.

## timeline/fusion.py
- `:62-70` sources kept sorted by source string. `:280-283` `max(key=delay_at or heard_at)` and
  `:310-313` `max(key=heard_at)`: first seen (lowest source name) wins ties. `:294`
  `round(track.lateness_s)` half-even. `:138-140,183-186` Interval min/max. `:197`
  `>= along - jitter`.
- Policy: rail.rank (position_preference), rail.position_fresh_s, rail.event_tolerance_s,
  progress.jitter_m.

## timeline/commit.py
- `:172` `log_p >= -agree_within`; `:175` `deciding[:1]` depends on beam order. `:161`
  `h in deciding` = value equality of Hypothesis (floats, tuples). `:215` td/td float share;
  `:218` td*float half-even; `:226-227` td/int half-even. `:249-259` sorted of 2 floats.
  `:257-258,283-284,302` `timedelta(seconds=float lateness)`. `:274,300`
  `total_seconds() > max_extrapolate_s`. `:188` `before.along < x <= after.along`.
- Policy: progress.agree_within, max_commit_lag_s, max_event_interval_s, max_inferred_spread_s,
  max_extrapolate_s.

## timeline/path.py
- `:251` **`shapely.simplify` (GEOS Douglas-Peucker, preserve_topology=False)** — Rust must
  reproduce GEOS's vertex selection exactly. `:256` exact float tuple equality mapping back.
- `:57` evaluation order `pi/180*R*cos(radians)`. `:71` `length2 == 0` guard, clamp.
- `:97,108,140-141` bisect_right/left → partition_point. `:175-177` local minima with asymmetric
  `<` vs `<=`. `:214,222` strict `<` on (cost,along): first seen wins; additions left-assoc
  `cost + skipped + lateral`. `:308` int/int true division. `geo.py:27-29` sequential cumsum.
- Policy: progress.shape_simplify_deg, stop_off_shape_m.

## timeline/plan.py
- `:80,87,126` exact float equality of call distances. `:85,90` (a+b)/2. `:130-131` ±inf.
  `:145,171,219-220` bisect_right. `:156` td*float and `:183` scaled_micros: both half-even and
  must agree. `:185-186` int/1e6. `:295-303` triggers sorted by
  (along_m, call, kind != "arrival"). `:344` PlanCache clears all above 50,000 plans; `:332-335`
  release change clears. Pure.
- Policy: lifecycle.arrival_radius_m, departure_margin_m; progress.* via build_path.

## timeline/progress.py
- `:127` `math.nextafter(origin, -inf)` = `f64::next_down`. `:223` max of elapsed; `:227` order
  `speed*max(0,elapsed)+reach`. `:279` `a - jitter - reckon_slack*r`. `:294` strict `>`: first
  live hypothesis (beam order) wins ties. `:293` score = `log_p + move + fit` left-assoc.
  `:271,285` `late` memo keyed by float (pure). `:309` chained float equality.
  `:344` `max(a, min(b, c, d))`. `:423` stable sort (-log_p, along_m, off_path is not None);
  full ties keep `_extend` output then `_held`. `:429` `< merge_m`. `:427` `top - log_p > prune`
  and `len >= beam` cut. `:501` `max(0.0, moved) - jitter > speed*elapsed`. `:504`
  `change >= 0`. `:405` `<= 0` guard.
- Policy: every ProgressPolicy field except the four commit ones.

## timeline/stops.py
- `:62` float equality. `:84-87` first call within reach wins. `:140`
  `min(timedelta(seconds=float dwell_s), gap/2 (half-even), gap-drive)`. `:151` `trigger - 1.0`.

## timeline/runtimes.py
- `:66-69` unbounded deque trimmed from the left only while `[0].end < new_end - window`; ends
  are not sorted — do not use a sorted structure. `:99` forget checks `v[-1]` (last appended),
  not the max. `:85-92` accumulate in deque order; `exp(-age/decay)`;
  `min(most, max(1/most, ratio))`. `:87` cutoff `keep_decays*decay` int. `:134`
  `(end-start) * ((r-1.0)*share)` td*float. `:167` running*r; `:168` float seconds.
  `:56-57,125-126` `or` on Instant: only None is falsy.
- Policy: prediction.runtime_decay_s, runtime_prior_weight, runtime_ratio_max,
  runtime_keep_decays, dwell_credit_s.

## timeline/estimate.py
- `:303` repeated `lateness * early_keep(mode)` per call (td*float, negative td). `:169`
  `credited*(1.0-share)`. `:168` clamp float share. `:454,459-461,482` timedelta(seconds=float).
  `:502` `abs(old-value) >= hold`. `:61-74` object reuse is perf only. `:212-219` max of an index
  with truthiness (None only).
- Policy: prediction.publish_deadband_s, dwell_credit_s, early_keep, max_early_departure_s,
  long_dwell_s, min_long_dwell_s, min_dwell_s (all by mode).

## emit/represent.py
- `:117-120` vehicles_by_journey inserts journeys in order of first vehicle (sorted VehicleId);
  that order feeds `gtfs_rt.py:220,245`. `:54` `max(made, default=updated_at)`. `:56` `<=` with
  timedelta(seconds=float publish_finished_s). `:102,109` sorted then min(sources). `:84-90` list
  order, first wins. `:126` `representing` set, used only for membership (`db.py:449`).

## emit/gtfs_rt.py
- `:259` `sorted(chosen, key=vehicle_id)` stable. Duplicate vehicle_ids possible (identity() can
  map two reports to one id; two runs can both be `train:<n>`): tie order from `vehicles_of`
  iteration, both entities emitted, `positions` dict keeps the last.
- `:66` `int(timestamp())`. `:108-111` lat/lon/bearing are proto float (f32): f64→f32 round to
  nearest.
- proto2 explicit presence: defaults still serialized → prost `Some(...)`:
  schedule_relationship=SCHEDULED (`:75`), incrementality=FULL_DATASET (`:215`), zero bearing,
  stop_sequence.
- Entities `SerializeToString(deterministic=True)` (field-number order) joined by hand-built
  `_field` (`:179-188`). `:73` strftime("%Y%m%d"); `:82-83` isoformat dates.
- `:236-241,260-263` caches keyed on object identity; recomputing is equivalent.

## emit/vehicles.py, emit/pairing.py
- `pairing.py:63` sorts journey groups by `repr(JourneyKey)` (`datetime.date(2026, 9, 5)`, no
  zero padding) — benign (one fix per report per tick), any total order works, repr is cheap to
  copy. `:59` journeys dict, last fix wins. `:64` combinations over reports sorted by report.
  `:76` sorted pairs. `:67,93` `total_seconds() >`/`>=`; `:72,74` `gap > apart_m`,
  `gap <= distance_m`.
- `vehicles.py:155-162` sorted held, `min(key=(rank, report))`. `:148-151` first cluster member
  found in registry wins. `:177` `max(clock or last_seen, last_seen)`. `:265-269` sorted dirty,
  members, attributes.
- `vehicles.py:251` `live.setdefault(report, {})`: every report with a vehicle key gets a `live`
  entry, attributes or not, and `_everything_dirty` identifies all of them (ported so, R7).
  `Evidence` is mutable and shared between `pairs` and `changed`; Rust snapshots it into
  `changed` after the confirmation flag is set, which is what Python's `changes()` returns.

## Long-lived containers and eviction (memory design)
- FeedState.vehicles: dropped in refresh when `now - last_seen > forget_after_s`
  (`core.py:424-426`); last_seen = observation.at (source clock).
- FeedState.instances: dropped when `now - heard_at > forget_after_s` and no vehicle bound
  (`:434-438`); also `_drop_unstarted` (`:542-555`) and rebase when the key no longer resolves.
- dirty: cleared each estimate_all. hops: `forget` each refresh; deques trimmed only on append.
- Per instance: `held` grows until confirmed; episode.departures cap 40; Hypothesis.history cut
  only on commit (`commit.py:314-321`), compressed by `_placed`, no hard cap; beam ≤ policy.beam.
- Index: append-only plus cached run trips (`index.py:347`); swept every sweep_index_every_s
  (keep_index_s); rebuilt daily at compact_index_at_s.
- PlanCache: paths unbounded between sweeps; plans cleared above 50k; release change clears.
- lru caches (pure): `_opaque` 8192, `_span`/`_admissible` 32768, times caches,
  `_source_progress` 64.
- FeedEmitter.trips/positions: rebuilt to current entries each serialize.
- VehicleBook: `live` and `identity` never evicted; `members` keys never removed; `trains`
  replaced each tick; `dirty` cleared by profiles().
- Pairings: `pairs` dropped in observe (apart, other journeys, hold_s) but observe only runs
  when there are fixes (`vehicles.py:194`); `changed` drained by changes().

## Data flow
- `Runner.process(obs)`: `batches()` (group by feed, sort, collect refs/lines/days) → `load()`
  (IndexLoader.ensure) → `run()` → `_step` each → `writer.write(effects)`. `_step` =
  `core.step` + invariants.
- `core.step(state, obs, ctx) -> [Effect]`: platform row without vehicle → apply_platform;
  cancellation without vehicle → `_cancel`; duplicate fix skipped; keyless (line, no key):
  episode.advance → keyless.continued | keyless.bind → confirm; keyed: `_continued` | `_bind`
  (keyed.bind) → `_stale`; new journey → `_open` (Instance + snapshots); if `_leads`:
  with_lead, `_lifecycle`, `timeline.advance(...) -> (Instance, effects)`, runtimes.record;
  history held until binding confirmed; `_set_vehicle` writes VehicleState; emits
  ObservationResult.
- `timeline.advance`: usable_delay, note_source, apply_point (fusion),
  `progress.update(plan, track, pos, at, …) -> Step` (beam `_extend`/`_held`/`_prune`,
  stops.seen, `commit.commit -> Committed`), `_cross` (→ WriteEvent), `_published`.
- `Runner.tick(now, emit)`: compact/sweep if due → per feed sorted: `core.refresh` (forget,
  stale/lost, `_arrived_unseen` terminus WriteEvent), `estimate_all` → writer.write →
  write_state (runs VehicleBook.track) → per feed `FeedEmitter.serialize(...)` → write_feed.
- FeedState: feed, vehicles{VehicleId: VehicleState}, instances{JourneyKey: Instance}, dirty,
  hops{HopKey: deque[(Instant, taken_s, planned_s)]}.
- Instance: journey, release_id, trip_id, mode, lifecycle, calls: tuple[CallState], freshness,
  progress, track (beam of Hypothesis, committed_m, at_stop…), reported_delay_s, lateness_s,
  lead, journeys/parts (rail), sources (SourceTrack per source, sorted), track_source,
  shown_source, published, held.
- estimate_all sets `Instance.lateness_s` (int, round()) and per call estimated
  arrival/departure, status (actual/inferred/no_realtime/scheduled/predicted), source_class,
  from current_lateness, waiting_is_not_early, dwell_credit, anchor_prediction, running_extra,
  minimum_drives, `_fresh`, `_seen_leaving`; then publish deadband, then monotone.
- `emit/db.py:443-457` iterates state.vehicles and state.instances in insertion order (IndexMap
  + shift_remove again).

## Platform libm (found in R4)
- On Windows, Rust's f64 math and CPython both call UCRT: 1.15M recorded arguments matched bit
  for bit (`tests/fixtures/libm.json`). Parity references are generated and compared on Windows.
  On Linux both use glibc; a musl static build would not match glibc. After the switch, parity
  with Python no longer matters, but committed fixtures assume UCRT: regenerate on Linux if CI
  runs the Rust tests there.
