# Source dossier: SŽ train map (`sz-mapa`)

Status: **a 25-hour capture (2026-10-05 21:57 to 2026-10-06 22:43 local, 2,973 polls, 9,300
train-days) replayed against release `20261006T194555Z-06c0829d89aa` (serving schema 5.0)**, plus the upstream JrUtil WIP scraper `jrutil/src/SzMapa.fs`
(dvdkon/jrutil commit `587a50010a1a3edaa2f27248bff85254b8dd9160`). Terms of use and licence:
none published; used anonymously through the proxy pool as an accepted risk.

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

## Egress

SŽ is reported to IP-ban addresses that run services against it. From R2 every request goes
through the proxy pool and sends no `User-Agent` (`docs/R2_SLICE.md` section 7). Upstream JrUtil's
`SzMapa.fs` polls every 10 s without a User-Agent; its `Grapp.fs` limits itself to 3 concurrent
and 5 requests per second and treats a 403 as blocked.

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

Reproduce with `obehy rt replay --release <release-dir> --from <day> --to <day> --out <dir>`
(`report.json` coverage per fleet, `episodes.parquet` one row per episode).

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

## Connector (`realtime/sources/sz.py`), measured

Checked on the 2026-10-06 capture (sampled one poll in 60, and a 2-hour replay 07:00-09:00 local
through the core against release `20261006T194555Z-06c0829d89aa`, with the serving 5.1 `sr70`
keys derived for the measurement):

- **Positions (SZ-Q1):** `pyproj` EPSG:5514 → WGS84. Trains standing at a point (`rr = 1`)
  decode a median 24 m from the catalogue's point (p90 96 m, p99 182 m; n = 5,426).
- **Names (SZ-Q4):** every `cna` in the sample is a `NÁZEV20` name of the catalogue; 77% name
  one code and 23% several (`Brno hl.n.` …), decided by the run's calls. Of about 26,000 point
  events in the replay, 99.5% place on the run. Of the 135 that do not, 96 name points the run
  does not have: a train diverted off its timetabled route (9887 via Lovosice instead of
  Litoměřice) or points missing from the static data (Kadaň on the Kadaň předměstí - Prunéřov
  part). 28 contradict a GPS event of a neighbouring call by more than the tolerance, and 11 are
  a train's first report naming a point already well behind it. A point repeated while other
  fields of the entry change resolves from the call the source placed the train at last (SZ-Q10).
- **Binding:** 1,653 of 1,660 SŽ train key groups and 142 of 150 DÚK train key groups bind in
  the replay window.
- **Prediction:** for the predicted next stop, `anchor_change` errs 91 s on average (bias
  −22 s, n = 2,751), plain propagation 92 s, SŽ's own prediction 100 s; the policy uses
  `anchor_change`.

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
diversion). Platforms are not in this endpoint; they come from the station boards
(`sz-tabule.md`).

## Quirks

| ID | Quirk | Handling |
|---|---|---|
| SZ-Q1 | positions in S-JTSK / Křovák (EPSG:5514) | connector transforms to WGS84 |
| SZ-Q2 | `cp`, `cr`, `nst`, `nsp` are bare `HH:mm`; `md` is local time without an offset | `md` resolved against `received_at` (`BASE_PLAN.md` 19.3, which covers the repeated autumn hour); a time > 12 h after `md` belongs to the previous day |
| SZ-Q3 | `cr` is the arrival while `rr = 1` and the departure after the train leaves the same `cna` | arrival and departure kept apart (section 20.7) |
| SZ-Q4 | `cna` is name-only (SR70 `NÁZEV20` names); `zst_sr70` has 6 digits with check digit, `nsn70` 5 without | the connector maps a unique `NÁZEV20` name to its code (`data/realtime/sr70-name20.csv`); else the run's last `NextPoint` of that name gives it; the call is the run's first with that `source_key(sr70)` at or after progress; 6-digit codes lose their check digit |
| SZ-Q5 | RegioJet R 1011xx: one TR with two timetables active the same day | fall back to `tn`; quarantine only if that is ambiguous too |
| SZ-Q6 | an entry changes in only 16% of polls; unchanged entries carry no new information | not counted as fresh fixes; never makes a train look stationary |
| SZ-Q7 | `nna` can be a track location, block post or junction | an operational point, not a stop |
| SZ-Q8 | bearing `a` is `""` when standing; `pde` is the string `"N min"` or `""` | parsed in the connector |
| SZ-Q9 | TRs absent from CZPTT, or present but not running that day | unmatched, shown as such |
| SZ-Q10 | `cna` stays the same while other fields of the entry change (delay, position), so a point repeats; another source may have passed it already | resolved from the call this source placed the train at last; a repeat of a recorded event changes nothing |
| SZ-Q11 | a train reports points its timetable does not have (diverted; `di` stays 0) | unresolved, counted; nothing moves |

## Open questions

- Confirm `e` = ETCS supervision with SŽ.
- Rounding of `cr` and `de`.
