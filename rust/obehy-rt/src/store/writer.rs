//! Core effects and state as rows: port of `emit/db.py`'s `Writer`.
//!
//! `write` turns one batch's effects into rows, `write_state` keeps the current-state tables
//! in step with core state; each is one transaction of the store. Ordering kept from Python:
//! partitions and terms are prepared outside the transaction (so a rolled-back write never
//! leaves a term number known but not stored); within it observations, snapshots, events, then
//! the live attributes (`Book::observe`, before assignments: what a report states may name its
//! vehicle), assignments, links, platforms, cancellations and the book's rows.

use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc;

use obehy_core::model::{
    BindingMethod, CallState, Derivation, Effect, Feed, FeedState, Instance, JourneyKey, Lifecycle,
    ObservationResult, PartSpan, VehicleId, VehicleKey,
};
use obehy_core::publish::{representing, shown};
use obehy_core::py::json::PyJson;
use obehy_core::runs::{current_part, published_parts};
use obehy_core::time::{Date, Instant, TimeDelta, local_date};

use super::cells::{Cell, Row};
use super::rows::{
    Drawn, calls_json, cancellation_row, event_row, link_row, observation_row, part_row,
    platform_row, trip_row, vehicle_row,
};
use super::storage::hour_of;
use super::vehicles::{Book, Report};
use super::{PARTS, Prepare, StateTable, Store, StoreResult, TRIPS, VEHICLES};

/// A feed's current-state rows as last written, kept up to date in place: per vehicle its row,
/// per journey its row, the calls JSON with the calls it was made from, what else the row was
/// made from, and its published parts' rows. Maps by typed keys iterate in the order of the
/// rows' key cells (within a feed: source and vehicle ID; namespace, key, service date; then
/// the part's trip ID), which is the order rows are sent in. `seen` marks the entries of the
/// current write; the others went.
#[derive(Debug, Default)]
struct Written {
    vehicles: BTreeMap<VehicleId, (Row, u64)>,
    trips: BTreeMap<JourneyKey, TripWritten>,
    seen: u64,
}

#[derive(Debug)]
struct TripWritten {
    row: Row,
    calls: Arc<[CallState]>,
    json: Arc<PyJson>,
    inputs: TripInputs,
    /// Published parts by trip ID, sorted: (trip ID, row).
    parts: Vec<(String, Row)>,
    seen: u64,
}

/// Everything of an instance its trip row and part rows read besides its journey and calls
/// (`trip_row`, `sources_json`, `published_parts`, `part_row`): the rows stay the same while
/// these do.
#[derive(Debug, Clone, PartialEq)]
struct TripInputs {
    release_id: String,
    trip_id: String,
    lifecycle: Lifecycle,
    lateness_s: Option<i64>,
    off_route: bool,
    stale: bool,
    updated_at: Instant,
    /// Per source track: (source, heard_at, position_at, delay_s), in the instance's order.
    sources: Vec<(String, Instant, Option<Instant>, Option<i64>)>,
    track_source: Option<String>,
    parts: Vec<PartSpan>,
    /// Whether the instance's own journey was its key (the rows' key cells are its own).
    keyed: bool,
}

impl TripInputs {
    fn of(i: &Instance, journey: &JourneyKey) -> TripInputs {
        TripInputs {
            release_id: i.release_id.clone(),
            trip_id: i.trip_id.clone(),
            lifecycle: i.lifecycle,
            lateness_s: i.lateness_s,
            off_route: i.off_route,
            stale: i.freshness.stale,
            updated_at: i.freshness.updated_at,
            sources: i
                .sources
                .iter()
                .map(|t| (t.source.clone(), t.heard_at, t.position_at, t.delay_s))
                .collect(),
            track_source: i.track_source.clone(),
            parts: i.parts.clone(),
            keyed: i.journey == *journey,
        }
    }

    fn matches(&self, i: &Instance, journey: &JourneyKey) -> bool {
        self.keyed
            && i.journey == *journey
            && self.lifecycle == i.lifecycle
            && self.lateness_s == i.lateness_s
            && self.off_route == i.off_route
            && self.stale == i.freshness.stale
            && self.updated_at == i.freshness.updated_at
            && self.release_id == i.release_id
            && self.trip_id == i.trip_id
            && self.track_source.as_deref() == i.track_source.as_deref()
            && self.sources.len() == i.sources.len()
            && self.sources.iter().zip(&i.sources).all(|(s, t)| {
                s.0 == t.source && s.1 == t.heard_at && s.2 == t.position_at && s.3 == t.delay_s
            })
            && self.parts == i.parts
    }
}

