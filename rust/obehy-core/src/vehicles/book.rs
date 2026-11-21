//! Vehicles beyond their positions (BASE_PLAN.md section 27): port of
//! `realtime/emit/vehicles.py` without its SQL (the store reads and writes what this decides).
//!
//! Which vehicle each source report is, the attributes sources state live, and each vehicle's
//! profile. A report on a rail run is the train its number names, `train:<number>` (DUK-Q8).
//! Any other report is a vehicle by, in turn: a registry key (its source's vehicle ID, else an
//! identifier it states); a confirmed co-located vehicle, kept as reports join and leave its
//! cluster ([`super::pairing`]); else its own `<source>:<source_vehicle_id>`. Identity and
//! precedence are decided here, once, when an input changes (a live attribute, a pairing, a
//! train number, the registry), and written: `rt.vehicle_state_current.vehicle_id`,
//! `rt.vehicle_profile`, `rt.vehicle_pairing`.
//!
//! # Memory
//!
//! Python never forgets a report: `live` (every report with a vehicle key, attributes or not),
//! `identity` and `members` (keys never removed, sets emptied) grow with every (source,
//! vehicle) seen for the life of the worker. Kept as is for parity. The proposed rule
//! ([`VehicleBook::forget_unseen`], not called by the runner yet): a report leaves `live`,
//! `identity` and its `members` set when no observation of it has been received for a policy
//! number of days (`forget_after_days`, to add) and it is not on a rail run now, and an emptied
//! `members` entry goes with it. With days it cannot change output within the harness windows
//! (hours); after them it can: a report seen again is identified afresh (the same answer unless
//! a co-located cluster held it by its sticky name), and its forgotten live attributes count
//! again only after a restart reloads `rt.vehicle_attribute_observed` (or when restated, which
//! writes them as new). `trains` is replaced each tick, `dirty` cleared by
//! [`VehicleBook::profiles`]; pairings in [`super::pairing`].

use std::collections::{BTreeMap, BTreeSet};

use super::pairing::{Evidence, Fix, Pair, Pairings, Report};
use super::precedence::{Profile, Statement, profile};
use super::registry::{BoundBy, Registry};
use super::vocabulary::Vocabulary;
use crate::model::{
    FeedState, JourneyKey, ObservationResult, VehicleAttribute, VehicleId, VehicleKey,
    VehicleStatus,
};
use crate::runs::run_vehicle_id;
use crate::time::{Date, Instant, TimeDelta, local_date};

/// Which vehicle a report is, and how (None: its own source form).
pub type Identity = (String, Option<BoundBy>);

/// A live attribute row for `rt.vehicle_attribute_observed`: source, source vehicle ID,
/// attribute, value, when.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AttributeRow {
    pub source: String,
    pub source_vehicle_id: String,
    pub attribute: String,
    pub value: String,
    pub at: Instant,
}

/// What one bound report on a journey shows the book this tick (`track`'s reading of a
/// vehicle state): on a rail run the train it is, else a fix if it is running with a position.
#[derive(Debug, Clone, PartialEq)]
pub struct Sighting {
    pub report: Report,
    pub last_seen: Instant,
    pub train: Option<String>,
    pub fix: Option<Fix>,
}

/// What the store writes after a write or a state write (`VehicleRows`).
#[derive(Debug, Clone, PartialEq)]
pub struct VehicleRows {
    pub attributes: Vec<AttributeRow>,
    pub gone: Vec<Pair>,
    pub kept: Vec<(Pair, Evidence)>,
    /// Delete every stored profile first.
    pub rewrite: bool,
    pub profiles: Vec<Profile>,
}

#[derive(Debug, Clone)]
pub struct VehicleBook {
    pub registry: Registry,
    pub vocabulary: Vocabulary,
    /// Per report, the live value of each attribute and when it was last written.
    live: BTreeMap<Report, Reported>,
    identity: BTreeMap<Report, Identity>,
    members: BTreeMap<String, BTreeSet<Report>>,
    /// The train number of each report bound to a rail run.
    trains: BTreeMap<Report, String>,
    pub pairings: Pairings,
    dirty: BTreeSet<String>,
    /// The first flush rewrites every profile.
    rewrite: bool,
    clock: Option<Instant>,
}

/// A report's live attributes and when it was last observed (for the eviction rule only).
#[derive(Debug, Clone, Default)]
struct Reported {
    seen: Option<Instant>,
    values: BTreeMap<String, (String, Instant)>,
}

fn opt_max(a: Option<Instant>, b: Instant) -> Instant {
    // Python's `max(a or b, b)`: the first wins a tie, which is the same instant.
    match a {
        Some(a) if a >= b => a,
        _ => b,
    }
}

