//! The index (R3): the sweep (`tests/unit/realtime/test_index_sweep.py`), run trips
//! (`test_rail_runs.py`'s index part) and station calls, over an index built directly as
//! `tests/realtime/builder.py`'s `Timetable.index()` builds it. The loader's tests, which read
//! rows, are in obehy-rt (`tests/index_loader.rs`).

use std::collections::BTreeSet;
use std::sync::Arc;

use obehy_core::geo::cumulative_m;
use obehy_core::index::{
    Call, DateSet, Index, KeyEntry, KeyRef, Location, Shape, Trip, calls_at_station, parent_ref,
};
use obehy_core::model::Feed;
use obehy_core::time::Date;

const D7: Date = Date::ymd(2026, 10, 7);
const D8: Date = Date::ymd(2026, 10, 8);

fn hhmm(text: &str) -> i32 {
    let (h, m) = text.split_once(':').expect("hh:mm");
    h.parse::<i32>().unwrap() * 3600 + m.parse::<i32>().unwrap() * 60
}

/// One trip of the timetable: `calls` are (spec, arrival, departure) with the builder's
/// spec syntax ("~X" a railway point, "X@P" served at post P).
struct Spec<'a> {
    key: &'a str,
    trip_id: Option<&'a str>,
    days: &'a [Date],
    calls: &'a [(&'a str, &'a str, &'a str)],
    run: Option<(&'a str, i16)>,
    first_sequence: i32,
    shape: bool,
}

impl<'a> Spec<'a> {
    fn new(key: &'a str, days: &'a [Date], calls: &'a [(&'a str, &'a str, &'a str)]) -> Self {
        Spec {
            key,
            trip_id: None,
            days,
            calls,
            run: None,
            first_sequence: 1,
            shape: true,
        }
    }
}

