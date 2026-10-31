# Source dossier: ČD train compositions (`cd`)

Status: **asked live on 2026-10-31** through the proxy pool (`proxied = true`, like SŽ):
nine trains through the real flow (API request, broker, API answer), each answer in
60–400 ms. One answer captured by hand on 2026-10-20 (train 607 from Cheb,
`tests/realtime/compositions/cd-607.json`); the live ones kept as fixtures are rj 577
(`cd-577.json`: through coaches, a railjet), train 860 (`cd-860.json`: an InterPanter) and
EN 443 (`cd-443-empty.json`: no coaches). Terms of use: none published for this service; it
is the backend of cd.cz's composition (řazení) pages. Asked only on request, within a token
bucket and a daily budget, never from the server's own address.

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
| `DestSt`, `DirectCoach` | a through coach's own destination; where it leaves and its next train (CD-Q5) | `destination`; `direct_coach_leaves_at`, `direct_coach_train` |
| `NoDetail`, `Notes[]` | no detail (the locomotive); notes | `notes` |

Service icons onto the vehicle vocabulary (`data/vehicles/attributes.toml`):

| Icon | ČD's text | Feature |
|---|---|---|
| `1`, `2`, `12` | Vůz 1./2. třídy, 1. a 2. třídy | `classes` |
| `wifi2` | WiFi - ve voze je plánováno bezdrátové připojení k internetu | `wifi` |
| `zas` | Vůz se zásuvkami 230V | `power_sockets` |
| `neginv` | Vůz vhodný pro přepravu cestujících na vozíku, vybavený zvedací plošinou | `wheelchair_lift` |
| `povkolo` | Rozšířená přeprava spoluzavazadel s povinnou rezervací místa pro jízdní kolo | `bicycle_reservation_required` |
| `vp` | Velkoprostorový vůz nebo velkoprostorový oddíl | `open_saloon` |
| `D` | Vůz nebo oddíly, vyhrazené pro cestující s dětmi do 10 let | `family_compartment` |
| `kino` | Dětské kino | `children_cinema` |
| `inv` | Vůz vhodný pro přepravu cestujících na vozíku | `wheelchair_accessible` |
| `mozkolo` | Rozšířená přeprava spoluzavazadel s možností rezervace místa pro jízdní kolo | `bicycle_reservation_possible` |
| `mozzav` | Úschova během přepravy s možností rezervace místa pro jízdní kolo | `luggage_storage` |
| `leh`, `luz` | Lehátkový vůz, Lůžkový vůz | `couchette`, `sleeper` |
| `rest`, `kal` | Restaurační vůz; Občerstvení | `restaurant`, `refreshments` |
| `tichy` | Tichý vůz nebo oddíl | `quiet_zone` |
| `business` | Business oddíl (povinná rezervace míst) | `business_compartment` |
| `damy` | Dámský oddíl | `ladies_compartment` |

Any other icon is kept as `raw:cd:<code>` with ČD's text, which the API shows.

## Matching

- **Which trains.** ČD's: the trip's CZPTT TR key names company `1154`
  (`Tr:1154:KASO---22301:00:2026`), on service dates within a day of today. The manifest
  declares it (`applies`), so the broker refuses other trains before `plan` and the API's trip
  says `composition: false` for them: the web does not ask.
- **Train.** `TrainNum` is the trip's `czptt:train_number` key.
- **Station.** The asked call, else the next passenger call the train has not left (the origin
  before it runs; never the terminus), by its `sr70` key (CD-Q1).
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
| CD-Q5 | `DirectCoach` is an object: `DestSt`, `OdpSt` (where the coach leaves this train) and `NaslVl` (the train it goes on in: `EN 477 Metropol`) | `direct_coach_leaves_at`, `direct_coach_train` |
| CD-Q7 | Kinds and cabs are only in the pictogram's name and the type: `hv_…` is a locomotive; a series as the type (`661`, `814.2`, `014`) a multiple unit's car; a side token of its own gives its cab (`814_l_…` front, `914_R_…` rear, `014_M_…` none); a control car's `RV_…` and a Panter's `661l`/`650L` say only that there is a cab | `kind` `unit`; `cab` = `front`, `rear` or `true` (side from where the unit ends) in details |
| CD-Q8 | The order is not always front first: R 619 asked from Cheb lists the locomotive first, asked from Praha-Holešovice the same coaches reversed with the locomotive last. Pictogram names come in either case (`hv_el`, `HV_EL`: ČD lowercases only the first entry) | kinds matched case-insensitively; the web shows the order as given and merges segments whose vehicles differ only in order |
| CD-Q6 | A train ČD has no composition for answers its train with `Coachs: null` (EN 443 after it left Praha, Os 19977) | not found |

Answered on 2026-10-31:

- **Train order is mostly front first**: the locomotive is listed first, and rj 577's through
  coaches to Budapest sit between it and the railjet set, which only works with the locomotive
  leading. Not always: R 619 from Praha-Holešovice comes reversed (CD-Q8).
- **A multiple unit is one entry per car**: an InterPanter is five entries (`661`, `662`,
  `064`, `662`, `660`), each with its own classes and services, and no locomotive.
- **Foreign coaches and through coaches are answered**: EC 113's coach to Kraków, rj 577's
  couchette and sleeper to Budapest (CD-Q5).
- **Not every ČD train has one**: Os 19977 (S6) and EN 443 asked after it left Praha answered
  no coaches (CD-Q6).
- `IsMimo`/`ExistsMimo` (true for R 743 and train 957) look like a changed, extraordinary
  formation; not kept yet.

## Open questions

- Which `Date` ČD expects for a train past midnight: its service date (sent) or the date at the
  asked station. EN 443 (21:15–03:51) found nothing from its second station on its service
  date, which does not settle it.
- How often a formation changes after departure, to set `ttl_s` from evidence (now 15 min).
