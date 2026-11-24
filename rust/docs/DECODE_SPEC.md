# `[channel.decode]` for obehy-sources (R2 spec)

Design survey of `src/obehy/realtime/sources/*`, `times.py`, `model_json.py`, `emit/db.py`
(2026-11-21, pyproj 3.8.0 / PROJ 9.8.1). This is the starting point for R2; the implementer may
adjust the shape where the code proves it wrong, but facts output must stay byte-identical to the
Python connectors (and payload errors must match: only error vs no error matters, not the text).

**After the switch (2026-11-21).** The Python connectors are gone and byte-identity is no longer
a goal: the Python semantics this survey lists (§4.2's built-ins, `json.loads`, CPython's
`html.parser`, `re`'s `$`, `int()`/`float()` of Unicode digits, Python `==` across bool and int,
PROJ's per-point operation choice) were replaced by plain Rust: serde_json (keys in document
order), scraper for the boards, `str::trim`/`parse`, ASCII digits, numbers matched by value and
booleans by truth, one Křovák operation (§5). The manifest shape and the quirks are unchanged;
the sections below remain the design record.

## 0. Conventions

- `entities`: the dotted path of the entity list; the empty path is the document itself
  (IDSOK-Q1).
- `values.<name>`: per entity, evaluated in declaration order. `path` dotted from the entity
  (`/`-prefixed = document root); a numeric segment indexes an array only; any non-object /
  non-array step → absent (= `decoding.table`). `from = "<value>"` derives from another value;
  `template` builds a string.
- `as` = coercion (exact Python behaviour in §4). Modifiers: `none = [...]` values meaning absent;
  `none_before = <UTC instant>`; `regex` + `captures` (Python `re.match` semantics);
  `required = true` drops the entity when absent (`"payload"` = PayloadError).
- Templates: `{v}` text (int decimal); `{v:d}` = Python `int(v)` (drops leading zeros);
  `{v:06d}`/`{v:03d}` Python zero padding (`-5` → `-00005`). Any absent value → template absent.
- Choosing by value: `by`/`cases`/`when`/`unless`/`skip`/`keep` use Python `==` on one value
  (`1 == 1.0 == true`, `2.0 == 2`, `true != 2`; strings only equal strings). A list in `match` =
  any of.
- Facts: emitted in list order; fact with a required input absent is omitted; input `name?` is
  optional (serialises null). `needs_time = true`: position dropped when observed_at absent.
  `if_fact = "trip_key"`: only if this observation already has a trip_key fact.
