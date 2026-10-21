# Source dossier: ČD train compositions (`cd`)

Status: **one answer captured by hand on 2026-10-20** (train 607 from Cheb, sent from the
browser with the request below; `tests/realtime/compositions/cd-607.json`). The connector
plans and decodes; it has not asked ČD itself yet. Terms of use: none published for this
service; it is the backend of cd.cz's composition (řazení) pages. Asked only on request, within
a token bucket and a daily budget, and not before the egress for it is confirmed.

## Endpoint

```text
GET https://razeniws.cdis.cz/apiws.svc/getTrains
    ?trainParams=[{"TrainNum":"607","SR70":"5475035","Date":"20.10.2026","GetPlan":"false"}]
    &lang=cs&client=TrainDetail&format=json
Referer: https://www.cd.cz/
```

- **One train per request** (the parameter is a list; one item is sent). `Date` is
  `dd.MM.yyyy`, the service date. `GetPlan=false`: the composition, not the planned one.
- **The answer holds from the asked station onward**: the coaches the train runs with from
  `SR70`. Asked from a later station after coaches are detached, it answers fewer.
- cd.cz's own page sends a `callback` (JSONP) and a cache buster; without them the answer is the
  same JSON in parentheses (CD-Q2).
- About 7 KB for an eight-coach train.

## Payload

`d.Trains[0].Coachs[]`, in train order, and `d.SvcDescEnum[]`, the texts of the service icons
used.

| Field | Content | Kept as |
|---|---|---|
| `Img`, `ImgAlt` | the coach's pictogram (`hv_el.svg`: a locomotive) and its Czech description | kind; `label` |
| `Type` | the coach type (`Bdmpz 896`), or `#3937` for a locomotive (CD-Q3) | `vehicle_type` |
| `Num`, `NumStar` | the coach number in the train (`367`), and a star (CD-Q4) | `coach_number`; `number_star` |
| `Services[]` | icon codes (`2`, `wifi2`, `zas`, `neginv`…), optionally for part of the route (`StartStation`, `DestStation`) | classes, features, texts |
| `DestSt`, `DirectCoach` | a through coach's own destination | `destination`; `direct_coach` |
| `NoDetail`, `Notes[]` | no detail (the locomotive); notes | `notes` |

Service icons onto the vehicle vocabulary (`data/vehicles/attributes.toml`):

| Icon | ČD's text | Feature |
|---|---|---|
| `1`, `2` | Vůz 1./2. třídy | `classes` |
| `wifi2` | WiFi - ve voze je plánováno bezdrátové připojení k internetu | `wifi` |
| `zas` | Vůz se zásuvkami 230V | `power_sockets` |
| `neginv` | Vůz vhodný pro přepravu cestujících na vozíku, vybavený zvedací plošinou | `wheelchair_lift` |
| `povkolo` | Rozšířená přeprava spoluzavazadel s povinnou rezervací místa pro jízdní kolo | `bicycle_reservation_required` |
| `vp` | Velkoprostorový vůz nebo velkoprostorový oddíl | `open_saloon` |
| `D` | Vůz nebo oddíly, vyhrazené pro cestující s dětmi do 10 let | `family_compartment` |
| `kino` | Dětské kino | `children_cinema` |

Any other icon is kept as `raw:cd:<code>` with ČD's text, which the API shows.

## Matching

- **Which trains.** ČD's: the trip's CZPTT TR key names company `1154`
  (`Tr:1154:KASO---22301:00:2026`). Other operators' trains are not asked.
- **Train.** `TrainNum` is the trip's `czptt:train_number` key.
- **Station.** The asked call, else the next passenger call the train has not left (the origin
  before it runs; never the terminus), by its `sr70` key (CD-Q1).
- **Days.** Only trains of service dates within a day of today.
- **Journey.** Answers are kept under the trip's `czptt:tr` key and service date. The parts of
  one train can share it (607: three parts of one CZPTT path on 2026-10-21), so each part shows
  the spans on its own calls.

Checked on the dev release of 2026-10-21: 607 from Cheb plans `5475035`, the sample's
station; RegioJet's 1011 (`Tr:3246:…`) is not asked. This week 607 runs an ad hoc path
(Chomutov město – Praha hl.n., three parts of one TR key); from Chomutov město it plans
`5458219`, and the whole flow (request, broker, API units and segments) ran on it with the
captured answer standing in for ČD's.

## Capabilities

| Capability | Status |
|---|---|
| composition | type-level: coach types and numbers, classes, services; from a station onward |
| vehicle_assignment | none (no vehicle numbers) |

## Quirks

| ID | Quirk | Handling |
|---|---|---|
| CD-Q1 | A station is `54` (the UIC country code) followed by the five-digit SR70, without the check digit: Cheb (SR70 `75035`) is `5475035` | `plan` writes it from the `sr70` key |
| CD-Q2 | Without a `callback` the JSON comes in parentheses with a `;` | stripped before parsing |
| CD-Q3 | The locomotive is a placeholder: `NoDetail`, no number, `Type` `#3937` (not a series) | a unit of kind `locomotive` without type; the raw `Type` kept |
| CD-Q4 | Every coach number in the sample has `NumStar: true`; what the star means is not documented | kept as `number_star` |

## Open questions

- Which `Date` ČD expects for a train past midnight: its service date (sent) or the date at the
  asked station.
- Whether multiple units (RegioPanter, InterPanter) are one `Coachs` entry per car or per unit.
- Whether ČD answers for foreign operators' coaches in a ČD train (EC with ÖBB, PKP coaches).
- How often a formation changes after departure, to set `ttl_s` from evidence (now 15 min).
