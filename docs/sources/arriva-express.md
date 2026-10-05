# Source dossier: Arriva Express (`arriva-express`)

Status: **one sample payload (2026-10-05, about 21:23 local), checked against release
`20261003T144231Z-53e241dba302`.** Endpoint URL, poll interval and terms of use are still to be
recorded.

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
| `lastStopName` | name, or `""` | last stop reached | `LastStop` / `CurrentStop` |
| `state` | `v pohybu`, `v zastávce` | moving / at a stop | `CurrentStop(at_stop)` when `v zastávce` |
| `delay` | string, signed minutes (`-7` … ) | delay | `Delay` fact (semantics below) |
| `latitude`, `longitude`, `angle` | WGS84, degrees (string) | position, bearing | `Position` with bearing |
| `spz` | licence plate, right-padded with spaces | vehicle | `VehicleKey` (trimmed); stable per physical bus |
| `updated` | ISO time labelled `+00:00` | time of the vehicle's last report | `observed_at` — see time semantics |

There is **no trip number, no next stop and no operating date**. The detail view on Arriva's
website (the earlier example with next stop, "Čas poslední polohy" and "Jede včas") shows more;
whether that comes from a separate endpoint is an open question.

## Matching (checked on the two express vehicles in the sample)

Through the inference engine (`BASE_PLAN.md` section 19): `LineRef(linkNumber)` → the line's
`jdf` trips active on the date, filtered by `Destination` (compared after normalizing spaces
and empty name parts: `Litvínov,,nádraží` = `Litvínov,nádraží`), then scored by the time
residual at the last stop and by GPS.

| Vehicle | Line | Observation | Unique candidate |
|---|---|---|---|
| `7AT9086` | `157710` Praha–Most–Chomutov–Litvínov | at Litvínov, nádraží, delay −2 | trip 27, Praha 19:45 → Litvínov 21:30 |
| `2TK5958` | `721341` Brno–Prostějov–Olomouc | standing at Olomouc, aut.nádr., delay −7 | trip 25, Brno 20:15 → Olomouc 21:35 |

Both resolve to exactly one active trip **when `updated` is read as local time**. Read as UTC
(23:23 local), neither has an active trip anywhere near.

Once matched, the plate keeps the binding (`BASE_PLAN.md` section 19.5); full re-inference runs
only at trip end, on a contradiction or when line or destination change. Express trips run
back and forth on the same line, so the circulation prior (section 22) will help pick the next
trip.

## Time and delay semantics

- **`updated` is most likely local time mislabelled `+00:00`** (see matching). Confirm by
  comparing with receipt time in a capture; the connector then reinterprets it as
  Europe/Prague.
- **`delay`** is signed whole minutes (negative values for early running). Reference event and
  rounding unknown; treated as `unknown` rounding (±59 s) at the last stop until calibrated.
  Long motorway gaps make own GPS delay plus a future travel-time provider the main delay
  source (`BASE_PLAN.md` section 21).
- A vehicle at its destination with a negative delay (both samples) has simply arrived early;
  the trip ends there.

## Open questions for the capture

- Endpoint URL, poll interval, terms of use.
- Confirm the `updated` timezone against receipt time.
- Is there a detail endpoint with next stop or trip identity?
- Rounding and reference event of `delay`.