- Per-entity order: required → skip/keep → profile → remaining values (incl. payload-fatal
  parses) → normalizers → facts. Output sorted by `RawRef.item` (equals Python everywhere,
  incl. PID's `observations.sort` pid.py:99).
- Errors: entity list absent/not an array, or root of the wrong type → PayloadError. Non-object
  entity skipped silently.
- `version` replaces `DECODER_VERSION`.

## 1. Per source / channel

### duk / vehicles (declarative + DUK-Q1)
```toml
[channel.decode]
version = 4
reader = "json"
entities = "VehicleList"
skip = { state = 255 }
feed = { by = "line", cases = [{ match = 0, value = "czptt" }], default = "jdf" }   # DUK-Q8
observed_at = { value = "gps", fresh = true }
normalize = [{ fn = "offset_applied_twice", quirk = "DUK-Q1", value = "gps", against = "activity", tolerance = "5m" }]
fact = [
  { t = "vehicle_key", id = "{id}" },                                                # DUK-Q7
  { t = "trip_key", by = "line", cases = [
      { match = 0, ns = "czptt:train_number", key = "{trip}" },                     # DUK-Q8
      { ns = "cis:line_trip", key = "{line:06d}:{trip:d}" } ] },                     # DUK-Q9
  { t = "position", lat = "lat", lon = "lon", bearing = "bearing?", zero_is_none = true, needs_time = true },
  { t = "delay", s = "delay", scale = 60, ref = "unknown" },
  { t = "source_state", by = "state", cases = [{ match = [2, 3], code = "pre_trip" }, { code = "running" }] },  # DUK-Q5
  { t = "vehicle_attribute", a = "low_floor", value = "low_floor" },
  { t = "vehicle_attribute", a = "air_conditioning", value = "air" },
  { t = "source_departure", at = "departure" },                                      # DUK-Q18
]
[channel.decode.values]
state = { path = "State", as = "py_int", required = true }
id    = { path = "ID", as = "py_int", required = true }
line  = { path = "CISLineID", as = "py_int", required = true }
trip  = { path = "RouteID", as = "py_int", required = true }
gps      = { path = "GPSPositionDT",  as = "iso", invalid = "fail", none_before = "1971-01-01T00:00:00Z" }  # DUK-Q2
activity = { path = "LastActivityDT", as = "iso", invalid = "fail", none_before = "1971-01-01T00:00:00Z" }
departure = { path = "TODepartureDT", as = "iso", invalid = "fail", none_before = "1971-01-01T00:00:00Z" }
lat = { path = "Latitude", as = "number" }
lon = { path = "Longitude", as = "number" }
bearing = { path = "Azimut", as = "number", none = [0] }                             # DUK-Q13
delay = { path = "Delay", as = "int" }
low_floor = { path = "HasLowfloor", as = "bool" }
air = { path = "isAirConditioned", as = "bool" }
```
Mapping: DUK-Q2, Q5, Q7, Q8, Q9, Q13, Q18. Normalizer: Q1. Q14/Q15 stay core semantics.

### pid / vehicles (declarative, 2 normalizers, 2 profiles)
```toml
[channel.decode]
version = 4
reader = "json"
entities = "features"
feed = { by = "route_type", cases = [{ match = 2, value = "czptt" }], default = "jdf" }
observed_at = { value = "fix_time", fresh = true }
normalize = [
  { fn = "pid_cancelled", quirk = "PID-Q4", state = "state", flag = "is_canceled", out = "cancelled" },
  { fn = "block_current", quirk = ["PID-Q6", "PID-Q4"], profile = "vehicle", group_by = "vehicle",
    state = "state", start = "start", running = ["on_track", "at_stop", "off_track"],
    off_route = "off_track", pre_trip = ["before_track", "before_track_delayed"], out = "trip_state" },
]
[channel.decode.values]
route_type = { path = "properties.trip.gtfs.route_type", as = "int", required = true }
trip_id = { path = "properties.trip.gtfs.trip_id", as = "text", none = [""] }
train_number = { path = "properties.trip.cis.trip_number", as = "int" }
cis_line = { path = "properties.trip.cis.line_id", as = "digits" }                   # PID-Q3
fleet = { path = "properties.trip.vehicle_registration_number", as = "int" }
vehicle = { by = "route_type", cases = [                                             # PID-Q1, PID-Q2
  { match = 2, first_of = ["{route_type}:{fleet}", "train:{train_number}"] },
  { first_of = ["{route_type}:{fleet}", "trip:{trip_id}"] } ] }
state = { path = "properties.last_position.state_position", as = "raw" }
is_canceled = { path = "properties.last_position.is_canceled", as = "raw" }
start = { path = "properties.trip.start_timestamp", as = "iso", invalid = "none" }
fix_time = { path = "properties.last_position.origin_timestamp", as = "iso", invalid = "none" }
lon = { path = "geometry.coordinates.0", as = "number" }
lat = { path = "geometry.coordinates.1", as = "number" }
bearing = { path = "properties.last_position.bearing", as = "number" }
delay = { path = "properties.last_position.delay.actual", as = "int" }
wheelchair = { path = "properties.trip.wheelchair_accessible", as = "bool" }
air = { path = "properties.trip.air_conditioned", as = "bool" }
usb = { path = "properties.trip.usb_chargers", as = "bool" }
operator = { path = "properties.trip.agency_name.real", as = "trimmed" }
category = { path = "properties.trip.vehicle_type.description_cs", as = "trimmed" }
[channel.decode.groups]
trip_keys = [
  { t = "trip_key", by = "route_type", cases = [
      { match = 2, ns = "czptt:train_number", key = "{train_number}" },
      { ns = "pid:gtfs_trip_id", key = "{trip_id}" } ] },
  { t = "trip_key", ns = "cis:line_trip", key = "{cis_line:06d}:{train_number}", unless = { route_type = 2 } },
]
[channel.decode.profile.cancellation]          # PID-Q4: the trip, no vehicle
when = { cancelled = true }
require_fact = "trip_key"
fact = [{ use = "trip_keys" }, { t = "trip_cancelled" }]
[channel.decode.profile.vehicle]
require = ["vehicle"]
fact = [
  { t = "vehicle_key", id = "{vehicle}" },
  { use = "trip_keys", when = { trip_state = ["pre_trip", "running"] } },
  { t = "position", lat = "lat", lon = "lon", bearing = "bearing?", zero_is_none = true, needs_time = true },
  { t = "vehicle_attribute", a = "wheelchair_accessible", value = "wheelchair" },
  { t = "vehicle_attribute", a = "air_conditioning", value = "air" },
  { t = "vehicle_attribute", a = "usb", value = "usb" },
  { t = "vehicle_attribute", a = "operator", value = "operator" },
  { t = "vehicle_attribute", a = "category", value = "category" },
  { t = "vehicle_attribute", a = "fleet_number", value = "{fleet}" },
  { t = "source_state", code = "{trip_state}", if_fact = "trip_key" },
  { t = "delay", s = "delay", ref = "unknown", when = { trip_state = "running" }, if_fact = "trip_key" },
]
```
Mapping: PID-Q1, Q2, Q3. Normalizers: Q4, Q6.

### pid / alerts, pid / infotexts — code decoders
`decoder = "gtfs-rt-alerts"` / `"pid-infotexts"`: they emit Alerts, not facts (GTFS-RT protobuf,
dedup + sort; PID-Q10 window expansion). `gtfs-rt-alerts` is generic: the manifest names the
namespaces of the feed's IDs, `keys = { trip = "pid:gtfs_trip_id", stop = …, route = … }`; an
informed entity of a kind without one is not read.

### ids-jmk / vehicles, ids-jmk / alerts — GTFS-RT code decoders
`decoder = "gtfs-rt-vehicles"` (version 1) reads a vehicle positions dataset from the typed
protobuf, no intermediate value tree: one observation per vehicle entity on the one `feed`; the
vehicle key is `VehicleDescriptor.id` (entities without one, and deleted ones, are skipped), the
trip key `trip_id` in `keys.trip`, `start_date` a `service_day`, the position (32-bit floats
read as their shortest decimal) only with a fix time within the clock skew, which is also
`observed_at`; the label is the vehicle attribute `label_attribute` names. No delay: that is
trip updates'. The alerts channel is `gtfs-rt-alerts` with `ids-jmk:gtfs_*` keys.

### sz-mapa / trains (declarative, normalizers + CRS)
```toml
[channel.decode]
version = 3
reader = "json"
entities = "result"                     # root must be an object
feed = "czptt"
observed_at = { value = "md", fresh = true }
normalize = [
  { fn = "catalogue_codes", quirk = "SZ-Q4", name = "point", catalogue = "data/realtime/sr70-name20.csv", out = "codes" },
  { fn = "minute_middle", quirk = "SZ-Q12", value = "nsp", out = "nsp_mid" },
]
fact = [
  { t = "vehicle_key", id = "{id}" },
  { t = "trip_key", ns = "czptt:tr", key = "Tr:{company}:{core}:{variant}:{year}" },
  { t = "trip_key", ns = "czptt:train_number", key = "{tn}" },
  { t = "service_day", day = "operating" },
  { t = "position", xy = "xy", crs = "EPSG:5514", round = 6, bearing = "a?", zero_is_none = true, needs_time = false },  # SZ-Q1
  { t = "point_event", name = "point", codes = "codes", scheduled = "cp?", actual = "cr", width = "59s",
    standing = { by = "rr", cases = [{ match = 1, value = true }], default = false } },
  { t = "next_point", name = "nna", sr70 = "zst?" },
  { t = "delay", s = "de", scale = 60, ref = "point" },                               # SZ-Q12 (core)
  { t = "next_stop_prediction", sr70 = "nsn70", scheduled = "nst?", predicted = "nsp_mid?" },
  { t = "trip_status", replacement_bus = { by = "s", cases = [{ match = 1, value = true }], default = false },
                       diverted = { by = "di", cases = [{ match = 1, value = true }], default = false } },
]
[channel.decode.document]
md = { path = "/md", as = "local:%d.%m.%Y %H:%M:%S" }                                # SZ-Q2
[channel.decode.values]
id = { path = "properties.id", as = "text", required = true,
       regex = '^TR/([^/]+)/([^/]+)/([^/]+)/([^/]+)/(\d{8})$', captures = ["company", "core", "variant", "year", "day"] }
operating = { from = "day", as = "yyyymmdd", required = true }
tn = { path = "properties.tn", as = "digits" }                                       # SZ-Q5
xy = { path = "geometry.coordinates", as = "pair" }
a = { path = "properties.a", as = "real_nb" }                                        # SZ-Q8
point = { path = "properties.cna", as = "trimmed" }
cr  = { path = "properties.cr",  as = "hhmm", near = "observed_at" }                 # SZ-Q2, SZ-Q3
cp  = { path = "properties.cp",  as = "hhmm", near = "observed_at" }
nst = { path = "properties.nst", as = "hhmm", near = "observed_at" }
nsp = { path = "properties.nsp", as = "hhmm", near = "observed_at" }
rr = { path = "properties.rr", as = "raw" }
nna = { path = "properties.nna", as = "trimmed" }                                    # SZ-Q7
zst = { path = "properties.zst_sr70", as = "sr70_5" }                                # SZ-Q4
nsn70 = { path = "properties.nsn70", as = "sr70_5" }
de = { path = "properties.de", as = "int" }
s = { path = "properties.s", as = "raw" }
di = { path = "properties.di", as = "raw" }
```
`near = "observed_at"`: `md` if fresh, else `received_at` (sz.py:113).

### sz-tabule / board — code decoder
`decoder = "sz-tabule"`, `version = 2`, `reader = "html"`: one board per poll, rows across two
tables, header-dependent label (SZT-Q2/Q9), the request's station (SZT-Q1); output (TripKey,
PlatformAssignment) pairs; SZT-Q4/Q5 in the decoder.

