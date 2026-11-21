//! The index loader over serving tables (R3): the Python worker's `test_index_running_only.py`
//! and the loader cases of its `test_realtime_index.py`, with the tables built in memory as its
//! timetable builder and `tests/db/serving_fixture.py` wrote them. The loader over PostgreSQL
//! and over a release's Parquet read the same rows (`examples/index_parity.rs`).

use obehy_core::index::{IndexLoader, KeyRef};
use obehy_core::model::Feed;
use obehy_core::time::Date;
use obehy_rt::index::ServingRows;
use obehy_rt::index::serving::{NONE, Strings, Tables};

const DAY: Date = Date::ymd(2026, 10, 8);
const NEXT: Date = Date::ymd(2026, 10, 9);
const LATER: Date = Date::ymd(2026, 10, 13);

struct Builder {
    s: Strings,
    t: Tables,
}

struct Call<'a> {
    location: &'a str,
    point: Option<&'a str>,
    passenger: bool,
    time: Option<i32>,
    pickup: i16,
}

fn call(location: &str, time: i32) -> Call<'_> {
    Call {
        location,
        point: None,
        passenger: true,
        time: Some(time),
        pickup: 0,
    }
}

impl Builder {
    fn new() -> Builder {
        Builder {
            s: Strings::default(),
            t: Tables::default(),
        }
    }

    fn id(&mut self, text: &str) -> u32 {
        self.s.intern(text)
    }

    fn opt(&mut self, text: Option<&str>) -> u32 {
        text.map_or(NONE, |t| self.s.intern(t))
    }

    fn location(&mut self, id: &str, name: &str, precision: &str) -> &mut Self {
        let (id, name, precision) = (self.id(id), self.id(name), self.id(precision));
        let l = &mut self.t.locations;
        l.location_id.push(id);
        l.name.push(name);
        l.longitude.push(Some(14.5));
        l.latitude.push(Some(50.7));
        l.coordinate_precision.push(precision);
        self
    }

    fn route(&mut self, id: &str, short_name: Option<&str>, mode: &str) -> &mut Self {
        let (id, short, mode, agency) = (
            self.id(id),
            self.opt(short_name),
            self.id(mode),
            self.id("agency:1"),
        );
        let r = &mut self.t.routes;
        r.route_id.push(id);
        r.short_name.push(short);
        r.mode.push(mode);
        r.agency_id.push(agency);
        self
    }

    fn key(&mut self, kind: &str, namespace: &str, identifier: &str, public_id: &str) -> &mut Self {
        let ids = [
            self.id(kind),
            self.id(namespace),
            self.id(identifier),
            self.id(public_id),
        ];
        let k = &mut self.t.source_keys;
        k.entity_kind.push(ids[0]);
        k.namespace.push(ids[1]);
        k.identifier.push(ids[2]);
        k.public_id.push(ids[3]);
        k.valid_from.push(Date::ymd(2026, 1, 1));
        k.valid_to.push(Date::ymd(2026, 12, 31));
        self
    }

