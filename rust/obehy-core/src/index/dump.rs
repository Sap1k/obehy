//! A canonical JSON form of an index's contents, for the cross-check against Python
//! (`rust/tools/index_crosscheck.py` writes the same from `index.Index`).
//!
//! Everything sorted; floats as Python's `repr` text, so equality is exact. Besides the maps it
//! records what the core derives from them: the trips running on each loaded date, the dormant
//! trips, and for each run trip the calls at each of its stations.

use super::{Index, IndexMiss, calls_at_station};
use crate::py::json::PyJson;
use crate::py::num::py_float_repr;
use crate::time::Date;

fn s(text: &str) -> PyJson {
    PyJson::str(text)
}

fn opt_s(text: Option<&str>) -> PyJson {
    text.map_or(PyJson::Null, s)
}

fn int(n: impl Into<i64>) -> PyJson {
    PyJson::Int(n.into())
}

fn float(x: f64) -> PyJson {
    PyJson::str(py_float_repr(x))
}

fn date(d: Date) -> PyJson {
    PyJson::str(d.isoformat())
}

fn list<T>(items: impl IntoIterator<Item = T>, f: impl FnMut(T) -> PyJson) -> PyJson {
    PyJson::List(items.into_iter().map(f).collect())
}

fn usize_int(n: usize) -> PyJson {
    PyJson::Int(i64::try_from(n).expect("an index"))
}

