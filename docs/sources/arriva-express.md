# Source dossier: Arriva Express (`arriva-express`)

Status: **connector `sources/arriva_express.py` (decoder version 1) and keyless inference
(`infer/keyless/`, 2026-10-12), replay only (`live_feeds = []`).** A 25-hour capture
(2026-10-05 21:57 to 2026-10-06 22:43 local, 2,972 polls, 12 vehicles on three lines) replays
against release `20261006T194555Z-06c0829d89aa` (serving schema 5.0) with every episode that
shows a departure bound to the trip its vehicle drives (see Matching). Going live waits for a
replay of a recent day. Terms of use are still to be recorded.

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
3. **Position**, before the first departure: the fix's timetable time on the candidate's path
   (within 300 m), against the delay.
4. **Decision:** the best candidate binds when its residual is at most 3 minutes (10 for a
   position alone) and the runner-up is at least 15 minutes worse; otherwise `ambiguous`.

Once bound, the plate keeps the binding (`BASE_PLAN.md` section 19.5) while the episode
continues, the time stays in the journey's window and its departures agree with the trip; a new
line or destination, or a departure off by 15 minutes, infers again. Stop events go to history
only once two departures agree with the trip. Express trips run back and forth on the same
line, so the circulation prior (section 22) will help pick the next trip.

Replay of the 25-hour capture (release `20261006T194555Z`, which has no route shapes for these
lines, so positions are judged on straight lines between stops):

- 65 episodes, 60 of them with a departure: all 60 bound, each to one trip for its whole
  length, and each to the trip whose scheduled times it fits (each vehicle's trips alternate
  direction with plausible layovers). 61 episodes bound in all, 96.2 % of reports.
- Unbound: three episodes `ambiguous` throughout. 7AT9085 reported destination Praha with next
  stop Litvínov (its origin) for two hours before turning into a Litvínov trip, and 6SA3614 an
  hour the same way; 7AT9084 sent nine reports at 14:39. One episode at 04:48, before any trip of
  its line, is `not_in_service`.
- The GTFS-RT consistency check is clean (60 trips, 6,446 stop time updates, 297 snapshots).
- Own GPS lateness against the source delay on 10,585 bound reports: median difference +61 s,
  absolute p50 132 s and p90 421 s. The source's delay is frozen since the last stop, which on
  motorway legs is 20 minutes and more. Which is closer to what happened is for `rt evaluate` on
  actual departures (milestone R4).
- Two polls returned `busesCurrentLocations: null` and count as bad payloads.

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

## Open questions

- Terms of use.
