//! `Rows` from one feed's serving tables held in memory: port of `index_files.FileRows`.
//!
//! The tables are those `obehy release load` copies into `static.*`; each method answers what
//! its SQL counterpart in `index_sql.SqlRows` selects, including what `release.load` derives on
//! loading (service dates from the calendar and its exceptions). Tables are read whole on first
//! use ([`super::parquet`]) into columns of interned strings, with an index by the column the
//! loader asks by; only the rows asked for become row structs.

use std::collections::BTreeSet;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use obehy_core::index::{
    CallRow, KeyRow, LocationRow, Rows, RowsError, RunPartRow, ShapePointRow, TripRow,
};
use obehy_core::time::Date;
use rustc_hash::{FxHashMap as HashMap, FxHashSet as HashSet};

use super::parquet;

/// No value (SQL NULL) in a string column.
pub const NONE: u32 = u32::MAX;

const TRAIN_NUMBER: &str = "czptt:train_number";

/// The strings of a feed's tables, each once.
#[derive(Debug, Default)]
pub struct Strings {
    ids: HashMap<Arc<str>, u32>,
    texts: Vec<Arc<str>>,
}

impl Strings {
    pub fn intern(&mut self, text: &str) -> u32 {
        if let Some(id) = self.ids.get(text) {
            return *id;
        }
        let id = u32::try_from(self.texts.len()).expect("fewer than 2^32 strings");
        let text: Arc<str> = Arc::from(text);
        self.texts.push(text.clone());
        self.ids.insert(text, id);
        id
    }

    pub fn get(&self, text: &str) -> Option<u32> {
        self.ids.get(text).copied()
    }

    pub fn text(&self, id: u32) -> &str {
        &self.texts[id as usize]
    }

    pub fn opt(&self, id: u32) -> Option<&str> {
        (id != NONE).then(|| self.text(id))
    }
}

/// `source_key`.
#[derive(Debug, Default)]
pub struct SourceKeys {
    pub entity_kind: Vec<u32>,
    pub namespace: Vec<u32>,
    pub identifier: Vec<u32>,
    pub public_id: Vec<u32>,
    pub valid_from: Vec<Date>,
    pub valid_to: Vec<Date>,
}

/// `trip`.
#[derive(Debug, Default)]
pub struct Trips {
    pub trip_id: Vec<u32>,
    pub route_id: Vec<u32>,
    pub service_id: Vec<u32>,
    pub headsign: Vec<u32>,
    pub shape_id: Vec<u32>,
    pub run_key: Vec<u32>,
    pub run_part: Vec<Option<i16>>,
}

/// `route`.
#[derive(Debug, Default)]
pub struct Routes {
    pub route_id: Vec<u32>,
    pub short_name: Vec<u32>,
    pub mode: Vec<u32>,
    pub agency_id: Vec<u32>,
}

/// `trip_call`.
#[derive(Debug, Default)]
pub struct Calls {
    pub trip_id: Vec<u32>,
    pub sequence: Vec<i32>,
    pub location_id: Vec<u32>,
    pub passenger_service: Vec<bool>,
    pub boarding_point_id: Vec<u32>,
    pub scheduled_arrival: Vec<Option<i32>>,
    pub scheduled_departure: Vec<Option<i32>>,
    pub pickup_type: Vec<i16>,
    pub dropoff_type: Vec<i16>,
}

/// `location`.
#[derive(Debug, Default)]
pub struct Locations {
    pub location_id: Vec<u32>,
    pub name: Vec<u32>,
    pub longitude: Vec<Option<f64>>,
    pub latitude: Vec<Option<f64>>,
    pub coordinate_precision: Vec<u32>,
}

/// `shape_point`.
#[derive(Debug, Default)]
pub struct ShapePoints {
    pub shape_id: Vec<u32>,
    pub sequence: Vec<i32>,
    pub longitude: Vec<f64>,
    pub latitude: Vec<f64>,
}

/// `service_calendar`.
#[derive(Debug, Default)]
pub struct Calendar {
    pub service_id: Vec<u32>,
    pub valid_from: Vec<Date>,
    pub valid_to: Vec<Date>,
    pub weekday_mask: Vec<i16>,
}

/// `service_exception`.
#[derive(Debug, Default)]
pub struct Exceptions {
    pub service_id: Vec<u32>,
    pub service_date: Vec<Date>,
    pub added: Vec<bool>,
}

