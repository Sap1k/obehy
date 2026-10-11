# Source dossier: MPV net regional vehicle maps (`mpvnet`)

Status: **probed on 2026-10-11** (Sunday, about 17:30 local): a few requests per portal, and
a 40-second recording of all four channels. Keys checked against release
`20261006T194555Z-06c0829d89aa` (serving schema 5.0). Recorded from 2026-10-11 on (`obehy
realtime` archives every manifest channel). No connector or replay yet. Terms of use are still
to be recorded.

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
| 0 | `001630` | CIS line (6 digits); a bare line number for city buses (MPVNET-Q3) |
| 1 | `630` | public line |
| 2 | `4106` | CIS trip number (*spoj*) |
| 3 | `Nové Město p.Smrkem,Ludvíkov,Jednota` | the last stop passed (empty before the first one) |
| 4 | `114` | delay in seconds, signed (hypothesis: at the stop in 3) |
| 5 | `Nové Město p.Smrkem,Přebytek` | the next stop (hypothesis) |
| 6 | `137` | delay in seconds, signed (hypothesis: current or at the stop in 5); `ds` equals 4 or 6 |
| 7 | `1046` | a minute of the day (`17:26`); which scheduled time is open |
| 8, 9 | `Świeradów-Zdrój,…`, `Liberec,aut.nádr.` | first and last stop of the trip |
| 10 | `0` / `1` | unknown flag |
| 11 | `6SJ4698`, `Ev. č. 226` | licence plate, or fleet number for city buses |
| 12–21 | `0` | flags, almost always 0 |

`cn` for a train (12 fields): `Sp 1815 | V41 | 1815 | <last station> | <delay s> | | | | |
<destination> | ±1 | `. Field 2 is the train number. Field 4 is the delay in seconds, always
equal to `ds` and to 60 × `dm`. The ±1 field is unknown; it was `1` for ČD and Länderbahn and
`-1` for Arriva vlaky.

## Keys against the release

At 17:30 on 2026-10-11, against release `20261006T194555Z`:

| Portal | Regional buses → `cis:line_trip` `<cn0>:<cn2>` | City buses | Trains → `czptt:train_number` |
|---|---|---|---|
| IDOL | 87 / 87 | none | 47 / 47 |
| JIKORD | 122 / 122 | none | 84 / 84 |
| ODIS | 161 / 161 | 110 DPO, unkeyed (MPVNET-Q3) | 61 / 62 |
| Zlín | 80 / 80 | 23 DSZO, unkeyed (MPVNET-Q3) | 43 / 43 |

Regional buses and trains carry literal keys, as DÚK does. Matching is by key, with no
inference.

## Quirks

| ID | Quirk | Handling |
|---|---|---|
| MPVNET-Q1 | the bounding box hardly filters: each portal returns its whole fleet, including interregional lines after they leave the region (a whole-country box gave 122 vehicles against 119 for the IDOL view) | one request per portal with a fixed whole-country box |
| MPVNET-Q2 | the trip is a pipe-separated string with different layouts for buses (22 fields) and trains (12 fields); some entries have `cn: null` (`cat: 0`) | the connector splits by kind and checks the field count; a vehicle without `cn` has no trip |
| MPVNET-Q3 | city buses (DPO Ostrava, DSZO Zlín; `cat: 3`) carry the bare public line (`8`) instead of a CIS line, and a fleet number instead of a plate | open: their lines in the release and the key rule are still to be checked |
| MPVNET-Q4 | `dt` and `tm` are local times without an offset | `times.resolve_local` against `received_at`, as DPMD-Q1 |
| MPVNET-Q5 | `x` is the latitude and `y` the longitude | swapped in the connector |
| MPVNET-Q6 | the delay is signed seconds, but `ds`'s sign letter says `p` for both 0 and early | the number is used, never the letter |
| MPVNET-Q7 | a request without `origin` gets HTTP 400 | the manifest sends it |

## Open questions

- Which delays and times fields 4, 6 and 7 of a bus are. Settle this on the 25-hour capture,
  against the trips' scheduled times.
- How the source behaves around midnight, DST and service starts.
- Whether a city bus's line and trip can be keyed against the release (MPVNET-Q3).
- Terms of use.