/// `Timetable.index()`: every key, trip and service date loaded.
fn timetable(feed: Feed, namespace: &str, specs: &[Spec<'_>]) -> Index {
    let f = feed.as_str();
    let mut index = Index::new("test-release", feed);
    let mut stops: Vec<String> = Vec::new();
    let mut all_days: BTreeSet<Date> = BTreeSet::new();
    let mut location = |index: &mut Index, name: &str| -> Arc<str> {
        let id = format!("{f}:{name}");
        if !stops.contains(&id) {
            let n = stops.len() as f64;
            stops.push(id.clone());
            let id: Arc<str> = index.pool.id(&id);
            index.locations.insert(
                id.clone(),
                Location {
                    location_id: id,
                    name: Arc::from(name),
                    lon: Some(14.0 + n * 0.01),
                    lat: Some(50.0),
                },
            );
        }
        index.pool.id(&id)
    };
    for (n, spec) in specs.iter().enumerate() {
        let trip_id = spec
            .trip_id
            .map_or_else(|| format!("{f}:t{}", n + 1), str::to_owned);
        let mut calls = Vec::new();
        let mut visits: Vec<Arc<str>> = Vec::new();
        for (i, (name, arrival, departure)) in spec.calls.iter().enumerate() {
            let (name, post) = name
                .trim_start_matches(['~', '?'])
                .split_once('@')
                .map_or((name.trim_start_matches(['~', '?']), None), |(a, b)| {
                    (a, Some(b))
                });
            let location_id = location(&mut index, name);
            let point_id = post.map(|p| location(&mut index, p));
            visits.push(location_id.clone());
            let visit = visits.iter().filter(|v| **v == location_id).count();
            let time = |t: &str| (!t.is_empty()).then(|| hhmm(t));
            calls.push(Call {
                sequence: spec.first_sequence + i as i32,
                location_id,
                point_id,
                arrival: time(arrival),
                departure: time(departure),
                visit_n: visit as u32,
                passenger_service: !spec.calls[i].0.starts_with('~'),
                on_request: spec.calls[i].0.starts_with('?'),
            });
        }
        let shape_id = spec.shape.then(|| format!("{trip_id}:shape"));
        if let Some(shape_id) = &shape_id {
            let points: Vec<(f64, f64)> = calls
                .iter()
                .map(|c: &Call| {
                    let l = &index.locations[c.placed_at()];
                    (l.lon.unwrap(), l.lat.unwrap())
                })
                .collect();
            let shape = Shape {
                shape_id: index.pool.id(shape_id),
                distances_m: cumulative_m(&points).into(),
                points: points.into(),
            };
            index
                .shapes
                .insert(index.pool.id(shape_id), Arc::new(shape));
        }
        let line = spec.key.split_once(':').map_or(spec.key, |(l, _)| l);
        let service_id = index.pool.id(&format!("{trip_id}:service"));
        let trip = Trip {
            trip_id: index.pool.id(&trip_id),
            route_id: index.pool.id(&format!("{f}:r{line}")),
            route_name: index.pool.id(line),
            mode: index
                .pool
                .id(if feed == Feed::Czptt { "rail" } else { "bus" }),
            service_id: service_id.clone(),
            headsign: None,
            shape_id: shape_id.map(|s| index.pool.id(&s)),
            calls: calls.into(),
            run_key: spec.run.map(|(r, _)| index.pool.id(r)),
            run_part: spec.run.map(|(_, p)| p),
            train_number: None,
            parts: Box::new([]),
            agency_id: Some(index.pool.id(&format!("{f}:agency:1"))),
        };
        if let Some((run, _)) = spec.run {
            let run = index.pool.id(run);
            let mut parts: Vec<Arc<str>> =
                index.runs.get(&run).map(|p| p.to_vec()).unwrap_or_default();
            parts.push(trip.trip_id.clone());
            index.runs.insert(run, parts.into());
        }
        let mut dates = DateSet::default();
        for day in spec.days {
            dates.insert(*day);
            all_days.insert(*day);
        }
        index.service_dates.insert(service_id, dates);
        let key = index.intern(&KeyRef::new(namespace, spec.key));
        let entry = KeyEntry {
            public_id: trip.trip_id.clone(),
            valid_from: Date::ymd(2026, 1, 1),
            valid_to: Date::ymd(2026, 12, 31),
        };
        let mut entries = index
            .key_entries
            .get_ref(&key)
            .map(|e| e.to_vec())
            .unwrap_or_default();
        entries.push(entry);
        index.key_entries.insert(key.clone(), entries.into());
        if let Some(parent) = parent_ref(&key) {
            let parent = index.intern(&parent);
            let route = KeyEntry {
                public_id: trip.route_id.clone(),
                valid_from: Date::ymd(2026, 1, 1),
                valid_to: Date::ymd(2026, 12, 31),
            };
            let mut routes = index
                .key_entries
                .get_ref(&parent)
                .map(|e| e.to_vec())
                .unwrap_or_default();
            if !routes.contains(&route) {
                routes.push(route);
            }
            index.key_entries.insert(parent.clone(), routes.into());
            let mut keys: BTreeSet<Arc<str>> = index
                .lines
                .get_ref(&parent)
                .map(|l| l.iter().cloned().collect())
                .unwrap_or_default();
            keys.insert(key.identifier.clone());
            index
                .lines
                .insert(parent, keys.into_iter().collect::<Vec<_>>().into());
        }
        index.trips.insert(trip.trip_id.clone(), Arc::new(trip));
    }
    let (first, last) = (*all_days.first().unwrap(), *all_days.last().unwrap());
    let mut day = first.add_days(-3);
    while day <= last.add_days(3) {
        index.loaded_dates.insert(day);
        day = day.add_days(1);
    }
    index
}

fn sweep_index() -> Index {
    let d8 = [D8];
    timetable(
        Feed::Jdf,
        "cis:line_trip",
        &[
            Spec::new(
                "582492:143",
                &d8,
                &[("A", "08:00", "08:00"), ("B@B2", "08:05", "08:05")],
            ),
            Spec::new(
                "582492:145",
                &d8,
                &[("C", "09:00", "09:00"), ("D", "09:05", "09:05")],
            ),
            Spec {
                run: Some(("PA1", 1)),
                ..Spec::new(
                    "600001:1",
                    &d8,
                    &[("E", "10:00", "10:00"), ("F", "10:05", "10:05")],
                )
            },
            Spec {
                run: Some(("PA1", 2)),
                ..Spec::new(
                    "600001:2",
                    &d8,
                    &[("F", "10:05", "10:05"), ("G", "10:10", "10:10")],
                )
            },
        ],
    )
}

fn set<'a>(items: impl IntoIterator<Item = &'a Arc<str>>) -> BTreeSet<String> {
    items.into_iter().map(|s| s.to_string()).collect()
}