/// The index's contents as canonical JSON.
pub fn dump(index: &Index) -> PyJson {
    let loaded: Vec<Date> = index.loaded_dates.iter().copied().collect();
    let keys = list(index.key_entries.sorted(), |(key, entries)| {
        PyJson::List(vec![
            s(&key.namespace),
            s(&key.identifier),
            list(entries.iter(), |e| {
                PyJson::List(vec![s(&e.public_id), date(e.valid_from), date(e.valid_to)])
            }),
        ])
    });
    let lines = list(index.lines.sorted(), |(key, ids)| {
        PyJson::List(vec![
            s(&key.namespace),
            s(&key.identifier),
            list(ids.iter(), |i| s(i)),
        ])
    });
    let mut trip_services: Vec<_> = index.trip_services.iter().collect();
    trip_services.sort();
    let mut service_dates: Vec<_> = index.service_dates.iter().collect();
    service_dates.sort_by(|a, b| a.0.cmp(b.0));
    let mut runs: Vec<_> = index.runs.iter().collect();
    runs.sort_by(|a, b| a.0.cmp(b.0));
    let mut trips: Vec<_> = index.trips.values().collect();
    trips.sort_by(|a, b| a.trip_id.cmp(&b.trip_id));
    let mut locations: Vec<_> = index.locations.values().collect();
    locations.sort_by(|a, b| a.location_id.cmp(&b.location_id));
    let mut shapes: Vec<_> = index.shapes.values().collect();
    shapes.sort_by(|a, b| a.shape_id.cmp(&b.shape_id));
    let mut location_keys: Vec<(&str, &str, &str)> = index
        .location_keys
        .iter()
        .flat_map(|(l, keys)| keys.iter().map(move |(ns, i)| (&**l, &**ns, &**i)))
        .collect();
    location_keys.sort_unstable();

    let mut running = Vec::new();
    for (trip_id, _) in &trip_services {
        for day in &loaded {
            match index.running_trip(trip_id, *day) {
                Ok(Some(_)) => running.push(PyJson::List(vec![s(trip_id), date(*day)])),
                Ok(None) => {}
                Err(IndexMiss(_)) => {
                    running.push(PyJson::List(vec![s(trip_id), date(*day), s("miss")]));
                }
            }
        }
    }
    let mut station_calls = Vec::new();
    for trip in trips.iter().filter(|t| !t.parts.is_empty()) {
        let mut codes: Vec<&str> = trip
            .calls
            .iter()
            .filter_map(|c| index.location_key(&c.location_id, "sr70").map(|k| &**k))
            .collect();
        codes.sort_unstable();
        codes.dedup();
        for code in codes {
            let wanted = [code];
            station_calls.push(PyJson::List(vec![
                s(&trip.trip_id),
                s(code),
                list(calls_at_station(trip, index, &wanted, 0, false), usize_int),
                list(calls_at_station(trip, index, &wanted, 1, true), usize_int),
            ]));
        }
    }

    PyJson::object([
        ("release_id", s(&index.release_id)),
        ("feed", s(index.feed.as_str())),
        ("loaded_dates", list(loaded.iter().copied(), date)),
        ("keys", keys),
        ("lines", lines),
        (
            "trip_services",
            list(trip_services, |(t, sv)| PyJson::List(vec![s(t), s(sv)])),
        ),
        (
            "service_dates",
            list(service_dates, |(sv, days)| {
                PyJson::List(vec![s(sv), list(days.iter(), date)])
            }),
        ),
        (
            "runs",
            list(runs, |(r, parts)| {
                PyJson::List(vec![s(r), list(parts.iter(), |p| s(p))])
            }),
        ),
        (
            "trips",
            list(trips, |t| {
                PyJson::List(vec![
                    s(&t.trip_id),
                    s(&t.route_id),
                    s(&t.route_name),
                    s(&t.mode),
                    s(&t.service_id),
                    opt_s(t.headsign.as_deref()),
                    opt_s(t.shape_id.as_deref()),
                    opt_s(t.run_key.as_deref()),
                    PyJson::opt(t.run_part, int),
                    opt_s(t.train_number.as_deref()),
                    opt_s(t.agency_id.as_deref()),
                    list(t.calls.iter(), |c| {
                        PyJson::List(vec![
                            int(c.sequence),
                            s(&c.location_id),
                            int(c.visit_n),
                            PyJson::Bool(c.passenger_service),
                            PyJson::opt(c.arrival, int),
                            PyJson::opt(c.departure, int),
                            PyJson::Bool(c.on_request),
                            opt_s(c.point_id.as_deref()),
                            opt_s(index.location_key(&c.location_id, "sr70").map(|k| &**k)),
                        ])
                    }),
                    list(t.parts.iter(), |p| {
                        PyJson::List(vec![
                            s(&p.trip_id),
                            opt_s(p.train_number.as_deref()),
                            usize_int(p.first),
                            usize_int(p.last),
                        ])
                    }),
                ])
            }),
        ),
        (
            "locations",
            list(locations, |l| {
                PyJson::List(vec![
                    s(&l.location_id),
                    s(&l.name),
                    PyJson::opt(l.lon, float),
                    PyJson::opt(l.lat, float),
                ])
            }),
        ),
        (
            "shapes",
            list(shapes, |sh| {
                PyJson::List(vec![
                    s(&sh.shape_id),
                    list(sh.points.iter().zip(sh.distances_m.iter()), |(p, d)| {
                        PyJson::List(vec![float(p.0), float(p.1), float(*d)])
                    }),
                    usize_int(sh.distances_m.len()),
                ])
            }),
        ),
        (
            "location_keys",
            list(location_keys, |(l, ns, i)| {
                PyJson::List(vec![s(l), s(ns), s(i)])
            }),
        ),
        (
            "keyed",
            list(index.keyed.sorted(), |(key, ids)| {
                PyJson::List(vec![
                    s(&key.namespace),
                    s(&key.identifier),
                    list(ids.iter(), |i| s(i)),
                ])
            }),
        ),
        ("running", PyJson::List(running)),
        (
            "dormant",
            list(index.dormant(&index.loaded_dates), |t| s(&t)),
        ),
        ("station_calls", PyJson::List(station_calls)),
    ])
}

/// Sizes of the index (the bench's `index_*` counts).
pub fn counts(index: &Index) -> PyJson {
    let n = |x: usize| usize_int(x);
    PyJson::object([
        ("keys", n(index.key_entries.len())),
        (
            "absent_keys",
            n(index
                .key_entries
                .iter()
                .filter(|(_, e)| e.is_empty())
                .count()),
        ),
        ("trips", n(index.trips.len())),
        (
            "calls",
            n(index.trips.values().map(|t| t.calls.len()).sum()),
        ),
        ("shapes", n(index.shapes.len())),
        (
            "shape_points",
            n(index.shapes.values().map(|s| s.points.len()).sum()),
        ),
        ("dates", n(index.loaded_dates.len())),
        ("trip_services", n(index.trip_services.len())),
        ("locations", n(index.locations.len())),
        ("runs", n(index.runs.len())),
    ])
}
