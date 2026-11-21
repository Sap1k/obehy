//! The index itself: `Index` of `realtime/index.py`, its lookups, the sweep and run trips.

use std::collections::BTreeSet;
use std::sync::Arc;

use rustc_hash::{FxHashMap as HashMap, FxHashSet as HashSet};

use super::pool::{Id, Pool};
use super::{
    Call, IndexMiss, KeyEntry, KeyRef, Location, Shape, Trip, TripPart, child_namespace, parent_ref,
};
use crate::model::{Feed, visit_numbers};
use crate::time::Date;

/// The dates a service runs on, of those loaded: sorted, without repeats (a handful).
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct DateSet(Vec<Date>);

impl DateSet {
    pub fn contains(&self, day: Date) -> bool {
        self.0.binary_search(&day).is_ok()
    }

    pub fn insert(&mut self, day: Date) {
        if let Err(at) = self.0.binary_search(&day) {
            self.0.insert(at, day);
        }
    }

    /// Whether it shares a date with `days`.
    pub fn meets(&self, days: &BTreeSet<Date>) -> bool {
        self.0.iter().any(|d| days.contains(d))
    }

    /// Keep only the dates in `days`.
    pub fn keep(&mut self, days: &BTreeSet<Date>) {
        self.0.retain(|d| days.contains(d));
        self.0.shrink_to_fit();
    }

    pub fn iter(&self) -> impl Iterator<Item = Date> + '_ {
        self.0.iter().copied()
    }

    pub fn len(&self) -> usize {
        self.0.len()
    }

    pub fn is_empty(&self) -> bool {
        self.0.is_empty()
    }
}

/// A map by source key, looked up by `(&str, &str)` without allocating: by namespace (a
/// handful), then identifier.
#[derive(Debug, Clone)]
pub struct KeyMap<V> {
    by_namespace: HashMap<Id, HashMap<Id, V>>,
}

impl<V> Default for KeyMap<V> {
    fn default() -> Self {
        KeyMap {
            by_namespace: HashMap::default(),
        }
    }
}

impl<V> KeyMap<V> {
    pub fn get(&self, namespace: &str, identifier: &str) -> Option<&V> {
        self.by_namespace.get(namespace)?.get(identifier)
    }

    pub fn get_ref(&self, key: &KeyRef) -> Option<&V> {
        self.get(&key.namespace, &key.identifier)
    }

    pub fn contains(&self, key: &KeyRef) -> bool {
        self.get_ref(key).is_some()
    }

    pub fn insert(&mut self, key: KeyRef, value: V) {
        self.by_namespace
            .entry(key.namespace)
            .or_default()
            .insert(key.identifier, value);
    }

    pub fn get_or_insert_with(&mut self, key: KeyRef, f: impl FnOnce() -> V) -> &mut V {
        self.by_namespace
            .entry(key.namespace)
            .or_default()
            .entry(key.identifier)
            .or_insert_with(f)
    }

    /// Keep the entries `keep` says, and give the freed capacity back.
    pub fn retain(&mut self, mut keep: impl FnMut(&KeyRef, &V) -> bool) {
        for (namespace, entries) in &mut self.by_namespace {
            entries.retain(|identifier, value| {
                keep(
                    &KeyRef {
                        namespace: namespace.clone(),
                        identifier: identifier.clone(),
                    },
                    value,
                )
            });
            entries.shrink_to_fit();
        }
        self.by_namespace.retain(|_, entries| !entries.is_empty());
        self.by_namespace.shrink_to_fit();
    }