/// The tables of one feed, for building [`ServingRows`] in memory (tests).
#[derive(Debug, Default)]
pub struct Tables {
    pub source_keys: SourceKeys,
    pub trips: Trips,
    pub routes: Routes,
    pub calls: Calls,
    pub locations: Locations,
    pub shape_points: ShapePoints,
    pub calendar: Calendar,
    pub exceptions: Exceptions,
}

type RowsOf = HashMap<u32, Vec<u32>>;

fn group(column: &[u32]) -> RowsOf {
    let mut rows: RowsOf = HashMap::default();
    for (row, value) in column.iter().enumerate() {
        rows.entry(*value).or_default().push(row as u32);
    }
    rows
}

/// The last row of each value, as a Python dict built over the rows keeps it.
fn last(column: &[u32]) -> HashMap<u32, u32> {
    column
        .iter()
        .enumerate()
        .map(|(row, value)| (*value, row as u32))
        .collect()
}

#[derive(Debug)]
struct KeyTable {
    t: SourceKeys,
    by_pair: HashMap<(u32, u32), Vec<u32>>,
    by_namespace: RowsOf,
}

#[derive(Debug)]
struct TripTable {
    t: Trips,
    by_trip: RowsOf,
    by_run: RowsOf,
    /// `_services`: the service of each trip (the last row's).
    services: HashMap<u32, u32>,
}

#[derive(Debug)]
struct Indexed<T> {
    t: T,
    by: RowsOf,
}

#[derive(Debug)]
struct Unique<T> {
    t: T,
    by: HashMap<u32, u32>,
}

/// `index_sql.Rows` of one feed from its serving tables.
#[derive(Debug)]
pub struct ServingRows {
    dir: Option<PathBuf>,
    strings: Strings,
    keys: Option<KeyTable>,
    trips: Option<TripTable>,
    routes: Option<Unique<Routes>>,
    calls: Option<Indexed<Calls>>,
    locations: Option<Unique<Locations>>,
    shape_points: Option<Indexed<ShapePoints>>,
    calendar: Option<Indexed<Calendar>>,
    exceptions: Option<Indexed<Exceptions>>,
}

fn key_table(t: SourceKeys) -> KeyTable {
    let mut by_pair: HashMap<(u32, u32), Vec<u32>> = HashMap::default();
    for row in 0..t.namespace.len() {
        by_pair
            .entry((t.namespace[row], t.identifier[row]))
            .or_default()
            .push(row as u32);
    }
    let by_namespace = group(&t.namespace);
    KeyTable {
        t,
        by_pair,
        by_namespace,
    }
}

fn trip_table(t: Trips) -> TripTable {
    let by_trip = group(&t.trip_id);
    let by_run = group(&t.run_key);
    let services = t
        .trip_id
        .iter()
        .zip(&t.service_id)
        .map(|(a, b)| (*a, *b))
        .collect();
    TripTable {
        t,
        by_trip,
        by_run,
        services,
    }
}

impl ServingRows {
    /// The serving directory of one feed, read table by table on first use.
    pub fn open(serving: &Path) -> Result<ServingRows, RowsError> {
        if !serving.join("trip.parquet").is_file() {
            return Err(format!("{} holds no serving tables", serving.display()).into());
        }
        Ok(ServingRows {
            dir: Some(serving.to_path_buf()),
            ..ServingRows::empty(Strings::default())
        })
    }

    /// Rows over tables already in memory.
    pub fn from_tables(strings: Strings, tables: Tables) -> ServingRows {
        let mut rows = ServingRows::empty(strings);
        rows.keys = Some(key_table(tables.source_keys));
        rows.trips = Some(trip_table(tables.trips));
        rows.routes = Some(Unique {
            by: last(&tables.routes.route_id),
            t: tables.routes,
        });
        rows.calls = Some(Indexed {
            by: group(&tables.calls.trip_id),
            t: tables.calls,
        });
        rows.locations = Some(Unique {
            by: last(&tables.locations.location_id),
            t: tables.locations,
        });
        rows.shape_points = Some(Indexed {
            by: group(&tables.shape_points.shape_id),
            t: tables.shape_points,
        });
        rows.calendar = Some(Indexed {
            by: group(&tables.calendar.service_id),
            t: tables.calendar,
        });
        rows.exceptions = Some(Indexed {
            by: group(&tables.exceptions.service_id),
            t: tables.exceptions,
        });
        rows
    }