/// One state table's changes in a write: the keys of the rows that went and of the rows that
/// changed or came, each in key order.
#[derive(Debug)]
struct Changes<K> {
    gone: Vec<K>,
    changed: Vec<K>,
}

impl<K> Default for Changes<K> {
    fn default() -> Self {
        Changes {
            gone: Vec::new(),
            changed: Vec::new(),
        }
    }
}

#[derive(Debug)]
pub struct Writer<S: Store, B: Book> {
    pub store: S,
    pub derivation: Derivation,
    /// Which vehicle each report is, live attributes and profiles.
    pub vehicles: B,
    state: BTreeMap<Feed, Written>,
    /// The vehicle book's eviction: forget reports unseen this long, checked this often (book
    /// time), next at; None: never (Python).
    forget: Option<(TimeDelta, TimeDelta, Option<Instant>)>,
    /// Reports the book forgot so far.
    pub forgotten: usize,
}

fn journey_key_cells(journey: &JourneyKey) -> Row {
    vec![
        Cell::text(journey.feed.as_str()),
        Cell::text(&journey.namespace),
        Cell::text(&journey.key),
        Cell::Date(journey.service_date),
    ]
}

impl<S: Store, B: Book> Writer<S, B> {
    /// Open `store` under `derivation`; `book` makes the vehicle book of what it holds.
    pub fn open(
        mut store: S,
        derivation: Derivation,
        book: impl FnOnce(super::StoredVehicles) -> B,
    ) -> StoreResult<Writer<S, B>> {
        let stored = store.open(&derivation)?;
        Ok(Writer {
            store,
            derivation,
            vehicles: book(stored),
            state: BTreeMap::new(),
            forget: None,
            forgotten: 0,
        })
    }

    /// The vehicle book forgets reports unseen for `after_s`, checked every `every_s` of book
    /// time at a state write (policy `emit.forget_reports_s`, `emit.sweep_index_every_s`).
    pub fn forget_reports(&mut self, after_s: Option<i64>, every_s: i64) {
        self.forget = after_s.filter(|s| *s > 0).map(|after| {
            (
                TimeDelta::from_seconds(after),
                TimeDelta::from_seconds(every_s),
                None,
            )
        });
    }

    fn evict(&mut self) {
        let (Some((after, every, next)), Some(clock)) = (&mut self.forget, self.vehicles.clock())
        else {
            return;
        };
        match *next {
            None => *next = Some(clock + *every),
            Some(due) if clock >= due => {
                self.forgotten += self.vehicles.forget_unseen(clock - *after);
                *next = Some(clock + *every);
            }
            Some(_) => {}
        }
    }

    /// `obehy ref import` loaded a dataset: identities and profiles are redone.
    pub fn reload_registry(&mut self) -> StoreResult<()> {
        let registry = self.store.reload_registry()?;
        self.vehicles.reload(registry);
        Ok(())
    }

    fn transaction(&mut self, body: impl FnOnce(&mut Self) -> StoreResult<()>) -> StoreResult<()> {
        self.store.begin()?;
        match body(self) {
            Ok(()) => self.store.commit(),
            Err(error) => {
                self.store.rollback();
                Err(error)
            }
        }
    }

    /// Container sizes for the periodic summary, summed over feeds: the state rows last
    /// written (vehicles, trips, parts) and the journeys' cached calls JSON. Each follows the
    /// state it mirrors: a row leaves when its vehicle or journey leaves the state.
    pub fn sizes(&self) -> (usize, usize, usize, usize) {
        self.state.values().fold((0, 0, 0, 0), |(v, t, p, c), w| {
            (
                v + w.vehicles.len(),
                t + w.trips.len(),
                p + w.trips.values().map(|t| t.parts.len()).sum::<usize>(),
                c + w.trips.len(),
            )
        })
    }

    // --- effects ---------------------------------------------------------------------------

