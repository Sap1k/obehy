# Source dossier: DÚK vehicle list (`duk`)

Status: **field meanings from the provider's documentation, checked against a 25-hour capture
(2026-10-05 21:58 to 2026-10-06 22:44 local, 5,945 polls) replayed against release
`20261006T194555Z-06c0829d89aa` (serving schema 5.0).** Terms of use are still to be recorded.

## Endpoint

```text
GET https://tabule.portabo.cz/api/v1-tabule/cis/GetTraffic/0
```

No authentication. `obehy rt record` polls it every 15 s (channel `duk/vehicles`); the response
is about 20–65 KB depending on the time of day. In the capture 5,944 of 5,945 polls succeeded (one
read timeout), with a median response time of 57 ms. Every poll returns a different payload, so
upstream freshness is measured per vehicle: a running vehicle's `GPSPositionDT` changes every
20 s at the median (10th–90th percentile 9–38 s), and the position is 28 s old at the median when
received (15 s–1.9 min).

## Response

`{"VehicleList": [...]}`, one entry per vehicle or train. A sample:

```json
{"ID":807,"Delay":0,"LineID":492,"RouteID":143,"HasLowfloor":true,
 "Longitude":13.78571,"Latitude":50.68414,"StationNode":307,"StationPost":1,"FinalNode":416,
 "ArrivalDT":"2026-10-05T20:05:49+02:00","TODepartureDT":"2026-10-05T20:06:00+02:00",
 "LastActivityDT":"2026-10-05T20:06:32+02:00","CISLineID":582492,
 "GPSPositionDT":"2026-10-05T20:06:31+02:00","Azimut":128,"State":0,
 "isAirConditioned":null,"qride_tripID":"CIST-582492-1-143","qride_linename":"492"}
```

## Fields (provider documentation)

| Field | Documented meaning | Use in Oběhy |
|---|---|---|
| `ID` | vehicle number (the fleet number printed on the bus; stable across trips and days) | `VehicleKey`; feeds circulation learning |
| `Delay` | current predicted delay, minutes | weak `Delay` fact (see semantics) |
| `LineID` | line number | `LineRef(public, DÚK area)` fallback |
| `RouteID` | trip (*spoj*) number; for trains the train number | `TripKey(cis_trip_id)` or `TripKey(train_number)` |
| `HasLowfloor` | vehicle accessibility | vehicle attribute |
| `Longitude`, `Latitude` | WGS84 position | `Position` |
| `StationNode`, `StationPost` | last stop (node, post) | not used: trips match by key, progress comes from GPS |
| `FinalNode` | destination (node) | not used |
| `ArrivalDT` | arrival time at the last stop (local time) | arrival evidence at the call our GPS progress places the vehicle at |
| `TODepartureDT` | timetabled departure from the last stop (local time) | identifies that call by scheduled time |
| `LastActivityDT` | time of the last data communication with the vehicle (local time) | freshness / staleness |
| `CISLineID` | CIS line number; `0` for trains | zero-padded to 6 digits → `TripKey(cis_line_id)` |
| `GPSPositionDT` | timestamp of the GPS position | `observed_at` of the position |
| `Azimut` | GPS bearing | heading for progress (section 20.4 of `BASE_PLAN.md`) |
| `State` | 255 off; 0 running; 1 at a stop/station; 2 waiting before running the trip; 3 running to the trip's first stop | see below |
| `isAirConditioned` | vehicle air conditioning (null throughout the capture) | vehicle attribute when populated |
| `qride_tripID` | trip identifier in QRide's GTFS | cross-check; `TripKey` only if that GTFS is ever imported |
| `qride_linename` | line name (`492`, `U1`) | display; `LineRef` fallback |

## Which entries are used

The capture saw 1,179 distinct `ID`s:

- **DÚK-range IDs (3–4 digits), 402 vehicles:** buses of the DÚK operators. `qride_tripID` is
  `CIST-<cis line>-1-<cis trip>`. The 9xxx numbers include the vehicles on the Prague-region 100xxx lines.
- **`30xxxx`, 93 vehicles:** DPmÚL (Ústí nad Labem city transport) vehicles, with a `30` prefix
  that keeps them apart from DÚK numbers.
