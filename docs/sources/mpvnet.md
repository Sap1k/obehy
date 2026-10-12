# Source dossier: MPV net regional vehicle maps (`mpvnet`)

Status: **probed on 2026-10-11** (Sunday, about 17:30 local): a few requests per portal, and
a 40-second recording of all four channels. Keys checked against release
`20261006T194555Z-06c0829d89aa` (serving schema 5.0). Recorded from 2026-10-11 on (`obehy
realtime` archives every manifest channel). Connector `sources/mpvnet.py` (decoder version 2).
Buses are live on jdf since 2026-10-12, after a day of capture settled the delay fields and
replayed clean (see Replay). Trains are not live (`live_feeds = ["jdf"]`) until their positions
prove to be GPS (MPVNET-Q8). Terms of use are still to be recorded.

MPV net runs the public vehicle maps of several regional integrated systems on one piece of
software. Each portal is one channel of this source:

| Channel | Portal | System | Vehicles at 17:30 |
|---|---|---|---|
| `mpvnet/idol` | `https://www.mpvnet.cz/idol/map` | IDOL, Liberecký kraj | 135 (88 buses, 47 trains) |
| `mpvnet/jikord` | `https://www.mpvnet.cz/jikord/map` | JIKORD, Jihočeský kraj | 207 (123 buses, 84 trains) |
| `mpvnet/odis` | `https://www.mpvnet.cz/odis/map` | ODIS, Moravskoslezský kraj | 335 (273 buses, 62 trains) |
| `mpvnet/zlin` | `https://www.mpvnet.cz/zlin/map` | IDZK, Zlínský kraj | 146 (103 buses, 43 trains) |

The portals barely overlap: 4 vehicles were in both ODIS and Zlín, and none were shared by any
other pair.

## Endpoint

```text
POST https://www.mpvnet.cz/<portal>/map/mapData
content-type: application/json; charset=UTF-8
origin: https://www.mpvnet.cz          required: without it the server answers 400 (MPVNET-Q7)
{"w":12.0,"s":48.5,"e":19.0,"n":51.1,"zoom":10,"showStops":false,"mapFilterId":null}
```

- No cookies.
- The page polls every `refreshInterval` = 10 s. We poll every 15 s.
- Each response is 40–120 KB of JSON, about 8–19 KB zstd-compressed in the archive, answered in
  50–150 ms (once 830 ms).
- The server (IIS) does not compress, even when asked.
- The bounding box barely filters (MPVNET-Q1). The manifest sends a fixed whole-country box.

Storage at 15 s: about 46 KB per round of four polls, about 270 MB a day.

## Response

```json
{"portal": "Idol", "refreshInterval": 10, "regionBorder": {"north": 51.02, ...},
 "transmitters": [
  {"t": "H", "c": 0, "dm": 2, "ds": "$z2~min.~17~s", "cr": "OAD Kolín MH",
   "x": 50.90275, "y": 15.20265, "dt": "11.10.2026", "tm": "17:27:37", "ico": "a_270.png",
   "cn": "001630|630|4106|Nové Město p.Smrkem,Ludvíkov,Jednota|114|Nové Město p.Smrkem,Přebytek|137|1046|Świeradów-Zdrój,…|Liberec,aut.nádr.|0|6SJ4698|0|0|0|0|0|0|0|0|0|0",
   "cat": 2},
  {"t": "T", "c": 681, "dm": 3, "ds": "$z3~min.~00~s", "n": "Sp 1815", "cr": "České dráhy",
   "x": 50.58363, "y": 15.559486, "dt": "11.10.2026", "tm": "17:08:00", "ico": "a_0.png",
   "cn": "Sp 1815|V41|1815|Martinice v Krkonoších|180|||||Trutnov hl.n.|1|", "cat": 1}]}
```

