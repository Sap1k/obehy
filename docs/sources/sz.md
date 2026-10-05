# Source dossier: SŽ train map (`sz-mapa`)

Status: **one sample payload (2026-10-05 20:16:40, 469 trains) checked against release
`20261003T144231Z-53e241dba302`**, plus the upstream JrUtil WIP scraper `jrutil/src/SzMapa.fs`
(dvdkon/jrutil commit `587a50010a1a3edaa2f27248bff85254b8dd9160`). Terms of use, rate limits
and licence: to be confirmed.

## Endpoint

```text
GET https://mapy.spravazeleznic.cz/serverside/request2.php?module=Layers\OsVlaky&&action=load
```

One poll returns every train on the SŽ map (about 200 KB). No authentication or cookies are
needed. `obehy rt record` polls it every 30 s (channel `sz-mapa/trains`). The response
envelope:

```text
md                  response timestamp, local Europe/Prague, "dd.MM.yyyy HH:mm:ss" → source clock
cached, cachedResult, executionTime, success, messages, userAccountExpired
result[]            GeoJSON Features
```

## Per train

```json
{"type":"Feature","id":"TR/3189/KASO---57501/00/2026/20261005",
 "geometry":{"type":"Point","coordinates":[-675460.9,-968190.4]},
 "properties":{"id":"TR/3189/KASO---57501/00/2026/20261005","type":"V",
  "a":220.517,"tt":"Os","tn":"16001","na":"","fn":"Rumburk","ln":"Mladá Boleslav město",
  "cna":"Nový Bor","de":21,"nna":"AHr Skalice u Č.L. z","r":"3189","rr":1,
  "d":"ARRIVA vlaky s.r.o.","s":0,"di":0,"cp":"19:54","cr":"20:15","pde":"21 min",
  "nsn":"Česká Lípa hl.n.","nsn70":"56809","nst":"20:15","nsp":"20:36",
  "zst_sr70":"567990","e":0}}
```

(Coordinates in the example are illustrative.)

| Field | Observed | Meaning | Use in Oběhy |
|---|---|---|---|
| `id` | `TR/<company>/<core>/<variant>/<year>/<yyyyMMdd>` | CZPTT **TR** identity plus the operating date | `TripKey(czptt_tr_id = "Tr:<company>:<core>:<variant>:<year>")` + `OperatingDate` |
| `geometry` | Point, **S-JTSK / Křovák (EPSG:5514)** | train position | transform to WGS84 → `Position` |
| `a` | float, or `""` (mostly when standing) | bearing | heading for progress |
| `type` | always `V` | — | ignore |
| `tt`, `tn`, `na` | `Os`, `R`, `Sp`, `EC`, `RJ`…; train number; train name | category, number, name | `TripKey(train_number)` cross-check; display |
| `fn`, `ln` | names | origin and destination | cross-checks |
| `cna` | name | last point reached or passed (passenger or operational) | progress |
| `rr` | 0 / 1 (129 of 469 standing) | 1 = standing at the last point | `CurrentStop(at_stop)` |
| `cp`, `cr` | `HH:mm` | last point: timetabled and actual time | actual arrival (standing) or departure/passage (moving) |
| `de` | int, −1 … ; 0 for half the trains | current delay in minutes, **signed** | `Delay` fact |
| `pde` | `"N min"` or `""` | predicted delay | prediction candidate |
| `nna`, `zst_sr70` | name, 6-digit code or null | next point (may be a track location such as `vl. v km 2,847`, a block post `AHr …` or a junction `Odb …`) and its SR70 with check digit | progress (`NextStop`) |
| `nsn`, `nsn70`, `nst`, `nsp` | name, **5-digit** SR70 (no check digit), `HH:mm` ×2, or empty | next passenger stop, timetabled and predicted time | prediction for the next stop |
| `r`, `d` | company code, name | operator | `Operator` |
| `s` | 0 / 1 (30 trains) | rail-replacement bus | NAD handling |
| `di` | always 0 in the sample | diverted | `trip_status` |
| `e` | 0 / 1 (103 trains) | unknown | open question |

Names are Unicode (escaped in the JSON).

## Matching (checked)

`id` → `Tr:<company>:<core>:<variant>:<year>` (exact string transform) looked up in
`source_trip_map` namespace `czptt_tr_id`, filtered by the binding's validity, the binding's
calendar and the trip calendar for the operating date in the `id`. Result for the sample:
**468 of 469 trains resolve to exactly one CZPTT timetable (PA)**; its trip parts form one rail
run. The one exception (RegioJet R 101128) has two timetables active that day and stays
quarantined unless position or next-stop facts separate them. A validity-range check without
calendars leaves 101 trains with 2–4 candidates, so calendars are essential. The train number
`tn` agrees with `rail_trip_key` wherever both resolve.

## Semantics

- **Times are bare `HH:mm` without a date.** Recover the date from `md`: a time more than 12 h
  after `md`'s time of day belongs to the previous day. `cr` is treated as truncated to the
  minute (`[T, T+59 s]`) until a capture shows otherwise.
- **Arrival vs departure at the last point.** With `rr = 1`, `cr` is the actual arrival. Once
  the train leaves (`rr` back to 0 at the same `cna`), the new `cr` is the departure, and the
  previous observation supplies the arrival. A train first seen already past the point gives a
  departure or passage only.
- **Points by name and SR70.** `cna` is name-only: map it to SR70 through the SR70 catalogue's
  20-character names, then to the run's call at or after current progress. `zst_sr70` is the
  6-digit SR70 with check digit; `nsn70` the 5-digit code without it.
- **Delay `de`** is signed whole minutes measured at the last point; strong evidence for rail.
- Unchanged train entries between polls carry no new information.

## Capabilities

`vehicle_position`, `trip_progress`, `stop_event` (actual arrival/departure/passage at passenger
and operational points), `delay`, `prediction` (next stop), `trip_status` (replacement bus,
diversion). Platforms are not in this endpoint; station departure boards are a separate channel,
still to be investigated.

## Open questions for the capture

- Meaning of `e`; rounding of `cr` and `de`.
- How fresh the geometry is relative to `md`; poll interval; terms of use.