### dpmd / vehicles (JSON reader over the recorded archive, normalizers)
The JS-literal reader (DPMD-Q2, Q5, Q8) stays the recorder filter `filter = "dpmd"`, as code in
obehy-sources. `decode` reads the archived JSON (dpmd.py:225).
```toml
[channel.decode]
version = 2
reader = "json"
entities = "buspositions"
feed = "jdf"
observed_at = { value = "time", fresh = true }
normalize = [
  { fn = "stale_body", quirk = "DPMD-Q9", value = "stamp", cut_at = ".", layout = "%Y-%m-%dT%H:%M:%S" },
  { fn = "popup_labels", quirk = "DPMD-Q2", value = "body", prefix = "label" },
  { fn = "moving_bearing", quirk = "DPMD-Q10", speed = "label.Rychlost", arrow = "arrow", out = "bearing" },
]
fact = [
  { t = "vehicle_key", id = "{vehicle}" },
  { t = "trip_key", ns = "cis:line_trip", key = "515{line:03d}:{spoj:d}" },
  { t = "position", lat = "lat", lon = "lon", bearing = "bearing?", zero_is_none = false, needs_time = true },
  { t = "vehicle_attribute", a = "fleet_number", value = "fleet" },
  { t = "vehicle_attribute", a = "model", value = "model" },
  { t = "vehicle_attribute", a = "licence_plate", value = "plate" },
  { t = "delay", s = "min", scale = 60, default = 0, ref = "unknown" },               # DPMD-Q4
]
[channel.decode.document]
stamp = { path = "/TimeStamp", as = "text", required = "payload" }
[channel.decode.values]
vehicle = { path = "vehicle", as = "text", none = [""], required = true }
body = { path = "body", as = "text", required = true }
line = { from = "label.Linka", as = "digits", trim = true, required = true }
spoj = { from = "label.Spoj", as = "digits", trim = true, required = true }
time = { from = "label.Čas", trim = true, as = "local:%d.%m.%Y %H:%M:%S" }           # DPMD-Q1
coords = { path = "coords", as = "text", regex = '^(\d+(?:\.\d+)?)N, (\d+(?:\.\d+)?)E$', captures = ["lat", "lon"], captures_as = "py_float" }
arrow = { path = "arrow", as = "raw" }
fleet = { from = "label.Autobus", as = "trimmed" }
model = { from = "label.Typ vozu", as = "trimmed" }
plate = { from = "label.SPZ", as = "trimmed" }
delay = { from = "label.Zpoždění", trim = true, regex = '^(\d+) min\.$', captures = ["min"], captures_as = "py_int" }
```
`stale_body` runs before entities; when it fires the payload gives zero observations.
DPMD-Q10 is a new quirk ID (dpmd.py:275-278, heading is noise at a standstill): add it to
`docs/sources/dpmd.md`.

