# Source dossier: Arriva Express (`arriva-express`)

Status: **connector `sources/arriva_express.py` (decoder version 1) and keyless inference
(`infer/keyless/`), live on jdf since 2026-10-12.** Two captures (2026-10-05/06,
25 hours; 2026-10-09 to 10-12 from the server, over a weekend) replay against release
`20261006T194555Z-06c0829d89aa` (serving schema 5.0) with every episode that shows a departure
bound to the trip its vehicle drives (see Matching). Terms of use are still to be recorded.

## Endpoint

```text
POST https://www.arriva.cz/api/graphql
content-type: application/json
x-enviroment: client        (the API's spelling)
origin, referer             the arriva.cz map page; a batch request without referer fails
                            with errorCode "referer.missing"
[{"query":"query busesCurrentLocation { busesCurrentLocations { angle delay destinationName
  lastStopName latitude longitude linkNumber state type mainType spz updated linkNumberAlias } }",
  "operationName":"busesCurrentLocation","variables":{}}]
```

No cookies are needed. The response is the whole fleet (about 56 KB, 160+ vehicles in the
evening). `obehy rt record` polls it every 30 s (channel `arriva-express/buses`) and stores only
the Arriva Express entries (filter `arriva-express@1`); each index line keeps the source size,
SHA-256 and kept/dropped counts. In the capture 2,967 of 2,972 polls succeeded (three timeouts,
two refused connections), median 121 ms.

## Scope

The endpoint returns Arriva's whole Czech bus fleet (`data.busesCurrentLocations`: regional
buses, city buses, Arriva Express). **Oběhy uses only Arriva Express**: entries with
`mainType = "ARRIVA EXPRESS"` (also `type = "Express"`, `linkNumberAlias = "AEx"`). The connector
drops every other entry; the regional and city services are covered by other sources or not at
all.

## Response

```json
[{"data": {"busesCurrentLocations": [
  {"angle": "349", "delay": "-2", "destinationName": "Litvínov,,nádraží",
   "lastStopName": "Litvínov,,nádraží", "latitude": 50.57783126831055,
   "longitude": 13.601530075073242, "linkNumber": "157710", "state": "v pohybu",
   "type": "Express", "mainType": "ARRIVA EXPRESS", "spz": "7AT9086   ",
   "updated": "2026-10-05T21:23:31.000+00:00", "linkNumberAlias": "AEx"}
]}}]
```

| Field | Observed | Meaning | Use in Oběhy |
|---|---|---|---|
| `mainType`, `type`, `linkNumberAlias` | `ARRIVA EXPRESS`, `Express`, `AEx` | product | filter (express only) |
| `linkNumber` | 6 digits, e.g. `157710`, `721341` | CIS line | `LineRef("cis:line", …)`: the line's `cis:line_trip` keys are the candidates |
| `destinationName` | `Litvínov,,nádraží` | destination stop name, JDF `Obec,Část,Místo` form with empty parts | `Destination` |
| `lastStopName` | name, or `""` | **the next stop** (despite the name; the website labels it "Následující zastávka"); it changes when the bus departs the stop named before | `NextStopName`; a change is a departure from the stop named until then |
| `state` | `v pohybu`, `v zastávce` | moving / at a stop | not used: it is not a trip state, and the core reads `SourceState` as one |
| `delay` | string, signed minutes (`-7` … ) | delay | `Delay(60 × d, departure)`, channel semantics `delay_floor_minute` (below) |
| `latitude`, `longitude`, `angle` | WGS84, degrees (string) | position, bearing | `Position` with bearing |
| `spz` | licence plate, right-padded with spaces | vehicle | `VehicleKey` (trimmed); stable per physical bus |
| `updated` | ISO time labelled `+00:00`, actually local time | time of the vehicle's last report | `observed_at` after reinterpretation as Europe/Prague |

