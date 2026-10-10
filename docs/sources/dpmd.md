# Source dossier: DPMD vehicle map (`dpmd`)

Status: **reverse-engineered from the public map page and a 3-minute capture (2026-10-10
12:25–12:28 local, a Saturday midday: 60 polls, 38 changed bodies, 9 buses in service, 56
parked).** Keys checked against release `20261006T194555Z-06c0829d89aa` (serving schema 5.0).
No connector, no replay yet. Terms of use are still to be recorded.

Dopravní podnik města Děčína, a.s. (DPMD) runs the Děčín city buses (lines 2xx). DÚK's vehicle
list does not carry them: at 12:30 on the same day, none of its 236 vehicles was on a `515xxx`
line. So this source would add Děčín city transport outright.

## Endpoint

```text
GET https://tabla.dpmdas.cz/data/TrafficState.js?v=<Date.now()>
```

No authentication. CORS `*`. The map at `https://doprava.dpmdas.cz/` fetches it every 2 s
(`cache: "no-store"`, 5 s timeout).

- **`v` is a cache buster**: the page sends `Date.now()` (epoch milliseconds), and any value works.
  It matters: without `v` the server (openresty) returned a copy 5 minutes old (DPMD-Q9).
- **The data is regenerated every 5 s**: `TimeStamp` advanced by 5 s in all 37 gaps of the
  capture, and every regeneration changes `datahash`.
- **Conditional requests work**: `If-None-Match` with the last `ETag` (weak, `W/"…"`) returns
  `304 Not Modified` while nothing changed, even with a new `v`. Polling every 3 s gave 38 bodies
  and 22 `304`s.
- **Size**: about 500 KB, or 34–39 KB with `Accept-Encoding: gzip`. Two thirds of it is the stop
  list (below), which barely changes.

The page shows only vehicles in service. `?all=1` on the page URL also shows the parked ones,
but the file always contains both. The page's other URL parameters (`bus`, `drv`, `fuel`,
`course`, `line`, `stop`) filter on the client only.

## Response

JavaScript, not JSON (DPMD-Q2). The page runs it through `new Function` and reads four
variables:

```js
/* {"TimeStamp":"2026-10-10T12:24:37.371"} */ busstops=[{…},…]; buspositions=[{…},…];
legends=[{…},…]; datahash='c4bbeb5e85acaa9da0e9bffd999d1b1c';
```

The objects are JavaScript literals with unquoted keys and backtick strings, and HTML strings
are concatenated with page variables (`` `…`+vehicleImageUrl + `834.jpg…` ``). A connector has
to read the literals itself; it must never evaluate them.

### `buspositions` (one per vehicle)

Top-level fields:

| Field | Example | Meaning |
|---|---|---|
| `id` | `02f766f8` | record hash |
| `line` | `201` | public line number; empty when parked |
| `vehicle` | `834` | fleet number |
| `driver` | `1310` | driver number: personal data, never stored (DPMD-Q8) |
| `course` | `6119` | duty / block (*turnus*), also `Turnus` in the body |
| `coords` | `50.78115082N, 14.22463036E` | position, a string with hemisphere letters |
| `arrow` | `67` | heading in degrees; it moves even when standing (0 km/h) |
| `fuel` | `C` / `E` / `D` / ` ` | CNG, electric, diesel, unknown |
| `color`, `hue`, `brightness` | `yellow`, `90` | marker colour: blue = parked; yellow with hue 90 on time, 240 one minute late, 0 two or more minutes late (`legends`) |
| `final1`, `final2` | `{name, coords}` or `null` | first and last stop of the trip |
| `shapes` | `<gpx>…<trk><name>AUT_00002</name>…` | the trip's path as GPX; the name is a route variant |
| `off` | `1` | present on parked vehicles only (DPMD-Q5) |
| `header`, `body`, `hint` | HTML / text | the popup; most trip fields are only in here |

Fields in `body`, as `<br>Label: value`:

