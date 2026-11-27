# Source dossier: IDS JMK GTFS-RT (`ids-jmk`)

Status: **probed and recorded on 2026-11-24** (Tuesday, 21:53–22:03 local: 41 polls, all
decoded) and replayed against release `20261124T070233Z` (Replay).
Decoders: the generic code decoders `gtfs-rt-vehicles` (version 1) and `gtfs-rt-alerts`.
Live on `jdf` and, for its trains, `czptt` (IDSJMK-Q1). Terms of use are still to be recorded.
Since 2026-11-27 its vehicles are not followed as oběhy (Keys).

IDS JMK is the integrated system of Jihomoravský kraj, run by KORDIS JMK: regional buses, Brno
trams, trolleybuses and buses (DPMB), and regional trains. KORDIS publishes the static GTFS
(`ids-jmk-gtfs`, the overlay on `jdf`) and this GTFS-RT dataset. Its public map
(`https://mapa.idsjmk.cz`) shows the same vehicles with a delay and names each one's trip and
run; it forbids automated download and needs the access token embedded in the page, so it is not
used (The map's API).

## Endpoint

```text
GET https://kordis-jmk.cz/gtfs/gtfsReal.dat
```

- No cookies, no key. Protobuf (`application/octet-stream`), about 106 KB.
- `Last-Modified` was 19 s before the request: the dataset is rewritten every 15–20 s. We poll
  the vehicles every 15 s and the alerts every 60 s (IDSJMK-Q4).

## Response

A `FeedMessage`, GTFS-RT 2.0, `FULL_DATASET`. On 2026-11-24 at 20:32 UTC: 855 entities, 798
vehicle positions and 57 alerts, no trip updates (IDSJMK-Q3).

```text
entity { id: "10690_24213225"
  vehicle { trip { trip_id: "22905" }
            position { latitude: 49.2130051 longitude: 16.5607338 bearing: 112 speed: 9 }
            current_status: IN_TRANSIT_TO  timestamp: 1795552337  stop_id: "U01621Z02"
            vehicle { id: "10690" label: "1069" license_plate: "" } } }
entity { alert { informed_entity { route_id: "423" }  effect: STOP_MOVED
                 header_text { "Výluka" (cs-CZ) }  description_text { … }  url { … } } }
```

- 674 of the 798 vehicles name a trip (`trip_id` only: no `start_date`, `route_id` or start
  time). All 674 are trips of the IDS JMK GTFS of the day: 396 bus, 158 rail, 73 tram, 47
  trolleybus. 18 trips were on two vehicles at once (coupled trams and train units).
- Fix age (header time − `timestamp`): median 5 s for vehicles with a trip, 90th percentile
  33 s; across all vehicles the 90th percentile is about 28 min (IDSJMK-Q7).

## Keys

- Trip: `ids-jmk:gtfs_trip_line_course` `<trip_id>` (serving 5.3): on `jdf` (jrutil-rs
  OVL-R131) the trip ID names every trip of its line/course, so it resolves to the one running
  on the day (IDSJMK-Q9); on `czptt` (OVL-R132) a train's trip ID names the national train
  whose number is its course on each date the line/course runs (IDSJMK-Q1). The channel's
  `trip_key_feed = "czptt"` sends an observation to `czptt` when its static data holds the key,
  else to `jdf`; one without a trip key follows its vehicle's last feed.
- Vehicle: `VehicleDescriptor.id`; the label is its fleet number (IDSJMK-Q2). Not a physical
  vehicle to follow (`[vehicles] physical = { default = false }`): with the trip named
  unreliably (IDSJMK-Q9) and no delays (IDSJMK-Q3), the oběhy cut from it were wrong. The
  assignments recorded until then are deleted from `history.vehicle_assignment` by hand, once.
- Alerts: trips, stops and routes by their GTFS IDs in the `ids-jmk:gtfs_*` namespaces the
  overlay publishes.

## Quirks

| ID | Quirk | Handling |
|---|---|---|
| IDSJMK-Q1 | regional trains are trips of the IDS JMK GTFS too, but the static build never overlays regional rail onto CZPTT, so their GTFS trip IDs have no output trip, and GTFS-RT does not say which trips are trains | jrutil-rs OVL-R132 keys each rail trip ID (`gtfs_trip_line_course`) to the CZPTT train numbered by its `api.txt` course, on the dates its line/course runs; the worker routes an observation to `czptt` when `czptt` holds its key (`trip_key_feed`, `Runner::route`), and a trip-less one to its vehicle's last feed (Replay) |
| IDSJMK-Q2 | `VehicleDescriptor.id` is the fleet number with a digit appended (`10690` for `1069`; 561 of 798), the label the fleet number itself (rail units have 6-digit IDs) | the ID is the vehicle key; the label the `fleet_number` attribute |
| IDSJMK-Q3 | no trip updates: the dataset carries no delay | no delay fact; progress and estimates come from the positions |
| IDSJMK-Q4 | the alerts are in the vehicles' dataset; there is no separate alerts URL | two channels on the same URL, the alerts polled every 60 s |
| IDSJMK-Q5 | `Cache-Control: max-age=86400` on a dataset rewritten every 15–20 s | none needed: the runtime keeps no HTTP cache |
| IDSJMK-Q6 | `stop_id` is zero-padded (`U01621Z02`) where the static GTFS is not (`U1621Z2`): 148 of 798 matched as given, 658 without the padding | not read; positions place the vehicle |
| IDSJMK-Q7 | vehicles out of service keep their last fix for hours (up to 5.5 h old) | the generic rule: a fix beyond the policy's clock skew carries no position |
| IDSJMK-Q8 | 124 vehicles had no trip (parked, between runs) | an observation with the vehicle alone: it ends the vehicle's last binding |
| IDSJMK-Q9 | for about a fifth of the runs the `trip_id` is a sibling calendar variant of the trip running today: the same line and course (`api.txt`) on other dates (26870 is `30/1202` on Friday 2026-11-20 only; on Tuesday 2026-11-24 `30/1202` is 38901). Against the export of the same day (2026-11-27, 22:10): 380 of 645 trip IDs named a trip not running that day, 264 of them with a sibling that did | the trip key is `ids-jmk:gtfs_trip_line_course`: jrutil-rs keys each crosswalked trip ID to every trip of its line/course with their dates (OVL-R131), so the literal lookup finds the run of the day. In the recording it named exactly one trip for every key, and the trip the trip ID names wherever that runs (Replay). Alerts read trip IDs the same way |
| IDSJMK-Q10 | the trip IDs are renumbered with every export: `trip_id` runs 1..N in file order, so one ID names a different trip in each (exports of 2026-11-20 and 2026-11-27: 36 of 41,157 shared IDs kept their line/course). The GTFS-RT names the IDs of the export KORDIS published last (on 2026-11-27 at 07:50 UTC); a release built from an earlier one binds nothing or the wrong trip until the next build | none yet: the release must carry the same export as the feed. Not fixable by keys; the map's line/course is stable (The map's API) |
| IDSJMK-Q11 | `Position.speed` is in km/h, not m/s (2026-11-27: median 10, 99th percentile 94, 49 of 764 above 40) | not read |
| IDSJMK-Q12 | the alerts' `informed_entity.route_id` is the line number (`423`) where the static GTFS's `route_id` is `L423D99`: none of 116 resolved on 2026-11-27, and the "Tweet" alerts inform nothing at all. So no IDS JMK alert reaches a trip, line or board; they show only in the full list | none yet: a static key from the line number to the route (jrutil-rs) would make them resolve |

