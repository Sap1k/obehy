//! `tests/realtime/builder.py`: a tiny timetable for core tests, built into an [`Index`] the way
//! Python's `Timetable.index()` builds it.
//!
//! Stops are placed 1 km apart eastwards in order of first use. Times are wall-clock `HH:MM`
//! (may exceed 24:00); a call is `(name, time)` or `(name, arrival, departure)` with `""` for
//! none. `~X` is a railway point without passenger service, `?X` a request stop, `X@P` a call at
//! X served at its post P. Rail: `.run("PA", part)`, `.train("6006")`, `.first_sequence(n)`.

use std::collections::BTreeSet;
use std::sync::Arc;

use obehy_core::geo::cumulative_m;
use obehy_core::index::{
    Call, DateSet, Index, KeyEntry, KeyRef, Location, Shape, Trip, parent_ref,
};
use obehy_core::model::Feed;
use obehy_core::time::{Date, Instant, NaiveDateTime, wall_instant};

pub const ORIGIN: (f64, f64) = (14.0, 50.0);
/// About 1 km at 50° N.
pub const STEP_LON: f64 = 0.014;

pub fn hhmm(text: &str) -> i32 {
    let (h, m) = text.split_once(':').expect("HH:MM");
    h.parse::<i32>().unwrap() * 3600 + m.parse::<i32>().unwrap() * 60
}

/// `tests.realtime.observations.local`: `2026-10-09 00:20[:SS]` as Europe/Prague wall time.
pub fn local(text: &str) -> Instant {
    let (day, time) = text.split_once(' ').expect("date and time");
    let parts: Vec<u32> = time.split(':').map(|p| p.parse().unwrap()).collect();
    let wall = NaiveDateTime::new(
        Date::from_isoformat(day).unwrap(),
        parts[0],
        parts[1],
        parts.get(2).copied().unwrap_or(0),
        0,
    )
    .unwrap();
    wall_instant(wall)
}

pub fn days_between(first: Date, last: Date) -> Vec<Date> {
    (0..=last.days_since(first))
        .map(|n| first.add_days(n))
        .collect()
}

/// One call: (spec, arrival, departure); a two-field call has both times.
pub type CallSpec = (String, Option<i32>, Option<i32>);

pub fn at(name: &str, time: &str) -> CallSpec {
    (name.to_owned(), Some(hhmm(time)), Some(hhmm(time)))
}

pub fn at2(name: &str, arrival: &str, departure: &str) -> CallSpec {
    let t = |x: &str| (!x.is_empty()).then(|| hhmm(x));
    (name.to_owned(), t(arrival), t(departure))
}

#[derive(Debug, Clone)]
pub struct TripSpec {
    pub trip_id: String,
    pub namespace: String,
    pub key: String,
    pub days: Vec<Date>,
    pub calls: Vec<CallSpec>,
    pub mode: String,
    pub headsign: Option<String>,
    pub shape: bool,
    pub run: Option<(String, i16)>,
    pub train: Option<String>,
    pub first_sequence: i32,
}

impl TripSpec {
    pub fn trip_id(mut self, trip_id: &str) -> Self {
        self.trip_id = trip_id.to_owned();
        self
    }
    pub fn mode(mut self, mode: &str) -> Self {
        self.mode = mode.to_owned();
        self
    }
    pub fn no_shape(mut self) -> Self {
        self.shape = false;
        self
    }
    pub fn run(mut self, run: &str, part: i16) -> Self {
        self.run = Some((run.to_owned(), part));
        self
    }
    pub fn train(mut self, train: &str) -> Self {
        self.train = Some(train.to_owned());
        self
    }
    pub fn first_sequence(mut self, n: i32) -> Self {
        self.first_sequence = n;
        self
    }
}

#[derive(Debug)]
pub struct Timetable {
    pub feed: Feed,
    pub namespace: String,
    pub trips: Vec<TripSpec>,
    pub aliases: Vec<(String, String, String)>,
}

pub fn timetable(feed: Feed, namespace: &str) -> Timetable {
    Timetable {
        feed,
        namespace: namespace.to_owned(),
        trips: Vec::new(),
        aliases: Vec::new(),
    }
}

impl Timetable {
    /// A trip of the default namespace, `edit` for the keyword arguments.
    pub fn trip(
        &mut self,
        key: &str,
        days: &[Date],
        calls: Vec<CallSpec>,
        edit: impl FnOnce(TripSpec) -> TripSpec,
    ) -> &mut Self {
        let spec = TripSpec {
            trip_id: format!("{}:t{}", self.feed.as_str(), self.trips.len() + 1),
            namespace: self.namespace.clone(),
            key: key.to_owned(),
            days: days.to_vec(),
            headsign: Some(calls.last().expect("calls").0.clone()),
            calls,
            mode: "bus".to_owned(),
            shape: true,
            run: None,
            train: None,
            first_sequence: 1,
        };
        self.trips.push(edit(spec));
        self
    }

    /// A further source key naming trips (rail: a TR naming every part of a run).
    pub fn key(&mut self, namespace: &str, key: &str, trip_ids: &[&str]) -> &mut Self {
        for trip_id in trip_ids {
            self.aliases
                .push((namespace.to_owned(), key.to_owned(), (*trip_id).to_owned()));
        }
        self
    }