There is **no trip number, no next stop and no operating date**, and none can be requested
(checked 2026-10-05): introspection is disabled, every trip, connection, next-stop and vehicle-ID
field name tried on `BusesCurrentLocationsType` is rejected, and `busesCurrentLocations` takes
no arguments. The website loads only this one query. Its popup makes no further request, and its
"Následující zastávka" is `lastStopName` relabelled. One other root field exists:
`trainsCurrentLocations` (accepts `trainNumber`, `delay`, `latitude`, `longitude`). It is not
recorded, because SŽ covers rail.

## Vehicle attributes

What the connector states about its vehicles (`VehicleAttribute`, the vocabulary in
`src/obehy/data/vehicles/attributes.toml`, `BASE_PLAN.md` section 27). Values are written on
change to `rt.vehicle_attribute_observed`; identifiers among them bind the report to a
registered vehicle.

| Field | Attribute |
|---|---|
| `spz` (trimmed) | `licence_plate` (an identifier) |

## Matching (replayed)

Reproduce with `obehy rt replay --release <run> --from <day> --to <day> --sources arriva-express
--feeds jdf --out <dir>`: `report.json` has a `keyless` block (groups, bound, ambiguous), and
`keyless.jsonl` one row per group with the journeys bound.

Keyless inference (`BASE_PLAN.md` section 19.2, `infer/keyless/`, policy `[keyless]`). A
vehicle's episode lasts while plate, line and destination stay the same (split after 30 minutes
without a report).

1. **Candidates:** the line's `cis:line_trip` keys whose trip runs on a candidate service date,
   ends at the destination (last passenger call, compared without commas, spaces and case:
   ARRIVA-Q6) and whose schedule, widened by 30 minutes on each side, covers the report. Express
   trips run about hourly in both directions, so this leaves 2–4 candidates for most episodes.
2. **Departures:** each time `lastStopName` changes from A to B, the bus has just left A
   (ARRIVA-Q2). `updated − delay` gives the scheduled departure from A within a minute (the delay
   is rounded down, ARRIVA-Q3), and the residual is how far the candidate's departure from A
   lies outside that minute; the score is the median over the episode. The stop left is placed
   on the candidate after the previous one; a name the candidate lacks is no evidence (ARRIVA-Q8).
   A change seen more than 2 km from the stop left gives no time (ARRIVA-Q9).
3. **Position**, before the first timed departure: the fix's timetable time on the candidate's
   path (within 300 m; between the stop before the next stop and the next stop, else anywhere),
   against the delay.
4. **Decision:** the best candidate binds when its residual is at most 3 minutes (10 for a
   position alone) and the runner-up is at least 15 minutes worse; otherwise `ambiguous`.

A bus is matched once, when it logs on and leaves its first stop (or by position before that).
Once bound, the plate keeps the binding (`BASE_PLAN.md` section 19.5) while the episode
continues and the time stays in the journey's window; only a new line or destination, or 30
minutes of silence, infers again. The bound trip is never re-scored on the way, because the
source's delay is stale between stops (ARRIVA-Q10): its delay comes from GPS progress. Stop
events go to history once the bus has left two stops of the trip, in order. Express trips run
back and forth on the same line, so the circulation prior (section 22) will help pick the next
trip.

Replays against release `20261006T194555Z`, which has no route shapes for these lines, so
positions are judged on straight lines between stops. Each bound episode was checked against
its trip's scheduled times (local), and each vehicle's trips alternate direction with
plausible layovers:

| Capture | Episodes | With a departure, bound | Bound in all | Reports bound | Bound to two trips | GTFS-RT check |
|---|---|---|---|---|---|---|
| 2026-10-05/06 (25 h, 2,972 polls) | 65 | 60 / 60 | 64 | 98.3 % | 0 | clean (63 trips) |
| 2026-10-09 to 10-12 (7,802 polls) | 173 | 158 / 158 | 162 | 98.8 % | 0 | clean (101 trips) |

- Unbound episodes are fragments of one to five reports, reports before the line's first trip
  (`not_in_service`), and two that stay `ambiguous` (6SA3614 on 2026-10-12, 22 minutes;
  1TS5949 on Sunday 2026-10-11, mostly outside any trip).