### mpvnet / idol, jikord, odis, zlin (declarative, one shared block)
Manifest-level `[decode.portal]`, each channel `decode = "portal"`; the observation's channel is
the channel name.
```toml
[decode.portal]
version = 3
reader = "json"
entities = "transmitters"
normalize = [
  { fn = "mpvnet_record", quirk = ["MPVNET-Q2", "MPVNET-Q3"], record = "cn", kind_field = "t", category = "cat",
    bus = { t = "H", fields = 22, cat = 2, line_field = 0, line_regex = '^\d{6}$' }, train = { t = "T", fields = 12 },
    out = "kind", fields_prefix = "rec" },
  { fn = "signed_min_sec", quirk = "MPVNET-Q6", value = "ds", regex = '^\$[zp](-?)(\d+)~min\.~(\d+)~s$', out = "ds_delay" },
]
feed = { by = "kind", cases = [{ match = "bus", value = "jdf" }, { match = "train", value = "czptt" }] }
observed_at = { value = "time", fresh = true }
require = { by = "kind", cases = [{ match = "bus", values = ["plate", "trip"] }, { match = "train", values = ["trip"] }] }
fact = [
  { t = "vehicle_key", id = "{vehicle}" },
  { t = "trip_key", by = "kind", cases = [
      { match = "bus", ns = "cis:line_trip", key = "{line:06d}:{trip:d}" },
      { match = "train", ns = "czptt:train_number", key = "{trip}" } ] },
  { t = "vehicle_attribute", a = "licence_plate", value = "plate", when = { kind = "bus" } },
  { t = "position", lat = "lat", lon = "lon", bearing = "deg?", zero_is_none = true, needs_time = true },
  { t = "delay", s = "delay", ref = "unknown" },
]
[decode.portal.values]
ds = { path = "ds", as = "text" }
plate = { from = "rec.11", as = "trimmed" }
trip = { from = "rec.2", as = "digits" }
line = { from = "rec.0", as = "text" }
dt = { path = "dt", as = "text" }
tm = { path = "tm", as = "text" }
time = { template = "{dt} {tm}", as = "local:%d.%m.%Y %H:%M:%S" }                    # MPVNET-Q4
lat = { path = "x", as = "number" }                                                  # MPVNET-Q5
lon = { path = "y", as = "number" }
bearing = { path = "ico", as = "text", regex = '^a_(\d{1,3})\.png$', captures = ["deg"], captures_as = "py_float" }
delay = { by = "kind", cases = [{ match = "bus", from = "rec.6", as = "py_int" },    # MPVNET-Q9
                                { match = "train", from = "ds_delay" }] }            # MPVNET-Q6
vehicle = { by = "kind", cases = [{ match = "bus", template = "{plate}" }, { match = "train", template = "train:{trip}" }] }
```
`kind` absent (no `cn`, wrong layout) drops the entity. Since decoder version 4 a city bus
(`cat` 3, a 1–3 digit line) is kind `city`: `line_ref`, `destination` and `next_stop_name`
(`scheduled` from field 7 as `minute_of_day`, both `townless`), no trip key (MPVNET-Q3, Q10). Q4, Q5, Q8, Q9 mapping;
Q2, Q3, Q6 normalizers; Q1, Q7 request side.