| Field | Observed | Meaning |
|---|---|---|
| `t` | `H` bus, `T` train | vehicle kind |
| `cat` | `1` train, `2` regional bus, `3` city bus (DPO, DSZO), `0` without a trip | category |
| `c` | `0` for buses; `681` ČD, `1106` Arriva vlaky, `1020` Die Länderbahn | train operator code |
| `cr` | `BusLine LK`, `České dráhy` | operator name |
| `x`, `y` | `50.9`, `15.2` | **latitude, longitude** (MPVNET-Q5) |
| `dt`, `tm` | `11.10.2026`, `17:27:37` | time of the fix, local, no offset (MPVNET-Q4). Trains have whole minutes, or a time from their own unit |
| `dm`, `ds` | `2`, `$z2~min.~17~s` | delay: `$z` late, `$p` on time or early, with seconds (MPVNET-Q6) |
| `ico` | `a_270.png`, `a_stoji.png` | bearing in degrees, or standing |
| `n` | `Sp 1815` | trains only: category and number |
| `cn` | pipe-separated | the trip record (MPVNET-Q2), below |

`cn` for a bus (22 fields):

| # | Example | Meaning |
|---|---|---|
| 0 | `001630` | CIS line (6 digits); for city buses the bare public line (MPVNET-Q3) |
| 1 | `630` | public line |
| 2 | `4106` | CIS trip number (*spoj*); for city buses a course number, not a trip (MPVNET-Q3) |
| 3 | `Nové Město p.Smrkem,Ludvíkov,Jednota` | the last stop passed (empty before the first one) |
| 4 | `114` | delay at the stop in 3, signed seconds: it grows while the bus stands there and freezes when it leaves |
| 5 | `Nové Město p.Smrkem,Přebytek` | the next stop |
| 6 | `137` | current delay, signed seconds, updated between stops (MPVNET-Q9) |
| 7 | `1046` | scheduled departure at the stop in 5, as a minute of the day (`17:26`) |
| 8, 9 | `Świeradów-Zdrój,…`, `Liberec,aut.nádr.` | first and last stop of the trip |
| 10 | `0` / `1` | unknown flag |
| 11 | `6SJ4698`, `Ev. č. 226` | licence plate, or fleet number for city buses |
| 12–21 | `0` | flags, almost always 0 |

`cn` for a train (12 fields): `Sp 1815 | V41 | 1815 | <last station> | <delay s> | | | | |
<destination> | ±1 | `. Field 2 is the train number. Field 4 is the delay in seconds, always
equal to `ds` and to 60 × `dm`. The ±1 field is unknown; it was `1` for ČD and Länderbahn and
`-1` for Arriva vlaky.

## Keys against the release

The 40-second recording at 17:33 on 2026-10-11, against release `20261006T194555Z`. A key counts
as resolved when it names exactly one trip that runs that day (or the day before, after
midnight) with the fix time inside its scheduled span, widened by 15 minutes:

| Portal | Regional buses → `cis:line_trip` `<cn0>:<cn2>` | City buses | Trains → `czptt:train_number` |
|---|---|---|---|
| IDOL | 74 / 89 | none | 41 / 47 (3 several, 3 none) |
| JIKORD | 124 / 126 | none | 77 / 84 (4 several, 3 none) |
| ODIS | 158 / 161 | 116 DPO: the key exists, the trip is wrong (MPVNET-Q3) | 56 / 63 (6 several, 1 none) |
| Zlín | 88 / 89 | 28 DSZO: as DPO (MPVNET-Q3) | 41 / 43 (2 several) |

"Several" are train numbers with several trip parts, which rail runs handle. Regional buses and
trains carry literal keys, as DÚK does. IDOL's 15 misses are trips numbered 4xxx on long lines
(670700, 670720, 670730, 690405) whose keys exist but are not scheduled at that time; check them
on the capture. Merely checking that a key exists is not enough here: it hides MPVNET-Q3.

## Replay

The 40-second capture (2026-10-11 17:33, release `20261006T194555Z`, jdf only):

- 443 of 462 running bus key groups bound (95.9 %); unbound: 54 observations `not_active`
  (mostly the IDOL 4xxx trips above), 3 `no_line`;
- the GTFS-RT consistency check is clean (413 trips, 9,026 stop time updates);
- bus fixes are 15–21 s old at the median and at most about 2 minutes. Train times are the time
  of the last event, often 15–20 minutes old, so most train positions fall outside the skew and
  are dropped (MPVNET-Q8).