    pub fn len(&self) -> usize {
        self.by_namespace.values().map(HashMap::len).sum()
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    /// Every entry, in no particular order.
    pub fn iter(&self) -> impl Iterator<Item = (KeyRef, &V)> {
        self.by_namespace.iter().flat_map(|(namespace, entries)| {
            entries.iter().map(move |(identifier, value)| {
                (
                    KeyRef {
                        namespace: namespace.clone(),
                        identifier: identifier.clone(),
                    },
                    value,
                )
            })
        })
    }

    /// Every entry, sorted by key (for output).
    pub fn sorted(&self) -> Vec<(KeyRef, &V)> {
        let mut all: Vec<_> = self.iter().collect();
        all.sort_by(|a, b| a.0.cmp(&b.0));
        all
    }
}

/// A mutable, append-only index for one feed of one release (`index.py` `Index`).
///
/// Every map is emptied of what no kept key, line or live trip needs by [`Index::sweep`] (the
/// worker sweeps every `sweep_index_every_s`, keeping keys named in `keep_index_s`, and dates
/// from yesterday or the oldest live journey's on); the rule for each is next to it. Lookups
/// never iterate a map: output order comes from sorting.
#[derive(Debug)]
pub struct Index {
    pub release_id: String,
    pub feed: Feed,
    /// Interned IDs; texts nothing else holds leave at each sweep.
    pub pool: Pool,
    /// Keys looked up, found or absent (empty). Kept: the sweep's keys, their parents, the
    /// trip keys of kept lines.
    pub key_entries: KeyMap<Box<[KeyEntry]>>,
    /// Loaded trips and cached run trips. Kept: trips a kept key names, the sweep's live
    /// trips, and every part of their runs.
    pub trips: HashMap<Id, Arc<Trip>>,
    /// Service dates of the loaded dates, by service. Kept: the services of kept trips and of
    /// kept `trip_services`, cut to the kept dates.
    pub service_dates: HashMap<Id, DateSet>,
    /// Dates whose service dates are loaded. Kept: from the sweep's day on.
    pub loaded_dates: BTreeSet<Date>,
    /// Kept: the locations and posts of kept trips' calls.
    pub locations: HashMap<Id, Location>,
    /// Kept: the shapes of kept trips.
    pub shapes: HashMap<Id, Arc<Shape>>,
    /// Rail: the trip parts of each loaded run (PA), in `run_part` order. Kept: the runs of
    /// kept trips.
    pub runs: HashMap<Id, Box<[Id]>>,
    /// The service of every trip a loaded key or run names, loaded or not: only trips that run
    /// on a loaded date are loaded, the others when a date they run on is. Kept: trips a kept
    /// key or kept run names.
    pub trip_services: HashMap<Id, Id>,
    /// Location source keys by location: (namespace, identifier). Never swept: one per
    /// station, bounded by the release.
    pub location_keys: HashMap<Id, Vec<(Id, Id)>>,
    /// The locations each location source key names, sorted. Never swept, as above.
    pub keyed: KeyMap<Box<[Id]>>,
    /// Keyless sources: the trip keys of each loaded line. Kept: the sweep's lines.
    pub lines: KeyMap<Box<[Id]>>,
}

impl Index {
    pub fn new(release_id: impl Into<String>, feed: Feed) -> Index {
        Index {
            release_id: release_id.into(),
            feed,
            pool: Pool::default(),
            key_entries: KeyMap::default(),
            trips: HashMap::default(),
            service_dates: HashMap::default(),
            loaded_dates: BTreeSet::new(),
            locations: HashMap::default(),
            shapes: HashMap::default(),
            runs: HashMap::default(),
            trip_services: HashMap::default(),
            location_keys: HashMap::default(),
            keyed: KeyMap::default(),
            lines: KeyMap::default(),
        }
    }

    /// `key` with its texts interned.
    pub fn intern(&mut self, key: &KeyRef) -> KeyRef {
        KeyRef {
            namespace: self.pool.id(&key.namespace),
            identifier: self.pool.id(&key.identifier),
        }
    }

    /// Keys whose static data has not been loaded (or looked up and found absent) yet.
    pub fn missing<'a>(&self, refs: impl IntoIterator<Item = &'a KeyRef>) -> BTreeSet<KeyRef> {
        refs.into_iter()
            .filter(|r| !self.key_entries.contains(r))
            .cloned()
            .collect()
    }

    pub fn keys(&self, namespace: &str, identifier: &str) -> Result<&[KeyEntry], IndexMiss> {
        self.key_entries
            .get(namespace, identifier)
            .map(|e| &**e)
            .ok_or_else(|| IndexMiss(format!("key {namespace}:{identifier} was not loaded")))
    }

    pub fn trip(&self, trip_id: &str) -> Result<&Arc<Trip>, IndexMiss> {
        self.trips
            .get(trip_id)
            .ok_or_else(|| IndexMiss(format!("trip {trip_id} was not loaded")))
    }

