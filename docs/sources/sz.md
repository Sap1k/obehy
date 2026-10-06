# Source dossier: SŽ train map (`sz-mapa`)

Status: **a 25-hour capture (2026-10-05 21:57 to 2026-10-06 22:43 local, 2,973 polls, 9,300
train-days) replayed against release `20261006T194555Z-06c0829d89aa` (serving schema 5.0)**, plus the upstream JrUtil WIP scraper `jrutil/src/SzMapa.fs`
(dvdkon/jrutil commit `587a50010a1a3edaa2f27248bff85254b8dd9160`). Terms of use, rate limits
and licence: to be confirmed.

## Endpoint

```text
GET https://mapy.spravazeleznic.cz/serverside/request2.php?module=Layers\OsVlaky&&action=load
```

One poll returns every train on the SŽ map (about 200 KB). No authentication or cookies are
needed. `obehy rt record` polls it every 30 s (channel `sz-mapa/trains`). All 2,973 polls in the
capture succeeded (median 145 ms). `md` is the response time, 0.7 s before receipt, not the age
of the data: a train's entry changes in only 16% of consecutive polls, so positions refresh about
every three minutes and a 60 s poll would lose little. The response envelope:

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
| `id` | `TR/<company>/<core>/<variant>/<year>/<yyyyMMdd>` | CZPTT **TR** identity plus the operating date | `TripKey(czptt:tr = "Tr:<company>:<core>:<variant>:<year>")` + `OperatingDate` |
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
| `e` | 0 / 1 (set at some point on 33% of train-days) | most likely: the train runs under ETCS supervision (see below) | train attribute for display; not used for matching or progress |

Names are Unicode (escaped in the JSON).

## Matching (replayed)

`id` → `Tr:<company>:<core>:<variant>:<year>` (exact string transform) resolves through
`source_key(namespace = czptt:tr)` to trips whose key validity covers the operating date in `id`
and whose service runs on it; the result is counted per run (`trip.run_key`, the trip parts of
one CZPTT path).

| | Train-days | Exactly one run | Ambiguous | Key, not running | No key |
|---|---:|---:|---:|---:|---:|
| Trains | 7,814 | 7,784 | 16 | 3 | 11 |
| Rail replacement (`s = 1`) | 1,486 | 1,486 | 0 | 0 | 0 |
| Total | 9,300 | 9,270 (99.7%) | 16 | 3 | 11 |

- **Ambiguous:** all 16 are RegioJet R 1011xx trains (company 3246) whose TR has two timetables
  active that day. Each resolves to exactly one run by train number
  (`source_key(namespace = czptt:train_number)`), so the connector falls back to `tn` when the TR
  is ambiguous, and quarantines only if that is ambiguous too.
- **No key:** 11 TRs absent from the CZPTT data (Os 36437, Sp 5283–5292, five Os 9145x trains of
  company 3407).
- **Not running:** 3 TRs whose timetables do not run on the reported date.
- The train number `tn` resolves 7,751 of the 7,814 trains alone; the TR stays the primary key.
- DÚK's vehicle feed also reports trains in its area by train number (`duk.md`); 876 of 919
  running DÚK train episodes resolve to one run, giving a second position and delay for the run.

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

## The `e` flag

`e` is undocumented. The 25-hour capture fits "running under ETCS supervision":

- It follows the track, not the train: 3,017 of 9,300 train-days switch it during the run, and
  60% of the 6,496 switches happen while the train stands at a station.
- Points passed with `e = 1` in 100% of observations lie on the corridor lines with ETCS in
  service: Praha – Kolín (Běchovice, Pečky, Velim), Kolín – Pardubice (Záboří, Řečany,
  Kostěnice), Pardubice – Česká Třebová (Zámrsk), Česká Třebová – Olomouc (Hoštejn, Lukavice,
  Moravičany), Přerov – Břeclav (Brodek u Přerova, Říkovice, Prosenice, Podivín) and
  Česká Třebová – Brno (Blansko, Rajhrad).
- It switches on and off at section boundaries: on leaving Praha hl.n., at Česká Třebová, at
  Brno-Horní Heršpice → Modřice, at Jezernice towards Drahotuše and Lipník; off at Praha-Libeň,
  Praha-Vršovice, Balabenka, Plzeň, České Budějovice, Olomouc, Kolín and Otrokovice.
- By category it is most common on long-distance trains (SC 88%, LE 88%, RJ 82%, IC 64% of
  observations; Os 13%), and practically absent for operators whose trains stay off those lines
  (Arriva 2%, GW Train, Die Länderbahn, RegioJet ÚK 0%); TL/TLX never.

## Capabilities

`vehicle_position`, `trip_progress`, `stop_event` (actual arrival/departure/passage at passenger
and operational points), `delay`, `prediction` (next stop), `trip_status` (replacement bus,
diversion). Platforms are not in this endpoint; station departure boards are a separate channel,
still to be investigated.

## Open questions

- Confirm `e` = ETCS supervision with SŽ.
- Rounding of `cr` and `de`.
- Terms of use.