    fn empty(strings: Strings) -> ServingRows {
        ServingRows {
            dir: None,
            strings,
            keys: None,
            trips: None,
            routes: None,
            calls: None,
            locations: None,
            shape_points: None,
            calendar: None,
            exceptions: None,
        }
    }

    /// Read every table now rather than on first use (to time the index apart from the files).
    pub fn read_all(&mut self) -> Result<(), RowsError> {
        self.load_keys()?;
        self.load_trips()?;
        self.load_routes()?;
        self.load_calls()?;
        self.load_locations()?;
        self.load_shape_points()?;
        self.load_calendar()
    }

    fn file(&self, table: &str) -> PathBuf {
        self.dir
            .as_ref()
            .expect("tables not in memory are read from a directory")
            .join(format!("{table}.parquet"))
    }

    fn load_keys(&mut self) -> Result<(), RowsError> {
        if self.keys.is_none() {
            let t = parquet::source_keys(&self.file("source_key"), &mut self.strings)?;
            self.keys = Some(key_table(t));
        }
        Ok(())
    }

    fn load_trips(&mut self) -> Result<(), RowsError> {
        if self.trips.is_none() {
            let t = parquet::trips(&self.file("trip"), &mut self.strings)?;
            self.trips = Some(trip_table(t));
        }
        Ok(())
    }

    fn load_routes(&mut self) -> Result<(), RowsError> {
        if self.routes.is_none() {
            let t = parquet::routes(&self.file("route"), &mut self.strings)?;
            self.routes = Some(Unique {
                by: last(&t.route_id),
                t,
            });
        }
        Ok(())
    }

    fn load_calls(&mut self) -> Result<(), RowsError> {
        if self.calls.is_none() {
            let t = parquet::calls(&self.file("trip_call"), &mut self.strings)?;
            self.calls = Some(Indexed {
                by: group(&t.trip_id),
                t,
            });
        }
        Ok(())
    }

    fn load_locations(&mut self) -> Result<(), RowsError> {
        if self.locations.is_none() {
            let t = parquet::locations(&self.file("location"), &mut self.strings)?;
            self.locations = Some(Unique {
                by: last(&t.location_id),
                t,
            });
        }
        Ok(())
    }

    fn load_shape_points(&mut self) -> Result<(), RowsError> {
        if self.shape_points.is_none() {
            let t = parquet::shape_points(&self.file("shape_point"), &mut self.strings)?;
            self.shape_points = Some(Indexed {
                by: group(&t.shape_id),
                t,
            });
        }
        Ok(())
    }

    fn load_calendar(&mut self) -> Result<(), RowsError> {
        if self.calendar.is_none() {
            let t = parquet::calendar(&self.file("service_calendar"), &mut self.strings)?;
            self.calendar = Some(Indexed {
                by: group(&t.service_id),
                t,
            });
        }
        if self.exceptions.is_none() {
            let t = parquet::exceptions(&self.file("service_exception"), &mut self.strings)?;
            self.exceptions = Some(Indexed {
                by: group(&t.service_id),
                t,
            });
        }
        Ok(())
    }

    /// The rows of `values` (looked up by text) in an index by value, in file order.
    fn rows_of(&self, by: &RowsOf, values: &[&str]) -> Vec<u32> {
        let wanted: HashSet<u32> = values.iter().filter_map(|v| self.strings.get(v)).collect();
        let mut rows: Vec<u32> = wanted
            .iter()
            .filter_map(|v| by.get(v))
            .flatten()
            .copied()
            .collect();
        rows.sort_unstable();
        rows
    }

    /// The location-kind keys of `namespace`, in file order.
    fn location_keys(&self, namespace: &str) -> Vec<u32> {
        let keys = self.keys.as_ref().expect("loaded");
        let location = self.strings.get("location");
        self.strings
            .get(namespace)
            .and_then(|ns| keys.by_namespace.get(&ns))
            .into_iter()
            .flatten()
            .copied()
            .filter(|row| Some(keys.t.entity_kind[*row as usize]) == location)
            .collect()
    }
}

/// The leading part of a composite key, as SQL's `split_part(identifier, ':', 1)`.
fn line(identifier: &str) -> &str {
    identifier
        .split_once(':')
        .map_or(identifier, |(head, _)| head)
}

fn owned(text: Option<&str>) -> Option<String> {
    text.map(str::to_owned)
}

/// ISO day of the week, 1 = Monday (1970-01-01 was a Thursday).
fn isoweekday(day: Date) -> i32 {
    (day.days_since_epoch() + 3).rem_euclid(7) + 1
}

