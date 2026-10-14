# Source dossier: PID vehicle positions from Golemio (`pid`)

Status: **probed 2026-10-15 around 00:10 local (a few full lists and deltas, 839 vehicles in the
first) with a test API key; recorded only.** Keys checked against release
`20261012T080259Z-8f416ec58c3a` (serving schema 5.x). Connector `sources/pid.py` (decoder
version 1). Not live (`live_feeds = []`) until a day of capture is replayed against the release
current at capture time: match rate per mode, quirks, observations per second and core cost.

PID (Pražská integrovaná doprava) covers Prague city buses, trams, metro, trolleybuses and the
ferry, the regional buses of Central Bohemia and the trains in its area. Golemio, Prague's data
platform, publishes the positions ROPID computes. BASE_PLAN.md §26.3, milestone R3.

## Endpoint

```text
GET https://api.golemio.cz/v2/vehiclepositions?limit=10000&includeNotTracking=true[&updatedSince=<x-last-modified>]
X-Access-Token: <API key>
Accept-Encoding: gzip
```

- **API key** in `X-Access-Token`, generated at `https://api.golemio.cz/api-keys`. The connector
  reads it as the secret `golemio` (`OBEHY_SECRET_GOLEMIO` or `[realtime.secrets] golemio`); the
  channel is not polled without it. The key is never archived or logged.
- **Rate limit** 20 requests per 8 s per key. One poll every 10 s.
- **Paging**: at most 10 000 vehicles per request (`limit`, `offset`); a full list at peak is
  expected well under that (to confirm in the capture).
- **Delta polling**: every response carries `x-last-modified`; `updatedSince=<that value>`
  returns only the vehicles updated since. The manifest's `cursor` sends it back on every poll and
  archives it in the poll's `request`; no cursor, a cursor older than 120 s or a restart gives a
  full list. Probe: full list 75 kB gzip (839 vehicles), a delta a few seconds later 62 vehicles,
  6 kB. A failed poll keeps the cursor, so the next delta covers the gap.
- `includeNotTracking=true` adds vehicles before and after their trip (forecast assignments,
  layovers). `includeNotPublic` (trips without a public schedule) is not requested.
- The response is cached for 5 s (`Cache-Control: s-maxage=5`).

## Response

A GeoJSON FeatureCollection; one Feature per vehicle on a trip. Times are ISO 8601 with offset.

| Field | Use |
| --- | --- |
| `geometry.coordinates` | `[lon, lat]` → `Position` |
| `properties.last_position.origin_timestamp` | fix time → `observed_at` (dropped beyond the policy skew, T15) |
| `…last_position.bearing` | bearing, may be null |
| `…last_position.delay.actual` | current delay in seconds → `Delay(…, "unknown")` while running |
| `…last_position.state_position` | `before_track(_delayed)`, `on_track`, `at_stop`, `off_track`, `after_track(_delayed)` (PID-Q4) |
| `…last_position.is_canceled` | cancelled trip (PID-Q4) |
| `…last_position.last_stop`, `next_stop` | PID GTFS stop ID, sequence, scheduled times (not used yet) |
| `properties.trip.gtfs.trip_id` | PID GTFS trip → `TripKey("pid:gtfs_trip_id")` |
| `…trip.gtfs.route_type` | GTFS mode; 2 is a train (feed czptt) |
| `…trip.cis.line_id`, `cis.trip_number` | CIS line + trip (regional buses) → fallback `TripKey("cis:line_trip")`; for trains `trip_number` is the train number |
| `…trip.vehicle_registration_number` | fleet number → `VehicleKey("<route_type>:<number>")` (PID-Q1) |
| `…trip.start_timestamp` | scheduled first departure (not used: the keyed date rule picks the date) |

## Keys against the release

Static PID trips carry `pid:gtfs_trip_id` keys from the regional overlay (157 105 in the probed
release: bus, tram, metro, trolleybus, ferry), regional buses also `cis:line_trip`, trains
`czptt:train_number` (the overlay leaves PID rail out; trains bind to the CZPTT run). Probe
against a release two days older than the data: 76 % of non-rail entries had their GTFS trip ID
in the release, 34 of 34 trains their train number. The misses are trip IDs absent from that
release; the capture replay measures this against a current one.

A 3-minute local recording at 00:24–00:27 local (17 polls, 3,639 observations) replayed against
that release bound 85/174 running bus key groups, 59/97 tram, 6/6 metro, 16/17 train and 0/4
trolleybus (`jdf/no_trip` 975 observations, `not_active` 184). Most misses were night lines:
the release has only 11–57 `pid:gtfs_trip_id` keys for each of the night trams 91–97, so night
and after-midnight trips look under-keyed by the static overlay (JrUtil), not mismatched by
the connector. The day capture is to show this by hour and line before going live.

## Quirks

| ID | Quirk | Handling |
| --- | --- | --- |
| PID-Q1 | `vehicle_registration_number` repeats across modes (and possibly operators) | vehicle key `<route_type>:<number>`; collisions to check in the capture |
| PID-Q2 | trains and metro have no fleet number | trains `train:<number>`, metro `trip:<gtfs trip>` |
| PID-Q3 | `cis.line_id` is `"none"` for trains and null for city lines | trains keyed by train number; the CIS key only where the line is numeric |
| PID-Q4 | after its trip, or when cancelled, an entry still names the trip | no trip key (vehicle and position only); before the trip `pre_trip`, without delay |

## Open questions (for the capture)

- Match rate per mode on a current release; how often the CIS fallback binds what the GTFS key
  does not.
- PID-Q1: fleet-number collisions within a mode across operators.
- How often a vehicle updates (observations per second at peak) and the core cost per
  observation; archive bytes per day with deltas.
- Overlap with DÚK (PID `100xxx` trips, DUK-Q11) and MPV net, and with SŽ on trains; the position
  order is DÚK, PID, SŽ (`rail.position_preference`, policy v7).
- Whether `delay.actual` before the trip (`before_track_delayed`) is a departure forecast worth
  keeping, and what the metro positions are (Golemio derives them from track segments).