### iredo / vehicles, idsok / vehicles (fully declarative, added after the switch)
One record layout for both (the same vendor software); IREDO wraps it in `{"connections": [...]}`
and is asked by POST, IDSOK answers a bare array (`root = "array"`, `entities = ""`).
```toml
keep = { kind = ["S", "K"] }
feed = { by = "kind", cases = [{ match = "S", value = "jdf" }, { match = "K", value = "czptt" }] }
observed_at = { value = "time", fresh = true }
fact = [
  { t = "vehicle_key", id = "trip:{id}" },                                    # Q3
  { t = "trip_key", by = "kind", cases = [
    { match = "S", ns = "cis:line_trip", key = "{line:06d}:{spoj:d}" },
    { match = "K", ns = "czptt:train_number", key = "{train}" } ] },
  { t = "position", lat = "lat", lon = "lon", bearing = "bearing?", zero_is_none = true, needs_time = true },
  { t = "delay", s = "delay", scale = 60, ref = "unknown" },
]
[channel.decode.values]
bus = { from = "id", regex = '^S-(\d{6})-(\d+)$', captures = ["line", "spoj"], captures_as = "py_int", when = { kind = "S" }, required = true }  # Q2
rail = { from = "id", regex = '^K-(\d+)$', captures = ["train"], when = { kind = "K" }, required = true }
time = { path = "time", as = "hhmmss" }                                       # Q4
```

### arriva-express / buses (fully declarative)
```toml
[channel.decode]
version = 2
reader = "json"
entities = "0.data.busesCurrentLocations"     # not a non-empty array / no list: PayloadError
keep = { main_type = "ARRIVA EXPRESS" }        # ARRIVA-Q7 (the recorder filter stays)
feed = "jdf"
observed_at = { value = "updated", fresh = true }
fact = [
  { t = "vehicle_key", id = "{plate}" },
  { t = "vehicle_attribute", a = "licence_plate", value = "plate" },
  { t = "line_ref", ns = "cis:line", value = "{line}" },
  { t = "destination", name = "destination" },
  { t = "next_stop_name", name = "next" },
  { t = "position", lat = "lat", lon = "lon", bearing = "bearing?", zero_is_none = true, needs_time = true },
  { t = "delay", s = "delay", scale = 60, ref = "departure" },                        # ARRIVA-Q3
]
[channel.decode.values]
main_type = { path = "mainType", as = "raw" }
plate = { path = "spz", as = "trimmed", required = true }                            # ARRIVA-Q5
line = { path = "linkNumber", as = "trimmed", required = true }
destination = { path = "destinationName", as = "trimmed", required = true }
next = { path = "lastStopName", as = "trimmed" }                                     # ARRIVA-Q2
updated = { path = "updated", as = "iso_local_digits" }                              # ARRIVA-Q1
lat = { path = "latitude", as = "number" }
lon = { path = "longitude", as = "number" }
bearing = { path = "angle", as = "number" }
delay = { path = "delay", as = "int_or_digits" }
```
Only `document[0]` is read (arriva_express.py:79).

### cd / composition (lookup) — code decoder
`decoder = "cd-composition"`: a lookup with `plan` (CD-Q1), emits Units; CD-Q2..Q8 in code.

## 2. Readers, filters, normalizers

Readers: `json` (§4.1), `html` (sz-tabule), `gtfs-rt` (the GTFS-RT code decoders).
Recorder filters (reader options; byte-identical because sha256 depends on them): `arriva-express`
(arriva_express.py:44-70), `dpmd` (JS literal reader + `recorded`, dpmd.py:62-222).