    /// The fully loaded index: every key, trip and service date of the timetable.
    pub fn index(&self) -> Index {
        let f = self.feed.as_str();
        let mut index = Index::new("test-release", self.feed);
        let mut stops: Vec<String> = Vec::new();
        let location = |index: &mut Index, stops: &mut Vec<String>, name: &str| -> Arc<str> {
            let id = format!("{f}:{name}");
            if !stops.contains(&id) {
                let n = stops.len() as f64;
                stops.push(id.clone());
                let id = index.pool.id(&id);
                let name = index.pool.id(name);
                index.locations.insert(
                    id.clone(),
                    Location {
                        location_id: id,
                        name,
                        lon: Some(ORIGIN.0 + n * STEP_LON),
                        lat: Some(ORIGIN.1),
                    },
                );
            }
            index.pool.id(&id)
        };
        let valid_from = Date::ymd(2026, 1, 1);
        let valid_to = Date::ymd(2026, 12, 31);
        let mut all_days: BTreeSet<Date> = BTreeSet::new();
        for spec in &self.trips {
            let mut calls = Vec::new();
            let mut visits: Vec<Arc<str>> = Vec::new();
            for (i, (raw, arrival, departure)) in spec.calls.iter().enumerate() {
                let bare = raw.trim_start_matches(['~', '?']);
                let (name, post) = match bare.split_once('@') {
                    Some((name, post)) => (name, Some(post)),
                    None => (bare, None),
                };
                let location_id = location(&mut index, &mut stops, name);
                let point_id = post.map(|p| location(&mut index, &mut stops, p));
                visits.push(location_id.clone());
                let visit = visits.iter().filter(|v| **v == location_id).count();
                calls.push(Call {
                    sequence: spec.first_sequence + i as i32,
                    location_id,
                    point_id,
                    arrival: *arrival,
                    departure: *departure,
                    visit_n: visit as u32,
                    passenger_service: !raw.starts_with('~'),
                    on_request: raw.starts_with('?'),
                });
            }
            let shape_id = spec.shape.then(|| format!("{}:shape", spec.trip_id));
            if let Some(shape_id) = &shape_id {
                let points: Vec<(f64, f64)> = calls
                    .iter()
                    .map(|c| {
                        let l = &index.locations[c.placed_at()];
                        (l.lon.unwrap(), l.lat.unwrap())
                    })
                    .collect();
                let id = index.pool.id(shape_id);
                let shape = Shape {
                    shape_id: id.clone(),
                    distances_m: cumulative_m(&points).into(),
                    points: points.into(),
                };
                index.shapes.insert(id, Arc::new(shape));
            }
            let line = spec.key.split_once(':').map_or(&*spec.key, |(l, _)| l);
            let pool = &mut index.pool;
            let service_id = pool.id(&format!("{}:service", spec.trip_id));
            let trip = Trip {
                trip_id: pool.id(&spec.trip_id),
                route_id: pool.id(&format!("{f}:r{line}")),
                route_name: pool.id(line),
                mode: pool.id(&spec.mode),
                service_id: service_id.clone(),
                headsign: pool.opt(spec.headsign.as_deref()),
                shape_id: pool.opt(shape_id.as_deref()),
                calls: calls.into(),
                run_key: spec.run.as_ref().map(|(r, _)| pool.id(r)),
                run_part: spec.run.as_ref().map(|(_, p)| *p),
                train_number: pool.opt(spec.train.as_deref()),
                parts: Box::new([]),
                agency_id: Some(pool.id(&format!("{f}:agency:1"))),
            };
            if let Some((run, _)) = &spec.run {
                let run = index.pool.id(run);
                let mut parts: Vec<Arc<str>> =
                    index.runs.get(&run).map(|p| p.to_vec()).unwrap_or_default();
                parts.push(trip.trip_id.clone());
                let order = |t: &Arc<str>| {
                    self.trips
                        .iter()
                        .find(|s| *s.trip_id == **t)
                        .and_then(|s| s.run.as_ref())
                        .map(|(_, p)| *p)
                };
                parts.sort_by_key(order);
                index.runs.insert(run, parts.into());
            }
            let mut dates = DateSet::default();
            for day in &spec.days {
                dates.insert(*day);
                all_days.insert(*day);
            }
            index.service_dates.insert(service_id, dates);
            let key = index.intern(&KeyRef::new(&spec.namespace, &spec.key));
            add_entry(&mut index, &key, trip.trip_id.clone(), valid_from, valid_to);
            if let Some(parent) = parent_ref(&key) {
                let parent = index.intern(&parent);
                let route = KeyEntry {
                    public_id: trip.route_id.clone(),
                    valid_from,
                    valid_to,
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
        for (namespace, key, trip_id) in &self.aliases {
            let key = index.intern(&KeyRef::new(namespace, key));
            let trip_id = index.pool.id(trip_id);
            add_entry(&mut index, &key, trip_id, valid_from, valid_to);
        }
        if let (Some(first), Some(last)) = (all_days.first(), all_days.last()) {
            for day in days_between(first.add_days(-3), last.add_days(3)) {
                index.loaded_dates.insert(day);
            }
        }
        index
    }
}

fn add_entry(index: &mut Index, key: &KeyRef, public_id: Arc<str>, from: Date, to: Date) {
    let mut entries = index
        .key_entries
        .get_ref(key)
        .map(|e| e.to_vec())
        .unwrap_or_default();
    entries.push(KeyEntry {
        public_id,
        valid_from: from,
        valid_to: to,
    });
    index.key_entries.insert(key.clone(), entries.into());
}

/// `with_unknown`: mark keys, and their parents unless present, as looked up and absent.
pub fn with_unknown(index: &mut Index, refs: &[(&str, &str)]) {
    for (namespace, identifier) in refs {
        let key = index.intern(&KeyRef::new(namespace, identifier));
        if !index.key_entries.contains(&key) {
            index.key_entries.insert(key.clone(), Box::new([]));
        }
        if let Some(parent) = parent_ref(&key) {
            let parent = index.intern(&parent);
            if !index.key_entries.contains(&parent) {
                index.key_entries.insert(parent, Box::new([]));
            }
        }
    }
}
