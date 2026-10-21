# Oběhy architecture

The diagrams and the cross-cutting contracts of the application. `BASE_PLAN.md` holds the
reasoning and the detail of each part; this document is the map. Section references (§) are to
`BASE_PLAN.md`.

## System overview

```mermaid
flowchart LR
  subgraph GHA["GitHub Actions — daily build"]
    direction TB
    srcS["static sources<br/>CIS JŘ · CZPTT · PID · IDS JMK · OSM"] --> build["obehy build"]
    build --> jrutil["JrUtil"]
    jrutil --> relpub[("GitHub Release<br/>jdf · czptt · jdf-filtered<br/>PMTiles · stop-registry")]
  end
  subgraph UP["Upstream"]
    rtapi["realtime APIs<br/>polled and on demand"]
    refsrc["reference data<br/>registers · models · rosters · images"]
  end
  subgraph SRV["Application server"]
    direction TB
    rel["obehy release<br/>fetch · load · activate"]
    refimp["obehy ref import"]
    worker["obehy realtime<br/>connectors + core"]
    jobs["obehy jobs<br/>nightly SQL"]
    api["obehy api<br/>/api/v1"]
    files[["files<br/>gtfs · gtfs-rt · vehicles.json · tiles"]]
    raw[("raw archive")]
    pg[("PostgreSQL<br/>control · static · ref · rt · history")]
  end
  web["web<br/>React + MapLibre"]
  ext["GTFS-RT consumers"]

  relpub --> rel --> pg
  rel --> files
  refsrc --> refimp --> pg
  rtapi <--> worker
  worker --> raw
  pg -->|indexes · NOTIFY| worker
  worker -->|state · events · history| pg
  worker --> files
  api -->|lookup requests| pg
  jobs <--> pg
  pg --> api
  api --> web
  files --> web
  files --> ext
```

| Process | Role |
|---|---|
| `obehy build` (GitHub Actions) | the only static compiler, through JrUtil (§11, §15) |
| `obehy release` | fetch, verify, load and activate releases into `static.*` (§16) |
| `obehy ref import` | load curated reference datasets into `ref.*` |
| `obehy realtime` | the one process that talks to realtime upstreams; connectors plus the pure core (§18) |
| `obehy jobs` | nightly set-wise SQL: `vehicle_day`, circulation edges, punctuality, retention |
| `obehy api` | FastAPI `/api/v1`; never calls upstream |

PostgreSQL schemas:

- `control`: releases, loads, publications.
- `static`: the serving relations per load, read through `active.*`.
- `ref`: curated reference datasets, a mirror of reviewed files.
- `rt`: observations, assignments, current state, lookups.
- `history`: journeys, actual events, vehicle days, the circulation model.

## Two kinds of input

| | Connector (`realtime/sources/`) | Importer (`reference/importers/`) |
|---|---|---|
| what | live data: positions, delays, events, assignments, compositions | reference datasets: vehicle registers, models, bindings, rosters, media |
| when | polled channels, or lazy lookups on demand | when the reviewed files change |
| output | `Observation`s into the core; raw bytes archived | a versioned snapshot loaded set-wise into `ref.*` |
| replay | from the raw archive | history names the dataset version it used |

Curated data is authored as **git-reviewed files** (like the stop registry) and imported; `ref.*`
is a mirror, never a source of truth. Public datasets live in `jrunify-ext-geodata/vehicles/`.
Private ones (such as the DÚK vehicle register) use the same format from a local path in
`obehy.local.toml` and are never committed. The app accepts no human input.

## Connector contract

A connector is `realtime/sources/<source>.py` plus `<source>.toml`. **It declares how it
fetches; the generic connector runtime (`realtime/runtime/`) executes that.** No connector
implements scheduling, rate limits, budgets or caching itself.

```text
[[channel]]            polled
  name, poll = interval | service_hours | adaptive, backoff, timeout
  feed routing, capabilities = [...], semantics = {...}   (time, delay, namespaces)

[[lookup]]             lazy: details fetched only when needed
  kind = "composition" (later: "vehicle_detail", "trip_detail", ...)
  request (URL with {placeholders} the plan fills), ttl_s, not_found_ttl_s, retry_s,
  rate = { per_minute, burst } (token bucket), daily_budget
```

Both are written as the same pure steps around one I/O step:

```text
channel:  fetch() → bytes → decode(bytes) → Observations

lookup:   plan(subject, view) → UpstreamRequest | not_available     pure
          fetch(request) → bytes                                     I/O, under budget
          decode(bytes, request) → the detail (a composition's units) pure
```

- **`subject`** is what the caller asks about: a journey, optionally from a call onward, or a
  vehicle.