| fn | quirk | semantics (replaces) |
|---|---|---|
| `offset_applied_twice` | DUK-Q1 | only if both present after `none_before`; re-parse the raw GPS text: if `offset > 0` and `abs(labelled − against − offset) ≤ tol` → `labelled − offset`, else `labelled`; the corrected value is not re-filtered for 1970 (duk.py:53-62, times.py:198-211) |
| `pid_cancelled` | PID-Q4 | `state == "canceled"` or flag is JSON `true` (identity, not `== 1`) (pid.py:103-105) |
| `block_current` | PID-Q6, Q4 | over vehicle-profile entities grouped by `vehicle` in item order: if any running, keep the first not `off_route` (else first running), `bound = (count non-off-route, or running when none on route) == 1`; else the earliest pre-trip by `start` (stable sort, absent last), else the first, `bound = true`. `out` = pre_trip/running when bound and state in those sets, else absent; others in the group dropped (pid.py:83-99, 140-164, 236-238) |
| `catalogue_codes` | SZ-Q4 | CSV utf-8-sig; rows need len ≥ 2, `len(row[0]) ≥ 5`, non-empty `row[1].strip()`; key `row[1].strip()`, codes `row[0][:5]` deduped, sorted as strings; missing name → `()` (sz.py:60-69, 224) |
| `minute_middle` | SZ-Q12 | +30 s (sz.py:188-191) |
| `stale_body` | DPMD-Q9 | `read_local(stamp.split(".")[0])`; absent or beyond skew → payload gives `[]` (not an error) (dpmd.py:241-243) |
| `popup_labels` | DPMD-Q2 | `dict(re.findall(r"<br>([^<:]{1,30}): ([^<]*)"))`, last duplicate wins, counts in code points (dpmd.py:262) |
| `moving_bearing` | DPMD-Q10 | speed `^(\d+) km/h$` on stripped label, `int > 0`; arrow a str with `isdigit` → `float(arrow)`, else absent (dpmd.py:275-278) |
| `mpvnet_record` | MPVNET-Q2, Q3 | `cn` a str split on `"\|"`; bus: `t == "H"`, 22 fields, `cat == 2` (Python ==), `^\d{6}$` on field 0; optional city: its own layout (`cat == 3`, `^\d{1,3}$`), kind `city`; train: `t == "T"`, 12 fields; else absent (mpvnet.py:72-97) |
| `signed_min_sec` | MPVNET-Q6 | `min*60 + s`, negated with the `-` group; `-0` → 0 (mpvnet.py:137-144) |

Time conventions (mapping level): `iso`, `iso_local_digits` (ARRIVA-Q1), `local:<layout>`
(DPMD-Q1, MPVNET-Q4, SZ-Q2), `hhmm` (SZ-Q2, SZT-Q5), `hhmmss` (IREDO-Q4, IDSOK-Q4), `minute_of_day` (MPVNET-Q3: digits or an integer below 1440, resolved like `hhmm`). CRS: `EPSG:5514` (SZ-Q1).
Add a test: every `quirk` named in a manifest exists in `docs/sources/<source>.md`.

## 3. Rust type sketch
```rust
#[derive(Deserialize)] #[serde(deny_unknown_fields)]
pub struct Decode {
    pub version: Option<u32>, pub reader: Reader, pub decoder: Option<CodeDecoder>,
    pub entities: Option<Path>,
    #[serde(default)] pub document: IndexMap<Name, ValueSpec>,
    #[serde(default)] pub values: IndexMap<Name, ValueSpec>,
    #[serde(default)] pub normalize: Vec<Normalizer>,
    pub skip: Option<ByValue>, pub keep: Option<ByValue>,
    #[serde(default)] pub require: Require,
    pub feed: Choice<Feed>, pub observed_at: Option<ObservedAt>,
    #[serde(default)] pub groups: IndexMap<Name, Vec<FactSpec>>,
    #[serde(default)] pub profile: IndexMap<Name, Profile>,
    #[serde(default)] pub fact: Vec<FactSpec>,
}
pub enum Coerce { Raw, Int, IntOrDigits, PyInt, PyFloat, Number, Real, RealNb, Bool, Text, Trimmed,
                  Digits, Sr70_5, Yyyymmdd, Pair, Iso, IsoLocalDigits, Local(Layout), Hhmm, Hhmmss, MinuteOfDay }
#[serde(tag = "fn", rename_all = "snake_case")] pub enum Normalizer { /* one per row above */ }
#[serde(tag = "t", rename_all = "snake_case")] pub enum FactSpec { /* one per facts.py type */, Use{..} }
// every FactSpec carries Gate { when, unless, if_fact } via #[serde(flatten)]
```
`Scalar` equality must implement Python `==` across int, float and bool.

## 4. Exact-form points

### 4.1 JSON in, facts out
1. `load_json` (decoding.py:15-19) = `json.loads(bytes)`: auto-detects UTF-8 BOM and UTF-16/32;
   accepts `NaN`, `Infinity`, `-Infinity`; arbitrary-size ints (serde_json
   `arbitrary_precision`); `1e400` → inf; duplicate key: last value wins at the first key's
   position; lone surrogates accepted (Rust can't — check the archive, flag as accepted
   divergence); correctly rounded float parsing (`float_roundtrip`).