impl VehicleBook {
    pub fn new(registry: Registry, vocabulary: Vocabulary) -> VehicleBook {
        let pairings = Pairings::new(vocabulary.pairing);
        VehicleBook {
            registry,
            vocabulary,
            live: BTreeMap::new(),
            identity: BTreeMap::new(),
            members: BTreeMap::new(),
            trains: BTreeMap::new(),
            pairings,
            dirty: BTreeSet::new(),
            rewrite: true,
            clock: None,
        }
    }

    /// `VehicleBook.load` without its queries: the registry, the stored pairings
    /// (`rt.vehicle_pairing`) and live attributes (`rt.vehicle_attribute_observed`, ordered by
    /// source, vehicle, attribute); every profile to be written.
    pub fn load(
        registry: Registry,
        vocabulary: Vocabulary,
        pairs: Vec<(Pair, Evidence)>,
        attributes: Vec<AttributeRow>,
    ) -> VehicleBook {
        let mut book = VehicleBook::new(registry, vocabulary);
        book.pairings.pairs.extend(pairs);
        for row in attributes {
            let reported = book
                .live
                .entry((row.source, row.source_vehicle_id))
                .or_default();
            reported.seen = Some(opt_max(reported.seen, row.at));
            reported.values.insert(row.attribute, (row.value, row.at));
            book.clock = Some(opt_max(book.clock, row.at));
        }
        book.everything_dirty();
        book
    }

    /// A new registry (`obehy ref import`): identities and profiles are redone.
    pub fn reload(&mut self, registry: Registry) {
        self.registry = registry;
        self.everything_dirty();
    }

    fn everything_dirty(&mut self) {
        let reports: BTreeSet<Report> = self
            .live
            .keys()
            .chain(self.identity.keys())
            .cloned()
            .collect();
        self.identity.clear();
        self.members.clear();
        if let Some(clock) = self.clock {
            let day = local_date(clock);
            for report in &reports {
                self.identify(report, day);
            }
        }
        self.dirty.extend(self.registry.entries.keys().cloned());
        self.dirty.extend(self.members.keys().cloned());
    }

    fn stated(&self, report: &Report) -> BTreeMap<&str, &str> {
        let identifiers = &self.vocabulary.identifiers;
        self.live
            .get(report)
            .map_or_else(BTreeMap::new, |reported| {
                reported
                    .values
                    .iter()
                    .filter(|(a, _)| identifiers.contains_key(*a))
                    .map(|(a, (v, _))| (a.as_str(), v.as_str()))
                    .collect()
            })
    }

    fn resolve(&self, report: &Report, day: Date) -> Identity {
        match self.trains.get(report) {
            Some(train) => (train.clone(), Some(BoundBy::Train)),
            None => self.vehicle(report, day),
        }
    }

    fn bound(&self, report: &Report, day: Date) -> Option<(String, BoundBy)> {
        self.registry.bound(
            &self.vocabulary,
            &report.0,
            &report.1,
            &self.stated(report),
            day,
        )
    }

    fn vehicle(&self, report: &Report, day: Date) -> Identity {
        if let Some((id, by)) = self.bound(report, day) {
            return (id, Some(by));
        }
        let members = self.pairings.cluster(report);
        if members.len() == 1 {
            return (format!("{}:{}", report.0, report.1), None);
        }
        for member in &members {
            if let Some((id, _)) = self.bound(member, day) {
                return (id, Some(BoundBy::CoLocated));
            }
        }
        // Sticky: the member a report of the cluster is already known by stays the vehicle as
        // reports join and leave, so a vehicle's ID does not change under the map.
        let names: BTreeSet<String> = members.iter().map(|(s, v)| format!("{s}:{v}")).collect();
        let held = members
            .iter()
            .filter_map(|m| self.identity.get(m))
            .map(|known| &known.0)
            .filter(|id| names.contains(*id))
            .min();
        if let Some(held) = held {
            return (held.clone(), Some(BoundBy::CoLocated));
        }
        let anchor = members
            .iter()
            .min_by(|a, b| {
                let rank = |r: &Report| self.vocabulary.source_rank("position", &r.0);
                (rank(a), *a).cmp(&(rank(b), *b))
            })
            .expect("a cluster holds its report");
        (
            format!("{}:{}", anchor.0, anchor.1),
            Some(BoundBy::CoLocated),
        )
    }

