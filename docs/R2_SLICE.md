# R2 slice: rail runs, SŽ and DÚK trains, platforms

The concrete contract of the second realtime slice (`BASE_PLAN.md` milestone R2; sections 19.4,
20.6, 23, 24 and 26.2). Architecture and reasons stay in `BASE_PLAN.md`; everything R1 fixed
(`docs/R1_SLICE.md`) still holds unless a section here changes it.

**Scope.** The `czptt` feed. Three sources contribute to one run:

- DÚK train GPS: the `duk/vehicles` entries with `CISLineID = 0`, keyed by train number and
  already decoded and routed to `czptt`;
- the SŽ train map (`sz-mapa/trains`);
- SŽ station boards (`sz-tabule/board`, new) for platforms.

One train is fused from DÚK and SŽ with provenance. Output is GTFS-RT `czptt.pb` plus history.

**Out of scope.** GRAPP (`grapp.spravazeleznic.cz`) per-train route pages, PID trains (R3),
compositions and the API. Platforms at stations whose boards number by platform rather than
track stay out of GTFS-RT (section 6).

## 1. Static contract: serving 5.1 (JrUtil main)

Location keys, all `entity_kind = location`, filled by JrUtil from `SR70.csv` (the
`jrunify-ext-geodata` catalogue) and the CZPTT tracks:

| Namespace | Encoding | Example | Target |
|---|---|---|---|
| `sr70` | 6-digit SR70 with check digit, as in the catalogue | `534149` | stop place or operational point |
| `sr70:name20` | the catalogue's 20-character name (`NÁZEV20`), verbatim | `Ústí n.Orl.město z` | stop place or operational point |
| `sr70:track` | `<sr70>:<track designation>` as in CZPTT | `534149:102` | boarding point |

- Location IDs stay opaque. The SŽ map's 5-digit codes (`nsn70`) are looked up as the `sr70`
  key whose first five digits match; the namespace encoding documents that the 5-digit form is
  the code without its check digit.
- Check digits are never computed: they are Luhn for most codes but not all (Poniklá is
  `571500`).
- The SŽ map names points with `NÁZEV20` names, and so does upstream JrUtil's `Grapp.fs`
  (`sr70_process.py --name=NÁZEV20`). A `sr70:name20` key naming several locations is
  ambiguous and quarantined at use, never guessed.
- Oběhy vendors `serving-v5.1.json`. `IndexLoader` loads the location keys of the run's calls
  together with the run.

## 2. Rail runs and identity

