# Source dossier: SŽ station boards (`sz-tabule`)

Status: **probed by hand on 2026-10-10** (Kolín and Poniklá, a few requests, before the egress
rule below). There is no capture yet; it is recorded with the R2 corpus (`docs/R2_SLICE.md`
section 11). Terms of use: none published; used anonymously as an accepted risk until told otherwise.

## Egress

SŽ is reported to IP-ban addresses that run services against it. Every request goes through the
proxy pool and sends no `User-Agent` (`docs/R2_SLICE.md` section 7). Never probe it directly.

## Endpoint

```text
POST https://mapy.spravazeleznic.cz/serverside/request2.php?module=Layers\InfoTabule&&action=loadTabule
Content-Type: application/x-www-form-urlencoded
SR70=534149&module=Layers%5CInfoTabule&action=loadTabule
```

- **Request and caching.** No cookies, token or `X-Requested-With` are needed. The response
  sets session cookies, which are ignored, and is `Cache-Control: no-store`.
- **Size and speed.** About 75 KB of HTML at Kolín (0.3–0.5 s), and about 17 KB at a small
  station.
- **Scope.** One station per request. There is no batch form.
- **The map's own use.** The map's info panel sends the same request with `onlyForVlak=<train
  number>`. That variant returns `{"n": "<platform>", "z": "<delay>"}`, but was `"-"` for most
  trains tried, including ones the full board showed with a platform. It is not used.
- **Request volume.** SŽ's web UI made about 170 requests in 3 minutes for one viewer.

## Payload

Two tables in `div.prijezd-odjezd`: `data-typ="prijezdy"` (arrivals) and `data-typ="odjezdy"`
(departures). Each has about 20 rows (`tr.row-even|row-odd`), whatever time span that covers.
Every value appears twice, once in the desktop cells (`d-none d-md-table-cell`) and once in the
mobile cells (`d-md-none`); the decoder reads the desktop cells by their column class.

| Cell (arrivals `inTaColP-n`, departures `inTaColO-n`) | Content |
|---|---|
| from / to | origin (arrivals) or destination and via stations (departures) |
| line | IDS line (`S2`, `R18`, `Ex1`) or empty |
| scheduled | `HH:mm` |
| actual | `HH:mm` or empty (with delay) |
| train | `Os 5825 ČD`: category, number and carrier; `data-trainnumber="5825"` |
| last position | `Kolín-Zálabí z 13:35 (10 min)`: point, time and delay, or empty |
| last column | platform or track, `-` if not assigned yet |

The header of the last column says what the value is. It reads `Nástupiště` (platform) at Kolín
and `Kolej` (track) at Poniklá.

## Matching

- **Row → run.** `data-trainnumber` → `source_key(czptt:train_number)`, dated by the scheduled
  time near reception (SZT-Q5).
- **Call.** The run's call at the station (`source_key(sr70)`) with that scheduled arrival
  (arrivals table) or departure (departures table).
- **Value.** It is always the call's shown platform label. Only `Kolej` values also map to a
  boarding point (`sr70:track`, and from there GTFS-RT `assigned_stop_id`). `Nástupiště` values
  stay labels (SZT-Q3).

## Capabilities

`platform` per call (arrival and departure). The actual times and last positions duplicate the
SŽ map and are not used.

## Quirks

| ID | Quirk | Handling |
|---|---|---|
| SZT-Q1 | the station must be the 6-digit SR70 with check digit; a 5-digit code returns an empty shell (1.4 KB); the check digit is not always Luhn (Poniklá `571500`) | codes come from `source_key(sr70)`, never computed |
| SZT-Q2 | the last column is `Nástupiště` (platform) at some stations and `Kolej` (track) at others | the header label is decoded per response into `PlatformAssignment.label` |
| SZT-Q3 | platform numbers do not match the static boarding points, which are CZPTT tracks (Kolín: board `1`–`5`, `1a`; static `100`–`116`); static assignments are not the real ones at larger stations anyway | the value overrides the shown label; with no track-to-platform layout and no per-platform coordinates, platform-numbered values never become stop IDs or GTFS-RT |
| SZT-Q4 | a platform is shown only about 60 min ahead; later rows show `-`; `ND` was also seen (meaning unconfirmed) | `-` and `ND` are "not assigned" and never clear an earlier assignment |
| SZT-Q5 | times are bare `HH:mm` | resolved to the nearest occurrence around reception (`docs/R2_SLICE.md` R13) |
| SZT-Q6 | a fixed number of rows (about 20 per table), not a fixed time span | demand scheduling reads busy stations more often (`[boards]` policy) |
| SZT-Q7 | the `onlyForVlak` per-train variant is mostly empty | not used |
| SZT-Q8 | addresses running services are reportedly IP-banned | proxy pool, no User-Agent, a 403 opens the circuit, a budget ceiling |

## Recording plan

`obehy rt record` with the demand scheduler (`sz-tabule/board`) against the active release, for
the same 25 hours as DÚK and the SŽ map. Each poll is archived like any channel. The station code
is part of the request, so it is kept in the index line.
