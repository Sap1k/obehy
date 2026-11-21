//! Core effects and state as rows: port of `emit/db.py`'s `Writer`.
//!
//! `write` turns one batch's effects into rows, `write_state` keeps the current-state tables
//! in step with core state; each is one transaction of the store. Ordering kept from Python:
//! partitions and terms are prepared outside the transaction (so a rolled-back write never
//! leaves a term number known but not stored); within it observations, snapshots, events, then
//! the live attributes (`Book::observe`, before assignments: what a report states may name its
//! vehicle), assignments, links, platforms, cancellations and the book's rows.

use std::cmp::Ordering;
use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc;

use obehy_core::model::{
    BindingMethod, CallState, Derivation, Effect, Feed, FeedState, JourneyKey, ObservationResult,
    VehicleKey,
};
use obehy_core::publish::{representing, shown};
use obehy_core::py::json::PyJson;
use obehy_core::runs::{current_part, published_parts};
use obehy_core::time::{Date, Instant, TimeDelta, local_date};

use super::cells::{Cell, Row, order_rows};
use super::rows::{
    Drawn, calls_json, cancellation_row, event_row, link_row, observation_row, part_row,
    platform_row, trip_row, vehicle_row,
};
use super::storage::hour_of;
use super::vehicles::{Book, Report};
use super::{PARTS, Prepare, StateTable, Store, StoreResult, TRIPS, VEHICLES};

/// A state table's key, ordered as Python orders key tuples.
#[derive(Debug, Clone, PartialEq)]
struct Key(Row);

impl Eq for Key {}

impl PartialOrd for Key {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

impl Ord for Key {
    fn cmp(&self, other: &Self) -> Ordering {
        order_rows(&self.0, &other.0)
    }
}

type Rows = BTreeMap<Key, Row>;

/// A feed's current-state rows as last written, and each journey's calls JSON with the calls
/// it was made from.
#[derive(Debug, Default)]
struct Written {
    vehicles: Rows,
    trips: Rows,
    parts: Rows,
    calls: BTreeMap<JourneyKey, (Arc<[CallState]>, Arc<PyJson>)>,
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
                p + w.parts.len(),
                c + w.calls.len(),
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
        let mut written: Vec<(Feed, Written)> = Vec::new();
        let outcome = self.transaction(|w| {
            for (feed, state) in &sorted {
                let now = w.feed_rows(*feed, state);
                let before = w.state.get(feed);
                let tables = [
                    (VEHICLES, &now.vehicles, before.map(|b| &b.vehicles)),
                    (TRIPS, &now.trips, before.map(|b| &b.trips)),
                    (PARTS, &now.parts, before.map(|b| &b.parts)),
                ];
                for (table, rows, old) in tables {
                    write_table(&mut w.store, *feed, table, rows, old)?;
                }
                written.push((*feed, now));
            }
            // Profiles of vehicles whose identity changed with these rows.
            let vehicle_rows = w.vehicles.rows(Vec::new());
            w.store.vehicles(&vehicle_rows)
        });
        match outcome {
            Ok(()) => {
                self.state.extend(written);
                Ok(())
            }
            Err(error) => {
                self.state.clear(); // unknown what was written: rewrite in full next time
                Err(error)
            }
        }
    }

    fn feed_rows(&mut self, feed: Feed, state: &FeedState) -> Written {
        let release_id = self.derivation.release_id.clone();
        let shown_set = representing(state);
        let mut now = Written::default();
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
            now.vehicles.insert(
                Key(vec![
                    Cell::text(feed.as_str()),
                    Cell::text(&vehicle.source),
                    Cell::text(&vehicle.source_vehicle_id),
                ]),
                row,
            );
        }
        let before = self.state.get(&feed);
        for (journey, instance) in &state.instances {
            // Python re-encodes only when the calls tuple is a new object; the `passed` flags
            // follow `left_index` only then (kept: the output harness pins it).
            let calls = match before.and_then(|b| b.calls.get(journey)) {
                Some((made_from, encoded)) if Arc::ptr_eq(made_from, &instance.calls) => {
                    (Arc::clone(made_from), encoded.clone())
                }
                _ => (Arc::clone(&instance.calls), Arc::new(calls_json(instance))),
            };
            now.trips.insert(
                Key(journey_key_cells(journey)),
                trip_row(instance, calls.1.clone()),
            );
            now.calls.insert(journey.clone(), calls);
            for part in published_parts(instance) {
                let mut key = journey_key_cells(journey);
                key.push(Cell::text(&part.trip_id));
                now.parts
                    .insert(Key(key), part_row(instance, &part, &release_id));
            }
        }
        now
    }

    /// Delete the history of service dates before a rebuild by replay.
    pub fn clear_history(&mut self, days: impl IntoIterator<Item = Date>) -> StoreResult<()> {
        let days: BTreeSet<Date> = days.into_iter().collect();
        let days: Vec<Date> = days.into_iter().collect();
        self.store.clear_history(&days)
    }
}

fn write_table<S: Store>(
    store: &mut S,
    feed: Feed,
    table: StateTable,
    rows: &Rows,
    old: Option<&Rows>,
) -> StoreResult<()> {
    let Some(old) = old else {
        let all: Vec<Row> = rows.values().cloned().collect();
        return store.replace_state(table, feed, &all);
    };
    let gone: Vec<Row> = old
        .keys()
        .filter(|key| !rows.contains_key(*key))
        .map(|key| key.0.clone())
        .collect();
    if !gone.is_empty() {
        store.delete_state(table, &gone)?;
    }
    let changed: Vec<Row> = rows
        .iter()
        .filter(|(key, row)| old.get(*key) != Some(*row))
        .map(|(_, row)| row.clone())
        .collect();
    if !changed.is_empty() {
        store.upsert_state(table, &changed)?;
    }
    Ok(())
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