| Concept | Key | Notes |
|---|---|---|
| Run instance (internal) | `RunKey(run_key, operating_date)` | `trip.run_key` is the CZPTT PA, and `run_part` orders its trip parts. One timeline per run. |
| Journey (public, history) | `JourneyKey(czptt, czptt:train_number, tn, service_date)` | One per train number over the run's parts. |
| Number change | `journey_link(kind = continues_as)` | Example: TL 106006 → 6006. Two journeys, one run instance. |
| Output | GTFS-RT per passenger trip part (`trip_id`, the part's own `stop_sequence`) | Operational points are never exported. |

- **The run plan** concatenates the parts' calls in `run_part` order: passenger calls and
  operational points, with the junction call shared by consecutive parts merged. CZPTT has no
  shapes, so the path is the chord path through all calls (`timeline/path.py`, `shaped =
  false`). Operational points make the chords follow the line closely.
- **Binding** is keyed as in R1. For the `czptt` feed a matched trip is lifted to its run: every
  part of the run must run on the operating date, or the binding is `ambiguous`.
  - The SŽ `id` carries the operating date, decoded as a `ServiceDay` fact. It is the only
    candidate date, so the 19.3 window still applies but the date is not inferred.
  - A DÚK train number goes through the normal date rule.
- `czptt:tr` and `czptt:pa` are recorded per journey (`journey_schedule.run_key`, `tr`).
- `FeedState.instances` is keyed by `InstanceKey = JourneyKey | RunKey`. A run instance holds
  `journeys: tuple[JourneySpan(journey, first_ordinal, last_ordinal), …]`.
- History effects (`SnapshotJourney`, `WriteEvent`, `AssignVehicle`) are projected onto the
  journey whose span contains the call. Events at operational points are history rows of that
  journey (`passenger_service = false`).

## 3. Facts (additive; `FACT_SCHEMA_VERSION` stays 1)

```text
ServiceDay(date)                                       the source's operating date (SŽ id)
PointEvent(name, scheduled: Instant?, actual: Interval, standing: bool)
                                                       SŽ cna/cp/cr/rr: reached or passed point
NextPoint(sr70, name)                                  SŽ nna/zst_sr70
NextStopPrediction(sr70_5, scheduled: Instant?, predicted: Instant?)
                                                       SŽ nsn70/nst/nsp
TripStatus(replacement_bus: bool, diverted: bool)      SŽ s/di
PlatformAssignment(station_sr70, kind: arrival|departure, scheduled: Instant, value, label)
                                                       board row; label = platform|track
Delay.reference += "point"                             SŽ de: measured at the last point
```

- All `HH:mm` times resolve through `times.py` against the payload clock (`md` for the map, the
  reception time for boards) to the nearest occurrence. This covers midnight and both DST
  changes; see table R13.
- A board observation carries no `VehicleKey`: it is **call evidence**. It binds by key and
  date like any observation and never creates or moves a vehicle.

## 4. Connectors

### SŽ map (`sources/sz.py`, `sz-mapa.toml`, feeds `czptt`)

| Field | Facts | Quirk |
|---|---|---|
| `id` | `VehicleKey(id)`, `TripKey(czptt:tr, Tr:…)`, `ServiceDay` | |
| `tn` | `TripKey(czptt:train_number)` as fallback, used only when the TR is ambiguous | SZ-Q5 |
| `geometry` | `Position`, EPSG:5514 → WGS84 (pyproj) | SZ-Q1 |
| `cna`, `cp`, `cr`, `rr` | `PointEvent`; `cr` is `[T, T+59 s]` | SZ-Q2, SZ-Q3 |
| `nna`, `zst_sr70` | `NextPoint` | SZ-Q7 |
| `de`, `pde` | `Delay(reference = point)`; `pde` kept for the predictor report | SZ-Q8 |
| `nsn70`, `nst`, `nsp` | `NextStopPrediction` | SZ-Q4 |
| `s`, `di` | `TripStatus` | |
| `e`, `d`, `r`, `tt`, `na` | not facts: kept in the raw archive only | |

- **SZ-Q6, unchanged entries.** The core keeps a per-source fingerprint of each train's last
  entry. An unchanged entry is liveness only: it is no new fix, and it never makes the train
  look stationary.
- **SZ-Q4, resolving a point.** A `PointEvent` resolves in this order:
  1. the run's last `NextPoint` with the same name, giving its `sr70` key;
  2. the `sr70:name20` key.

  Either way the result is restricted to the run's calls at or after the committed frontier,
  taking the first such visit. An unresolved or ambiguous point is reported
  (`point_unresolved`) and moves nothing.

### DÚK trains (`sources/duk.py`, unchanged decoder)

The worker and replay run `--feeds jdf,czptt`. DUK-Q8 entries bind by train number. DÚK GPS on
a chord path uses the unshaped sigma.

### SŽ station boards (`sources/sz_tabule.py`, `sz-tabule.toml`)

- **Request.** `POST …/request2.php?module=Layers\InfoTabule&&action=loadTabule`, body
  `SR70=<6-digit>&module=Layers%5CInfoTabule&action=loadTabule`. The response is about 75 KB of
  HTML: about 20 arrivals and 20 departures, and only trains due within about 60 minutes carry a
  platform.
- **Decode.** The stdlib `html.parser`, keyed on the `prijezdy`/`odjezdy` tables, the
  `data-trainnumber` attribute, the scheduled-time cell and the last cell. The last column's
  header (`Nástupiště` or `Kolej`) gives `label`. Each row is one observation:
  `TripKey(czptt:train_number)` plus `PlatformAssignment`.
- The quirks are in `docs/sources/sz-tabule.md` (SZT-Q1 onwards).

## 5. Fusion on one run

No source owns a run. Each source keeps its own `SourceTrack` on the instance:

- its lead vehicle (DUK-Q11 per source);
- `heard_at`;
- the call range it has covered;
- its fingerprint and last `NextPoint`;
- its last delay and prediction.

The selected values carry provenance: source, channel and reason.

| Capability | Rule |
|---|---|
| Position | DÚK GPS while fresh and plausible, else the last changed SŽ position (larger sigma). Contradictory positions are never averaged; the loser is kept as a conflict. |
| Progress and events | An SŽ `PointEvent` commits the call's event with the minute interval and is a floor: hypotheses behind it are pruned. A DÚK GPS crossing inside that minute narrows the interval to the intersection. A GPS crossing outside it by more than `rail.event_tolerance_s` loses to SŽ and is logged as a conflict. |
| Current delay | GPS lateness while DÚK is fresh, else SŽ `de` at its point. |
| Prediction | Policy `rail.predictor`: `anchor_change`, `sz` or `propagate` (below). |
| Coverage and staleness | Each source's coverage is a policy scope: `duk` is the Ústecký kraj polygon, `sz` is national. A run is stale only when every source covering its current position is silent past `stale_after_s(rail)`. DÚK falling silent outside its scope ends DÚK's coverage and does not make the run stale. |
| Platform | §24 order: the fresh board assignment, then a previous still-valid one, then the scheduled boarding point, then unspecified. |

The predictors:

- **`anchor_change`:** `pred(next) = S(next) + own_delay_now + (sz_pred(next) − sz_delay_now)`;
  later calls propagate from `pred(next)`.
- **`sz`:** SŽ's `nsp` for the next stop, then propagation.
- **`propagate`:** R1 propagation from the current delay.

Replay computes all three in shadow and reports their error against the events observed later.
The policy default is set from that report.

## 6. Platforms

- **Binding.** A `PlatformAssignment` binds by train number and the date of its scheduled time.
  It attaches to the run's call at the station (through the `sr70` key) whose scheduled time for
  that kind matches.
- **An origin train without an instance** gets a `forecast` instance from the assignment, so
  trains not yet on the map still get their platform. A row matching no call is reported
  (`platform_unbound`).
- **Storage.** `CallState.platform = (value, label, boarding_point_id | None, assigned_at,
  source)`. The newest assignment wins; `-` and empty mean "not yet assigned" and never clear a
  value.
- **Display.** The board value is the platform label shown for the call (history and
  realtime state). It overrides the scheduled boarding point's `public_code`, which at larger
  stations is a CZPTT track and not the real assignment. The label kind (`platform` or `track`)
  is kept with it.
- **Mapping** (`track` labels only). A `Kolej` value is looked up in `sr70:track`. A hit sets
  `boarding_point_id`; a miss is reported (`platform_unmapped`, per station).
- **Platform labels are never mapped.** Static boarding points are CZPTT tracks (Kolín has
  tracks `100`–`116`, while its board shows platforms `1`–`5`). No track-to-platform layout is
  available, and there are no per-platform coordinates, so a platform-numbered value has no stop
  to point at. It is a label only, never a stop ID.
- **GTFS-RT.** Only a mapped track becomes `StopTimeUpdate.stop_time_properties.assigned_stop_id`
  on the part's call, emitted even when the call has no predicted times. Platform-numbered
  stations add nothing to GTFS-RT.

## 7. Egress and demand polling (generic runtime)

**SŽ IP-bans addresses that run services against it.** Every channel to an SŽ host declares
`egress = "sz"`.

**The proxy pool.**
- It is the user's Webshare pool. Its list URL is per user and secret: `[realtime.egress.sz]
  proxy_list_url = "<secret>"` in the gitignored `config/obehy.local.toml`, or
  `OBEHY_EGRESS_SZ_PROXY_LIST_URL` in the gitignored deploy `.env`.
- The runtime downloads the list at start and every `egress.refresh_h`. Each line is
  `ip:port:username:password`. The pool is held in memory only, and an empty or malformed list
  keeps the previous pool.

**Request rules.**
- Proxies are used round-robin per request. A proxy is benched for `egress.cooldown_s` on a
  403, 429, connection error or timeout, and the request is retried once on the next proxy.
- If all proxies are benched, the channel's circuit opens.
- **It fails closed.** A channel with no configured pool never connects directly.
- **No `User-Agent` header**: the urllib opener has `addheaders = []`. No other identifying
  headers are sent.
- Logs and `source_health` name a proxy by its list index only.

**Demand polling** (`poll = { kind = "demand", query = "station_boards" }`) is the first
`[[lookup]]`-style channel.
- Each minute the runtime runs a named SQL query over the active release: stations with at
  least `boards.min_boarding_points` boarding points and passenger calls due in the next
  `boards.window_min`.
- **A station is read:**
  - once when a call enters `boards.early_min`;
  - again if a call is within `boards.near_min` and the last read is older than
    `boards.near_refresh_s`;
  - again if a call is within `boards.imminent_min` and the last read is older than
    `boards.imminent_refresh_s`.
- Reads are coalesced per station, ordered by the soonest due call (unread first), sequential
  (concurrency 1), and capped at `boards.ceiling_per_min`. Reads over the cap are dropped and
  counted (`board_demand_dropped`).
- **Backoff.** A 403/429/5xx response, or a latency above twice the rolling median, halves the
  rate for `boards.slowdown_s`.
- Demand peaks at about 3,700 calls per hour at about 860 stations (2026-10-06), an estimated
  45–70 reads per minute. SŽ's own map made about 57 requests per minute for one viewer.

```toml
# realtime/sources/sz-tabule.toml
source = "sz-tabule"
manifest_version = 1

[[channel]]
name = "board"
egress = "sz"
poll = { kind = "demand", query = "station_boards" }
backoff = { after_failures = 3, max_s = 900 }
timeout_s = 15
request = { method = "POST", url = 'https://mapy.spravazeleznic.cz/serverside/request2.php?module=Layers\InfoTabule&&action=loadTabule', body = 'SR70={sr70}&module=Layers%5CInfoTabule&action=loadTabule' }
feeds = ["czptt"]
capabilities = ["trip_key", "platform"]
```

`sz-mapa.toml` gains `egress = "sz"`. The recorder uses the same runtime, so recording SŽ also
needs the pool.

## 8. Policy additions (`policy-v2.toml`; `policy_version` changes)

| Section | Values |
|---|---|
| `[rail]` | `predictor`, `sz_position_sigma_m`, `point_interval_s = 59`, `event_tolerance_s`, `max_position_conflict_m` |
| `[coverage]` | per source: `duk = "realtime/coverage/ustecky-kraj.geojson"`, `sz = "national"` |
| `[egress]` | `refresh_h`, `cooldown_s` |
| `[boards]` | `min_boarding_points = 2`, `window_min = 60`, `early_min = 45`, `near_min = 15`, `near_refresh_s = 300`, `imminent_min = 5`, `imminent_refresh_s = 120`, `ceiling_per_min = 60`, `slowdown_s = 900` |

`stale_after_s`, `predict_without_data_s` and the time windows gain `rail` entries. The
Ústecký kraj polygon is a curated, git-reviewed file under `src/obehy/data/realtime/coverage/`,
simplified from the RÚIAN region boundary.

## 9. Database (`0007_rail.sql`)

```text
history.journey_link        feed, from journey key, to journey key, kind (continues_as |
                            splits_from | joins), service_date, derivation_id; monthly partitions
history.platform_evidence   journey, location_id, visit_n, kind; value, label,
                            boarding_point_id, source, first_seen, last_seen, derivation_id
history.journey_schedule    + run_key text, tr text (nullable; rail only)
rt.source_health            + dropped (demand reads over the cap), proxy_benched
```

## 10. Rail scenario table

| ID | Scenario | Expected |
|---|---|---|
| R1 | A two-part run with one train number | One instance and one journey; GTFS-RT has two TripUpdates, one per part, each with its own sequences. |
| R2 | The number changes at part 2 | One instance, two journeys, and `journey_link continues_as`. |
| R3 | A run crossing midnight | The service date is the operating date throughout; times after midnight are `24:xx+` of that date. |
| R4 | A TR with two timetables active that day (SZ-Q5) | Falls back to `tn`; if that is ambiguous too, the binding is `ambiguous`. |
| R5 | DÚK binds the train number and SŽ the TR of the same run | One instance with two `SourceTrack`s. |
| R6 | DÚK leaves Ústecký kraj while SŽ continues | DÚK's coverage ends and the run is not stale. When SŽ is silent too, the run becomes stale. |
| R7 | An SŽ point event during a DÚK GPS gap | The event is committed with the `[T, T+59 s]` interval and progress cannot fall behind it. |
| R8 | An unchanged SŽ entry (SZ-Q6) | Liveness only: no fix, no event, and no stationary inference. |
| R9 | `cna` is an operational point (SZ-Q7) | The passage is recorded in history and never exported. |
| R10 | A platform on the board for a train without an instance | A `forecast` instance; GTFS-RT carries `assigned_stop_id` without times. |
| R11 | The board shows `-` after an assignment | The assignment is kept. |
| R12 | A `Nástupiště` value (a platform label) | It is the shown label in history and state; nothing goes to GTFS-RT. |
| R13 | A board read at 23:50 lists a 00:10 departure; read during the repeated autumn hour | The next service day; the nearest occurrence is used. |
| R14 | DÚK GPS 3 km from SŽ's last point | Fresh GPS wins if on the path. The conflict is recorded and the SŽ evidence is kept. |
| R15 | A GPS crossing inside SŽ's minute, then one 3 minutes outside it | Intersected; then SŽ wins and a conflict is logged. |

## 11. Acceptance (milestone R2 exit)

**Pinned corpus.** 25 hours of DÚK, the SŽ map and SŽ boards, recorded after a CI release with
serving 5.1 is active, and pinned against that release. The 2026-10-05/06 capture predates the
keys and serves only the point-name measurement.

1. All R-table, SZ-Q and SZT-Q scenario tests pass. The R1 golden digests are unchanged.
2. Replay is byte-identical across two runs (GTFS-RT snapshots and the history dump).
3. Every emitted `trip_id`, `stop_sequence` and `assigned_stop_id` exists in the release's
   `gtfs.zip`. The GTFS-RT validator is clean on sampled ticks, or is reported as skipped
   without the token.
4. SŽ binds at least 99.7% of train-days (the `sz.md` baseline), and DÚK train episodes bind at
   least 876 of 919. Every unbound item carries a reason.
5. The report lists:
   - point resolution rate;
   - platform rows by `mapped` / `unmapped` / `unbound`, per station;
   - predictor error per predictor;
   - board reads per minute against the ceiling, `board_demand_dropped`, and proxy benching.
6. A live hour on the server with `--feeds jdf,czptt`: no 403s, request rates within budget,
   and `czptt.pb` validates.

## 12. Settled assumptions and measurements

- **Terms of use.** SŽ publishes none for these endpoints. We run on them as an accepted risk:
  anonymous through the proxy pool, within budget, and stopped on a cease-and-desist.
- **Operating date.** When a train is matched by its CZPTT TR from the map `id`, the date in the
  `id` is the CZPTT service date. This is an invariant, not a question. Replay counts
  `ServiceDay` facts whose date has no running run for the TR (`date_mismatch`), and the
  rail-runs commit reports that count on the 2026-10-05/06 capture.
- **Point names.** With `sr70:name20` keys, `cna` names should always resolve. Replay measures
  it (`point_unresolved`, per name), first on a bounded, sampled scan of the 2026-10-05/06
  capture.
- **No track-to-platform layout exists for us**, and there are no per-platform coordinates.
  Platform-numbered boards are display labels only (section 6).