    /// What `track` reads of the states: per feed (sorted), per vehicle (sorted), the bound
    /// ones.
    pub fn sightings<'a>(states: impl IntoIterator<Item = &'a FeedState>) -> Vec<Sighting> {
        let mut states: Vec<&FeedState> = states.into_iter().collect();
        states.sort_by_key(|s| s.feed);
        let mut out = Vec::new();
        for state in states {
            let mut vehicles: Vec<_> = state.vehicles.iter().collect();
            vehicles.sort_by(|a, b| a.0.cmp(b.0));
            for (vehicle, current) in vehicles {
                let Some(binding) = &current.binding else {
                    continue;
                };
                let report = (vehicle.source.clone(), vehicle.source_vehicle_id.clone());
                let instance = state.instances.get(&binding.journey);
                let mut sighting = Sighting {
                    report,
                    last_seen: current.last_seen,
                    train: None,
                    fix: None,
                };
                if let Some(instance) = instance
                    && !instance.journeys.is_empty()
                {
                    // The run, not co-location, makes a run's reports one.
                    sighting.train = Some(run_vehicle_id(instance));
                } else if let Some(position) = &current.position
                    && current.status == VehicleStatus::Running
                {
                    sighting.fix = Some(Fix {
                        report: sighting.report.clone(),
                        journey: binding.journey.clone(),
                        lon: position.lon,
                        lat: position.lat,
                        at: current.position_at.unwrap_or(current.last_seen),
                    });
                }
                out.push(sighting);
            }
        }
        out
    }

    /// This tick's reports on journeys: the train each one on a rail run is, and pairing
    /// evidence from the fixes of the others; the identities a train or a confirmed or dropped
    /// pairing changes.
    pub fn track<'a>(&mut self, states: impl IntoIterator<Item = &'a FeedState>) {
        let sightings = VehicleBook::sightings(states);
        self.track_sightings(sightings);
    }

    /// `track` over what it reads of the states.
    pub fn track_sightings(&mut self, sightings: Vec<Sighting>) {
        let mut trains: BTreeMap<Report, String> = BTreeMap::new();
        let mut fixes: Vec<Fix> = Vec::new();
        for sighting in sightings {
            self.clock = Some(opt_max(self.clock, sighting.last_seen));
            if let Some(train) = sighting.train {
                trains.insert(sighting.report, train);
            } else if let Some(fix) = sighting.fix {
                fixes.push(fix);
            }
        }
        if trains != self.trains {
            let changed: BTreeSet<Report> = trains
                .keys()
                .chain(self.trains.keys())
                .filter(|r| trains.get(*r) != self.trains.get(*r))
                .cloned()
                .collect();
            self.trains = trains;
            if let Some(clock) = self.clock {
                let day = local_date(clock);
                for report in &changed {
                    self.identify(report, day);
                }
            }
        }
        let Some(now) = fixes.iter().map(|f| f.at).max() else {
            return;
        };
        let moved = self.pairings.observe(&fixes, now);
        let affected: BTreeSet<Report> = moved
            .iter()
            .flat_map(|r| self.pairings.cluster(r))
            .collect();
        let day = local_date(now);
        for report in &affected {
            self.identify(report, day);
        }
    }

    /// The vehicle a source's report is, as the state tables last wrote it (`identify`), so
    /// GTFS-RT names vehicles as the API does; a report not identified yet by its source form.
    pub fn vehicle_id(&self, vehicle: &VehicleId) -> String {
        let report = (vehicle.source.clone(), vehicle.source_vehicle_id.clone());
        match self.identity.get(&report) {
            Some(found) => found.0.clone(),
            None => vehicle.to_string(),
        }
    }

    /// Which vehicle a report on a journey of `day` is in history: the vehicle the live map
    /// names, but a report on a rail run is its own unit there, not the train.
    pub fn worked_by(&mut self, report: &Report, day: Date) -> String {
        if self.trains.contains_key(report) {
            return self.vehicle(report, day).0;
        }
        match self.identity.get(report) {
            Some(known) => known.0.clone(),
            None => self.identify(report, day).0,
        }
    }

    /// Which vehicle a report is on `day`: the train of the rail run it is on, else by a
    /// registry key, else a co-located partner, else its own source form.
    pub fn identify(&mut self, report: &Report, day: Date) -> Identity {
        let found = self.resolve(report, day);
        let before = self.identity.get(report);
        if before != Some(&found) {
            if let Some(before) = before.cloned() {
                if let Some(members) = self.members.get_mut(&before.0) {
                    members.remove(report);
                }
                self.dirty.insert(before.0);
            }
            self.identity.insert(report.clone(), found.clone());
            self.members
                .entry(found.0.clone())
                .or_default()
                .insert(report.clone());
            self.dirty.insert(found.0.clone());
        }
        found
    }

    /// The live attributes to write: a changed value, or one not written for `touch_s`.
    pub fn observe(&mut self, results: &[ObservationResult]) -> Vec<AttributeRow> {
        let touch = TimeDelta::from_seconds(self.vocabulary.touch_s);
        let mut rows = Vec::new();
        for result in results {
            let obs = &result.observation;
            let Some(vehicle) = obs.first::<VehicleKey>() else {
                continue;
            };
            let report = (obs.source.clone(), vehicle.source_vehicle_id.clone());
            self.clock = Some(opt_max(self.clock, obs.received_at));
            let reported = self.live.entry(report.clone()).or_default();
            reported.seen = Some(opt_max(reported.seen, obs.received_at));
            let values = &mut reported.values;
            let mut changed = false;
            for fact in obs.all::<VehicleAttribute>() {
                match values.get(&fact.attribute) {
                    Some((value, at)) if *value == fact.value => {
                        if obs.received_at - *at < touch {
                            continue;
                        }
                    }
                    _ => changed = true,
                }
                values.insert(
                    fact.attribute.clone(),
                    (fact.value.clone(), obs.received_at),
                );
                rows.push(AttributeRow {
                    source: report.0.clone(),
                    source_vehicle_id: report.1.clone(),
                    attribute: fact.attribute.clone(),
                    value: fact.value.clone(),
                    at: obs.received_at,
                });
            }
            if changed {
                let (vehicle_id, _) = self.identify(&report, local_date(obs.received_at));
                self.dirty.insert(vehicle_id);
            }
        }
        rows
    }

    /// The profiles of the vehicles whose inputs changed, and forget that they did.
    pub fn profiles(&mut self) -> Vec<Profile> {
        let mut built = Vec::new();
        for vehicle_id in std::mem::take(&mut self.dirty) {
            let mut live = Vec::new();
            if let Some(members) = self.members.get(&vehicle_id) {
                for report in members {
                    if let Some(reported) = self.live.get(report) {
                        for (attribute, (value, at)) in &reported.values {
                            live.push(Statement::live(attribute, value, &report.0, *at));
                        }
                    }
                }
            }
            let entry = self.registry.entries.get(&vehicle_id);
            if entry.is_none() && live.is_empty() {
                continue;
            }
            let curated = self
                .registry
                .values
                .get(&vehicle_id)
                .map_or(&[][..], Vec::as_slice);
            built.push(profile(&vehicle_id, entry, curated, live, &self.vocabulary));
        }
        built
    }

    /// What the store writes: the live attributes `observed`, the pairings changed since the
    /// last call, the changed profiles (all of them, and the old ones deleted, the first time).
    pub fn rows(&mut self, observed: Vec<AttributeRow>) -> VehicleRows {
        let mut gone = Vec::new();
        let mut kept = Vec::new();
        for (pair, evidence) in self.pairings.changes() {
            match evidence {
                None => gone.push(pair),
                Some(e) => kept.push((pair, e)),
            }
        }
        let profiles = self.profiles();
        let rewrite = std::mem::replace(&mut self.rewrite, false);
        VehicleRows {
            attributes: observed,
            gone,
            kept,
            rewrite,
            profiles,
        }
    }

    /// The identity each report was last given (sorted), for checks and the periodic summary.
    pub fn identities(&self) -> &BTreeMap<Report, Identity> {
        &self.identity
    }

    /// The train number of each report on a rail run, as of the last `track`.
    pub fn trains(&self) -> &BTreeMap<Report, String> {
        &self.trains
    }

    /// The latest time the book has seen.
    pub fn clock(&self) -> Option<Instant> {
        self.clock
    }

    /// The vehicles whose profiles are to be written next.
    pub fn dirty(&self) -> &BTreeSet<String> {
        &self.dirty
    }

    /// Container sizes for the periodic summary: live reports, identities, members.
    pub fn sizes(&self) -> (usize, usize, usize) {
        (self.live.len(), self.identity.len(), self.members.len())
    }

    /// The proposed eviction rule (module docs): forget every report last observed before
    /// `before` that is not on a rail run now, from `live`, `identity` and `members` (an
    /// emptied entry goes); how many. Not called by the runner: Python never forgets, and the
    /// port keeps parity until the rule is decided.
    pub fn forget_unseen(&mut self, before: Instant) -> usize {
        let stale: Vec<Report> = self
            .live
            .iter()
            .filter(|(report, r)| {
                r.seen.is_none_or(|seen| seen < before) && !self.trains.contains_key(*report)
            })
            .map(|(report, _)| report.clone())
            .collect();
        for report in &stale {
            self.live.remove(report);
            if let Some((id, _)) = self.identity.remove(report)
                && let Some(members) = self.members.get_mut(&id)
            {
                members.remove(report);
            }
        }
        self.members.retain(|_, m| !m.is_empty());
        stale.len()
    }
}

/// A journey key as `rt.vehicle_pairing.journey` stores it (`_journey_json`): feed, namespace,
/// key, ISO service date.
pub fn journey_json(journey: &JourneyKey) -> [(&'static str, String); 4] {
    [
        ("feed", journey.feed.as_str().to_owned()),
        ("namespace", journey.namespace.clone()),
        ("key", journey.key.clone()),
        ("service_date", journey.service_date.isoformat()),
    ]
}
