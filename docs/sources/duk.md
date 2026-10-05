# Source dossier: DÚK vehicle list (`duk`)

Status: **field meanings from the provider's documentation; one sample payload
(2026-10-05 20:06) checked against release `20261003T144231Z-53e241dba302`.** Endpoint URL, poll
interval and terms of use are still to be recorded.

## Response

`{"VehicleList": [...]}`, one entry per vehicle. A sample:

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
| `RouteID` | trip (*spoj*) number | `TripKey(cis_trip_id)` |
| `HasLowfloor` | vehicle accessibility | vehicle attribute |
| `Longitude`, `Latitude` | WGS84 position | `Position` |
| `StationNode`, `StationPost` | last stop (node, post) | not used: trips match by key, progress comes from GPS |
| `FinalNode` | destination (node) | not used |
| `ArrivalDT` | arrival time at the last stop (local time) | arrival evidence at the call our GPS progress places the vehicle at |
| `TODepartureDT` | timetabled departure from the last stop (local time) | identifies that call by scheduled time |
| `LastActivityDT` | time of the last data communication with the vehicle (local time) | freshness / staleness |
| `CISLineID` | CIS line number | zero-padded to 6 digits → `TripKey(cis_line_id)` |
| `GPSPositionDT` | timestamp of the GPS position | `observed_at` of the position |
| `Azimut` | GPS bearing | heading for progress (section 20.4 of `BASE_PLAN.md`) |
| `State` | 255 off; 0 running; 1 at a stop/station; 2 waiting before running the trip; 3 running to the trip's first stop | see below |
| `isAirConditioned` | vehicle air conditioning (null in the whole sample) | vehicle attribute when populated |
| `qride_tripID` | trip identifier in QRide's GTFS | cross-check; `TripKey` only if that GTFS is ever imported |
| `qride_linename` | line name (`492`, `U1`) | display; `LineRef` fallback |

## Which entries are used

- **3–4 digit `ID`s:** DÚK vehicles (133 in the sample), `qride_tripID` `CIST-<cis line>-1-<cis trip>`.
- **`40xxxx`:** Teplice city buses (16 in the sample), real vehicle numbers prefixed with `40` so
  they do not clash with DÚK numbers. Their `qride_tripID` has a different structure
  (`CIST-585102-10202-91-1188`). `ArrivalDT`/`TODepartureDT` are null, `Delay` is mostly 0, and
  **`GPSPositionDT` is UTC mislabelled as `+02:00`** (exactly `LastActivityDT` + 2 h); the
  connector corrects it. Delay comes from our own GPS progress.
- **`20xxx`:** trains (`Os-6887-1499`, `R-619-33`), dropped; rail realtime comes from SŽ.

## Matching (checked)

`zero-padded CISLineID + RouteID` resolves **148 of 149** vehicles (132/133 DÚK, 16/16 Teplice)
to exactly one line and trip in the `jdf` package. Some resolve to `:det` (výluka) trips.
PID-overlaid lines have trip IDs with an `:overlay:<hash>` suffix, so the lookup goes through
`road_trip_key`, not trip-ID parsing. The operating date comes from date inference
(section 19.3). The single miss (`522588`, line 588) is a CIS line absent from the national
export (CIS has `001588` and `220588`); it falls back to `LineRef` inference or stays
quarantined.

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
  without stop mapping. Departures come from GPS progress (section 21).
- **`Delay`** is a predicted delay, rounded to whole minutes and never negative. It does not
  distinguish arrival from departure: it is computed sometimes against the scheduled arrival
  and sometimes against the scheduled departure at the last stop, so it jumps by the dwell slack
  and must not move the state on its own. Semantics: `rounding = round`, `signed = false`,
  `reference = last arrival or departure`. The constraint spans both events:
  `[min(S_arr(k), S_dep(k)) + 60d − 30 s, max(S_arr(k), S_dep(k)) + 60d + 29 s]`, and `0` means
  "at most about half a minute late", not on time. It is weak evidence; own GPS delay is DÚK's
  delay source. `−1` appeared four times in the sample and is treated as "no value".

## Open questions for the capture

- Endpoint URL, poll interval, terms of use.
- Meaning of `Delay = −1`.