    pub fn runs_on(&self, service_id: &str, day: Date) -> Result<bool, IndexMiss> {
        if !self.loaded_dates.contains(&day) {
            return Err(IndexMiss(format!(
                "service dates for {day} were not loaded"
            )));
        }
        Ok(self
            .service_dates
            .get(service_id)
            .is_some_and(|d| d.contains(day)))
    }

    /// The trip if it runs on `day`, else None; a trip that does not run need not be loaded.
    pub fn running_trip(&self, trip_id: &str, day: Date) -> Result<Option<&Arc<Trip>>, IndexMiss> {
        let service_id = match self.trip_services.get(trip_id) {
            Some(service) => service,
            None => &self.trip(trip_id)?.service_id,
        };
        if self.runs_on(service_id, day)? {
            Ok(Some(self.trip(trip_id)?))
        } else {
            Ok(None)
        }
    }

    /// The trips named but not loaded that run on one of `days`, sorted.
    pub fn dormant(&self, days: &BTreeSet<Date>) -> Vec<Id> {
        let mut found: Vec<Id> = self
            .trip_services
            .iter()
            .filter(|(trip_id, service_id)| {
                !self.trips.contains_key(*trip_id)
                    && self
                        .service_dates
                        .get(*service_id)
                        .is_some_and(|d| d.meets(days))
            })
            .map(|(trip_id, _)| trip_id.clone())
            .collect();
        found.sort();
        found
    }

    pub fn location(&self, location_id: &str) -> Result<&Location, IndexMiss> {
        self.locations
            .get(location_id)
            .ok_or_else(|| IndexMiss(format!("location {location_id} was not loaded")))
    }

    pub fn shape(&self, shape_id: &str) -> Result<&Arc<Shape>, IndexMiss> {
        self.shapes
            .get(shape_id)
            .ok_or_else(|| IndexMiss(format!("shape {shape_id} was not loaded")))
    }

    /// The location's source key in `namespace` (rail: `sr70`), if it has one.
    pub fn location_key(&self, location_id: &str, namespace: &str) -> Option<&Id> {
        self.location_keys
            .get(location_id)?
            .iter()
            .find(|(ns, _)| &**ns == namespace)
            .map(|(_, identifier)| identifier)
    }

    /// Locations a source key names (rail: `sr70:track` boarding points).
    pub fn keyed_locations(&self, namespace: &str, identifier: &str) -> &[Id] {
        self.keyed.get(namespace, identifier).map_or(&[], |l| &**l)
    }

    /// The trip keys of a line (`cis:line` -> its `cis:line_trip` identifiers).
    pub fn line_keys(&self, namespace: &str, line: &str) -> Result<&[Id], IndexMiss> {
        self.lines
            .get(namespace, line)
            .map(|l| &**l)
            .ok_or_else(|| IndexMiss(format!("line {namespace}:{line} was not loaded")))
    }

    pub fn add_location_key(&mut self, location_id: &str, namespace: &str, identifier: &str) {
        let (location_id, namespace, identifier) = (
            self.pool.id(location_id),
            self.pool.id(namespace),
            self.pool.id(identifier),
        );
        let keys = self.location_keys.entry(location_id.clone()).or_default();
        match keys.iter_mut().find(|(ns, _)| *ns == namespace) {
            Some(entry) => entry.1 = identifier.clone(),
            None => keys.push((namespace.clone(), identifier.clone())),
        }
        let named = self.keyed.get_or_insert_with(
            KeyRef {
                namespace,
                identifier,
            },
            || Box::new([]),
        );
        if !named.contains(&location_id) {
            let mut all = named.to_vec();
            all.push(location_id);
            all.sort();
            *named = all.into_boxed_slice();
        }
    }