impl Rows for ServingRows {
    fn keys(
        &mut self,
        namespaces: &[&str],
        identifiers: &[&str],
    ) -> Result<Vec<KeyRow>, RowsError> {
        self.load_keys()?;
        self.load_trips()?;
        let (keys, trips, s) = (
            self.keys.as_ref().expect("loaded"),
            self.trips.as_ref().expect("loaded"),
            &self.strings,
        );
        let mut wanted: HashSet<(u32, u32)> = HashSet::default();
        for (namespace, identifier) in namespaces.iter().zip(identifiers) {
            if let (Some(n), Some(i)) = (s.get(namespace), s.get(identifier)) {
                wanted.insert((n, i));
            }
        }
        let mut rows: Vec<u32> = wanted
            .iter()
            .filter_map(|pair| keys.by_pair.get(pair))
            .flatten()
            .copied()
            .collect();
        let t = &keys.t;
        let sort_key = |row: &u32| {
            let r = *row as usize;
            (
                s.text(t.namespace[r]),
                s.text(t.identifier[r]),
                s.text(t.public_id[r]),
                t.valid_from[r],
                *row,
            )
        };
        rows.sort_by(|a, b| sort_key(a).cmp(&sort_key(b)));
        let trip = s.get("trip");
        Ok(rows
            .into_iter()
            .map(|row| {
                let r = row as usize;
                let service_id = if Some(t.entity_kind[r]) == trip {
                    trips
                        .services
                        .get(&t.public_id[r])
                        .map(|v| s.text(*v).to_owned())
                } else {
                    None
                };
                KeyRow {
                    namespace: s.text(t.namespace[r]).to_owned(),
                    identifier: s.text(t.identifier[r]).to_owned(),
                    public_id: s.text(t.public_id[r]).to_owned(),
                    entity_kind: s.text(t.entity_kind[r]).to_owned(),
                    valid_from: t.valid_from[r],
                    valid_to: t.valid_to[r],
                    service_id,
                }
            })
            .collect())
    }

    fn line_keys(
        &mut self,
        namespace: &str,
        lines: &[&str],
    ) -> Result<Vec<(String, String)>, RowsError> {
        self.load_keys()?;
        let keys = self.keys.as_ref().expect("loaded");
        let wanted: HashSet<&str> = lines.iter().copied().collect();
        let found: BTreeSet<(String, String)> = self
            .strings
            .get(namespace)
            .and_then(|ns| keys.by_namespace.get(&ns))
            .into_iter()
            .flatten()
            .map(|row| self.strings.text(keys.t.identifier[*row as usize]))
            .filter(|i| wanted.contains(line(i)))
            .map(|i| (line(i).to_owned(), i.to_owned()))
            .collect();
        Ok(found.into_iter().collect())
    }

    fn trips(&mut self, trip_ids: &[&str]) -> Result<Vec<TripRow>, RowsError> {
        self.load_keys()?;
        self.load_trips()?;
        self.load_routes()?;
        let (keys, trips, routes, s) = (
            self.keys.as_ref().expect("loaded"),
            self.trips.as_ref().expect("loaded"),
            self.routes.as_ref().expect("loaded"),
            &self.strings,
        );
        let wanted: HashSet<u32> = trip_ids.iter().filter_map(|t| s.get(t)).collect();
        let mut numbers: HashMap<u32, &str> = HashMap::default();
        for row in s
            .get(TRAIN_NUMBER)
            .and_then(|ns| keys.by_namespace.get(&ns))
            .into_iter()
            .flatten()
        {
            let r = *row as usize;
            if wanted.contains(&keys.t.public_id[r]) {
                let identifier = s.text(keys.t.identifier[r]);
                let number = numbers.entry(keys.t.public_id[r]).or_insert(identifier);
                if identifier < *number {
                    *number = identifier;
                }
            }
        }
        let t = &trips.t;
        let mut found = Vec::new();
        for row in self.rows_of(&trips.by_trip, trip_ids) {
            let r = row as usize;
            let Some(route) = routes.by.get(&t.route_id[r]) else {
                continue; // the SQL joins the route
            };
            let route = *route as usize;
            let short_name = s.opt(routes.t.short_name[route]);
            found.push(TripRow {
                trip_id: s.text(t.trip_id[r]).to_owned(),
                route_id: s.text(t.route_id[r]).to_owned(),
                route_name: short_name.unwrap_or(s.text(t.route_id[r])).to_owned(),
                mode: s.text(routes.t.mode[route]).to_owned(),
                service_id: s.text(t.service_id[r]).to_owned(),
                headsign: owned(s.opt(t.headsign[r])),
                shape_id: owned(s.opt(t.shape_id[r])),
                run_key: owned(s.opt(t.run_key[r])),
                run_part: t.run_part[r],
                agency_id: owned(s.opt(routes.t.agency_id[route])),
                train_number: numbers.get(&t.trip_id[r]).map(|n| (*n).to_owned()),
            });
        }
        Ok(found)
    }