- **`40xxxx`, 34 vehicles:** Teplice city buses with a `40` prefix. Their `qride_tripID` has a
  different structure (`CIST-585102-10202-91-1188`). **`GPSPositionDT` is UTC mislabelled as
  `+02:00`** (exactly `LastActivityDT` + 2 h in every row); the connector corrects it. DPmÚL
  vehicles do not have this offset.
- **Trains, 649 IDs (`20xxx`):** `CISLineID` is `0`, `RouteID` is the train number, `LineID` and
  `qride_linename` the regional rail line (`U2`, `L7`), and `qride_tripID` is
  `<category>-<train number>-<n>` (`Os-6917-1526`). The `ID` names a train, not a vehicle. They
  resolve through the CZPTT package by train number (below); SŽ remains the rail source of
  record, and DÚK trains add a second position and delay for the same run.

Neither the `30` nor the `40` prefix can be stripped to reach a vehicle register number: the
remainders collide with unrelated vehicles of other operators.

## Vehicle register

DÚK's vehicle register (`duk-vhc-data.csv`, received out of band, not public) lists 522 vehicles
of six operators with model, operator, year of manufacture, low floor, contactless payment, air
conditioning, alternative fuel and USB chargers, keyed by `ID`. It covers 284 of the 403 DÚK-range
vehicles seen in the capture; its low-floor flag agrees with `HasLowfloor` for all 284. The 119
missing are mostly the 9xxx vehicles. It does not cover the `30xxxx` and `40xxxx` fleets.

## Matching (replayed)

Reproduce with `obehy rt replay --release <release-dir> --from <day> --to <day> --out <dir>`
(`report.json` coverage per fleet, `episodes.parquet` one row per episode).

A vehicle's polls are grouped into episodes: consecutive polls with the same `CISLineID` and
`RouteID`, split after 30 minutes without one. Each episode resolves through
`source_key(namespace = cis:line_trip, identifier = <6-digit line>:<trip>)` to trips whose service
runs on the operating date (the poll's local date or the day before) and whose scheduled span,
widened by an hour on each side, overlaps the episode.

| Fleet | Running episodes | Exactly one trip | Ambiguous | Not resolved |
|---|---:|---:|---:|---:|
| DÚK range | 4,410 | 4,370 (99.1%) | 0 | 40 |
| Teplice `40xxxx` | 763 | 738 (96.7%) | 0 | 25 |
| DPmÚL `30xxxx` | 1,973 | 1,040 (52.7%) | 0 | 933 |
| Trains (CZPTT, train number) | 919 | 876 (95.3%) | 1 | 42 |

- **DÚK range:** the 40 misses are 21 episodes on lines absent from the national export (for
  example `522588`, `599894`, `626`) and 19 where the vehicle reports a trip that does not run at
  that time (a stale key, below).
- **DPmÚL:** DPmÚL publishes each line as up to nine parallel JDF versions (regular, weekday-only,
  weekend-only and one per Christmas special day, each nominally valid for a year). JrUtil's
  `merge-jdf` kept the wrong version for lines 46, 71, 74–76, 79, 83 and 89, so the weekday trips
  the vehicles report were missing. Fixed in JrUtil `6a86278` (date-by-date version resolution).
  DPmÚL trip numbers are real JDF numbers: `n` on weekdays and `300 + n` (`20xx`/`23xx` on some
  lines) on weekends.
- **DPmÚL after the fix:** replayed against packages built on 2026-10-08 with JrUtil `17179c9`,
  1,573 of the 1,973 running episodes (79.7%) resolve to exactly one trip.
  - The rest is mostly upstream behaviour, not a JDF error. 268 of the 275 episodes whose trip
    does not run that day report a weekend number (`3xx`) on a weekday.
  - Line 595200 shows the pattern. Both source versions (VLD batches 4312 and 10217) run
    trips 2–220 on working days and 302–452 on weekends and holidays. Vehicle 300066 runs
    the weekday trips all of Tuesday 2026-10-06, but at the loop terminus (Mírová, node
    12057) it briefly reports the weekend number: `18` at 05:48, `310` for a single poll at
    05:53, then `18` again at 05:59.
  - Of the 268 such episodes, 150 lie between two resolved trips of the same vehicle (each
    within 15 minutes) and 47 next to one. The other 71 are isolated, e.g. the first report
    of the day (`302` at 04:20). They last 5.2 minutes at the median, against 34.7 for
    resolved episodes.
  - A short off-calendar key from a DPmÚL vehicle must therefore not rebind it to another
    trip.
- **Trains:** 19 episodes (18 train numbers) have CZPTT train-number keys, but no timetable
  for them runs that day; the rest are time mismatches.

Episodes in State 2 or 3 (before the trip starts) are mostly not time-compatible with the trip
they report and must not bind a vehicle to a trip.

## Stale trip keys

Vehicles keep reporting their previous trip's key while parked or driving to the next start: bus
812 reported `582488/130` (its last trip of the previous evening) from 02:48 to 04:37 in State 3.
A few vehicles report one key for most of the day (up to 61 episodes of the same trip). Matching
therefore checks every episode against the trip's scheduled span, and a vehicle's repeated
episodes of one trip collapse to the episode that overlaps the schedule.

27 trips were claimed by two vehicles on the same day: 16 one after the other (a vehicle swap)
and 11 at the same time, mostly overlaid PID 100xxx detour trips.

## Circulations

Chaining each vehicle's uniquely resolved trips per operating day (after collapsing stale
repeats) gives 5,165 links between consecutive trips: 90% start at the exact stop where the
previous trip ended, 92% within 500 m, the median layover is 15 minutes, and 15 overlap in the
timetable. Resolved trips cover 98.8% of DÚK-range and 99.4% of Teplice running time. The 171
links that jump more than 5 km are deadhead runs or vehicles reporting a wrong line, and get low
confidence in circulation learning.