fn strings(items: &[&str]) -> BTreeSet<String> {
    items.iter().map(|s| s.to_string()).collect()
}

#[test]
fn a_sweep_keeps_what_kept_keys_and_live_trips_need() {
    let mut index = sweep_index();
    let key = KeyRef::new("cis:line_trip", "582492:143");
    let before = index.keys("cis:line_trip", "582492:143").unwrap().to_vec();
    index.sweep([&key], [], ["jdf:t3"], D7); // the runner: from yesterday

    // The kept key with its line, the live trip with every part of its run; a call's post
    // stays with its stop place, or planning the trip fails on it.
    let keys: BTreeSet<KeyRef> = index.key_entries.iter().map(|(k, _)| k).collect();
    assert_eq!(
        keys,
        BTreeSet::from([
            KeyRef::new("cis:line", "582492"),
            KeyRef::new("cis:line_trip", "582492:143"),
        ])
    );
    assert_eq!(
        set(index.trips.keys()),
        strings(&["jdf:t1", "jdf:t3", "jdf:t4"])
    );
    assert_eq!(set(index.runs.keys()), strings(&["PA1"]));
    assert_eq!(
        set(index.shapes.keys()),
        strings(&["jdf:t1:shape", "jdf:t3:shape", "jdf:t4:shape"])
    );
    let locations: BTreeSet<String> = index
        .locations
        .keys()
        .map(|l| l.split(':').nth(1).unwrap().to_owned())
        .collect();
    assert_eq!(locations, strings(&["A", "B", "B2", "E", "F", "G"]));
    assert_eq!(
        set(index.service_dates.keys()),
        strings(&["jdf:t1:service", "jdf:t3:service", "jdf:t4:service"])
    );
    assert_eq!(index.loaded_dates.first(), Some(&D7));
    // What was kept reads as before; what was dropped is no longer loaded, so the loader
    // loads it again when an observation names it.
    assert_eq!(index.keys("cis:line_trip", "582492:143").unwrap(), before);
    let dropped = KeyRef::new("cis:line_trip", "582492:145");
    assert_eq!(index.missing([&dropped]), BTreeSet::from([dropped.clone()]));
    // The pool forgets the IDs only the dropped trips held.
    assert!(index.pool.len() < 40, "{}", index.pool.len());
}

#[test]
fn a_kept_line_keeps_its_trips_keys() {
    let mut index = sweep_index();
    let line = KeyRef::new("cis:line", "582492");
    index.sweep([], [&line], [], D8);
    assert_eq!(set(index.trips.keys()), strings(&["jdf:t1", "jdf:t2"]));
    let lines: Vec<_> = index.lines.sorted();
    assert_eq!(lines.len(), 1);
    assert_eq!(lines[0].0, line);
    assert_eq!(
        set(lines[0].1.iter()),
        strings(&["582492:143", "582492:145"])
    );
}

/// `test_rail_runs._run`: A ~X B | B C, part 1 (A..B, X a railway point) and part 2 (B..C).
fn rail_index(extra_part: bool) -> Index {
    let d8 = [D8];
    let mut specs = vec![
        Spec {
            trip_id: Some("czptt:trip:PA1:1"),
            run: Some(("PA1", 1)),
            shape: false,
            ..Spec::new(
                "106006",
                &d8,
                &[
                    ("A", "08:00", "08:00"),
                    ("~X", "08:05", "08:05"),
                    ("B", "08:10", "08:10"),
                ],
            )
        },
        Spec {
            trip_id: Some("czptt:trip:PA1:2"),
            run: Some(("PA1", 2)),
            shape: false,
            first_sequence: 3,
            ..Spec::new(
                "6006",
                &d8,
                &[("B", "08:10", "08:12"), ("C", "08:20", "08:20")],
            )
        },
    ];
    if extra_part {
        specs.push(Spec {
            trip_id: Some("czptt:trip:PA1:2b"),
            run: Some(("PA1", 2)),
            shape: false,
            first_sequence: 3,
            ..Spec::new(
                "6006",
                &d8,
                &[("B", "08:10", "08:12"), ("C", "08:21", "08:21")],
            )
        });
    }
    let mut index = timetable(Feed::Czptt, "czptt:train_number", &specs);
    index.add_location_key("czptt:A", "sr70", "54000");
    index.add_location_key("czptt:B", "sr70", "54001");
    index.add_location_key("czptt:X", "sr70", "54001");
    index
}

