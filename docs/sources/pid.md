# Source dossier: PID vehicle positions from Golemio (`pid`)

Status: **probed 2026-10-15 around 00:10 local with a test API key; a 12.75 h server capture
replayed at 97–100 % per mode; live on jdf and czptt since 2026-10-15.** Keys first checked
against release `20261012T080259Z-8f416ec58c3a` (serving schema 5.x). Connector `sources/pid.py`
(decoder version 2).

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

A GeoJSON FeatureCollection; one Feature per trip of each vehicle's block (PID-Q6). Times are ISO 8601 with offset.

| Field | Use |
| --- | --- |
| `geometry.coordinates` | `[lon, lat]` → `Position` |
| `properties.last_position.origin_timestamp` | fix time → `observed_at` (dropped beyond the policy skew, T15) |
| `…last_position.bearing` | bearing, may be null |
| `…last_position.delay.actual` | current delay in seconds → `Delay(…, "unknown")` while running |
| `…last_position.state_position` | `before_track(_delayed)`, `on_track`, `at_stop`, `off_track`, `after_track(_delayed)`, `canceled` (PID-Q4) |
| `…last_position.tracking` | true exactly on the running states (`on_track`, `at_stop`, `off_track`) |
| `…last_position.is_canceled` | cancelled trip (PID-Q4) |
| `…last_position.last_stop`, `next_stop` | PID GTFS stop ID, sequence, scheduled times (not used yet) |
| `properties.trip.gtfs.trip_id` | PID GTFS trip → `TripKey("pid:gtfs_trip_id")` |
| `…trip.gtfs.route_type` | GTFS mode; 2 is a train (feed czptt) |
| `…trip.cis.line_id`, `cis.trip_number` | CIS line + trip (regional buses) → fallback `TripKey("cis:line_trip")`; for trains `trip_number` is the train number |
| `…trip.vehicle_registration_number` | fleet number → `VehicleKey("<route_type>:<number>")` (PID-Q1) |
| `…trip.start_timestamp` | scheduled first departure: orders a vehicle's upcoming trips (PID-Q6); the keyed date rule still picks the date |

## Vehicle attributes

What the connector states about its vehicles (`VehicleAttribute`, the vocabulary in
`src/obehy/data/vehicles/attributes.toml`, `BASE_PLAN.md` section 27). Values are written on
change to `rt.vehicle_attribute_observed`; identifiers among them bind the report to a
registered vehicle.

| Field | Attribute |
|---|---|
| `trip.wheelchair_accessible` | `wheelchair_accessible` |
| `trip.air_conditioned` | `air_conditioning` |
| `trip.usb_chargers` | `usb` |
| `trip.agency_name.real` | `operator` (the operator actually running the trip; it wins over other live sources) |
| `trip.vehicle_type.description_cs` | `category` |
| `trip.vehicle_registration_number` | `fleet_number` |

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

Prod capture 2026-10-14 22:00 – 2026-10-15 10:45 UTC (4,112 polls, 12 failed, all but 6 deltas)
replayed with SŽ map against release 20261015T014009Z (built after the JrUtil night-trip fix,
JrUtil 101bc65) and decoder 2 (PID-Q6): running key groups bound bus 17,197/17,208 (99.9 %),
tram 3,670/3,699 (99.2 %, was 67 % before the fix), metro 845/845, trolleybus 520/520, ferry
175/175, train 1,145/1,180 (97.0 %); SŽ map alongside 99.7 %. 2.89 M PID observations; jdf
`no_key` 108 k are vehicles after their last trip or between trips without a next one (PID-Q4,
Q6). Whole replay, both sources, 4.65 M observations in 27 min on the dev machine; live, PID
is about 60 observations a second. Archive: about 290 MB compressed for the 12.75 h (a daytime
delta is about 2.8 MB uncompressed, since nearly every vehicle moves within 10 s).

## Quirks

| ID | Quirk | Handling |
| --- | --- | --- |
| PID-Q1 | `vehicle_registration_number` repeats across modes (and possibly operators) | vehicle key `<route_type>:<number>`; collisions to check in the capture |
| PID-Q2 | trains and metro have no fleet number | trains `train:<number>`, metro `trip:<gtfs trip>` |
| PID-Q3 | `cis.line_id` is `"none"` for trains and null for city lines | trains keyed by train number; the CIS key only where the line is numeric |
| PID-Q4 | after its trip, or when cancelled (`canceled`, `is_canceled`), an entry still names the trip | no trip key (vehicle and position only); before the trip `pre_trip`, without delay |
| PID-Q7 | a regional bus is also reported by DÚK, which names it by CIS line and trip; PID's own key is its GTFS trip ID | a journey whose trip and day the observation's CIS key also finds is named by the CIS key (core `IDENTITY_NAMESPACES`), so both sources share one journey, one TripUpdate and one vehicle |
| PID-Q6 | `includeNotTracking` lists every trip of a vehicle's block (the previous `after_track`, the running one, the upcoming `before_track` ones), all at the vehicle's one position; in the capture about 45 % of vehicles in a poll had 2+ entries | one observation per vehicle and payload: the running entry (on the route over `off_track`; two still on it at once, 0.5 %, bind no trip), else the next trip to start as `pre_trip`, else the vehicle alone |
| PID-Q8 | at a trip's scheduled start Golemio reports the vehicle running it wherever it is, still driving to the first stop (late from its previous run or deadheading), and projects it onto the trip's path: `delay` then says it is far early (2026-10-15, vehicle `3:7694` on line 388 at 07:11, heading out of Prague: `delay` −1421 s, placed 12 km along) | a running state starts a trip only at the first stop or once the vehicle is seen moving along the path no further ahead of the timetable than the start prior allows, also after the scheduled start (core `_seen_starting`, as DUK-Q19); a trip not yet departed is never shown early |

## Open questions (for the capture)

- How often the CIS fallback binds what the GTFS key does not.
- PID-Q1: in the capture no fleet number appeared twice within a mode in one poll; 7 moved
  between related operators over the night (OAD Kolín / ČSAD Polkost, ARRIVA / ČSAD Benešov).
- Overlap with DÚK (PID `100xxx` trips, DUK-Q11) and MPV net, and with SŽ on trains; the position
  order is DÚK, PID, SŽ (`rail.position_preference`, policy v7).
- Whether `delay.actual` before the trip (`before_track_delayed`) is a departure forecast worth
  keeping, and what the metro positions are (Golemio derives them from track segments).