    /// One batch's effects: rows in one transaction.
    pub fn write(&mut self, effects: &[Effect]) -> StoreResult<()> {
        let mut results = Vec::new();
        let mut snapshots = Vec::new();
        let mut events = Vec::new();
        let mut assigned = Vec::new();
        let mut links = Vec::new();
        let mut platforms = Vec::new();
        let mut cancellations = Vec::new();
        for effect in effects {
            match effect {
                Effect::ObservationResult(e) => results.push(e),
                Effect::SnapshotJourney(e) => snapshots.push(e),
                Effect::WriteEvent(e) => events.push(e),
                Effect::AssignVehicle(e) => assigned.push(e),
                Effect::LinkJourneys(e) => links.push(e),
                Effect::RecordPlatform(e) => platforms.push(e),
                Effect::RecordCancellation(e) => cancellations.push(e),
                Effect::Unresolved(_) => {}
            }
        }
        let mut prepare = Prepare {
            hours: results
                .iter()
                .map(|r| hour_of(r.observation.received_at))
                .collect(),
            days: snapshots
                .iter()
                .map(|e| e.journey.service_date)
                .chain(platforms.iter().map(|e| e.journey.service_date))
                .chain(cancellations.iter().map(|e| e.journey.service_date))
                .collect(),
            terms: Vec::new(),
        };
        for s in &snapshots {
            for c in s.calls.iter() {
                prepare.terms.push(c.location_id.clone());
                prepare.terms.push(c.name.clone());
            }
        }
        for e in &events {
            prepare.terms.push(e.location_id.clone());
            prepare.terms.push(e.method.as_str().to_owned());
            prepare.terms.push(e.source.clone());
        }
        self.store.prepare(&prepare)?;
        let release_id = self.derivation.release_id.clone();
        self.transaction(|w| {
            let rows: Vec<Row> = results
                .iter()
                .map(|r| observation_row(r, &release_id))
                .collect();
            w.store.observations(&rows)?;
            w.store.snapshots(&snapshots)?;
            let rows: Vec<Row> = events.iter().map(|e| event_row(e)).collect();
            w.store.events(&rows)?;
            // Live attributes first: what a report states may name its vehicle.
            let attributes = w.vehicles.observe(&results);
            let rows = w.assignments(&results, &assigned);
            w.store.assignments(&rows)?;
            let rows: Vec<Row> = links.iter().map(|l| link_row(l)).collect();
            w.store.links(&rows)?;
            let rows: Vec<Row> = platforms.iter().map(|e| platform_row(e)).collect();
            w.store.platforms(&rows)?;
            let rows: Vec<Row> = cancellations.iter().map(|c| cancellation_row(c)).collect();
            w.store.cancellations(&rows)?;
            let vehicle_rows = w.vehicles.rows(attributes);
            w.store.vehicles(&vehicle_rows)
        })
    }

    fn assignments(
        &mut self,
        results: &[&ObservationResult],
        assigned: &[&obehy_core::model::AssignVehicle],
    ) -> Vec<Row> {
        type Seen = BTreeMap<(String, String, JourneyKey), (Instant, Instant, BindingMethod)>;
        let mut seen: Seen = BTreeMap::new();
        let mut note = |key: (String, String, JourneyKey), at: Instant, method: BindingMethod| {
            let entry = seen.entry(key).or_insert((at, at, method));
            // `min`/`max` keep the first of equal instants: the same value either way.
            entry.0 = entry.0.min(at);
            entry.1 = entry.1.max(at);
        };
        for result in results {
            let obs = &result.observation;
            let (Some(journey), Some(vehicle)) = (&result.journey, obs.first::<VehicleKey>())
            else {
                continue;
            };
            // History is keyed by public journeys (rail: train numbers), not by the run.
            let public: &[JourneyKey] = if result.public.is_empty() {
                std::slice::from_ref(journey)
            } else {
                &result.public
            };
            for journey in public {
                let key = (
                    obs.source.clone(),
                    vehicle.source_vehicle_id.clone(),
                    journey.clone(),
                );
                note(key, obs.at(), result.method);
            }
        }
        for assignment in assigned {
            let v = &assignment.vehicle;
            let key = (
                v.source.clone(),
                v.source_vehicle_id.clone(),
                assignment.journey.clone(),
            );
            note(key, assignment.at, assignment.method);
        }
        seen.into_iter()
            .map(|((source, vehicle, journey), (lo, hi, method))| {
                let report: Report = (source, vehicle);
                let worked_by = self.vehicles.worked_by(&report, journey.service_date);
                vec![
                    Cell::text(journey.feed.as_str()),
                    Cell::Text(report.0),
                    Cell::Text(report.1),
                    Cell::text(&journey.namespace),
                    Cell::text(&journey.key),
                    Cell::Date(journey.service_date),
                    Cell::Time(lo),
                    Cell::Time(hi),
                    Cell::text(method.as_str()),
                    Cell::Text(worked_by),
                ]
            })
            .collect()
    }

