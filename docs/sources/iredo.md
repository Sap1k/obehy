# Source dossier: IREDO vehicle map (`iredo`)

Status: **probed on 2026-11-21** (Saturday, about 21:15 local): a few requests, no recording.
Live on both feeds from the start (no record-first phase). Decoder: the manifest's declarative
`[channel.decode]` (version 1). Terms of use are still to be recorded.

IREDO is the integrated system of Královéhradecký and Pardubický kraj. Its public map at
`https://iredo.online/map` shows regional buses, a few town lines with CIS numbers (Trutnov
city lines run by Arriva, `695216`) and trains. The two regional city operators (DPmHK, DPMP)
were not in the payload. IDSOK (`docs/sources/idsok.md`) runs the same software and payload.

## Endpoint

```text
POST https://iredo.online/map/mapData
content-type: application/json
{"w":12.0,"s":48.5,"e":19.0,"n":51.1,"zoom":10}
```

- No cookies, no key. A GET on the same URL answers the web app's HTML shell.
- The page polls every `refreshInterval` = 10 s. We poll every 15 s.
- The response is about 25 KB of JSON for 71 vehicles.
- The box and the zoom do not filter (IREDO-Q1): the whole fleet comes back for any box and
  zoom tried (7, 9, 12).

## Response

```json
{"language": "cs", "initialZoom": 9, "refreshInterval": 10, "lat": 50.1, "lng": 15.75,
 "connections": [
  {"id": "S-642305-120", "name": "642305 120", "vehicleType": "A", "sourceType": "S", "delay": 2,
   "operator": "CDS s.r.o. Náchod", "dep": "Hradec Králové,,Terminál HD", "dest": "Broumov,,žel.st.",
   "depTime": "21:10", "destTime": "23:13", "time": "21:15:44", "lat": 50.21719, "lon": 15.82767,
   "lineNumber": 642305, "angle": 79, "lineType": "D", "extLineName": "305",
   "desc": "305 Broumov-Náchod-Nové Město n.Met.-Hradec Králové-Praha", "isDetour": true,
   "passengerCount": 18},
  {"id": "K-1845", "name": "V2 (Sp 1845)", "vehicleType": "V", "sourceType": "K", "delay": 0,
   "operator": "České dráhy, a.s.", "dep": "Hradec Králové hl.n.", "dest": "Doudleby n.Orlicí",
   "depTime": "21:06", "destTime": "21:48", "time": "21:15:00", "lat": 50.22028, "lon": 15.907968,
   "trainNumber": "1845", "extLineName": "V2"}]}
```

| Field | Observed | Meaning |
|---|---|---|
| `id` | `S-642305-120`, `K-1845` | `S-<CIS line>-<trip>` for buses, `K-<train number>` for trains: the only place a bus's trip number is (IREDO-Q2) |
| `sourceType` | `S` 31, `K` 40 | bus (CIS timetable) or train |
| `vehicleType` | `A`, `V` | bus, train |
| `lineNumber` | `642305` | CIS line (buses only) |
| `trainNumber` | `"1845"` | trains only |
| `lineType` | `V`, `A`, `D`, `Z` | line category (regional, city, long-distance, ...); unused |
| `extLineName` | `305`, `V2`, `S36` | public line |
| `time` | `21:15:44` | time of the fix, local time of day without a date (IREDO-Q4); trains in whole minutes (IREDO-Q5) |
| `lat`, `lon` | `50.2`, `15.8` | position |
| `angle` | `79` | bearing; absent for trains |
| `delay` | `2` | delay in whole minutes; absent for 2 of 71 |
| `operator` | `ČD`, `Transdev Čechy`, `Leo Express Tenders`, `ARRIVA vlaky` | operator name |
| `dep`, `dest`, `depTime`, `destTime` | | first and last stop and their times |
| `passengerCount`, `isDetour` | | sometimes present; unused |

There is no vehicle ID, plate or fleet number (IREDO-Q3).

## Keys

- Buses: `cis:line_trip` `<lineNumber:06d>:<trip>` on `jdf`. The trip comes from `id`. The
  shared namespace makes a trip also reported by MPV net IDOL (interregional lines such as
  `690405`) or DÚK one journey.
- Trains: `czptt:train_number` `<trainNumber>` on `czptt`.

Not checked against a release yet; the first live day shows the bound share in
`/debug/realtime/unmatched` and the replay report. A 3-minute recording (2026-11-21 21:03 UTC,
12 polls, all decoded) gave 93 bus and 343 train observations; bus fixes were 21 s old at the
median (4–493 s).

## Quirks

| ID | Quirk | Handling |
|---|---|---|
| IREDO-Q1 | the request's box and zoom do not filter; every request returns the whole fleet | one request with a fixed whole-country box |
| IREDO-Q2 | the model has a `serviceNumber` field, but IREDO never sends it: the trip number is only in `id` (`S-642305-120`) | the line and trip are read from `id` by a regex; IDSOK is decoded the same way |
| IREDO-Q3 | no vehicle identity at all: `id` names the trip | the vehicle key is `trip:<id>`, and no mode is physical (`[vehicles] physical = { default = false }`), so no oběhy are learned |
| IREDO-Q4 | `time` is a local time of day without a date | `hhmmss`: resolved to the reading nearest the reception, within 12 h (midnight safe) |
| IREDO-Q5 | train times are whole minutes (the operators' on-board units round them), up to a minute ahead of the reception, and some are an older event's (up to about 20 minutes, as MPVNET-Q8); positions are operator GPS, finer than SŽ's | kept; a time beyond the skew drops the position (`needs_time`); trains rank above `sz-mapa` in `rail.position_preference` |

## Open questions

- Terms of use.
- How the source behaves around midnight and DST, and whether trains past midnight keep the
  previous day's train number.