## Replay

Recording 2026-11-24 21:53–22:03 local (41 polls, 31,133 vehicle observations, 762 per poll)
replayed on `jdf` against release `20261124T070233Z-1bda58bef584`: 396 of 736 running key
groups bound. By mode (trip key groups):

| Mode | Bound | `not_active` / `not_in_service` (IDSJMK-Q9) | No key |
|---|---|---|---|
| bus | 278 | 104 / 15 | 11 |
| tram | 68 | – | 15 |
| trolleybus | 34 | 15 / – | 7 |
| rail | – | – | 167 (IDSJMK-Q1) |

Without rail, 380 of 547 groups bound (69 %) by `gtfs_trip_id`; by line/course (IDSJMK-Q9)
511 of 547 resolve to one trip of the day (93 %). 4,062 observations named no trip
(IDSJMK-Q8). The release of 2026-11-22 gave the same numbers.

The key of IDSJMK-Q9, on the golden road slice built with OVL-R131 (jrutil-rs
`docs/golden.md`, the IDS JMK snapshot of 2026-11-21; the slice has fewer keys than a national
release), the same recording, once per key: by `gtfs_trip_id` 279 of 736 groups bound, by
`gtfs_trip_line_course` 378. Every group bound before stays bound to the same trip; 99 more
bind (90 were `not_active`, 9 `not_in_service`); no key names two trips on the day.

Trains (IDSJMK-Q1), recording 2026-11-24 22:42–22:52 local (40 polls, 26,527 observations)
replayed on a slice: CZPTT compiled from the messages of the 1,339 train numbers of IDS JMK's
`api.txt` (10,148 of 221,736) with OVL-R132, which keyed 3,523 of the feed's 3,526 rail trip
IDs; the golden road slice as `jdf` (its bus numbers are not comparable). Before, all 116 rail
key groups were `jdf` without a key; with `trip_key_feed`, all 116 went to `czptt`: 103 bound,
2 `ambiguous` (R 1132 has two CZPTT trips on Tuesdays 9–24 November, in the national package
too), 11 `not_in_service` (units already naming a morning trip). No road group went to `czptt`.

## The map's API

Looked at by hand on 2026-11-27, not recorded: every response says automated download is
forbidden and to ask KORDIS JMK for API access (Open questions).

- `GET https://mapa.idsjmk.cz/api/vehicles`, read by the page every 5 s, about 117 KB. Needs the
  header `X-Access-Token`, a token inlined in the page's HTML that expires (the page reloads
  itself on 403); the runtime has no such fetch.
- A vehicle: `ID`, `IDB` and `IDC` (the fleet numbers of a coupled set), `VType` (5 is a train),
  `LineID`, `RouteID`, `Course` (the run, *pořadí*), `Delay` (minutes), `LastStopID`,
  `FinalStopID`, `IsInactive`, a position and bearing.
- `LineID`/`RouteID` is the line/course of `api.txt`, the key `ids-jmk:line_course` the overlay
  already publishes: 24 of 25 vehicles looked up in the snapshot of 2026-11-20 (the 25th a
  special line). A train's `RouteID` is its national train number. So the map would bind
  literally with neither OVL-R131 nor OVL-R132.
- `/api/tweets`: the current events (*mimořádnosti*) with their lines. `/api/diversions`: the
  planned ones (*výluky*) with a point, a type (`Z`, `P`: a stop moved; `V`: a closure), a
  validity and line names. `/api/alerts`: a site-wide banner, usually empty.

## Open questions

- Terms of use.
- The map's API in place of this dataset: KORDIS JMK is to be asked for access.
- How the dataset behaves around midnight and DST (no `start_date`: the core picks the service
  date of each trip).
- Rail (IDSJMK-Q1) in a national release: the first build with OVL-R132.
- IDSJMK-Q9 in a national release: the first build with OVL-R131.