    // --- current state and rebuilds -------------------------------------------------------

    /// Bring the current-state tables in line with `states`: a feed's first write replaces its
    /// rows; later writes upsert the rows that changed since and delete the ones that went.
    pub fn write_state(&mut self, states: &[(Feed, &FeedState)]) -> StoreResult<()> {
        let mut sorted: Vec<(Feed, &FeedState)> = states.to_vec();
        sorted.sort_by_key(|(feed, _)| *feed);
        // Pairings first: they may change who a report is.
        self.vehicles.track(&sorted);
        self.evict();
        let outcome = self.transaction(|w| {
            for (feed, state) in &sorted {
                w.write_feed(*feed, state)?;
            }
            // Profiles of vehicles whose identity changed with these rows.
            let vehicle_rows = w.vehicles.rows(Vec::new());
            w.store.vehicles(&vehicle_rows)
        });
        if outcome.is_err() {
            self.state.clear(); // unknown what was written: rewrite in full next time
        }
        outcome
    }

    /// One feed's state tables: in full at its first write, else what changed.
    fn write_feed(&mut self, feed: Feed, state: &FeedState) -> StoreResult<()> {
        let first = !self.state.contains_key(&feed);
        let mut written = self.state.remove(&feed).unwrap_or_default();
        written.seen += 1;
        let vehicles = self.vehicle_rows(feed, state, &mut written);
        let (trips, parts) = self.trip_rows(state, &mut written);
        if first {
            let all: Vec<Row> = written
                .vehicles
                .values()
                .map(|(row, _)| row.clone())
                .collect();
            self.store.replace_state(VEHICLES, feed, &all)?;
            let all: Vec<Row> = written.trips.values().map(|t| t.row.clone()).collect();
            self.store.replace_state(TRIPS, feed, &all)?;
            let all: Vec<Row> = written
                .trips
                .values()
                .flat_map(|t| t.parts.iter().map(|(_, row)| row.clone()))
                .collect();
            self.store.replace_state(PARTS, feed, &all)?;
        } else {
            let gone: Vec<Row> = vehicles.gone.iter().map(|v| vehicle_key(feed, v)).collect();
            let changed: Vec<Row> = vehicles
                .changed
                .iter()
                .map(|v| written.vehicles[v].0.clone())
                .collect();
            write_changes(&mut self.store, VEHICLES, &gone, &changed)?;
            let gone: Vec<Row> = trips.gone.iter().map(journey_key_cells).collect();
            let changed: Vec<Row> = trips
                .changed
                .iter()
                .map(|j| written.trips[j].row.clone())
                .collect();
            write_changes(&mut self.store, TRIPS, &gone, &changed)?;
            let gone: Vec<Row> = parts.gone.iter().map(|(j, p)| part_key(j, p)).collect();
            let changed: Vec<Row> = parts.changed.into_iter().map(|(_, row)| row).collect();
            write_changes(&mut self.store, PARTS, &gone, &changed)?;
        }
        self.state.insert(feed, written);
        Ok(())
    }