- ARRIVA-Q9 was found on the second capture: 6SA3598 kept its origin as the next stop for the
  whole Praha – Teplice motorway leg, and the late name change first bound it to the trip an
  hour later. Since the fix it binds the right trip only, and three episodes of the first
  capture that had looked unbindable (7AT9085, 6SA3614: the origin as next stop for one or two
  hours while driving the trip) bind to the trips their positions show. On straight-line paths
  such a vehicle stays `ambiguous` on the motorway until the name changes; route shapes let the
  position bind it.
- Own GPS lateness against the source delay on 10,585 bound reports of the first capture:
  median difference +61 s, absolute p50 132 s and p90 421 s. The source's delay is frozen since
  the last stop, which on motorway legs is 20 minutes and more. Which is closer to what
  happened is for `rt evaluate` on actual departures (milestone R4).
- Three polls returned `busesCurrentLocations: null` and count as bad payloads.

## Time and delay semantics

- **`updated` is local time mislabelled `+00:00`** (confirmed: in every poll of the capture it
  reads two hours after the UTC receipt time minus the report's age, which is 0.5 minutes at
  the median and at most 5.4 minutes; two hours is the Prague summer-time offset). The connector
  reinterprets it as Europe/Prague.
- **`delay`** is signed whole minutes (negative values for early running), measured at the
  departure from the stop before `lastStopName` and **truncated** (rounded down): the scheduled
  departure minus `updated − delay` at the first poll after a stop change lies between −1.0 and
  −0.1 minutes (10th to 90th percentile over 267 stop changes). Semantics: `rounding = floor`,
  `signed = true`, `reference = departure from the previous stop`, so a value `d` constrains that
  departure to `[S_dep + 60d, S_dep + 60d + 59 s]`. Long motorway gaps still make own GPS delay
  plus a future travel-time provider the main delay source between stops (`BASE_PLAN.md`
  section 21).
- A vehicle at its destination with a negative delay has simply arrived early; the trip ends
  there.

## Quirks

| ID | Quirk | Handling |
|---|---|---|
| ARRIVA-Q1 | `updated` is local time labelled `+00:00` (the real offset changes at DST) | connector reinterprets it as Europe/Prague, resolved against `received_at` in the repeated autumn hour (`BASE_PLAN.md` 19.3) |
| ARRIVA-Q2 | `lastStopName` is the **next** stop; it changes when the bus departs the stop named before | `NextStopName`; a change from A is a departure from A (`infer/keyless/episode.py`) |
| ARRIVA-Q3 | `delay` is signed, truncated, measured at the departure from the previous stop | `Delay(60 × d, departure)` with channel semantics `delay_floor_minute`: `d` means 60d to 60d + 59 s |
| ARRIVA-Q4 | no trip number, no operating date, none requestable | keyless inference only (`infer/keyless/`) |
| ARRIVA-Q5 | `spz` is right-padded with spaces | trimmed in the connector |
| ARRIVA-Q6 | destination names keep empty JDF parts and differ in comma spacing | compared without commas and spaces, against the candidate's own calls only |
| ARRIVA-Q7 | the response is the whole fleet; a batch request without `referer` fails | request headers as above; only Arriva Express entries are kept (filter and connector) |
| ARRIVA-Q8 | `lastStopName` also names places that are not stops of the timetable (`křížení D46 na D1`, a motorway junction on 721341) | a departure from a name the candidate lacks is neither evidence nor contradiction |
| ARRIVA-Q9 | `lastStopName` can stay on a stop long after the bus left it (the origin, for a whole motorway leg of an hour or two), with delay 0 | a name change more than `departure_radius_m` (2 km) from the stop left gives no time; the position is judged on the whole path when it is not between the stop before the next stop and the next stop |
| ARRIVA-Q10 | `delay` changes only when the bus leaves a stop: between stops it stays at the last stop's value however late the bus gets (a bus stuck on the D8 at Zdiby, due in Praha 20 minutes ago, still reports 3 minutes) | used only at the moment of a departure, to match the trip once; a bound trip is never re-scored, and the delay shown comes from GPS progress (`estimate.own_lateness`) |

## Open questions

- Terms of use.