2. Facts JSON (emit/db.py:162) = `json.dumps(facts, separators=(",",":"))` with **ensure_ascii
   on**: non-ASCII → `\uXXXX` lowercase hex, astral → surrogate pairs. Key order exactly as
   model_json.py:45-100, wrapped `{"v":5,"facts":[…]}`. Float repr (`1e-07`, `1e+16`;
   `NaN`/`Infinity` tokens — `number("nan")` passes `fix()` since NaN ≠ 0); `50.0`.
3. Instants: `isoformat()` in UTC `+00:00`, fraction only if nonzero (6 digits). Parsing
   truncates to microseconds (fromisoformat truncates a 7th digit).
4. Recorder filter output feeds sha256 → RawRef: Arriva (arriva_express.py:69) and DPMD
   (dpmd.py:221) write `ensure_ascii=False`, compact separators; escapes `\" \\ \b \f \n \r \t`
   and `\u00xx` lowercase for other controls. Arriva re-serialises the whole document (float repr
   and dict order matter). DPMD literal NUMBER `-?\d+(?:\.\d+)?`: float if it has `.`, else int,
   no exponent (dpmd.py:47, 100-104).

### 4.2 Python built-ins (as surveyed; replaced by plain Rust after the switch)
- `str.strip()/split()`: Python whitespace = `char::is_whitespace` plus `\x1c`-`\x1f`.
- `str.isdigit()`: Unicode Numeric_Type Decimal or Digit (`²` true). `int(str)`: Decimal only,
  surrounding whitespace, sign, `_` between digits. So `²` passes isdigit and fails int(): that
  ValueError drops the **whole payload** wherever an isdigit value later goes through int()
  (`line_trip` pid.py:183, dpmd.py:270, mpvnet.py:86).
- `float(str)` (decoding.py:35, dpmd.py:279): same plus nan/inf/infinity any case.
- `re`: `\d` = Unicode Nd (Rust regex default). `$` without MULTILINE also matches before a final
  `\n` → translate to `\n?\z` (sz.py:49 vehicle_key then keeps that `\n`). `read_local` uses
  fullmatch.
- `py_int` on JSON (duk.py:89-92): int as is, bool 0/1, float truncated, str per int(); NaN or
  other → drop entity; ±Infinity → OverflowError uncaught → payload error.
- `int`/`number`/`integer`: a bool is never a number (decoding.py:28-55; pid.py:60-63).
- `real`: SZ coords use `isinstance(x, int|float)` so `true` is 1.0 and `false` 0 → no position
  (sz.py:202); bearing `a` excludes bool (sz.py:206-210).

### 4.3 Times
- `within_skew`/`fresh` inclusive `≤` (times.py:221; decoding.py:58-64).
- `read_local` (times.py:161-185): fast regex first; else strptime decides: a space in the format
  = `\s+`; `%d` = `3[01]|[12]\d|0[1-9]|[1-9]| [1-9]`; `%Y` exactly 4 digits; `%H %M %S` one or two
  digits; case-insensitive. Invalid date → absent. `_readings` (times.py:135-149) tries fold 0
  and 1, keeps readings that round-trip (spring gap → absent); min by `(|r − received|, r)`.
- `hhmm` (sz.py:80-89; times.py:187-195): stripped, `^\d{1,2}:\d{2}$`, h ≤ 23, m ≤ 59;
  candidate dates = the local day of the reference's *UTC hour* (times.py:89-106) ± 1 day;
  keep readings within 12 h inclusive; closest wins, ties to earlier.
- `hhmmss` (added after the switch for IREDO/IDSOK): `^\d{1,2}:\d{2}:\d{2}$` after trimming,
  h ≤ 23, m and s ≤ 59; resolved like `hhmm` (`near`, else the reception).
- DUK `iso` (duk.py:43-50): naive → absent; filter `utc.year <= 1970`; parse error **fatal**.
  PID `iso` (pid.py:66-73) catches → absent.
- `iso_local_digits` (times.py:214-218; arriva_express.py:141-149): fromisoformat full grammar
  (`Z`, `+0200`, basic format), drop tz, resolve_local; any ValueError → absent.
- PointEvent.hi = cr + 59 s (sz.py:226). `standing` and TripStatus flags use Python `== 1`
  (sz.py:175, 227).

### 4.4 Keys, ordering, items
- `line_trip` = `f"{int(line):06d}:{int(trip)}"` (decoding.py:75-79).
- Train numbers stay raw strings with leading zeros in SZ `tn` (sz.py:153), mpvnet trains
  (mpvnet.py:93-94), SZT; DUK's (duk.py:99) is `str(int)`.
