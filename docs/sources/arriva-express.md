# Source dossier: Arriva Express (`arriva-express`)

Status: **a 25-hour capture (2026-10-05 21:57 to 2026-10-06 22:43 local, 2,972 polls, 12
vehicles on three lines) replayed against release `20261006T194555Z-06c0829d89aa` (serving schema
5.0).** Terms of use are still to be recorded.

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
| `linkNumber` | 6 digits, e.g. `157710`, `721341` | CIS line | `LineRef(cis_line_id)`; resolves to `jdf:route:<line>` |
| `destinationName` | `Litvínov,,nádraží` | destination stop name, JDF `Obec,Část,Místo` form with empty parts | `Destination` |
| `lastStopName` | name, or `""` | **the next stop** (despite the name; the website labels it "Následující zastávka"); it changes when the bus departs the previous stop | `NextStop`; a change is a departure from the preceding call |
| `state` | `v pohybu`, `v zastávce` | moving / at a stop | `CurrentStop(at_stop)` when `v zastávce` |
| `delay` | string, signed minutes (`-7` … ) | delay | `Delay` fact (semantics below) |
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

Reproduce with `obehy rt replay --release <release-dir> --from <day> --to <day> --out <dir>`
(`report.json` coverage per fleet, `episodes.parquet` one row per episode).

Through the inference engine (`BASE_PLAN.md` section 19). The capture saw three lines, each with
four vehicles: `580916` (Praha – Teplice), `157710` (Praha – Most – Litvínov) and `721341`
(Brno – Olomouc). A vehicle's polls form an episode while plate, line and destination stay the
same (split after 30 minutes without a poll).

1. **Candidates:** `LineRef(linkNumber)` → the line's `jdf` trips running on the date whose last
   stop equals `destinationName` (compared without commas and spaces:
   `Brno, Benešova tř.,hotel GRAND` = `Brno,Benešova tř.hotel GRAND`) and whose schedule, widened
   by 30 minutes on each side, overlaps the episode. Express trips run about hourly in both directions, so
   this leaves 2–4 candidates for most episodes.
2. **Time score:** each time `lastStopName` changes, the bus has just departed the stop before
   it. `updated − delay` at that first poll is compared with the candidate's scheduled departure
   from the preceding stop; the score is the median absolute residual over the episode.

Of 65 episodes, 61 had at least one stop change; all 61 resolve to exactly one trip with a best
median residual of at most 0.9 minutes, against a median of 59.5 minutes for the runner-up. The
other four are a few minutes long without a stop change (one at 04:48 before any trip of its
line, so no candidate). Comparing against the reported stop itself instead of the preceding one
gives a systematic 10-minute residual: the reason `lastStopName` is read as the next stop.

Once matched, the plate keeps the binding (`BASE_PLAN.md` section 19.5); full re-inference runs
only at trip end, on a contradiction or when line or destination change. Express trips run
back and forth on the same line, so the circulation prior (section 22) will help pick the next
trip.

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
| ARRIVA-Q2 | `lastStopName` is the **next** stop; it changes when the bus departs the previous one | `NextStop`; a change is a departure from the preceding call |
| ARRIVA-Q3 | `delay` is signed, truncated, measured at the departure from the previous stop | `rounding = floor`, `reference = departure from the previous stop` |
| ARRIVA-Q4 | no trip number, no operating date, none requestable | keyless inference only (`infer/keyless/`) |
| ARRIVA-Q5 | `spz` is right-padded with spaces | trimmed in the connector |
| ARRIVA-Q6 | destination names keep empty JDF parts and differ in comma spacing | compared without commas and spaces, against the candidate's own calls only |
| ARRIVA-Q7 | the response is the whole fleet; a batch request without `referer` fails | request headers as above; only Arriva Express entries are kept |

## Open questions

- Terms of use.
