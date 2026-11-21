# Source dossier: IDSOK vehicle map (`idsok`)

Status: **probed on 2026-11-21** (Saturday, about 21:15 local): a few requests, no recording.
Live on both feeds from the start (no record-first phase). Decoder: the manifest's declarative
`[channel.decode]` (version 1). Terms of use are still to be recorded.

IDSOK is the integrated system of Olomoucký kraj. Its public map at `https://cestujok.cz`
shows regional buses, Olomouc city trams and buses (DPMO, CIS lines `895xxx`) and trains. The
software and the record are the same as IREDO's (`docs/sources/iredo.md`); only the envelope
differs.

## Endpoint

```text
GET https://cestujok.cz/idspublicservices/api/service/position
```

- No cookies, no key, no parameters.
- The response is about 40 KB of JSON for 87 vehicles.
- `jrunify-ext-geodata/SOURCE_AUDIT.md` notes that CestujOK reset the TLS handshake for
  requests from a host outside the Czech Republic. If the server sees the same, the channel
  backs off; the fix would be egress, not decoding.

## Response

A bare JSON array (IDSOK-Q1):

```json
[{"id": "S-895002-359", "sourceType": "S", "vehicleType": "E", "lineType": "A",
  "lineNumber": 895002, "serviceNumber": 359, "name": "Tram 2",
  "desc": "Neředín, krem. - Palackého - Nám. Republiky - Hlavní nádraží - Fibichova",
  "dep": "Olomouc, Neředín, krematorium", "depTime": "21:08", "dest": "Olomouc, Fibichova",
  "destTime": "21:25", "lat": 49.59543, "lon": 17.24281, "angle": 70, "time": "21:14:45",
  "delay": 2, "operator": "Dopravní podnik města Olomouce, a.s."},
 {"id": "K-13861", "sourceType": "K", "vehicleType": "V", "trainNumber": "13861",
  "name": "M42 (Os 13861)", "dep": "Červenka", "depTime": "21:13", "dest": "Litovel předm.",
  "destTime": "21:22", "lat": 49.726818, "lon": 17.080293, "time": "21:15:00", "delay": 0,
  "operator": "České dráhy, a.s."}]
```

The fields are IREDO's, plus `serviceNumber` (the trip number, also in `id`). Observed:
`sourceType` `S` 65 (buses `A` 48, trams `E` 17), `K` 22; operators DPMO 33, ČD 22, Arriva 19,
Vojtila Trans 9, FTL 4. `delay` (minutes) was absent for 9 of 87. There is no vehicle ID
(IDSOK-Q3).

## Keys

- Buses and trams: `cis:line_trip` `<lineNumber:06d>:<trip>` on `jdf`, the trip read from `id`
  as for IREDO.
- Trains: `czptt:train_number` `<trainNumber>` on `czptt`.

Not checked against a release yet. A 3-minute recording (2026-11-21 21:03 UTC, 13 polls, all
decoded) gave 490 bus and tram and 192 train observations; bus fixes were 22 s old at the median
(2–384 s).

## Quirks

| ID | Quirk | Handling |
|---|---|---|
| IDSOK-Q1 | the payload is a bare array, not an object | `root = "array"`, `entities = ""` (the document itself) |
| IDSOK-Q2 | the trip number is in `serviceNumber` and in `id` | read from `id`, as IREDO-Q2, so both manifests decode alike |
| IDSOK-Q3 | no vehicle identity at all: `id` names the trip | the vehicle key is `trip:<id>`; no mode is physical |
| IDSOK-Q4 | `time` is a local time of day without a date; trains in whole minutes | `hhmmss`, nearest reading to the reception (as IREDO-Q4); trains rank above `sz-mapa` (as IREDO-Q5) |

## Open questions

- Terms of use.
- Whether the server's location triggers the TLS resets.