    /// A trip running on `days` (exceptions added, as `serving_rows` writes calendars).
    #[allow(clippy::too_many_arguments)]
    fn trip(
        &mut self,
        trip_id: &str,
        route_id: &str,
        days: &[Date],
        run: Option<(&str, i16)>,
        shape: bool,
        first_sequence: i32,
        calls: &[Call<'_>],
    ) -> &mut Self {
        let service = format!("{trip_id}:service");
        let shape_id = shape.then(|| format!("{trip_id}:shape"));
        let ids = (
            self.id(trip_id),
            self.id(route_id),
            self.id(&service),
            self.opt(shape_id.as_deref()),
            self.opt(run.map(|r| r.0)),
        );
        let t = &mut self.t.trips;
        t.trip_id.push(ids.0);
        t.route_id.push(ids.1);
        t.service_id.push(ids.2);
        t.headsign.push(NONE);
        t.shape_id.push(ids.3);
        t.run_key.push(ids.4);
        t.run_part.push(run.map(|r| r.1));
        let c = &mut self.t.calendar;
        c.service_id.push(ids.2);
        c.valid_from.push(days[0]);
        c.valid_to.push(days[days.len() - 1]);
        c.weekday_mask.push(0);
        for day in days {
            let e = &mut self.t.exceptions;
            e.service_id.push(ids.2);
            e.service_date.push(*day);
            e.added.push(true);
        }
        for (n, spec) in calls.iter().enumerate() {
            let (location, point) = (self.id(spec.location), self.opt(spec.point));
            let c = &mut self.t.calls;
            c.trip_id.push(ids.0);
            c.sequence.push(first_sequence + n as i32);
            c.location_id.push(location);
            c.passenger_service.push(spec.passenger);
            c.boarding_point_id.push(point);
            c.scheduled_arrival.push(spec.time);
            c.scheduled_departure.push(spec.time);
            c.pickup_type.push(spec.pickup);
            c.dropoff_type.push(0);
            if shape {
                let p = &mut self.t.shape_points;
                p.shape_id.push(ids.3);
                p.sequence.push(n as i32 + 1);
                p.longitude.push(14.0 + n as f64 / 100.0);
                p.latitude.push(50.0);
            }
        }
        self
    }

    fn loader(self, feed: Feed) -> IndexLoader<ServingRows> {
        IndexLoader::new(ServingRows::from_tables(self.s, self.t), "run-test", feed)
    }
}

fn key(namespace: &str, identifier: &str) -> KeyRef {
    KeyRef::new(namespace, identifier)
}

fn trip_ids<V, S>(map: &std::collections::HashMap<std::sync::Arc<str>, V, S>) -> Vec<String> {
    let mut ids: Vec<String> = map.keys().map(|k| k.to_string()).collect();
    ids.sort();
    ids
}

/// `test_index_running_only._loader`: one key naming three variants on three dates.
fn variants() -> IndexLoader<ServingRows> {
    let mut b = Builder::new();
    b.route("jdf:r582492", Some("582492"), "bus");
    for stop in ["jdf:B", "jdf:C"] {
        b.location(stop, stop, "exact");
    }
    let calls = [call("jdf:B", 6 * 3600), call("jdf:C", 6 * 3600 + 1200)];
    for (trip, day) in [
        ("jdf:t-thu", DAY),
        ("jdf:t-fri", NEXT),
        ("jdf:t-later", LATER),
    ] {
        b.trip(trip, "jdf:r582492", &[day], None, true, 1, &calls);
        b.key("trip", "cis:line_trip", "582492:144", trip);
    }
    b.key("route", "cis:line", "582492", "jdf:r582492");
    b.loader(Feed::Jdf)
}

/// `keyed.running`: the trips a key names that run on `day`.
fn running(loader: &IndexLoader<ServingRows>, day: Date) -> Vec<String> {
    let index = &loader.index;
    index
        .keys("cis:line_trip", "582492:144")
        .unwrap()
        .iter()
        .filter(|e| e.valid_on(day))
        .filter_map(|e| index.running_trip(&e.public_id, day).unwrap())
        .map(|t| t.trip_id.to_string())
        .collect()
}

#[test]
fn only_the_variants_running_on_loaded_dates_load() {
    let mut loader = variants();
    let k = key("cis:line_trip", "582492:144");
    loader.ensure([&k], [DAY], []).unwrap();
    assert_eq!(trip_ids(&loader.index.trips), ["jdf:t-thu"]);
    assert_eq!(
        trip_ids(&loader.index.trip_services),
        ["jdf:t-fri", "jdf:t-later", "jdf:t-thu"]
    );
    assert_eq!(running(&loader, DAY), ["jdf:t-thu"]);
}

#[test]
fn a_new_date_loads_the_variants_running_on_it() {
    let mut loader = variants();
    let k = key("cis:line_trip", "582492:144");
    loader.ensure([&k], [DAY], []).unwrap();
    assert!(loader.needs([], [NEXT], []));
    loader.ensure([], [NEXT], []).unwrap();
    assert!(!loader.needs([&k], [DAY, NEXT], []));
    assert_eq!(trip_ids(&loader.index.trips), ["jdf:t-fri", "jdf:t-thu"]);
    assert_eq!(running(&loader, NEXT), ["jdf:t-fri"]);
}

#[test]
fn a_sweep_keeps_what_a_later_date_needs() {
    let mut loader = variants();
    let k = key("cis:line_trip", "582492:144");
    loader.ensure([&k], [DAY], []).unwrap();
    loader.index.sweep([&k], [], [], DAY);
    assert_eq!(
        trip_ids(&loader.index.trip_services),
        ["jdf:t-fri", "jdf:t-later", "jdf:t-thu"]
    );
    loader.ensure([], [NEXT], []).unwrap();
    assert!(loader.index.trips.contains_key("jdf:t-fri"));
}

#[test]
fn the_loaders_sweep_forgets_keys_not_named_since() {
    let mut loader = variants();
    let k = key("cis:line_trip", "582492:144");
    let at = obehy_core::time::Instant::from_isoformat("2026-10-08T06:00:00+00:00").unwrap();
    loader.ensure([&k], [DAY], []).unwrap();
    loader.touch([&k], [], at);
    let later = at + obehy_core::time::TimeDelta::from_seconds(3600);
    loader.sweep(at, [], [], DAY);
    assert!(loader.index.trips.contains_key("jdf:t-thu"));
    loader.sweep(later, [], [], DAY);
    assert!(loader.used.is_empty() && loader.index.trips.is_empty());
    assert!(loader.needs([&k], [], []));
}

/// `test_realtime_index._timetable` as serving rows.
fn two_lines() -> IndexLoader<ServingRows> {
    let mut b = Builder::new();
    b.route("jdf:r582492", Some("582492"), "bus");
    b.route("jdf:r582493", None, "bus");
    for stop in ["jdf:A", "jdf:B", "jdf:C", "jdf:D"] {
        b.location(stop, stop, "exact");
    }
    let h = |hh: i32, mm: i32| hh * 3600 + mm * 60;
    b.trip(
        "jdf:t1",
        "jdf:r582492",
        &[DAY],
        None,
        true,
        1,
        &[
            call("jdf:A", h(23, 50)),
            call("jdf:B", h(24, 5)),
            call("jdf:A", h(24, 20)),
        ],
    );
    b.trip(
        "jdf:t2",
        "jdf:r582492",
        &[DAY, NEXT],
        None,
        true,
        1,
        &[call("jdf:B", h(6, 0)), call("jdf:C", h(6, 20))],
    );
    b.trip(
        "jdf:t3",
        "jdf:r582493",
        &[NEXT],
        None,
        true,
        1,
        &[call("jdf:C", h(7, 0)), call("jdf:D", h(7, 10))],
    );
    b.key("trip", "cis:line_trip", "582492:143", "jdf:t1")
        .key("trip", "cis:line_trip", "582492:144", "jdf:t2")
        .key("trip", "cis:line_trip", "582493:1", "jdf:t3")
        .key("route", "cis:line", "582492", "jdf:r582492")
        .key("route", "cis:line", "582493", "jdf:r582493");
    b.loader(Feed::Jdf)
}

#[test]
fn loaded_trips_carry_visit_numbers_and_the_route_name_fallback() {
    let mut loader = two_lines();
    let refs = [
        key("cis:line_trip", "582492:143"),
        key("cis:line_trip", "582492:144"),
    ];
    let index = loader
        .ensure(&refs, [DAY.add_days(-1), DAY, NEXT], [])
        .unwrap();
    let t1 = index.trip("jdf:t1").unwrap();
    let visits: Vec<u32> = t1.calls.iter().map(|c| c.visit_n).collect();
    assert_eq!(visits, [1, 1, 2]);
    assert_eq!(&*t1.route_name, "582492");
    assert_eq!(index.shape("jdf:t1:shape").unwrap().points.len(), 3);
    assert!(index.runs_on("jdf:t2:service", NEXT).unwrap());
    assert_eq!(index.keys("cis:line", "582492").unwrap().len(), 1);
    assert!(!index.trips.contains_key("jdf:t3")); // loaded lazily, only when asked for
    let refs = [key("cis:line_trip", "582493:1")];
    let index = loader.ensure(&refs, [], []).unwrap();
    assert_eq!(&*index.trip("jdf:t3").unwrap().route_name, "jdf:r582493");
}

#[test]
fn unknown_keys_are_remembered_and_dates_extend() {
    let mut loader = two_lines();
    let refs = [
        key("cis:line_trip", "999999:1"),
        key("cis:line_trip", "582492:9"),
    ];
    let index = loader.ensure(&refs, [DAY], []).unwrap();
    assert!(index.keys("cis:line_trip", "999999:1").unwrap().is_empty());
    assert!(index.keys("cis:line", "999999").unwrap().is_empty());
    assert!(index.keys("cis:line_trip", "582492:9").unwrap().is_empty());
    assert!(!index.keys("cis:line", "582492").unwrap().is_empty());
    let refs = [key("cis:line_trip", "582492:144")];
    loader.ensure(&refs, [DAY], []).unwrap();
    assert!(loader.index.runs_on("jdf:t2:service", DAY).unwrap());
    loader.ensure([], [NEXT], []).unwrap();
    assert!(loader.index.runs_on("jdf:t2:service", NEXT).unwrap());
}

#[test]
fn a_keyless_line_loads_its_trip_keys_and_trips() {
    let mut loader = two_lines();
    let lines = [key("cis:line", "582492"), key("cis:line", "999999")];
    let index = loader.ensure([], [DAY], &lines).unwrap();
    let keys: Vec<&str> = index
        .line_keys("cis:line", "582492")
        .unwrap()
        .iter()
        .map(|k| &**k)
        .collect();
    assert_eq!(keys, ["582492:143", "582492:144"]);
    assert!(index.line_keys("cis:line", "999999").unwrap().is_empty());
    assert!(index.keys("cis:line", "999999").unwrap().is_empty()); // inference says no_line
    assert_eq!(trip_ids(&index.trips), ["jdf:t1", "jdf:t2"]);
    assert!(index.keys("cis:line_trip", "582493:1").is_err()); // another line stays unloaded
}

#[test]
fn rail_trips_load_their_run_train_numbers_and_sr70_keys() {
    let (station, point) = ("czptt:location:CZ54000", "czptt:location:CZ54001");
    let track = format!("{station}:platform:2");
    let run_key = "Pa:0054:KT----06600A:00:2026";
    let day = Date::ymd(2026, 10, 7);
    let mut b = Builder::new();
    b.route("czptt:route:1", None, "rail")
        .location(station, "Česká Lípa hl.n.", "exact")
        .location(point, "Česká Lípa střelnice", "exact")
        .location(&track, "Česká Lípa hl.n.", "missing");
    let mut first = call(station, 6 * 3600 + 360);
    first.point = Some(&track); // not exactly placed: no post
    let mut through = call(point, 6 * 3600 + 480);
    through.passenger = false;
    through.pickup = 1;
    b.trip(
        "czptt:trip:1:1",
        "czptt:route:1",
        &[day],
        Some((run_key, 1)),
        false,
        1,
        &[first, call(point, 6 * 3600 + 480)],
    );
    b.trip(
        "czptt:trip:1:2",
        "czptt:route:1",
        &[day],
        Some((run_key, 2)),
        false,
        2,
        &[through],
    );
    b.key("trip", "czptt:train_number", "6600", "czptt:trip:1:1")
        .key("trip", "czptt:train_number", "16600", "czptt:trip:1:2")
        .key("location", "sr70", "54000", station)
        .key("location", "sr70", "54001", point)
        .key("location", "sr70:track", "54000:2", &track);
    let mut loader = b.loader(Feed::Czptt);

    let refs = [key("czptt:train_number", "6600")];
    let index = loader.ensure(&refs, [day], []).unwrap();

    let parts: Vec<&str> = index.runs[run_key].iter().map(|p| &**p).collect();
    assert_eq!(parts, ["czptt:trip:1:1", "czptt:trip:1:2"]);
    let part = index.trip("czptt:trip:1:1").unwrap().clone();
    assert_eq!(
        (
            part.run_key.as_deref(),
            part.run_part,
            part.train_number.as_deref()
        ),
        (Some(run_key), Some(1), Some("6600"))
    );
    assert_eq!(part.calls[0].point_id, None);
    let run = index.run(&part, day).unwrap();
    let spans: Vec<_> = run
        .parts
        .iter()
        .map(|p| (&*p.trip_id, p.train_number.as_deref(), p.first, p.last))
        .collect();
    assert_eq!(
        spans,
        [
            ("czptt:trip:1:1", Some("6600"), 0, 1),
            ("czptt:trip:1:2", Some("16600"), 1, 1),
        ]
    );
    assert_eq!(
        index.location_key(point, "sr70").map(|k| &**k),
        Some("54001")
    );
    let tracks: Vec<&str> = index
        .keyed_locations("sr70:track", "54000:2")
        .iter()
        .map(|l| &**l)
        .collect();
    assert_eq!(tracks, [track.as_str()]);
}