    /// The vehicle rows now, into `written`; which went and which changed.
    fn vehicle_rows(
        &mut self,
        feed: Feed,
        state: &FeedState,
        written: &mut Written,
    ) -> Changes<VehicleId> {
        let release_id = self.derivation.release_id.clone();
        let shown_set = representing(state);
        let seen = written.seen;
        let mut changes = Changes::default();
        for (vehicle, v) in &state.vehicles {
            let b = v.binding.as_ref();
            let instance = b.and_then(|b| state.instances.get(&b.journey));
            let trip_id = b.map(|b| match instance {
                None => b.trip_id.clone(),
                Some(instance) => current_part(instance),
            });
            let mut drawn = match &v.position {
                None => Drawn {
                    lat: None,
                    lon: None,
                    bearing: None,
                    at: v.position_at,
                },
                Some(p) => Drawn {
                    lat: Some(p.lat),
                    lon: Some(p.lon),
                    bearing: p.bearing,
                    at: v.position_at,
                },
            };
            let represents = shown_set.contains(vehicle);
            if represents && let Some(p) = shown(instance, vehicle) {
                drawn = Drawn {
                    lat: Some(p.lat),
                    lon: Some(p.lon),
                    bearing: p.bearing,
                    at: Some(p.at),
                };
            }
            let report: Report = (vehicle.source.clone(), vehicle.source_vehicle_id.clone());
            let identity = self.vehicles.identify(&report, local_date(v.last_seen));
            let row = vehicle_row(feed, v, trip_id, drawn, &release_id, identity, represents);
            match written.vehicles.get_mut(vehicle) {
                Some((old, at)) => {
                    *at = seen;
                    if *old != row {
                        *old = row;
                        changes.changed.push(vehicle.clone());
                    }
                }
                None => {
                    written.vehicles.insert(vehicle.clone(), (row, seen));
                    changes.changed.push(vehicle.clone());
                }
            }
        }
        written.vehicles.retain(|vehicle, (_, at)| {
            let kept = *at == seen;
            if !kept {
                changes.gone.push(vehicle.clone());
            }
            kept
        });
        changes.changed.sort();
        changes
    }

    /// The trip and part rows now, into `written`; which went and which changed. A journey
    /// whose instance would give the same rows as last time (the same calls and the same
    /// [`TripInputs`]) is not made again.
    fn trip_rows(
        &mut self,
        state: &FeedState,
        written: &mut Written,
    ) -> (Changes<JourneyKey>, PartChanges) {
        let release_id = &self.derivation.release_id;
        let seen = written.seen;
        let mut trips: Changes<JourneyKey> = Changes::default();
        let mut parts = PartChanges::default();
        for (journey, instance) in &state.instances {
            let entry = written.trips.get_mut(journey);
            let unchanged = entry.as_ref().is_some_and(|e| {
                Arc::ptr_eq(&e.calls, &instance.calls) && e.inputs.matches(instance, journey)
            });
            if unchanged {
                entry.expect("an unchanged journey was written").seen = seen;
                continue;
            }
            // Python re-encodes only when the calls tuple is a new object; the `passed` flags
            // follow `left_index` only then (kept: the output harness pins it).
            let (calls, json) = match &entry {
                Some(e) if Arc::ptr_eq(&e.calls, &instance.calls) => {
                    (Arc::clone(&e.calls), Arc::clone(&e.json))
                }
                _ => (Arc::clone(&instance.calls), Arc::new(calls_json(instance))),
            };
            let row = trip_row(instance, Arc::clone(&json));
            // By key: a repeated trip ID keeps its last row, as a map insert does.
            let mut now_parts: Vec<(String, Row)> = Vec::new();
            for part in published_parts(instance) {
                let row = part_row(instance, &part, release_id);
                match now_parts.binary_search_by(|(t, _)| t.cmp(&part.trip_id)) {
                    Ok(at) => now_parts[at].1 = row,
                    Err(at) => now_parts.insert(at, (part.trip_id, row)),
                }
            }
            let fresh = TripWritten {
                row,
                calls,
                json,
                inputs: TripInputs::of(instance, journey),
                parts: now_parts,
                seen,
            };
            match entry {
                Some(e) => {
                    if e.row != fresh.row {
                        trips.changed.push(journey.clone());
                    }
                    parts.compare(journey, &e.parts, &fresh.parts);
                    *e = fresh;
                }
                None => {
                    trips.changed.push(journey.clone());
                    parts.compare(journey, &[], &fresh.parts);
                    written.trips.insert(journey.clone(), fresh);
                }
            }
        }
        written.trips.retain(|journey, t| {
            let kept = t.seen == seen;
            if !kept {
                trips.gone.push(journey.clone());
                parts.compare(journey, &t.parts, &[]);
            }
            kept
        });
        trips.changed.sort();
        parts.sort();
        (trips, parts)
    }

    /// Delete the history of service dates before a rebuild by replay.
    pub fn clear_history(&mut self, days: impl IntoIterator<Item = Date>) -> StoreResult<()> {
        let days: BTreeSet<Date> = days.into_iter().collect();
        let days: Vec<Date> = days.into_iter().collect();
        self.store.clear_history(&days)
    }
}

