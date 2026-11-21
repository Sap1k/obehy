//! Fill an [`Index`] lazily by source key: the loader logic of `realtime/index_sql.py`
//! (`IndexLoader`), written against the [`Rows`] a release offers.
//!
//! The rows come from obehy-rt: PostgreSQL `static.*` of one load, or a release directory's
//! serving Parquet. Both answer each question exactly as `index_sql.SqlRows` does, ordering
//! included; the loader decides what to ask (only trips running on loaded dates, the others
//! when a date they run on is loaded; rail trips with every part of their run, their train
//! numbers and the `sr70` keys of their locations).

use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc;

use rustc_hash::{FxHashMap as HashMap, FxHashSet as HashSet};

use super::pool::Id;
use super::{Call, Index, KeyEntry, KeyRef, Location, Shape, Trip, child_namespace, parent_ref};
use crate::geo::cumulative_m;
use crate::model::Feed;
use crate::time::{Date, Instant};

/// Whatever a [`Rows`] implementation fails with (a database or file error).
pub type RowsError = Box<dyn std::error::Error + Send + Sync>;

/// The index could not be loaded: its rows could not be read.
#[derive(Debug, thiserror::Error)]
pub enum LoadError {
    #[error("reading index rows: {0}")]
    Rows(#[from] RowsError),
}

/// `static.source_key` joined with its trip's service.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct KeyRow {
    pub namespace: String,
    pub identifier: String,
    pub public_id: String,
    pub entity_kind: String,
    pub valid_from: Date,
    pub valid_to: Date,
    /// The trip's service; None for a key naming a route.
    pub service_id: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TripRow {
    pub trip_id: String,
    pub route_id: String,
    /// The route's short name, else its ID.
    pub route_name: String,
    pub mode: String,
    pub service_id: String,
    pub headsign: Option<String>,
    pub shape_id: Option<String>,
    pub run_key: Option<String>,
    pub run_part: Option<i16>,
    pub agency_id: Option<String>,
    /// The smallest `czptt:train_number` key naming the trip.
    pub train_number: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CallRow {
    pub trip_id: String,
    pub sequence: i32,
    pub location_id: String,
    /// The visit number: the call's place among the trip's calls at its location.
    pub visit_n: i64,
    pub passenger_service: bool,
    pub arrival: Option<i32>,
    pub departure: Option<i32>,
    /// Pickup or drop-off type 3 (coordinate with the driver).
    pub on_request: bool,
    /// The boarding point, if its coordinates are exact.
    pub point_id: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RunPartRow {
    pub run_key: String,
    pub trip_id: String,
    pub service_id: String,
}

#[derive(Debug, Clone, PartialEq)]
pub struct LocationRow {
    pub location_id: String,
    pub name: String,
    pub lon: Option<f64>,
    pub lat: Option<f64>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct ShapePointRow {
    pub shape_id: String,
    pub lon: f64,
    pub lat: f64,
}

/// The static rows an [`IndexLoader`] asks for, of one feed of one release
/// (`index_sql.Rows`). Every method answers what its SQL in `index_sql.SqlRows` selects, in
/// the order it promises.
pub trait Rows {
    /// The keys of each (namespace, identifier) pair (`namespaces[i]`, `identifiers[i]`),
    /// ordered by namespace, identifier, public ID and `valid_from`.
    fn keys(&mut self, namespaces: &[&str], identifiers: &[&str])
    -> Result<Vec<KeyRow>, RowsError>;

    /// (line, identifier) of the keys in `namespace` whose leading part is one of `lines`;
    /// distinct, in no promised order.
    fn line_keys(
        &mut self,
        namespace: &str,
        lines: &[&str],
    ) -> Result<Vec<(String, String)>, RowsError>;

    /// One row per trip found (joined with its route), in no promised order.
    fn trips(&mut self, trip_ids: &[&str]) -> Result<Vec<TripRow>, RowsError>;

    /// The calls of the trips, ordered by trip and sequence.
    fn calls(&mut self, trip_ids: &[&str]) -> Result<Vec<CallRow>, RowsError>;

    /// Each run's parts, ordered by run key, `run_part` (NULLs last) and trip.
    fn run_parts(&mut self, run_keys: &[&str]) -> Result<Vec<RunPartRow>, RowsError>;

    /// (sr70 code, location_id) of the locations, ordered.
    fn station_keys(&mut self, location_ids: &[&str]) -> Result<Vec<(String, String)>, RowsError>;

    /// (`sr70:track` key, location_id) of the tracks of the stations `codes`, ordered.
    fn track_keys(&mut self, codes: &[&str]) -> Result<Vec<(String, String)>, RowsError>;

    /// The locations found, in no promised order.
    fn locations(&mut self, location_ids: &[&str]) -> Result<Vec<LocationRow>, RowsError>;

    /// The shapes' points, ordered by shape and sequence.
    fn shape_points(&mut self, shape_ids: &[&str]) -> Result<Vec<ShapePointRow>, RowsError>;

    /// (service_id, service date) for the services running on any of `days`, in no promised
    /// order.
    fn service_dates(
        &mut self,
        service_ids: &[&str],
        days: &[Date],
    ) -> Result<Vec<(String, Date)>, RowsError>;
}

fn strs<'a>(ids: impl IntoIterator<Item = &'a Id>) -> Vec<&'a str> {
    ids.into_iter().map(|id| &**id).collect()
}

/// Loads one feed of one release into an [`Index`], a batch of keys at a time.
#[derive(Debug)]
pub struct IndexLoader<R> {
    pub rows: R,
    pub index: Index,
    /// When an observation last named each key: what [`IndexLoader::sweep`] keeps. An entry
    /// leaves at the sweep after it is older than `keep_index_s`.
    pub used: HashMap<KeyRef, Instant>,
    /// The same for lines (keyless sources).
    pub lines_used: HashMap<KeyRef, Instant>,
}

impl<R: Rows> IndexLoader<R> {
    pub fn new(rows: R, release_id: impl Into<String>, feed: Feed) -> Self {
        IndexLoader {
            rows,
            index: Index::new(release_id, feed),
            used: HashMap::default(),
            lines_used: HashMap::default(),
        }
    }

    pub fn touch<'a>(
        &mut self,
        refs: impl IntoIterator<Item = &'a KeyRef>,
        lines: impl IntoIterator<Item = &'a KeyRef>,
        at: Instant,
    ) {
        for key in refs {
            let key = self.index.intern(key);
            self.used.insert(key, at);
        }
        for line in lines {
            let line = self.index.intern(line);
            self.lines_used.insert(line, at);
        }
    }

    /// Drop the static data of keys and lines no observation named since `since`, unless a
    /// live journey (its key `refs`, its `trip_ids`) needs it; service dates before `day` go
    /// too. It all reloads when named again.
    pub fn sweep<'t, 'r>(
        &mut self,
        since: Instant,
        trip_ids: impl IntoIterator<Item = &'t str>,
        refs: impl IntoIterator<Item = &'r KeyRef>,
        day: Date,
    ) {
        self.used.retain(|_, at| *at >= since);
        self.lines_used.retain(|_, at| *at >= since);
        self.used.shrink_to_fit();
        self.lines_used.shrink_to_fit();
        let kept: HashSet<KeyRef> = self
            .used
            .keys()
            .cloned()
            .chain(refs.into_iter().cloned())
            .collect();
        let lines: Vec<KeyRef> = self.lines_used.keys().cloned().collect();
        self.index.sweep(&kept, &lines, trip_ids, day);
    }

    /// Whether [`IndexLoader::ensure`] with these would read rows.
    pub fn needs<'a>(
        &self,
        refs: impl IntoIterator<Item = &'a KeyRef>,
        dates: impl IntoIterator<Item = Date>,
        lines: impl IntoIterator<Item = &'a KeyRef>,
    ) -> bool {
        if lines
            .into_iter()
            .any(|l| child_namespace(&l.namespace).is_some() && !self.index.lines.contains(l))
        {
            return true;
        }
        let mut refs: HashSet<KeyRef> = refs.into_iter().cloned().collect();
        let parents: Vec<KeyRef> = refs.iter().filter_map(parent_ref).collect();
        refs.extend(parents);
        !self.index.missing(&refs).is_empty()
            || !dates
                .into_iter()
                .all(|d| self.index.loaded_dates.contains(&d))
    }

