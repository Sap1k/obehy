# Source dossier: IDS JMK GTFS-RT (`ids-jmk`)

Status: **probed and recorded on 2026-11-24** (Tuesday, 21:53–22:03 local: 41 polls, all
decoded) and replayed against release `20261124T070233Z` (Replay).
Decoders: the generic code decoders `gtfs-rt-vehicles` (version 1) and `gtfs-rt-alerts`.
Live on `jdf` only (IDSJMK-Q1). Terms of use are still to be recorded.

IDS JMK is the integrated system of Jihomoravský kraj, run by KORDIS JMK: regional buses, Brno
trams, trolleybuses and buses (DPMB), and regional trains. KORDIS publishes the static GTFS
(`ids-jmk-gtfs`, the overlay on `jdf`) and this GTFS-RT dataset. Its public map
(`https://mapa.idsjmk.cz`) shows the same vehicles with a delay, but names each vehicle's run
(*pořadí*), not its trip, and needs the access token embedded in the page; it is not used.

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

- Trip: `ids-jmk:gtfs_trip_line_course` `<trip_id>` on `jdf` (serving 5.3, jrutil-rs
  OVL-R131): the trip ID names every trip of its line/course, so it resolves to the one running
  on the day (IDSJMK-Q9). Rail trips have no key (IDSJMK-Q1).
- Vehicle: `VehicleDescriptor.id`; the label is its fleet number (IDSJMK-Q2).
- Alerts: trips, stops and routes by their GTFS IDs in the `ids-jmk:gtfs_*` namespaces the
  overlay publishes.

## Quirks

| ID | Quirk | Handling |
|---|---|---|
| IDSJMK-Q1 | regional trains are trips of the IDS JMK GTFS too, but the static build never overlays regional rail onto CZPTT, so their GTFS trip IDs have no key, and GTFS-RT does not say which trips are trains | the channel is `jdf` only; a train's observation stays unmatched. Binding them needs jrutil-rs to publish `ids-jmk:gtfs_trip_id` on `czptt` (by the train number of `api.txt` and date) and a feed chosen by the key |
| IDSJMK-Q2 | `VehicleDescriptor.id` is the fleet number with a digit appended (`10690` for `1069`; 561 of 798), the label the fleet number itself (rail units have 6-digit IDs) | the ID is the vehicle key; the label the `fleet_number` attribute |
| IDSJMK-Q3 | no trip updates: the dataset carries no delay | no delay fact; progress and estimates come from the positions |
| IDSJMK-Q4 | the alerts are in the vehicles' dataset; there is no separate alerts URL | two channels on the same URL, the alerts polled every 60 s |
| IDSJMK-Q5 | `Cache-Control: max-age=86400` on a dataset rewritten every 15–20 s | none needed: the runtime keeps no HTTP cache |
| IDSJMK-Q6 | `stop_id` is zero-padded (`U01621Z02`) where the static GTFS is not (`U1621Z2`): 148 of 798 matched as given, 658 without the padding | not read; positions place the vehicle |
| IDSJMK-Q7 | vehicles out of service keep their last fix for hours (up to 5.5 h old) | the generic rule: a fix beyond the policy's clock skew carries no position |
| IDSJMK-Q8 | 124 vehicles had no trip (parked, between runs) | an observation with the vehicle alone: it ends the vehicle's last binding |
| IDSJMK-Q9 | for about a fifth of the runs the `trip_id` is a sibling calendar variant of the trip running today: the same line and course (`api.txt`) on other dates (26870 is `30/1202` on Friday 2026-11-20 only; on Tuesday 2026-11-24 `30/1202` is 38901) | the trip key is `ids-jmk:gtfs_trip_line_course`: jrutil-rs keys each crosswalked trip ID to every trip of its line/course with their dates (OVL-R131), so the literal lookup finds the run of the day. In the recording it named exactly one trip for every key, and the trip the trip ID names wherever that runs (Replay). Alerts read trip IDs the same way |

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

## Open questions

- Terms of use.
- How the dataset behaves around midnight and DST (no `start_date`: the core picks the service
  date of each trip).
- Rail (IDSJMK-Q1).
- IDSJMK-Q9 in a national release: the first build with OVL-R131.
