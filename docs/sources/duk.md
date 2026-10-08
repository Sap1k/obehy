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
  the vehicles report were missing. Fixed in JrUtil `6a86278` (date-by-date version resolution);
  the next release should resolve them like the other fleets. DPmÚL trip numbers are real JDF
  numbers: `n` on weekdays and `300 + n` (`20xx`/`23xx` on some lines) on weekends.
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

## Open questions

- Terms of use.
- Why some vehicles never report a negative delay.