The first day of capture (2026-10-11 16:09 UTC to 2026-10-12 12:51 UTC, about 4,930 polls per
portal, over midnight and the Monday start, same release, jdf only, decoder version 1):

- 20,685 of 20,941 running bus key groups bound (98.8 %); unbound observations: 52,230
  `not_active`, 1,703 `no_line`, 1,176 `no_trip`, 467 `not_in_service`, against 2.80 M bound;
- the GTFS-RT consistency check is clean (20,584 trips, 3.15 M stop time updates);
- 6–10 timeouts per portal, no unreadable body, 4 gaps of at most 157 s.

The bus fields against the timetable over the same day (2.86 M regional bus records; the stop
names in fields 3 and 5 match the trip's calls for 96 % and 94 %):

- field 7 equals the scheduled departure at the stop in field 5 to the minute in 94.3 %, and the
  scheduled times at the stop in 3 in about 3 %;
- within one stretch between stops field 4 does not change in 80.6 % of consecutive readings, and
  grows by one second per second while the bus stands at the stop in 3; it equals field 6 when
  the bus reaches a stop;
- field 6 changes in 77 % of consecutive readings, falling while a late bus catches up;
- `ds` equals field 6 in 66 % and field 4 in 62 % (MPVNET-Q9).

## Quirks

| ID | Quirk | Handling |
|---|---|---|
| MPVNET-Q1 | the bounding box hardly filters: each portal returns its whole fleet, including interregional lines after they leave the region (a whole-country box gave 122 vehicles against 119 for the IDOL view) | one request per portal with a fixed whole-country box |
| MPVNET-Q2 | the trip is a pipe-separated string with different layouts for buses (22 fields) and trains (12 fields); some entries have `cn: null` (`cat: 0`) | the connector splits by kind and checks the field count; a vehicle without `cn` has no trip |
| MPVNET-Q3 | city buses (DPO Ostrava, DSZO Zlín; `cat: 3`) carry the bare public line (`8`), a **course number** in the trip field, and a fleet number instead of a plate. The lines are fixed CIS prefixes plus the 3-digit line: DPO `915` (116 of 116 keys exist), DSZO `825`, and `826` for the Otrokovice lines 55, 56, 57 and 93. But no key named a trip running at that time, and one number serves several lines at once (DPO `47` on lines 39, 46, 55 and 56; DSZO `37` on 31, 32 and 33): it is the vehicle's course (*pořadí*), not the *spoj* | out of scope (decided 2026-10-11): neither operator publishes its courses, and the regional buses are the coverage that matters. The connector drops `cat: 3` entries |
| MPVNET-Q4 | `dt` and `tm` are local times without an offset | `times.resolve_local` against `received_at`, as DPMD-Q1 |
| MPVNET-Q5 | `x` is the latitude and `y` the longitude | swapped in the connector |
| MPVNET-Q6 | the delay is signed seconds, but `ds`'s sign letter says `p` for both 0 and early | the number is used, never the letter |
| MPVNET-Q7 | a request without `origin` gets HTTP 400 | the manifest sends it |
| MPVNET-Q8 | a train's `dt`/`tm` is the time of its last event, not of the position (ČD in whole minutes); trains have no unit identity | the train number is its vehicle key; positions older than the skew are dropped; trains stay off live until it is known whether their positions are GPS or derived (from SŽ, which would duplicate `sz-mapa`) |
| MPVNET-Q9 | a bus's `ds` lags its current delay: it often still shows field 4, the delay at the last stop, while field 6 has moved on (bus 5L53480 on `670951:3`, 2026-10-12 06:03–06:04 UTC: field 6 191 → 182 → 169 s, `ds` 174 s throughout) | the bus delay is field 6; `ds` is used for trains only, where it equals field 4 |

## Open questions

- Field 4 is an observed departure delay at a named stop. The core has no stop-event fact from
  a vehicle source yet; use it when it does.
- How the source behaves around DST.
- Terms of use.