- DPMD key `515` + `{line:03d}` (dpmd.py:270); DPMD has no zero check on position (dpmd.py:279).
- `RawRef.item` = index in the source list, counting skipped entries; SZT counts rows only of
  tables passing the header checks (sz_tabule.py:148-149, 167); a row's platform and track
  observations share an item.
- Fixed orders: PID attributes wheelchair, air, usb, operator, category, fleet (pid.py:199-220);
  SZ facts (sz.py:148-175); SZT tables in document order (sz_tabule.py:142).
- sz-tabule HTML must follow CPython `html.parser` semantics (no implicit closes,
  convert_charrefs, duplicate attrs last wins, nested td replaces the open one, text joined then
  whitespace-normalised). Port the tokenizer, test on archived boards. `_labels` matches the
  last header after normalising and `.lower()` (sz_tabule.py:171-174). Parts split on `" / "`
  exactly then strip; `{"", "-", "ND"}` dropped.
- Alerts: entities a set, sorted by `(kind, id, service_date or "")` (pid_alerts.py:57) —
  TypeError if one trip has dated and undated entries; alerts sorted by id; ServiceTime.instant
  fold=0; infotexts `_time`/`_seconds` errors fatal (pid_alerts.py:99-108).

## 5. SŽ Křovák (SZ-Q1)
sz.py:55-57 `Transformer.from_crs("EPSG:5514","EPSG:4326", always_xy=True)`; sz.py:204
`lon, lat = transform(float(x), float(y))` (East-North, negative); sz.py:211 `round(lat, 6),
round(lon, 6)`. Bearing not rounded.

PROJ picks one of four operations per point by area of use. As built after the switch
(`obehy-sources/src/krovak.rs`): one operation everywhere, S-JTSK to WGS 84 (5) (EPSG:5239,
Czechia, about 1 m): inverse Křovák on Bessel, geocentric, the seven-parameter Helmert
(`x=572.213 y=85.334 z=461.94 rx=-4.9732 ry=-1.529 rz=-5.2484 s=3.5378`, coordinate frame),
geodetic on WGS 84 (Bowring). Inside Czechia's area it agrees with PROJ to about 1e-11 degrees;
outside it (PROJ's Slovak and fallback operations) positions move by up to about a metre.

## 6. As built (R2, 2026-11-21)

The manifests (`src/obehy/data/realtime/sources/*.toml`) hold the final shape; the Rust types are in
`obehy-core/src/config/decode.rs` (read strictly), the engine in `obehy-sources/src/mapping.rs`.
Where the code moved away from the sketch above:

- **Normalizers are values.** A `[values]` entry with `fn` (and its `quirk`) instead of a
  `normalize` list: later values read their output (DPMD's `label.Linka`, MPV net's `rec.11`,
  DÚK's `fix_time`). The payload-level DPMD-Q9 is a `guard`; PID-Q6's choice across entities
  is a profile's `select`.
- **Choices** have no `default` key: a case without `match` is the default; a case of a scalar
  (`feed`, a flag, a state code) holds it under `value`.
- **Errors.** `root` (`object`, `array`), `non_empty` and an `errors` table with Python's texts
  (`json` prefix, `root`, `entities`; `required = "payload"` uses the latter), so the stream's
  `payload_error` records match too. `[document]` paths are plain, not `/`-prefixed.
- **Values** take `when`/`unless` (a gated-off value is absent but not missing for `required`:
  MPV net's `plate`, PID's `cis_key`, which is a value so that its `int()` fails the payload
  exactly where Python's `_keys` does), value-level `fresh` (SŽ `md`), and `near = "<value>"`
  (falling back to reception). `skip`/`keep` are checked right after the leading required values
  or the values they read, whichever is later (DÚK: after the four `int()`s, as Python).
- **Coercions**: `integer` (= `decoding.integer`) for `int_or_digits`; `real` without bool for
  SŽ's `a` (the pair has its own bool rule); `float` for regex captures (`float()`, an error if
  it raises). `offset_applied_twice` reads the raw text (`text = "gps_text"`);
  `moving_bearing` has `needs` (Python reads the arrow only for a position it keeps);
  `mpvnet_record` makes one record value (`rec.kind`, `rec.<n>`).
- Code decoders: `decoder = "sz-tabule" | "gtfs-rt-vehicles" | "gtfs-rt-alerts" |
  "pid-infotexts" | "cd-composition"` (ČD under `[lookup.decode]`); `version` is only for
  facts. The GTFS-RT ones read `keys`, `gtfs-rt-vehicles` also a plain `feed` and
  `label_attribute`.

Declarative: DÚK, PID positions, SŽ map (with Křovák), DPMD (over the recorder filter's JSON),
the four MPV net portals (one `[decode.portal]`), Arriva Express. Code: SŽ boards, PID alerts
and infotexts, ČD compositions, and the two recorder filters (`arriva_express::recorded`,
`dpmd::recorded`). Křovák (§5) is pure Rust.