    fn calls(&mut self, trip_ids: &[&str]) -> Result<Vec<CallRow>, RowsError> {
        self.load_calls()?;
        self.load_locations()?;
        let (calls, locations, s) = (
            self.calls.as_ref().expect("loaded"),
            self.locations.as_ref().expect("loaded"),
            &self.strings,
        );
        let t = &calls.t;
        let mut rows = self.rows_of(&calls.by, trip_ids);
        rows.sort_by(|a, b| {
            let (a, b) = (*a as usize, *b as usize);
            (s.text(t.trip_id[a]), t.sequence[a]).cmp(&(s.text(t.trip_id[b]), t.sequence[b]))
        });
        let exact = s.get("exact");
        let mut visits: HashMap<(u32, u32), i64> = HashMap::default();
        Ok(rows
            .into_iter()
            .map(|row| {
                let r = row as usize;
                let visit = visits.entry((t.trip_id[r], t.location_id[r])).or_insert(0);
                *visit += 1;
                let post = t.boarding_point_id[r];
                let placed = (post != NONE)
                    .then(|| locations.by.get(&post))
                    .flatten()
                    .is_some_and(|l| Some(locations.t.coordinate_precision[*l as usize]) == exact);
                CallRow {
                    trip_id: s.text(t.trip_id[r]).to_owned(),
                    sequence: t.sequence[r],
                    location_id: s.text(t.location_id[r]).to_owned(),
                    visit_n: *visit,
                    passenger_service: t.passenger_service[r],
                    arrival: t.scheduled_arrival[r],
                    departure: t.scheduled_departure[r],
                    on_request: t.pickup_type[r] == 3 || t.dropoff_type[r] == 3,
                    point_id: placed.then(|| s.text(post).to_owned()),
                }
            })
            .collect())
    }

    fn run_parts(&mut self, run_keys: &[&str]) -> Result<Vec<RunPartRow>, RowsError> {
        self.load_trips()?;
        let (trips, s) = (self.trips.as_ref().expect("loaded"), &self.strings);
        let t = &trips.t;
        let mut rows = self.rows_of(&trips.by_run, run_keys);
        // NULLs sort last in PostgreSQL's ascending order.
        let sort_key = |row: &u32| {
            let r = *row as usize;
            (
                s.text(t.run_key[r]),
                t.run_part[r].is_none(),
                t.run_part[r].unwrap_or(0),
                s.text(t.trip_id[r]),
                *row,
            )
        };
        rows.sort_by(|a, b| sort_key(a).cmp(&sort_key(b)));
        Ok(rows
            .into_iter()
            .map(|row| {
                let r = row as usize;
                RunPartRow {
                    run_key: s.text(t.run_key[r]).to_owned(),
                    trip_id: s.text(t.trip_id[r]).to_owned(),
                    service_id: s.text(t.service_id[r]).to_owned(),
                }
            })
            .collect())
    }

    fn station_keys(&mut self, location_ids: &[&str]) -> Result<Vec<(String, String)>, RowsError> {
        self.load_keys()?;
        let wanted: HashSet<u32> = location_ids
            .iter()
            .filter_map(|l| self.strings.get(l))
            .collect();
        let keys = &self.keys.as_ref().expect("loaded").t;
        let s = &self.strings;
        let mut found: Vec<(String, String)> = self
            .location_keys("sr70")
            .into_iter()
            .map(|row| row as usize)
            .filter(|r| wanted.contains(&keys.public_id[*r]))
            .map(|r| {
                (
                    s.text(keys.identifier[r]).to_owned(),
                    s.text(keys.public_id[r]).to_owned(),
                )
            })
            .collect();
        found.sort();
        Ok(found)
    }