- **`view`** is read-only context the worker already holds:
  - the journey's schedule snapshot;
  - live state (progress, next call, bound vehicle);
  - identifier crosswalks (`source_key`/`call_key` namespaces such as SR70, and `ref` mappings
    to the upstream's own station or vehicle IDs).

  This is how a lookup can ask, for example, for "the composition from the SR70 of the next
  passenger call".
- **The cache key is the connector's `request.cache_key`**, so different questions that need
  the same upstream call share one fetch.
- **Responses are archived like polls** (`<source>/<lookup>` in the raw archive) and logged
  with their cache key in `rt.lookup_fetch`: the recorded pairs. A detail is not core state, so
  nothing replays it through the core; an archived answer can be decoded again.
- **The API never calls upstream.** It reads the journey's compositions and the last answer to
  its request; with no answer or an older one it upserts `rt.lookup_request`, NOTIFYs
  `obehy_lookup` and answers `pending` (202, `retry_after`) or `stale`. That request row is the
  only thing the API writes, on a small pool of its own. The lookup broker
  (`realtime/runtime/lookups.py`, its own connection and thread in the worker):
  - reads the view in SQL (`realtime/sql/lookup_view.sql`: the trip's calls with their SR70,
    which the train has left, its keys);
  - reuses `rt.detail_cache` by cache key, so requests planned to one key share one fetch;
  - enforces the token bucket (a request without a token waits for a later pass) and the daily
    budget (spent: failed);
  - prefetch rules: none yet.

Examples:
- DÚK: a 15 s channel.
- SŽ: a 30 s channel.
- 55p.cz-style vehicle assignments (future, only with permission): a 30–60 min channel.
- ČD compositions: a lookup only, keyed by train, date and station.
- A minimal source: a 60 s channel with line, trip and delay.

## Capabilities route facts

A connector declares per channel which capabilities it supplies. Each fact goes to the component
that consumes it; a missing capability means that component never hears from the source.

| Capability | Consumed by | Without it |
|---|---|---|
| trip identity (`TripKey`, `LineRef`, …) | infer | observation unmatched |
| `vehicle_key` | vehicle resolve, binding continuity, tours | anonymous evidence: no vehicle, tour or features |
| position | progress, own GPS delay | no VehiclePosition; delay only from the source |
| progress, stop events | timeline | — |
| delay | timeline, under declared semantics | — |
| prediction | timeline candidates | own propagation |
| vehicle attributes, media | `rt.vehicle_attribute_observed` | registry or model defaults only |
| composition | `rt.composition`, segments per journey | none |
| vehicle_assignment | `rt.vehicle_assignment` → `vehicle_day` | tours only from `vehicle_key` bindings |
| platform | platform evidence | static boarding point |
| alert | alert mapper | — |

A "line + trip + delay minutes" source is `TripKey` + `Delay(unknown reference)`. It gives one
wide timeline constraint and a TripUpdate only, with low confidence. It needs no special case.

## Realtime worker

```mermaid
flowchart TB
  subgraph CONN["sources/ — each connector declares how it fetches"]
    direction LR
    duk["duk<br/>poll 15 s"]
    sz["sz-mapa<br/>poll 30 s"]
    arr["arriva-express<br/>poll 30 s"]
    mini["minimal source<br/>poll 60 s"]
    p55["55p (future)<br/>poll 30–60 min"]
    cd["cd<br/>lookup: composition"]
  end
  subgraph RUN["connector runtime — generic"]
    sched["poll scheduler<br/>interval · hours · backoff"]
    broker["lookup broker<br/>cache · coalescing · budget · prefetch"]
  end
  api["obehy api"] -->|"rt.lookup_request"| broker
  broker -->|"units"| comp[("rt.composition<br/>rt.detail_cache")]
  sched -->|"fetch · decode"| CONN
  broker -->|"plan · fetch · decode"| CONN
  ctx -.->|"view: schedule · live state · crosswalks"| broker
  CONN -->|raw bytes| raw[("raw archive")]
  CONN -->|Observations| vres

  subgraph CORE["core.step — pure, partitioned by feed"]
    direction TB
    vres["vehicle resolve"] --> inf["infer<br/>keyed · keyless"]
    inf --> split{"evidence by capability"}
    split -->|"position · events · delay · prediction"| tl["timeline"]
    tl --> arb["arbitration"]
    inf --> vst["vehicle state"]
    tl --> vst
  end
  ctx[("release index · vehicle index<br/>policy · clock")] --> CORE

  split -->|attributes| attr[("rt.vehicle_attribute_observed")]
  split -->|vehicle_assignment| vas[("rt.vehicle_assignment")]
  split -->|platform| arb
  arb --> emit["emit tick<br/>GTFS-RT · trip state · history"]
  vst --> vjson["vehicles.json"]
```

- **The core is partitioned by feed.** No in-memory state crosses `jdf` and `czptt`, and each
  observation is routed to one feed. DÚK already emits into both: buses go to `jdf`, trains to
  `czptt`. A vehicle seen in both feeds has two states, joined only in SQL. Sharding by feed
  later needs no redesign.
- **The observation envelope.** `rt.observation` stores `raw_ref`, `decoder_version`,
  `fact_schema_version` and the decoded facts as JSONB, so observations can always be re-decoded
  from raw inside the archive window.
- Live operation and replay run the same `core.step` (§18.1).

## Lazy detail, end to end

```mermaid
sequenceDiagram
  participant W as web
  participant A as api
  participant P as PostgreSQL
  participant B as lookup broker
  participant C as cd connector
  participant U as upstream
  W->>A: GET trip composition from station S
  A->>P: compositions of the journey · last answer to the request
  alt answered lately
    P-->>A: units, segments
    A-->>W: 200 fresh (or not_available)
  else not asked, or answered long ago
    A->>P: upsert lookup_request + NOTIFY
    A-->>W: 202 pending, retry_after (200 stale with an older answer)
    P-->>B: NOTIFY
    B->>P: view (lookup_view.sql)
    B->>C: plan(subject, view)
    Note over B,C: view = the trip's calls · which it has left ·<br/>identifier crosswalks (SR70, train number)
    C-->>B: UpstreamRequest (cache_key) or not_available
    B->>B: detail_cache by cache_key · token bucket · budget
    B->>C: fetch(request)
    C->>U: HTTP
    U-->>C: payload
    B->>C: decode(bytes, request)
    C-->>B: units (from S onward)
    B->>P: raw archived · lookup_fetch · rt.composition (merged) · detail_cache · answered
    W->>A: retry
    A-->>W: 200 fresh
  end
```

## Vehicles, compositions and tours

```mermaid
erDiagram
  OPERATOR ||--o{ VEHICLE : operates
  VEHICLE_MODEL ||--o{ VEHICLE : "is of"
  VEHICLE_MODEL ||--o{ MODEL_ATTRIBUTE : defaults
  VEHICLE ||--o{ VEHICLE_ATTRIBUTE : curated
  VEHICLE ||--o{ VEHICLE_ATTRIBUTE_OBSERVED : live
  VEHICLE ||--o{ VEHICLE_SOURCE_BINDING : "known as"
  VEHICLE ||--o{ VEHICLE_MEDIA : pictured
  VEHICLE_MODEL ||--o{ VEHICLE_MEDIA : pictured
  REF_DATASET ||--o{ VEHICLE_ATTRIBUTE : versions
  REF_DATASET ||--o{ ROSTER : versions
  JOURNEY ||--o{ COMPOSITION : segments
  COMPOSITION ||--|{ COMPOSITION_UNIT : contains
  COMPOSITION_UNIT }o--o| VEHICLE : exact
  COMPOSITION_UNIT }o--o| VEHICLE_MODEL : type
  JOURNEY ||--o{ VEHICLE_ASSIGNMENT : "worked by"
  VEHICLE ||--o{ VEHICLE_ASSIGNMENT : works
  VEHICLE ||--o{ VEHICLE_DAY : works
  VEHICLE_DAY ||--|{ VEHICLE_DAY_STEP : chains
  VEHICLE_DAY_STEP }o--|| JOURNEY : is
  JOURNEY ||--o{ JOURNEY_LINK : "continues as · splits · joins"
  ROSTER ||--|{ ROSTER_STEP : orders
  ROSTER_STEP }o--o| VEHICLE_MODEL : "planned type"
  ROSTER_STEP }o--|| TRIP_KEY : "train number"
  CIRCULATION_EDGE }o--|| TRIP_KEY : prev
  CIRCULATION_EDGE }o--|| TRIP_KEY : next
  JOURNEY }o--|| TRIP_KEY : "key + date"
```

- **A vehicle can be identified at three levels.**
  - An exact vehicle: a source report bound to a `vehicle_id` by a registry key (its
    source's vehicle ID, else a plate, EVN or series number it states), or confirmed
    co-located with a report of another source on the same journey.
  - A vehicle type: `vehicle_model_id`.
  - A category only.
- **The registry** (`reference/`, `ref.*`): curated datasets imported by `obehy ref import`,
  ranked by priority. Oběhy's own list (priority 0) over seeds such as the private DÚK
  register (priority 10).
  - `vehicle_id` is opaque, minted in the curated files, and never recycled. Keys carry
    validity ranges, because fleet numbers are reused and plates change.
  - A report without a key or partner is its own `{source}:{source_vehicle_id}`, and
    re-attaches once a key is curated. Pairings never write keys: they are review candidates
    (`rt.vehicle_pairing`).
- **Attributes.** A closed vocabulary (`data/vehicles/attributes.toml`), mapped by each
  connector. Per attribute: curated > live (by source order) > model default, disagreements
  flagged and every statement kept. Media rows carry licence and attribution.
- **The worker resolves, the API reads.** The emitter's `VehicleBook` (`realtime/emit/
  vehicles.py`, `pairing.py`) decides each report's vehicle and each vehicle's profile when an
  input changes (a live value, a pairing, `NOTIFY obehy_registry` after an import) and writes
  `rt.vehicle_state_current.vehicle_id`/`bound_by`, `rt.vehicle_profile` and
  `rt.vehicle_pairing`.
- **Compositions** are units with their own spans within a journey, from `(location, visit)`
  to `(location, visit)` or open; segments are derived where spans start and end.
  - Kept per journey key (`rt.composition`, `rt.composition_unit`): the trip's CZPTT TR key,
    which the parts of one train can share, so each trip shows the spans on its own calls.
    `rt.composition_spans` resolves spans to a trip's calls; the API derives segments from them
    in SQL.
  - An answer "from station S onward" stays open until a later answer closes it; adjacent
    identical answers merge.
  - A unit names an optional vehicle (bound through the registry's EVN or series-number keys)
    and/or an optional type, so a type-only composition still yields features. Two sources on
    one segment will combine per unit: exact vehicles from one, coach detail from ČD (one
    source so far: units are listed per source).
- **`vehicle_assignment`** (vehicle → journey) is separate from composition. It comes from
  bound live reports, or from sources naming the vehicle of each train, and feeds
  `vehicle_day`, the observed tours.
- **Alerts** (`realtime/alerts.py`, `emit/alerts.py`) are not core state: alert channels write
  whole sets to `rt.alert` on their own connection, SQL resolves their entities per feed
  against the active release (`rt.alert_target`), and the emit tick appends them to each
  feed's GTFS-RT. An alert stating a trip's fate reaches the core as a fact, never as an alert.
- **Tours come from four sources:**
  - static `block_key`;
  - imported rosters, by train number + validity;
  - learned circulation edges (§22);
  - observed `vehicle_day`.

  Forecast precedence is block > roster > learned edge. Observed actuals always win for
  history.

## Trip lifecycle

```mermaid
stateDiagram-v2
  [*] --> forecast: block, roster or learned edge
  [*] --> pre_trip: key seen before start
  forecast --> pre_trip: bound by own key
  pre_trip --> running: departs first call
  running --> off_route: outside tolerance longer than T
  off_route --> running: rejoins path
  running --> finished: arrives at last call
  finished --> [*]: vehicle to layover
```

Details: §20.7.

## Identity and public contracts

These are fixed now because changing them later breaks history or consumers.

- **A journey is `(feed, key namespace, key, service date)`** (§29).
  - For road transport the key is `cis:line_trip`.
  - **For rail it is the train number + service date.** A number change en route gives two
    journeys joined by a `journey_link` (`continues_as`); splits and joins are links too.
  - The CZPTT run and the TR are attributes.
  - Physical continuity comes from vehicle assignments and compositions, never from merging
    journeys.
- **The API refers to trips by the feed's `trip_id` and service date**
  (`/api/v1/trips/{trip_id}/{service_date}`) and to vehicles by `vehicle_id`
  (`/api/v1/vehicles/{vehicle_id}`). Source keys only redirect (`/api/v1/trips/by-key/…`); a
  trip ID of an older timetable version answers 410 with its journey key. History stays keyed
  by journey.
- **The public realtime model is the same for every call.** A call has an `estimate`, a
  `status` (`scheduled`, `predicted`, `actual`, `inferred`, `no_realtime` or `cancelled`) and a
  `source_class`. Intervals, confidence and provenance are in the debug API only. Public
  fields are only ever added.

## Module map

```text
src/obehy/
├── cli.py, pipeline/, national_*.py, ...   static build (GitHub Actions)
├── release/                                release mirror
├── reference/          importers/, load.py (versioned snapshot → ref.*)
├── realtime/
│   ├── sources/        connectors: <source>.py + <source>.toml
│   ├── runtime/        poll scheduler, lookup broker
│   ├── model.py, times.py, clock.py, core.py
│   ├── index/, infer/, timeline/, vehicles.py
│   ├── emit/, worker.py, record.py, archive.py, replay.py, evaluate.py
│   └── sql/            nightly set-wise jobs
├── api/                FastAPI /api/v1
└── data/               policies, connector manifests, vehicles/attributes.toml
web/                    frontend (later)
```