    /// Load every key in `refs` (and its parent) not seen yet, every trip key of the `lines`
    /// (keyless sources), and service dates for `dates`; return the index.
    pub fn ensure<'a>(
        &mut self,
        refs: impl IntoIterator<Item = &'a KeyRef>,
        dates: impl IntoIterator<Item = Date>,
        lines: impl IntoIterator<Item = &'a KeyRef>,
    ) -> Result<&mut Index, LoadError> {
        let new_lines: BTreeSet<KeyRef> = lines
            .into_iter()
            .filter(|l| child_namespace(&l.namespace).is_some() && !self.index.lines.contains(l))
            .cloned()
            .collect();
        let mut wanted: HashSet<KeyRef> = refs.into_iter().cloned().collect();
        wanted.extend(new_lines.iter().cloned());
        let parents: Vec<KeyRef> = wanted.iter().filter_map(parent_ref).collect();
        wanted.extend(parents);
        if !new_lines.is_empty() {
            let new_lines: Vec<KeyRef> = new_lines.into_iter().collect();
            wanted.extend(self.load_line_keys(&new_lines)?);
        }
        let missing = self.index.missing(&wanted);
        let new_dates: BTreeSet<Date> = dates
            .into_iter()
            .filter(|d| !self.index.loaded_dates.contains(d))
            .collect();
        if !new_dates.is_empty() {
            let services: BTreeSet<Id> = self
                .index
                .service_dates
                .keys()
                .chain(self.index.trip_services.values())
                .cloned()
                .collect();
            self.load_dates(&services, &new_dates, true)?;
            let woken = self.index.dormant(&new_dates);
            if !woken.is_empty() {
                self.load_trips(&woken)?;
            }
        }
        if !missing.is_empty() {
            self.load_keys(missing)?;
        }
        Ok(&mut self.index)
    }

    fn load_keys(&mut self, refs: BTreeSet<KeyRef>) -> Result<(), LoadError> {
        let namespaces: Vec<&str> = refs.iter().map(|r| &*r.namespace).collect();
        let identifiers: Vec<&str> = refs.iter().map(|r| &*r.identifier).collect();
        let rows = self.rows.keys(&namespaces, &identifiers)?;
        let index = &mut self.index;
        let mut found: HashMap<(String, String), Vec<KeyEntry>> = HashMap::default();
        let mut services: HashMap<Id, Id> = HashMap::default();
        for row in rows {
            let public_id = index.pool.id(&row.public_id);
            if row.entity_kind == "trip"
                && let Some(service_id) = &row.service_id
            {
                services.insert(public_id.clone(), index.pool.id(service_id));
            }
            found
                .entry((row.namespace, row.identifier))
                .or_default()
                .push(KeyEntry {
                    public_id,
                    valid_from: row.valid_from,
                    valid_to: row.valid_to,
                });
        }
        for key in &refs {
            let entries = found
                .remove(&(key.namespace.to_string(), key.identifier.to_string()))
                .unwrap_or_default();
            let key = index.intern(key);
            index.key_entries.insert(key, entries.into_boxed_slice());
        }
        self.name_trips(services)
    }

    /// Record the service of each named trip and load those that run on a loaded date; the
    /// others load when a date they run on does (`ensure`).
    fn name_trips(&mut self, services: HashMap<Id, Id>) -> Result<(), LoadError> {
        let unknown: BTreeSet<Id> = services
            .values()
            .filter(|s| !self.index.service_dates.contains_key(*s))
            .cloned()
            .collect();
        self.index
            .trip_services
            .extend(services.iter().map(|(t, s)| (t.clone(), s.clone())));
        if !unknown.is_empty() && !self.index.loaded_dates.is_empty() {
            let loaded = self.index.loaded_dates.clone();
            self.load_dates(&unknown, &loaded, false)?;
        }
        for service_id in unknown {
            self.index.service_dates.entry(service_id).or_default();
        }
        let dates = &self.index.loaded_dates;
        let mut wanted: Vec<Id> = services
            .iter()
            .filter(|(trip_id, service_id)| {
                !self.index.trips.contains_key(*trip_id)
                    && self.index.service_dates[*service_id].meets(dates)
            })
            .map(|(trip_id, _)| trip_id.clone())
            .collect();
        if !wanted.is_empty() {
            wanted.sort();
            self.load_trips(&wanted)?;
        }
        Ok(())
    }

    /// The trip keys of each line, by the source's key format (`<line>:<trip>`, as
    /// [`parent_ref`] splits it); recorded in `Index::lines`.
    fn load_line_keys(&mut self, lines: &[KeyRef]) -> Result<HashSet<KeyRef>, LoadError> {
        let mut found: HashMap<KeyRef, BTreeSet<String>> = HashMap::default();
        let namespaces: BTreeSet<&Id> = lines.iter().map(|l| &l.namespace).collect();
        for namespace in namespaces {
            let child = child_namespace(namespace).expect("a line namespace");
            let mut values: Vec<&str> = lines
                .iter()
                .filter(|l| &l.namespace == namespace)
                .map(|l| &*l.identifier)
                .collect();
            values.sort_unstable();
            for (line, identifier) in self.rows.line_keys(child, &values)? {
                found
                    .entry(KeyRef::new(namespace, &line))
                    .or_default()
                    .insert(identifier);
            }
        }
        let mut keys = HashSet::default();
        for line in lines {
            let child = self
                .index
                .pool
                .id(child_namespace(&line.namespace).expect("a line namespace"));
            let identifiers: Box<[Id]> = found
                .get(line)
                .into_iter()
                .flatten()
                .map(|i| self.index.pool.id(i))
                .collect();
            for identifier in &identifiers {
                keys.insert(KeyRef {
                    namespace: child.clone(),
                    identifier: identifier.clone(),
                });
            }
            let line = self.index.intern(line);
            self.index.lines.insert(line, identifiers);
        }
        Ok(keys)
    }

    fn load_trips(&mut self, trip_ids: &[Id]) -> Result<(), LoadError> {
        let ids = strs(trip_ids);
        let heads = self.rows.trips(&ids)?;
        let call_rows = self.rows.calls(&ids)?;
        let index = &mut self.index;
        let mut calls: HashMap<String, Vec<Call>> = HashMap::default();
        let mut locations: BTreeSet<Id> = BTreeSet::new();
        for row in call_rows {
            let location_id = index.pool.id(&row.location_id);
            let point_id = index.pool.opt(row.point_id.as_deref());
            locations.insert(location_id.clone());
            locations.extend(point_id.clone());
            calls.entry(row.trip_id).or_default().push(Call {
                sequence: row.sequence,
                location_id,
                point_id,
                arrival: row.arrival,
                departure: row.departure,
                visit_n: u32::try_from(row.visit_n).expect("a visit number"),
                passenger_service: row.passenger_service,
                on_request: row.on_request,
            });
        }
        let mut shapes: BTreeSet<Id> = BTreeSet::new();
        let mut services: BTreeSet<Id> = BTreeSet::new();
        let mut runs: BTreeSet<Id> = BTreeSet::new();
        for head in heads {
            let pool = &mut index.pool;
            let trip = Trip {
                trip_id: pool.id(&head.trip_id),
                route_id: pool.id(&head.route_id),
                route_name: pool.id(&head.route_name),
                mode: pool.id(&head.mode),
                service_id: pool.id(&head.service_id),
                headsign: pool.opt(head.headsign.as_deref()),
                shape_id: pool.opt(head.shape_id.as_deref()),
                calls: calls.remove(&head.trip_id).unwrap_or_default().into(),
                run_key: pool.opt(head.run_key.as_deref()),
                run_part: head.run_part,
                train_number: pool.opt(head.train_number.as_deref()),
                parts: Box::new([]),
                agency_id: pool.opt(head.agency_id.as_deref()),
            };
            if let Some(run_key) = &trip.run_key
                && !index.runs.contains_key(run_key)
            {
                runs.insert(run_key.clone());
            }
            if let Some(shape_id) = &trip.shape_id
                && !index.shapes.contains_key(shape_id)
            {
                shapes.insert(shape_id.clone());
            }
            if !index.service_dates.contains_key(&trip.service_id) {
                services.insert(trip.service_id.clone());
            }
            index.trips.insert(trip.trip_id.clone(), Arc::new(trip));
        }
        let new_locations: Vec<Id> = locations
            .into_iter()
            .filter(|l| !index.locations.contains_key(l))
            .collect();
        self.load_locations(&new_locations)?;
        if self.index.feed == Feed::Czptt {
            self.load_rail_keys(&new_locations)?;
        }
        if !shapes.is_empty() {
            self.load_shapes(&shapes)?;
        }
        if !services.is_empty() && !self.index.loaded_dates.is_empty() {
            let loaded = self.index.loaded_dates.clone();
            self.load_dates(&services, &loaded, false)?;
        }
        for service_id in services {
            self.index.service_dates.entry(service_id).or_default();
        }
        if !runs.is_empty() {
            self.load_runs(&runs)?;
        }
        Ok(())
    }

    /// Every part of each run, in `run_part` order; the parts that run on a loaded date load
    /// like any trip.
    fn load_runs(&mut self, run_keys: &BTreeSet<Id>) -> Result<(), LoadError> {
        let rows = self.rows.run_parts(&strs(run_keys))?;
        let index = &mut self.index;
        let mut parts: HashMap<String, Vec<Id>> = HashMap::default();
        let mut services: HashMap<Id, Id> = HashMap::default();
        for row in rows {
            let trip_id = index.pool.id(&row.trip_id);
            services.insert(trip_id.clone(), index.pool.id(&row.service_id));
            parts.entry(row.run_key).or_default().push(trip_id);
        }
        for run_key in run_keys {
            let found = parts.remove(&**run_key).unwrap_or_default();
            index.runs.insert(run_key.clone(), found.into_boxed_slice());
        }
        self.name_trips(services)
    }

    /// `sr70` keys of the locations, and the `sr70:track` keys of those stations' tracks.
    fn load_rail_keys(&mut self, location_ids: &[Id]) -> Result<(), LoadError> {
        if location_ids.is_empty() {
            return Ok(());
        }
        let mut codes: BTreeSet<String> = BTreeSet::new();
        for (identifier, public_id) in self.rows.station_keys(&strs(location_ids))? {
            self.index.add_location_key(&public_id, "sr70", &identifier);
            codes.insert(identifier);
        }
        if codes.is_empty() {
            return Ok(());
        }
        let codes: Vec<&str> = codes.iter().map(String::as_str).collect();
        for (identifier, public_id) in self.rows.track_keys(&codes)? {
            self.index
                .add_location_key(&public_id, "sr70:track", &identifier);
        }
        Ok(())
    }

    fn load_locations(&mut self, location_ids: &[Id]) -> Result<(), LoadError> {
        if location_ids.is_empty() {
            return Ok(());
        }
        for row in self.rows.locations(&strs(location_ids))? {
            let location_id = self.index.pool.id(&row.location_id);
            let location = Location {
                location_id: location_id.clone(),
                name: Arc::from(row.name),
                lon: row.lon,
                lat: row.lat,
            };
            self.index.locations.insert(location_id, location);
        }
        Ok(())
    }

    fn load_shapes(&mut self, shape_ids: &BTreeSet<Id>) -> Result<(), LoadError> {
        let mut points: HashMap<String, Vec<(f64, f64)>> = HashMap::default();
        for row in self.rows.shape_points(&strs(shape_ids))? {
            points
                .entry(row.shape_id)
                .or_default()
                .push((row.lon, row.lat));
        }
        for shape_id in shape_ids {
            let vertices = points.remove(&**shape_id).unwrap_or_default();
            // Distances come from the geometry; feed `distance_traveled` units vary.
            let distances = cumulative_m(&vertices);
            let shape = Shape {
                shape_id: shape_id.clone(),
                points: vertices.into_boxed_slice(),
                distances_m: distances.into_boxed_slice(),
            };
            self.index.shapes.insert(shape_id.clone(), Arc::new(shape));
        }
        Ok(())
    }

    fn load_dates(
        &mut self,
        service_ids: &BTreeSet<Id>,
        days: &BTreeSet<Date>,
        extend: bool,
    ) -> Result<(), LoadError> {
        let mut running: BTreeMap<String, Vec<Date>> = BTreeMap::new();
        if !service_ids.is_empty() {
            let days: Vec<Date> = days.iter().copied().collect();
            for (service_id, day) in self.rows.service_dates(&strs(service_ids), &days)? {
                running.entry(service_id).or_default().push(day);
            }
        }
        for service_id in service_ids {
            let dates = self
                .index
                .service_dates
                .entry(service_id.clone())
                .or_default();
            for day in running.remove(&**service_id).unwrap_or_default() {
                dates.insert(day);
            }
        }
        if extend {
            self.index.loaded_dates.extend(days.iter().copied());
        }
        Ok(())
    }
}