    /// Keep only what the kept keys, lines and trips need; drop the rest in place and shrink.
    ///
    /// Kept: the keys `refs`, their parents and the trip keys of the kept `lines`; their
    /// trips, the trips `trip_ids` (live journeys, run trips included), every part of a kept
    /// trip's run; and those trips' shapes, locations and service dates, for dates from `since`
    /// on. The services of the kept keys' and runs' trips not loaded are kept too, so a date
    /// loaded later finds the ones that run on it. Whatever is dropped reloads when an
    /// observation names it again. Location source keys (one per station) are kept.
    pub fn sweep<'r, 'l, 't>(
        &mut self,
        refs: impl IntoIterator<Item = &'r KeyRef>,
        lines: impl IntoIterator<Item = &'l KeyRef>,
        trip_ids: impl IntoIterator<Item = &'t str>,
        since: Date,
    ) {
        let kept_lines: HashSet<KeyRef> = lines
            .into_iter()
            .filter(|r| self.lines.contains(r))
            .cloned()
            .collect();
        let mut wanted: HashSet<KeyRef> = refs.into_iter().cloned().collect();
        for line in &kept_lines {
            // The line's own key too: a line without trips has no trip key to keep it as a
            // parent, and a kept line is read through it.
            wanted.insert(line.clone());
            let identifiers = self.lines.get_ref(line).expect("a kept line is loaded");
            if let Some(child) = child_namespace(&line.namespace) {
                let child: Id = Arc::from(child);
                for identifier in identifiers {
                    wanted.insert(KeyRef {
                        namespace: child.clone(),
                        identifier: identifier.clone(),
                    });
                }
            }
        }
        let parents: Vec<KeyRef> = wanted.iter().filter_map(parent_ref).collect();
        wanted.extend(parents);
        self.key_entries.retain(|key, _| wanted.contains(key));
        drop(wanted);
        let mut named: HashSet<Id> = HashSet::default();
        for (_, entries) in self.key_entries.iter() {
            named.extend(entries.iter().map(|e| e.public_id.clone()));
        }
        let mut trips: HashSet<Id> = named
            .iter()
            .cloned()
            .chain(trip_ids.into_iter().map(Arc::from))
            .filter(|t| self.trips.contains_key(t))
            .collect();
        let runs: HashSet<Id> = trips
            .iter()
            .filter_map(|t| self.trips[t].run_key.clone())
            .filter(|r| self.runs.contains_key(r))
            .collect();
        for run in &runs {
            for part in &self.runs[run] {
                if self.trips.contains_key(part) {
                    trips.insert(part.clone());
                }
                named.insert(part.clone());
            }
        }
        self.trips.retain(|t, _| trips.contains(t));
        self.runs.retain(|r, _| runs.contains(r));
        self.trip_services.retain(|t, _| named.contains(t));
        drop((trips, runs, named));
        self.loaded_dates.retain(|d| *d >= since);
        let mut services: HashSet<Id> = self.trip_services.values().cloned().collect();
        let mut shapes: HashSet<Id> = HashSet::default();
        let mut locations: HashSet<Id> = HashSet::default();
        for trip in self.trips.values() {
            services.insert(trip.service_id.clone());
            shapes.extend(trip.shape_id.clone());
            for call in &trip.calls {
                locations.insert(call.location_id.clone());
                locations.insert(call.placed_at().clone());
            }
        }
        self.shapes.retain(|s, _| shapes.contains(s));
        self.locations.retain(|l, _| locations.contains(l));
        let dates = &self.loaded_dates;
        self.service_dates.retain(|s, days| {
            days.keep(dates);
            services.contains(s)
        });
        self.lines.retain(|line, _| kept_lines.contains(line));
        drop((services, shapes, locations, kept_lines));
        self.trips.shrink_to_fit();
        self.runs.shrink_to_fit();
        self.trip_services.shrink_to_fit();
        self.shapes.shrink_to_fit();
        self.locations.shrink_to_fit();
        self.service_dates.shrink_to_fit();
        self.pool.sweep();
    }

    /// The run trip of a trip part on `day`; a trip outside a run is its own run. Built run
    /// trips are cached among the trips: the one read that writes.
    pub fn run(&mut self, trip: &Arc<Trip>, day: Date) -> Result<Arc<Trip>, IndexMiss> {
        let Some(run_key) = trip.run_key.clone() else {
            return Ok(trip.clone());
        };
        if !trip.parts.is_empty() {
            return Ok(trip.clone());
        }
        let part_ids = self
            .runs
            .get(&run_key)
            .ok_or_else(|| IndexMiss(format!("run {run_key} was not loaded")))?;
        let mut running: Vec<Arc<Trip>> = Vec::new();
        for part in part_ids {
            if let Some(found) = self.running_trip(part, day)? {
                running.push(found.clone());
            }
        }
        if running.is_empty() {
            running.push(trip.clone());
        }
        let numbers: Vec<Option<i16>> = running.iter().map(|p| p.run_part).collect();
        let distinct: HashSet<Option<i16>> = numbers.iter().copied().collect();
        if distinct.len() != numbers.len() {
            // Two versions of one part running the same day: no run is built, the binding is
            // ambiguous (`parts` stays empty).
            let mut ambiguous = Trip::clone(trip);
            ambiguous.trip_id = Arc::from(format!("run:{run_key}:ambiguous"));
            return Ok(Arc::new(ambiguous));
        }
        let parts: Vec<String> = numbers
            .iter()
            .map(|n| n.map_or_else(|| "None".to_owned(), |n| n.to_string()))
            .collect();
        let run_id = format!("run:{run_key}:{}", parts.join(","));
        if let Some(cached) = self.trips.get(run_id.as_str()) {
            return Ok(cached.clone());
        }
        let run_id = self.pool.id(&run_id);
        let built = Arc::new(run_trip(run_id.clone(), run_key, &running));
        self.trips.insert(run_id, built.clone());
        Ok(built)
    }
}