## State

| `State` | Meaning | Effect |
|---|---|---|
| 255 | off | no progress; position ignored; vehicle offline |
| 0 | running | between stops: ordering constraint on the current gap |
| 1 | at a stop or station | `CurrentStop(at_stop)` |
| 2 | waiting before running the trip | trip not started: forecast assignment only, no progress or actual events |
| 3 | running to the trip's first stop | trip not started (positioning run): forecast assignment only; its position must not advance the trip |

States 2 and 3 matter for progress integrity: a vehicle driving empty to its first stop can pass
the trip's later stops, and must not trigger events for them.

## Time and delay semantics

- **Timed source.** `GPSPositionDT` is the position's event time; `LastActivityDT` the
  vehicle's last communication. `ArrivalDT` uses `1970-01-01T02:00:00+02:00` for "none".
- **Arrival vs departure.** `ArrivalDT` is the actual arrival at the last stop and
  `TODepartureDT` that stop's scheduled departure, which identifies the call on our trip
  without stop mapping. For bus 812 on 2026-10-06 this placed an arrival on 379 of 383 stops of
  its 12 trips. Departures come from GPS progress (section 21). DPmÚL and Teplice vehicles never
  send `ArrivalDT`.
- **`Delay`** is a signed delay in whole minutes. Negative values are common for DÚK-range
  vehicles and trains (3.4% of running rows; 185 of the 188 vehicles of Dopravní společnost Ústeckého kraje report one at some point),
  but not every vehicle reports them: bus 812 arrived more than a minute early at 40 stops and
  never reported a negative delay. Values of −30 minutes and below (0.1% of rows, down to −545)
  are garbage. DPmÚL and Teplice vehicles report 0 or positive values only (0 in about 77% of
  rows). It does not distinguish arrival from departure: it is computed sometimes against the
  scheduled arrival and sometimes against the scheduled departure at the last stop, so it jumps
  by the dwell slack and must not move the state on its own. Semantics: `rounding = round`,
  `signed = true` (DÚK range and trains; unsigned for `30xxxx`/`40xxxx`),
  `reference = last arrival or departure`, values ≤ −30 discarded. The constraint spans both
  events: `[min(S_arr(k), S_dep(k)) + 60d − 30 s, max(S_arr(k), S_dep(k)) + 60d + 29 s]`. It is
  weak evidence; own GPS delay is DÚK's delay source.

## Quirks

Each quirk is handled where stated and has a scenario test named after its ID
(`AGENTS.md`, quirk ledger).