/// A table's changes: the rows that went (by key), then the rows that changed or came.
fn write_changes<S: Store>(
    store: &mut S,
    table: StateTable,
    gone: &[Row],
    changed: &[Row],
) -> StoreResult<()> {
    if !gone.is_empty() {
        store.delete_state(table, gone)?;
    }
    if !changed.is_empty() {
        store.upsert_state(table, changed)?;
    }
    Ok(())
}

fn vehicle_key(feed: Feed, vehicle: &VehicleId) -> Row {
    vec![
        Cell::text(feed.as_str()),
        Cell::text(&vehicle.source),
        Cell::text(&vehicle.source_vehicle_id),
    ]
}

fn part_key(journey: &JourneyKey, trip_id: &str) -> Row {
    let mut key = journey_key_cells(journey);
    key.push(Cell::text(trip_id));
    key
}

/// The part table's changes: the keys of the rows that went, and the rows that changed or
/// came; each in key order once sorted.
#[derive(Debug, Default)]
struct PartChanges {
    gone: Vec<(JourneyKey, String)>,
    changed: Vec<((JourneyKey, String), Row)>,
}

impl PartChanges {
    /// A journey's part rows from `before` to `now` (each by trip ID).
    fn compare(&mut self, journey: &JourneyKey, before: &[(String, Row)], now: &[(String, Row)]) {
        for (trip_id, _) in before {
            if !now.iter().any(|(t, _)| t == trip_id) {
                self.gone.push((journey.clone(), trip_id.clone()));
            }
        }
        for (trip_id, row) in now {
            if before.iter().find(|(t, _)| t == trip_id).map(|(_, r)| r) != Some(row) {
                self.changed
                    .push(((journey.clone(), trip_id.clone()), row.clone()));
            }
        }
    }

    fn sort(&mut self) {
        self.gone.sort();
        self.changed.sort_by(|a, b| a.0.cmp(&b.0));
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::bench::NullStore;
    use crate::store::vehicles::{AttributeRow, PlainBook, RegistryRows, Report, VehicleRows};

    /// A plain book with a clock, recording what it was asked to forget.
    #[derive(Debug, Default)]
    struct Clocked {
        clock: Option<Instant>,
        forgot: Vec<Instant>,
    }

    impl Book for Clocked {
        fn observe(&mut self, results: &[&ObservationResult]) -> Vec<AttributeRow> {
            PlainBook.observe(results)
        }
        fn rows(&mut self, attributes: Vec<AttributeRow>) -> VehicleRows {
            PlainBook.rows(attributes)
        }
        fn worked_by(&mut self, report: &Report, day: Date) -> String {
            PlainBook.worked_by(report, day)
        }
        fn identify(&mut self, report: &Report, day: Date) -> (String, Option<String>) {
            PlainBook.identify(report, day)
        }
        fn track(&mut self, _: &[(Feed, &FeedState)]) {}
        fn reload(&mut self, _: RegistryRows) {}
        fn clock(&self) -> Option<Instant> {
            self.clock
        }
        fn forget_unseen(&mut self, before: Instant) -> usize {
            self.forgot.push(before);
            1
        }
    }

    #[test]
    fn the_book_forgets_unseen_reports_every_interval_of_its_own_time() {
        let derivation = Derivation {
            core_version: "c".into(),
            policy_version: "p".into(),
            release_id: "r".into(),
        };
        let mut writer = Writer::open(NullStore, derivation, |_| Clocked::default()).unwrap();
        let at = Instant::from_unix_seconds;
        writer.write_state(&[]).unwrap(); // no clock yet: nothing to do
        writer.forget_reports(Some(100), 10);
        for seconds in [1_000, 1_005, 1_009, 1_010, 1_015, 1_020] {
            writer.vehicles.clock = Some(at(seconds));
            writer.write_state(&[]).unwrap();
        }
        assert_eq!(writer.vehicles.forgot, [at(910), at(920)]);
        assert_eq!(writer.forgotten, 2);
        writer.forget_reports(None, 10); // as Python: never
        writer.vehicles.clock = Some(at(2_000));
        writer.write_state(&[]).unwrap();
        assert_eq!(writer.forgotten, 2);
    }
}