| Label | Example | Meaning |
|---|---|---|
| `Čas` | `10.10.2026 12:24:38` | time of the fix, local, 1 s resolution, no offset (DPMD-Q1) |
| `Řidič` | `1310` | driver again (DPMD-Q8) |
| `Autobus`, `Typ vozu`, `SPZ` | `834`, `MAN`, `9U51792` | fleet number, model, plate |
| `Linka` | `201` | line |
| `Spoj` | `231` | trip number (*spoj*), the CIS trip number |
| `Turnus` | `6119` | block |
| `Konečná` | `Nemocnice` | headsign (terminus) |
| `Rychlost` | `5 km/h` | speed |
| `Zpoždění` | `2 min.` | delay in whole minutes; absent when on time (DPMD-Q4) |
| `Dopravce` | `DPMD, a.s.` | operator (always DPMD in the capture) |

`Linka`, `Spoj`, `Turnus`, `Konečná` and `Zpoždění` appear only while the vehicle is on a trip.
Below the fields, under *Jízdní řád*, the body lists the trip's calls as `HH:MM - <short stop
name>` (`12:07 - Hl.nádr.`), and `&darr;&nbsp;&nbsp;&darr;&nbsp;&nbsp;&darr;` is inserted
between the last stop passed and the next one. That is a next-stop signal (DPMD-Q3). A trip
that has finished has no marker.

### `busstops` (one per stop post)

226 posts. Each has `stopid1` (node, `0000000002`), `stopid2` (post, `000000000200001`, called
the GTFS id) and `coords`. Its body has `Sloupek` (post), `Pasport`, `GTFS`, `Označení CIS`
(CIS stop number, `66601`), `Pásmo CIS` (fare zone) and `ID oblasti`. Static data:
it is not needed every poll, and it makes up 69 % of the bytes (343 of 498 KB).

## Keys against the release

DPMD lines are in the JDF export under agency `62240935` (DPMD's IČO) with CIS line number
**`515` + the public line** (`201` → `515201`). That holds for all 15 DPMD lines in the release
(201, 202, 204, 208, 209, 210, 212, 214, 216, 217, 218, 229, 232, 233, 237) and all 1,587 of
their trip keys. The key is `cis:line_trip` `515<Linka>:<Spoj>`, built the way DÚK's
`CISLineID` is padded (DUK-Q9) and never inferred. A line outside the release (a new number)
stays unmatched as `no_line`.

The 9 vehicles in service at 12:28:

| Key | First departure (live list / release) | Runs 2026-10-10 |
|---|---|---|
| `515201:231` | 11:50 Chrochvice / 11:50 | yes (finished, at the terminus: DPMD-Q6) |
| `515201:232` | 12:12 Nemocnice / 12:12 | yes |
| `515201:233` | 12:20 Chrochvice / 12:20 | yes |
| `515204:230` | 12:28 Březiny / 12:28 | yes |
| `515214:205` | 12:17 Hl.nádr. / 12:17 | yes |
| `515218:216` | 12:00 Horní Oldřichov / 12:00 | yes (finished) |
| `515229:241` | 12:07 Bynov / 12:07 | yes |
| `515229:242` | 12:18 Nebočady / 12:18 | yes |
| `515216:303` | none listed / 09:00 ZOO Děčín | **no**: line 216 is not running (season ended 2026-09-28); a wrong selection (DPMD-Q7) |

8 of 9 resolve exactly, with matching first departures.

## Time and delay semantics

- **Times are Prague local time without an offset**: `TimeStamp` (ISO, milliseconds) and `Čas`
  (`dd.mm.yyyy HH:MM:SS`). Both are read with `times.resolve_local` against the reception time.
- **Fixes are fresh.** A vehicle in service has a new `Čas` in almost every regeneration (28–38
  distinct values over 38 bodies). At a regeneration it is 3.6 s old at the median (95th
  percentile 18 s, maximum 23 s), and once −1 s (`Čas` one second after `TimeStamp`). Add the
  poll interval for the age at reception.
- **The delay is whole minutes, never negative.** It is absent at 0, and the capture saw only
  1, 2 and 3 min (196 readings). Bus 115 had already passed a 12:30 stop at 12:28 with no delay,
  so early running reads as on time (DPMD-Q4). It is a weak constraint like DÚK's: its reference
  (arrival or departure) is unknown, and GPS progress against the timetable is the better
  measure.
- The trip's call times in the body are the timetable (they match the release), not
  predictions.

## Quirks

| ID | Behaviour | Handling (planned) |
|---|---|---|
| DPMD-Q1 | `TimeStamp` and `Čas` are local times without an offset | `times.resolve_local` against `received_at`; ambiguous hours resolve to the reading closest to reception |
| DPMD-Q2 | the payload is JavaScript (backtick literals, unquoted keys, HTML concatenated with page variables), and the trip fields live only inside the popup HTML | the connector parses the literals and the `<br>Label: value` lines with a narrow parser; never evaluated; a shape change is a `PayloadError` |
| DPMD-Q3 | the next stop is only a `&darr;` marker between two lines of the popup timetable, whose stop names are shortened (`Hl.nádr.`) | `NextStop` by position in the list, checked against the trip's call times, never by name |
| DPMD-Q4 | the delay is whole minutes, absent at 0 and never negative: early running shows as on time | absent `Zpoždění` on a trip is `Delay(0)` as a weak constraint; GPS progress decides |
| DPMD-Q5 | parked vehicles (`off: 1`, blue) stay in the list with their last position, up to about two weeks old in the capture | dropped by the connector |
| DPMD-Q6 | a finished trip's key stays on the vehicle at the terminus, with its last delay (`515201:231` ended 12:24 and was still reported at 12:28 with 2 min) | as DUK-Q4: the journey finishes from GPS progress and the vehicle is in layover |
| DPMD-Q7 | now and then a bus is logged into the wrong trip: bus 705 reported `515216:303` at 12:28 on 2026-10-10, a trip of seasonal line 216, which is not running (its last day was 2026-09-28; DPMD's own popup showed no timetable for it). A transient driver selection error, not a numbering scheme | taken literally: `not_in_service` until the driver selects the right trip |
| DPMD-Q8 | every vehicle carries its driver's number (`driver`, `Řidič`) | personal data: a recorder filter removes it before archiving; the core never sees it |
| DPMD-Q9 | without a fresh `v` the server returns a cached copy (5 minutes old on 2026-10-10) | always send `v=<epoch ms>`, and compare `TimeStamp` with reception (a stale body is skipped) |

## Recording plan

- **Channel `dpmd/vehicles`, polled every 10 s.** Data regenerates every 5 s, but fixes move at
  bus speeds and DÚK runs at 15 s. Send `Accept-Encoding: gzip`, `If-None-Match` and a fresh
  `v`; a `304` costs nothing.
- **Recorder filter `dpmd@1`.** It keeps `TimeStamp`, `datahash` and the vehicles in service, and
  drops the driver fields (DPMD-Q8), `busstops`, `shapes` and the parked vehicles. As with
  `arriva-express@1`, the index line keeps the source size and SHA-256. The stop list and the
  shapes can be snapshotted daily if they are ever needed (stop matching, variant names).
- **Facts:**
  - `VehicleKey(vehicle)`;
  - `TripKey("cis:line_trip", "515<Linka>:<Spoj>")` while on a trip;
  - `Position` from `coords` with `arrow` as bearing, dropped below a few km/h because the
    heading is noise at a standstill;
  - `Delay` (DPMD-Q4);
  - `NextStop` (DPMD-Q3).
  There is no source state: DPMD has no pre-trip state, and a vehicle without `Linka` is not on
  a trip. The manifest semantics stay at their defaults (no DÚK readings).
- **Feed:** jdf.

## Other DPMD data (not recorded)

`https://tabla.dpmdas.cz/` (the stop departure boards) reads
`https://tabla.dpmdas.cz/data/Stops.json` (346 nodes and posts with coordinates, CIS `StopId`
and fare zone; `Version` gives the timetable period, for example `20261008110737::20261008::20261212`)
and `https://tabla.dpmdas.cz/data/<node>.json?ts=<epoch ms>` every 5 s. The board lists the
next departures from a node within 120 minutes:
- `RouteName`, `FinalStation`, `Platform`, `Carrier` (DPMD, and DÚK regional lines as `DSÚK`);
- `TimeToDeparture` in minutes, `Delay` and `CarNumber`;
- a `RouteId` hash rather than a trip number.

It holds predictions for DÚK lines as well, but without a trip key it would need inference.
Note it for R2 arbitration.

## Open questions

- Terms of use: the endpoint is public with CORS `*`, and nothing is published about reuse.
- How the source behaves around midnight, DST and service starts: the capture covers only
  3 minutes at midday. A 25-hour capture (`obehy rt record`) is the next step, as for the other
  sources.
- Does `Zpoždění` ever show early running at another time of day, or larger delays as hours?