| ID | Quirk | Handling |
|---|---|---|
| DUK-Q1 | Teplice `40xxxx`: `GPSPositionDT` is UTC labelled with the local offset (`+02:00`, `+01:00` in winter) | connector ignores the label and reads the digits as UTC; the clock-skew check (`BASE_PLAN.md` 19.3) drops anything still off |
| DUK-Q2 | `ArrivalDT` = `1970-01-01T02:00:00+02:00` means "none" | connector drops it |
| DUK-Q3 | DPmÚL weekend numbers (`300 + n`) reported briefly on weekdays | taken literally: a different trip; unmatched when it does not run; no reinterpretation |
| DUK-Q4 | vehicles keep the previous trip's key while parked or positioning, overnight too | yesterday's instance fails the date window: `not_in_service`; never extends the old trip |
| DUK-Q5 | State 2/3: the trip has not started; an empty run may pass the trip's later stops | pre-trip: forecast assignment only, no progress or events |
| DUK-Q6 | `Delay` mixes arrival and departure reference and jumps by the dwell slack; ≤ −30 is garbage; `30xxxx`/`40xxxx` are unsigned | weak constraint spanning both events; values ≤ −30 discarded |
| DUK-Q7 | `30`/`40` prefixes cannot be stripped to a register number | the prefixed `ID` is the vehicle key |
| DUK-Q8 | `20xxx` entries are trains: `ID` names a train, `CISLineID = 0`, `RouteID` = train number | `TripKey(czptt:train_number)`; not a vehicle for circulations |
| DUK-Q9 | `CISLineID` is an integer without leading zeros | connector pads it to 6 digits |
| DUK-Q10 | lines missing from the national export (`522588`, `599894`, `626`) | unmatched, shown as such; a static data gap, not a matching problem |
| DUK-Q11 | one trip claimed by two vehicles at once (mostly PID `100xxx` detour trips) | both bind the instance (section 19.5); one position per feed by arbitration |
| DUK-Q12 | DPmÚL and Teplice vehicles never send `ArrivalDT` | arrivals come from GPS progress only |
| DUK-Q13 | `Azimut` is exactly 0 in about 5 % of entries, often while the vehicle moves | 0 is read as no bearing; progress uses bearing only when present |
| DUK-Q14 | in State 2/3, `Delay` is the time since the trip's scheduled departure, growing while the vehicle stands (vehicle 171, 2026-10-09: `582480:140` of 21:59 in the depot at 23:52, `Delay` 113) | source delay ignored until the trip starts (manifest `pre_trip_delay_is_elapsed`) |
| DUK-Q15 | State 2/3 with a trip key whose scheduled end has passed: a stale key from the depot (same vehicle) | `stale_key`: not bound, and a journey that never started is dropped; a running vehicle keeps the late-running window (manifest `pre_trip_after_end_is_stale`) |
| DUK-Q16 | DPmÚL night lines (`59504x`) run Friday nights under the weekend numbers: on 2026-10-09 at 23:51 a bus reported `595041:309` (Sat/Sun, 23:51–00:20) while the timetable runs the identical weekday trip `9` that night; `595043:2310` likewise. Weekday `n` and weekend `300 + n` have identical times | taken literally (as DUK-Q3): `not_in_service`, no realtime. Not handled; a DPmÚL-only twin rule (bind `n` ↔ `300 + n` when only the twin runs at that time) is the candidate fix |
| DUK-Q17 | DPmÚL trip numbers absent from the release: `595042:201`, `595046:512` (2026-10-09 night; lines 42 and 46 have only 1–14 and 301–314) | `no_trip`. Not investigated: probably a temporary or diversion JDF version missing from the national export or dropped by `merge-jdf` |
| DUK-Q18 | the onboard unit misses a departure: State 3 all through the trip, `TODepartureDT` already the same departure on the next run day, `StationNode` frozen (vehicle 177, 2026-10-10: `582803:206` of 00:54 driven about 3 min late, `TODepartureDT` 2026-10-11 00:54) | running once, after the scheduled start, the source plans the same wall-clock departure on a later day and the vehicle is on the trip's path past the first stop; the source delay stays ignored while it says State 2/3, delays come from GPS. Applies to every source: a vehicle driving its trip is never left without realtime |
| DUK-Q19 | DPmÚL vehicles never send a pre-trip state: State 0 (running) also while laying over at the terminus under the next trip, on a stand past the static first stop (vehicle 300813, 2026-10-06: `595081:54` of 10:31 from 09:48 on, at Brná 300 m along the path, leaving at 10:31) | before the scheduled start a running state starts the trip only while the vehicle waits at the first stop or once it is seen moving along the path past it; standing beyond the first stop, it waits for its time. Applies to every source: an early departure is observed, never assumed |

## Open questions

- Terms of use.
- Why some vehicles never report a negative delay.