#[test]
fn a_run_joins_its_parts_at_the_shared_junction() {
    let mut index = rail_index(false);
    let part = index.trip("czptt:trip:PA1:1").unwrap().clone();
    let run = index.run(&part, D8).unwrap();
    assert_eq!(&*run.trip_id, "run:PA1:1,2");
    let calls: Vec<_> = run
        .calls
        .iter()
        .map(|c| {
            (
                c.sequence,
                &*c.location_id,
                c.visit_n,
                c.passenger_service,
                c.arrival,
                c.departure,
            )
        })
        .collect();
    let t = |s: &str| Some(hhmm(s));
    assert_eq!(
        calls,
        vec![
            (1, "czptt:A", 1, true, t("08:00"), t("08:00")),
            (2, "czptt:X", 1, false, t("08:05"), t("08:05")),
            (3, "czptt:B", 1, true, t("08:10"), t("08:12")),
            (4, "czptt:C", 1, true, t("08:20"), t("08:20")),
        ]
    );
    let parts: Vec<_> = run
        .parts
        .iter()
        .map(|p| (&*p.trip_id, p.first, p.last))
        .collect();
    assert_eq!(
        parts,
        vec![("czptt:trip:PA1:1", 0, 2), ("czptt:trip:PA1:2", 2, 3)]
    );
    assert!(run.shape_id.is_none() && run.run_part.is_none());
    // Built once, then cached among the trips; a run trip is its own run.
    assert!(Arc::ptr_eq(&index.run(&part, D8).unwrap(), &run));
    assert!(Arc::ptr_eq(
        &index.trip("run:PA1:1,2").unwrap().clone(),
        &run
    ));
    assert!(Arc::ptr_eq(&index.run(&run, D8).unwrap(), &run));

    // Station calls: by SR70 code, from `start`, passenger calls only if asked.
    let at = |codes: &[&str], start: i64, passenger: bool| -> Vec<usize> {
        calls_at_station(&run, &index, codes, start, passenger).collect()
    };
    assert_eq!(at(&["54001"], 0, false), vec![1, 2]);
    assert_eq!(at(&["54001"], 0, true), vec![2]);
    assert_eq!(at(&["54001", "54000"], -5, false), vec![0, 1, 2]);
    assert_eq!(at(&["54001"], 2, false), vec![2]);
    assert_eq!(at(&["99999"], 0, false), Vec::<usize>::new());
}

#[test]
fn two_versions_of_one_part_running_the_same_day_are_ambiguous() {
    let mut index = rail_index(true);
    let part = index.trip("czptt:trip:PA1:1").unwrap().clone();
    let run = index.run(&part, D8).unwrap();
    assert_eq!(&*run.trip_id, "run:PA1:ambiguous");
    assert!(run.parts.is_empty());
    assert!(!index.trips.contains_key("run:PA1:ambiguous"));
}

#[test]
fn misses_name_what_was_not_loaded() {
    let index = rail_index(false);
    assert_eq!(
        index.trip("nope").unwrap_err().to_string(),
        "trip nope was not loaded"
    );
    assert_eq!(
        index
            .runs_on("czptt:trip:PA1:1:service", Date::ymd(2027, 1, 1))
            .unwrap_err()
            .to_string(),
        "service dates for 2027-01-01 were not loaded"
    );
    assert!(index.runs_on("czptt:trip:PA1:1:service", D8).unwrap());
    assert!(!index.runs_on("czptt:trip:PA1:1:service", D7).unwrap());
    assert!(
        index
            .running_trip("czptt:trip:PA1:1", D7)
            .unwrap()
            .is_none()
    );
    assert_eq!(
        index.location_key("czptt:B", "sr70").map(|k| &**k),
        Some("54001")
    );
    assert_eq!(
        set(index.keyed_locations("sr70", "54001")),
        strings(&["czptt:B", "czptt:X"])
    );
    let call = &index.trip("czptt:trip:PA1:1").unwrap().calls[0];
    assert_eq!(call.time().unwrap(), hhmm("08:00"));
}
