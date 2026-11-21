//! Real-journey traces (`tests/realtime/corpus/*.json.gz`, `tests/realtime/traces.py`'s
//! `from_json`): an index slice and the journey's observations, rebuilt as Python rebuilds them.
//! A trace may also carry `trip_services` (trips named but not loaded), which the corpus format
//! lacks.

use std::io::Read;
use std::path::Path;
use std::sync::Arc;

use obehy_core::index::{Call, DateSet, Index, KeyEntry, KeyRef, Location, Shape, Trip};
use obehy_core::model::json::facts_from_json;
use obehy_core::model::{Feed, Observation, RawRef};
use obehy_core::time::{Date, Instant};
use serde_json::Value;

pub fn read_gz_json(path: &Path) -> Value {
    let file = std::fs::File::open(path).unwrap_or_else(|e| panic!("{}: {e}", path.display()));
    let mut text = String::new();
    flate2::read::GzDecoder::new(file)
        .read_to_string(&mut text)
        .unwrap_or_else(|e| panic!("{}: {e}", path.display()));
    serde_json::from_str(&text).unwrap_or_else(|e| panic!("{}: {e}", path.display()))
}

fn s(value: &Value) -> &str {
    value.as_str().expect("a string")
}

fn opt_s(value: &Value) -> Option<&str> {
    value.as_str()
}

fn date(value: &Value) -> Date {
    Date::from_isoformat(s(value)).expect("an ISO date")
}

fn int(value: &Value) -> i64 {
    value.as_i64().expect("an integer")
}

pub fn observation(o: &Value) -> Observation {
    Observation {
        source: s(&o["source"]).to_owned(),
        channel: s(&o["channel"]).to_owned(),
        feed: s(&o["feed"]).parse::<Feed>().expect("a feed"),
        received_at: Instant::from_isoformat(s(&o["received_at"])).expect("an instant"),
        observed_at: opt_s(&o["observed_at"])
            .map(|t| Instant::from_isoformat(t).expect("an instant")),
        raw: RawRef {
            sha256: s(&o["sha256"]).to_owned(),
            item: int(&o["item"]),
        },
        decoder_version: int(&o["decoder_version"]),
        facts: facts_from_json(&o["facts"]).expect("facts"),
    }
}

/// The trace's index and observations.
pub fn from_json(data: &Value) -> (Index, Vec<Observation>) {
    let feed = s(&data["feed"]).parse::<Feed>().expect("a feed");
    let mut index = Index::new(s(&data["release_id"]), feed);
    for item in data["keys"].as_array().unwrap() {
        let r = &item[0];
        let key = index.intern(&KeyRef::new(s(&r[0]), s(&r[1])));
        let entries: Vec<KeyEntry> = item[1]
            .as_array()
            .unwrap()
            .iter()
            .map(|e| KeyEntry {
                public_id: index.pool.id(s(&e[0])),
                valid_from: date(&e[1]),
                valid_to: date(&e[2]),
            })
            .collect();
        index.key_entries.insert(key, entries.into());
    }
    for item in data
        .get("lines")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        let r = &item[0];
        let key = index.intern(&KeyRef::new(s(&r[0]), s(&r[1])));
        let keys: Vec<Arc<str>> = item[1]
            .as_array()
            .unwrap()
            .iter()
            .map(|k| index.pool.id(s(k)))
            .collect();
        index.lines.insert(key, keys.into());
    }
    for t in data["trips"].as_array().unwrap() {
        let mut calls = Vec::new();
        for c in t["calls"].as_array().unwrap() {
            calls.push(Call {
                sequence: int(&c[0]) as i32,
                location_id: index.pool.id(s(&c[1])),
                visit_n: int(&c[2]) as u32,
                passenger_service: c[3].as_bool().unwrap(),
                arrival: c[4].as_i64().map(|v| v as i32),
                departure: c[5].as_i64().map(|v| v as i32),
                // Older traces stop before `on_request` and `point_id` (their defaults).
                on_request: c.get(6).and_then(Value::as_bool).unwrap_or(false),
                point_id: c.get(7).and_then(Value::as_str).map(|p| index.pool.id(p)),
            });
        }
        let pool = &mut index.pool;
        let trip = Trip {
            trip_id: pool.id(s(&t["trip_id"])),
            route_id: pool.id(s(&t["route_id"])),
            route_name: pool.id(s(&t["route_name"])),
            mode: pool.id(s(&t["mode"])),
            service_id: pool.id(s(&t["service_id"])),
            headsign: pool.opt(opt_s(&t["headsign"])),
            shape_id: pool.opt(opt_s(&t["shape_id"])),
            calls: calls.into(),
            run_key: pool.opt(t.get("run_key").and_then(Value::as_str)),
            run_part: t.get("run_part").and_then(Value::as_i64).map(|p| p as i16),
            train_number: pool.opt(t.get("train_number").and_then(Value::as_str)),
            parts: Box::new([]),
            agency_id: pool.opt(t.get("agency_id").and_then(Value::as_str)),
        };
        index.trips.insert(trip.trip_id.clone(), Arc::new(trip));
    }
    for item in data
        .get("runs")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        let run = index.pool.id(s(&item[0]));
        let parts: Vec<Arc<str>> = item[1]
            .as_array()
            .unwrap()
            .iter()
            .map(|p| index.pool.id(s(p)))
            .collect();
        index.runs.insert(run, parts.into());
    }
    for item in data
        .get("location_keys")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        index.add_location_key(s(&item[0]), s(&item[1]), s(&item[2]));
    }
    for (service, days) in data["service_dates"].as_object().unwrap() {
        let mut set = DateSet::default();
        for d in days.as_array().unwrap() {
            set.insert(date(d));
        }
        let service = index.pool.id(service);
        index.service_dates.insert(service, set);
    }
    for d in data["loaded_dates"].as_array().unwrap() {
        index.loaded_dates.insert(date(d));
    }
    for loc in data["locations"].as_array().unwrap() {
        let id = index.pool.id(s(&loc[0]));
        let location = Location {
            location_id: id.clone(),
            name: index.pool.id(s(&loc[1])),
            lon: loc[2].as_f64(),
            lat: loc[3].as_f64(),
        };
        index.locations.insert(id, location);
    }
    for shape in data["shapes"].as_array().unwrap() {
        let id = index.pool.id(s(&shape["shape_id"]));
        let points: Vec<(f64, f64)> = shape["points"]
            .as_array()
            .unwrap()
            .iter()
            .map(|p| (p[0].as_f64().unwrap(), p[1].as_f64().unwrap()))
            .collect();
        let distances: Vec<f64> = shape["distances_m"]
            .as_array()
            .unwrap()
            .iter()
            .map(|d| d.as_f64().unwrap())
            .collect();
        index.shapes.insert(
            id.clone(),
            Arc::new(Shape {
                shape_id: id,
                points: points.into(),
                distances_m: distances.into(),
            }),
        );
    }
    if let Some(services) = data.get("trip_services").and_then(Value::as_object) {
        for (trip, service) in services {
            let (trip, service) = (index.pool.id(trip), index.pool.id(s(service)));
            index.trip_services.insert(trip, service);
        }
    }
    let observations = data["observations"]
        .as_array()
        .unwrap()
        .iter()
        .map(observation)
        .collect();
    (index, observations)
}
