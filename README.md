# Oběhy

A Swiss-army knife for Czech public-transport operations and realtime data.

Oběhy builds two coordinated nationwide GTFS feeds (`jdf` for road and urban transport, `czptt`
for rail) with JrUtil's Rust compiler jrutil-rs, runs those builds on GitHub Actions, and will
load each published release into a PostgreSQL mirror on the application server, which also runs
the realtime core: trip
inference from sources of very different quality, delay estimation, learned vehicle circulations
(*oběhy*), per-feed GTFS-RT and the project API. JrUtil is the only static compiler; stable
public IDs come from its identity rules and the reviewed registry in `jrunify-ext-geodata`.
PostgreSQL is never the static compiler.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the diagrams, [BASE_PLAN.md](BASE_PLAN.md) for the
architecture decisions, [STATIC_PIPELINE.md](STATIC_PIPELINE.md)
for the static boundary, and [PROGRESS.md](PROGRESS.md) for the current state and next steps.

## Development

Requirements: Python 3.13 and [uv](https://docs.astral.sh/uv/). The shared OSM
builder requires the native `osmium-tool` command. On Windows it automatically uses `osmium`
from the default WSL distribution when no native executable is on `PATH`.

```powershell
uv sync
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run pyright
```

## Machine-local configuration and shared OSM

Copy `config/obehy.example.toml` to the gitignored `config/obehy.local.toml` and set absolute
paths for the work directory, active merged OSM PBF, JrUnify-Ext-GeoData checkout (its
`gazetteer/` holds the compiler's gazetteers; `gazetteer_dir` overrides it) and either a jrutil-rs checkout (`[jrutil] directory`, built with cargo) or a compiler command.
`obehy build` and `obehy-osm` accept `--config PATH`; there is no sibling-checkout or
parent-directory fallback.

## The realtime worker binary

The realtime worker is `obehy-rt`, a Rust program in `rust/` (`docs/RUST_PORT.md`,
`rust/README.md`); Python keeps the API, releases, migrations, the jobs and the diagnostics that
read the worker's output. Build it with the stable toolchain (on Windows, linking with the
installed MSVC) and run it from the repository, where it finds the manifests and the policy:

```bash
cd rust && cargo build --release -p obehy-rt     # rust/target/release/obehy-rt
```

## Recording realtime payloads

`obehy-rt record` polls every realtime source with a connector manifest and archives their
payloads, unprocessed, for later replay. Channels are declared per connector in
`src/obehy/data/realtime/sources/<source>.toml`. Recording needs no `obehy.local.toml`:

```bash
obehy-rt record --once                      # one poll per channel, smoke test
nohup obehy-rt record --archive data/rt-raw > rt-record.log 2>&1 &
```

`--sources duk,sz-mapa` limits the sources and `--duration 2d` stops after a set time. The
recorder stops cleanly on SIGINT/SIGTERM. A restart appends to the same archive.

Proxied channels (`proxied = true` in their manifest: SŽ, and ČD's composition lookup) are
only ever sent through the one proxy pool, with no User-Agent: SŽ blocks addresses that run
services against it. Save your proxy list (Webshare's `ip:port:username:password` lines) to a
file outside git and name it in the gitignored local config, or set
`OBEHY_EGRESS_PROXY_LIST_FILE`. It is re-read every 6 hours (Webshare's download links are
regenerated, so they are not used). Without one, proxied channels are not polled; the other
sources still are.

```toml
[realtime.egress]
proxy_list_file = "D:/secrets/sz-proxies.txt"
```

## Production feed pair

The main CLI fetches the current national and regional inputs, compiles both packages in one
`jrutil build` (the PID and IDS JMK overlay, estimated posts, route shapes and the filtered road
feed included), checks them and publishes the pair atomically:

```powershell
uv run obehy build
uv run obehy build --estimated-posts
uv run obehy build --refresh-osm
```

Prepared OSM is the default. `--refresh-osm` updates it before source downloads. Every network
source (national JDF, CZPTT, PID and IDS JMK GTFS) is then fetched up front, with three attempts
each, into `workdir/runs/production/<run-id>/sources` (`fetch-log.json` records every attempt);
an unreachable source fails the run before any compilation. With `[jrutil] directory` set, the
command builds the compiler (`cargo build --release --locked -p jrutil` in that checkout), writes
the build specification to `workdir/runs/production/<run-id>/build.toml` and runs `jrutil build
--spec <file> --progress-events` (`STATIC_PIPELINE.md` lists its sections). `--jobs N` sets the
compiler's threads (`RAYON_NUM_THREADS`). The compiler writes the two consumer packages to
`artifact_root/releases/<run-id>/jdf` and `czptt` and validates them; Oběhy then checks their GTFS
stops, writes `release.json` and switches `artifact_root/current.json`.

It also writes a filtered JDF GTFS to `jdf-filtered/gtfs.zip`, following
[gtfs-processor](https://github.com/0xaa55h/gtfs-processor): the national JDF without the lines
other feeds publish, by the rules in `jrunify-ext-geodata/filtered-jdf/rules-v1.json`. Use
`--skip-filtered-jdf` to omit it. With a stop registry (`jrunify-ext-geodata/registry/stops.csv`)
the release's `stop-registry/` keeps the review candidates. Failed runs leave the previous pointer
unchanged and retain their logs, sources, diagnostics and failure report under
`workdir/runs/production/<run-id>`; a successful run removes its sources unless `--keep-work` is
given. `--progress auto` uses Rich on an interactive terminal and periodic text when redirected;
`rich`, `plain`, and `off` can be selected explicitly.

Estimated posts are off by default. `--estimated-posts` adds directed road/tram routing on the
transit-routing extract with the packaged learned scorer
(`src/obehy/data/post-inference/learned-v1.json`) unless `--post-inference-policy=FILE` names
another. Routed contexts are reused across runs from `workdir/cache/routing` (override with
`[paths] routing_cache_dir`; disable with `--no-routing-cache`); output is the same with or
without the cache.

Build the regional OSM snapshot explicitly:

```powershell
uv run obehy-osm build
uv run obehy-osm build --verify
```

The command tracks the Geofabrik MD5 sidecars for Czechia, Austria, Bavaria, Saxony, Slovakia,
Dolnośląskie, Opolskie, and Śląskie. Source extracts are cached under `workdir/osm/extracts`.
Their hashes, the Osmium identity, and the active output identity are recorded in the manifest
next to the configured `osm_file`; matching inputs and output safely skip regeneration. There
are no historical merged copies or hard-link publication. The same command uses native
`osmium tags-filter` to create the extracts the compiler reads: a node-only railway-location PBF
for CZPTT, the JDF post-candidate nodes and the JDF transit-routing base (bus-usable road ways,
tram ways, barriers and turn restrictions). No Python code parses or transforms OSM objects.
`obehy build` only validates these artifacts; it never downloads, merges, or filters OSM unless
`--refresh-osm` is given. Downloads, cache decisions, native merging/filtering, hashing, and
publication all report progress.

SŽ SR70 coordinates are authoritative for rail and OSM fills gaps. See
[NATIONAL_CZPTT.md](NATIONAL_CZPTT.md) for the CZPTT source snapshot, GVD year selection,
coordinate matching, line changes, platform handling, IDS zones and diagnostics.

## Serving database

The compiler writes `jrutil-production` packages (bundle 3, serving schema 5.0; see
`STATIC_PIPELINE.md`). They load into a PostgreSQL + PostGIS mirror (`BASE_PLAN.md` section 16):

```powershell
docker compose up -d                       # PostGIS 17 for development and DB tests
$env:OBEHY_DATABASE_URL = "postgresql://..."
uv run obehy db migrate
uv run obehy release fetch                 # newest build -> data/releases/<run-id>; prints the dir
uv run obehy release load <release-dir>
uv run obehy release activate <run-id>     # or --rollback; --if-newer skips older releases
uv run obehy release status                # with the station pairing's outcomes; `stale` when
                                           # its policy or overrides changed (import them again)
```

The database may run on another host (start `compose.yaml` there); instead of the environment
variable, set `[database] url` in `config/obehy.local.toml`. Readers query the `active.*` views
only. `release fetch` downloads the newest `build-*` GitHub release (or the given run id),
checks every asset against the digest GitHub records and every package against `release.json`
and its manifest, and keeps the two newest release directories (`--keep`). `GITHUB_TOKEN`
raises the API rate limit but is not needed. DB tests in `tests/db/` run when
`OBEHY_TEST_DATABASE_URL` or `[database] test_url` names a database that may create databases.

`deploy/obehy-release-update` runs fetch, load, then `activate --if-newer`, which leaves alone
any release no newer than one activated before (so a rollback sticks until the next build),
and points `data/releases/active` at what it activated. Activation keeps the data of the active
release only (`--keep 0` by default; about 7.5 GB in PostgreSQL; a rollback reloads one of the
fetched releases) and drops older loads and what failed loads left behind; `obehy release prune`
does the same on demand. On the server `release load --check-room` waits (until the next hourly
run) while the disk lacks room for another copy plus the realtime guard's free space.

## Running the server

CI publishes `ghcr.io/sap1k/obehy:latest` (the Python side) and
`ghcr.io/sap1k/obehy-worker:latest` (`obehy-rt worker`), with `:sha-<commit>`, from the
`Dockerfile`'s `server` and `worker` targets for every green push to main. `deploy/compose.yaml` runs the server from it: PostGIS, migrations,
an hourly release update, the realtime worker, the nightly jobs and nginx serving over plain
HTTP to your reverse proxy, which owns TLS and the hostname. `/get-feeds/` lists every feed
under a stable name (`cz-jdf-gtfs.zip`, `cz-jdf-filtered-gtfs.zip`, `cz-czptt-gtfs.zip` from
the active release, `cz-jdf-gtfs-rt.pb` live; `feeds.json` for scripts); the release update
rewrites the list (`obehy release feed-list`). Each download is capped at about 100 Mbit/s.
Copy `deploy/` to the server and next to it write `.env`:

```bash
POSTGRES_PASSWORD=...     # URL-safe characters only; it goes into the database URL
OBEHY_HTTP_PORT=8080      # where nginx listens (plain HTTP) for your reverse proxy
SYSTEM_RAM_GB=6           # the host's RAM in whole GB; PostgreSQL is sized to about half
OBEHY_SECRET_GOLEMIO=...  # Golemio API key (api.golemio.cz/api-keys); without it PID is not polled
```

```bash
docker compose up -d
```

The first release update downloads and loads a full release; the worker restarts until it is
active. The packages must be public on GitHub (or `docker login ghcr.io` on the server).

## Running the API

`obehy api serve` runs the project API (`BASE_PLAN.md` section 30) on the configured database;
it reads the active release and the worker's realtime state and never calls upstream:

```powershell
uv run obehy api serve --port 8000          # http://127.0.0.1:8000/api/v1/docs
```

It answers under `/api/v1`: `status`; `stops?q=|bbox=|near=`; `stop-places/<id>` with its
`departures` and `arrivals` (and `departures|arrivals?stops=a,b` for several, paged by `next`);
`trips/<trip_id>/<service_date>` (and `trips/by-key/<namespace>/<key>/<service_date>`, which
redirects) with its `composition?from=` (asked of the worker: 202 until answered) and
`circulation`, and `trips/<trip_id>/history`; `POST journeys/enrich` (MOTIS itinerary legs → the Oběhy
trips they ride, for the native app); `lines?q=`, `lines/<route_id>` with its
`timetable[?date=]` and `trips`; `vehicles?bbox=`, `vehicles/<vehicle_id>` and its `days`;
`circulations` (a source's oběhy on a date, by company and mode or by route) and its `scopes`;
`alerts?line=|stop=|trip=` and `alerts/<id>`; and
`debug/…` (raw rows, uncached, shapes may change). The interactive docs are
`/api/v1/docs`; the OpenAPI document is
`/api/v1/openapi.json`. Trip IDs go into paths percent-encoded (CZPTT IDs contain `%`).
Limits are in `src/obehy/data/api/api-v1.toml`, the readable static codes in `codes-v1.toml`
next to it. On the server, `deploy/compose.yaml` runs it as the `api` service and nginx
forwards `/api/` to it.

## Web frontend

`web/` is the app (Vite, React, Mantine; `BASE_PLAN.md` section 30). In development it proxies
`/api` to obehy.cz; set `OBEHY_API=http://127.0.0.1:8000` to use a local `obehy api serve`.

```powershell
cd web
npm ci
npm run dev                                  # http://localhost:5173
npm test; npm run lint; npm run typecheck
```

Its API types are generated from the OpenAPI document. After an API change, run
`uv run obehy api openapi --out web/openapi.json` and `npm run gen:api` (CI fails when either
is stale). On the server the `web` service is the `obehy-web` image, which is nginx with the
built app: it serves the app at `/`, next to the feeds and `/api/`.

## Vehicle registry

`obehy ref import <dataset>` mirrors a curated vehicle dataset into `ref.*` (`BASE_PLAN.md`
section 27) and tells a running worker to reload it. `vehicles` is Oběhy's own list (CSV in
`jrunify-ext-geodata/vehicles/`: `vehicle_id`, a source key or identifiers such as
`licence_plate`, `evn`, `series_number`, and attributes); `duk-register` is a seed in the same
format (`jrunify-ext-geodata/vehicles/duk.csv`); `pid-vehicles` is PID's vehicle models and
equipment from Golemio, which `obehy ref fetch pid-vehicles` adds to a CSV a few vehicles at a
time (it needs the Golemio key; `docs/sources/pid.md`). The file comes from `--path` or
`[reference]` in the config; an unchanged version is skipped.

```powershell
uv run obehy ref import vehicles --path D:\src\jrunify-ext-geodata\vehicles\vehicles.csv
uv run obehy ref import duk-register --path D:\src\jrunify-ext-geodata\vehicles\duk.csv
uv run obehy ref fetch pid-vehicles --path D:\work\pid-vehicles.csv
uv run obehy ref import pid-vehicles --path D:\work\pid-vehicles.csv
```

On the server the `jobs` service fetches PID's models at start and then hourly from 05:00 to
23:00 (Golemio describes only the vehicles running at the moment), importing every 200 vehicles
(`ref fetch --import`) and keeping the list in the data volume (`deploy/obehy-nightly`). The
curated files are imported by hand until they ship with releases.

## Station pairs

Activation pairs each railway station with the stop in front of it (`BASE_PLAN.md` section 7.4);
ties stay unpaired and are listed, with their candidates, at `/api/v1/debug/station-pairing`.
Settle them, or correct a pair, in `jrunify-ext-geodata/stops/station-pairs.csv`
(`station_id,stop_id,decision,note`; `decision` is `pair` or `apart`, an empty `stop_id` with
`apart` keeps the station alone) and import it: the active release is paired again whenever its
pairing is not current, so after deploying migration 0024 or a new pairing policy, import the
file again (unchanged is fine). A newly activated release pairs itself.

```powershell
uv run obehy ref import station-pairs --path D:\src\jrunify-ext-geodata\stops\station-pairs.csv
```

`/api/v1/debug/vehicles/registry` shows what each dataset holds and where sources disagree;
`/api/v1/debug/vehicles/pairings` lists reports seen running side by side, candidates for keys.

## Replaying realtime payloads

`obehy-rt replay` runs archived payloads through the same pipeline as the live worker, with a
simulated clock, against a release directory's serving files:

```powershell
obehy-rt replay --from-files <release-dir> --from 2026-10-05 --to 2026-10-06 `
  --archive ../rt-data/rt-raw --out ../work/replay --gtfs-rt-every 60 [--write-history]
```

It writes GTFS-RT snapshots; `--write-history` rebuilds `rt.observation` and the history of the
replayed days in the database. The replay report (results by reason and, per DÚK fleet, the
share of running key groups bound to a journey; prediction quality with `--quality`) is made
by Python from the worker's output stream:

```powershell
obehy-rt compare --level output --from-files <release-dir> --from 2026-10-12 --to 2026-10-12 `
  --sources dpmd --archive data/rt-raw --diagnostics --quality --out ../work/dpmd.jsonl.gz
uv run obehy rt report --from-stream ../work/dpmd.jsonl.gz --out ../work/dpmd-report
```

`obehy-rt compare ... --check <reference.jsonl.gz>` tells where a stream parts from a reference.

`obehy rt corpus pin` copies archive days into a pinned corpus. `obehy-rt bench --from-files
<release-dir> ... --out DIR` times the live worker's own path (decode, core, estimates, rows,
GTFS-RT) over archived polls without database writes and writes `bench.json` with CPU per
phase and hourly memory.

## Running the realtime worker

```powershell
obehy-rt worker --archive data/rt-raw --gtfs-rt data/gtfs-rt
```

The worker needs an active release. It rebuilds its state from the last hours of stored
observations (warm replay), polls and archives every channel, runs the DÚK, SŽ map and SŽ
station board connectors through the core, writes `data/gtfs-rt/jdf.pb` (buses) and
`data/gtfs-rt/czptt.pb` (trains: DÚK GPS and the SŽ map fused per run, platforms where the board
numbers tracks) every emit tick and history continuously, and rebases live journeys when a new
release is activated. Every 10 minutes it logs each channel's polls and the CPU and wall time
of each phase (archive, decode, index, core, write, estimate, GTFS-RT, state) as a share of a
core. SŽ channels poll only with the egress proxy list configured (see
"Recording realtime payloads"); station boards are read per station while trains are due there,
at most 60 a minute. Rail point events and the SŽ station codes need a release with serving 5.1
`sr70` keys. Vehicles' working days are cut live from history (`history.vehicle_chains`);
`obehy jobs nightly` (03:30 Prague in the server stack) drops history service dates (and their
compositions) older than
`[retention] history_days` (21; about 0.5 GB a day with PID) and raw archive days older than
`raw_days` per source (yesterday and today, SŽ station boards two weeks; pin days to keep
with `obehy rt corpus pin` first).
`obehy jobs hourly` (every hour in the server stack) drops `rt.observation` hours older than
`observation_hours` (8; all sources are about 10 GB a day) and keeps `[guard] free_gb` free on
the data disk by deleting the oldest raw days, then history days. The design is in
`docs/R1_SLICE.md` and, for rail, `docs/R2_SLICE.md`.

The worker also answers what the API is asked to look up (`rt.lookup_request`): ČD train
compositions (`src/obehy/data/realtime/sources/cd.toml`, `docs/sources/cd.md`), one request per
train and station, at most 6 a minute and 1000 a day, each answer archived under
`data/rt-raw/cd/composition`. `obehy jobs nightly` deletes lookup requests and their fetch
records after `raw_days`; compositions are kept.