    fn track_keys(&mut self, codes: &[&str]) -> Result<Vec<(String, String)>, RowsError> {
        self.load_keys()?;
        let wanted: HashSet<&str> = codes.iter().copied().collect();
        let keys = &self.keys.as_ref().expect("loaded").t;
        let s = &self.strings;
        let mut found: Vec<(String, String)> = self
            .location_keys("sr70:track")
            .into_iter()
            .map(|row| row as usize)
            .filter(|r| wanted.contains(line(s.text(keys.identifier[*r]))))
            .map(|r| {
                (
                    s.text(keys.identifier[r]).to_owned(),
                    s.text(keys.public_id[r]).to_owned(),
                )
            })
            .collect();
        found.sort();
        Ok(found)
    }

    fn locations(&mut self, location_ids: &[&str]) -> Result<Vec<LocationRow>, RowsError> {
        self.load_locations()?;
        let (locations, s) = (self.locations.as_ref().expect("loaded"), &self.strings);
        let t = &locations.t;
        Ok(location_ids
            .iter()
            .filter_map(|id| s.get(id).and_then(|v| locations.by.get(&v)))
            .map(|row| {
                let r = *row as usize;
                LocationRow {
                    location_id: s.text(t.location_id[r]).to_owned(),
                    name: s.text(t.name[r]).to_owned(),
                    lon: t.longitude[r],
                    lat: t.latitude[r],
                }
            })
            .collect())
    }

    fn shape_points(&mut self, shape_ids: &[&str]) -> Result<Vec<ShapePointRow>, RowsError> {
        self.load_shape_points()?;
        let (points, s) = (self.shape_points.as_ref().expect("loaded"), &self.strings);
        let t = &points.t;
        let mut rows = self.rows_of(&points.by, shape_ids);
        rows.sort_by(|a, b| {
            let (a, b) = (*a as usize, *b as usize);
            (s.text(t.shape_id[a]), t.sequence[a]).cmp(&(s.text(t.shape_id[b]), t.sequence[b]))
        });
        Ok(rows
            .into_iter()
            .map(|row| {
                let r = row as usize;
                ShapePointRow {
                    shape_id: s.text(t.shape_id[r]).to_owned(),
                    lon: t.longitude[r],
                    lat: t.latitude[r],
                }
            })
            .collect())
    }

    /// As `release.load` derives `static.service_date`: the calendar's weekdays inside its
    /// validity (bit 0 of the mask is Monday) less the removed dates, plus the added ones.
    fn service_dates(
        &mut self,
        service_ids: &[&str],
        days: &[Date],
    ) -> Result<Vec<(String, Date)>, RowsError> {
        self.load_calendar()?;
        let (calendar, exceptions, s) = (
            self.calendar.as_ref().expect("loaded"),
            self.exceptions.as_ref().expect("loaded"),
            &self.strings,
        );
        let wanted: HashSet<Date> = days.iter().copied().collect();
        let mut added: BTreeSet<(u32, Date)> = BTreeSet::new();
        let mut removed: HashSet<(u32, Date)> = HashSet::default();
        for row in self.rows_of(&exceptions.by, service_ids) {
            let r = row as usize;
            let (service, day) = (exceptions.t.service_id[r], exceptions.t.service_date[r]);
            if wanted.contains(&day) {
                if exceptions.t.added[r] {
                    added.insert((service, day));
                } else {
                    removed.insert((service, day));
                }
            }
        }
        let mut running: BTreeSet<(&str, Date)> =
            added.iter().map(|(sv, d)| (s.text(*sv), *d)).collect();
        for service_id in service_ids {
            let Some(service) = s.get(service_id) else {
                continue;
            };
            for row in calendar.by.get(&service).into_iter().flatten() {
                let r = *row as usize;
                let t = &calendar.t;
                for day in days {
                    if t.valid_from[r] <= *day
                        && *day <= t.valid_to[r]
                        && i32::from(t.weekday_mask[r]) & (1 << (isoweekday(*day) - 1)) != 0
                        && !removed.contains(&(service, *day))
                    {
                        running.insert((s.text(service), *day));
                    }
                }
            }
        }
        Ok(running
            .into_iter()
            .map(|(sv, d)| (sv.to_owned(), d))
            .collect())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn weekdays_and_lines() {
        assert_eq!(isoweekday(Date::ymd(2026, 10, 5)), 1); // a Monday
        assert_eq!(isoweekday(Date::ymd(2026, 10, 11)), 7);
        assert_eq!(isoweekday(Date::ymd(1969, 12, 31)), 3);
        assert_eq!(line("582492:143"), "582492");
        assert_eq!(line("54000"), "54000");
    }
}