/// The indexes of `trip`'s calls from `start` at a station whose SR70 code is one of `codes`,
/// in call order; only passenger calls with `passenger`. How a rail source's station (a point,
/// a prediction, a board row) is placed on a run.
pub fn calls_at_station<'a>(
    trip: &'a Trip,
    index: &'a Index,
    codes: &'a [&'a str],
    start: i64,
    passenger: bool,
) -> impl Iterator<Item = usize> + 'a {
    let from = usize::try_from(start.max(0)).unwrap_or(usize::MAX);
    trip.calls
        .iter()
        .enumerate()
        .skip(from)
        .filter(move |(_, call)| {
            (!passenger || call.passenger_service)
                && index
                    .location_key(&call.location_id, "sr70")
                    .is_some_and(|code| codes.contains(&&**code))
        })
        .map(|(i, _)| i)
}

fn timed(call: &Call) -> bool {
    call.passenger_service || call.arrival.is_some() || call.departure.is_some()
}

/// The parts concatenated into one trip: the junction call shared by consecutive parts is
/// merged (arrival of the earlier part, departure of the later), and railway points without
/// any time are left out (they cannot be placed).
pub fn run_trip(run_id: Id, run_key: Id, parts: &[Arc<Trip>]) -> Trip {
    let mut calls: Vec<Call> = Vec::new();
    let mut spans: Vec<TripPart> = Vec::new();
    for part in parts {
        let mut first: Option<usize> = None;
        for call in part.calls.iter().filter(|c| timed(c)) {
            match calls.last_mut() {
                Some(previous) if previous.sequence == call.sequence => {
                    *previous = Call {
                        sequence: call.sequence,
                        location_id: call.location_id.clone(),
                        point_id: previous.point_id.clone().or_else(|| call.point_id.clone()),
                        arrival: previous.arrival.or(call.arrival),
                        departure: call.departure.or(previous.departure),
                        visit_n: 0,
                        passenger_service: previous.passenger_service || call.passenger_service,
                        on_request: previous.on_request && call.on_request,
                    };
                }
                _ => calls.push(call.clone()),
            }
            first.get_or_insert(calls.len() - 1);
        }
        if let Some(first) = first {
            spans.push(TripPart {
                trip_id: part.trip_id.clone(),
                train_number: part.train_number.clone(),
                first,
                last: calls.len() - 1,
            });
        }
    }
    let visits = visit_numbers(calls.iter().map(|c| &*c.location_id));
    for (call, visit) in calls.iter_mut().zip(visits) {
        call.visit_n = visit;
    }
    let (head, tail) = (&parts[0], &parts[parts.len() - 1]);
    Trip {
        trip_id: run_id,
        route_id: head.route_id.clone(),
        route_name: head.route_name.clone(),
        mode: head.mode.clone(),
        service_id: head.service_id.clone(),
        headsign: tail.headsign.clone(),
        shape_id: None,
        calls: calls.into_boxed_slice(),
        run_key: Some(run_key),
        run_part: None,
        train_number: None,
        parts: spans.into_boxed_slice(),
        agency_id: head.agency_id.clone(),
    }
}
