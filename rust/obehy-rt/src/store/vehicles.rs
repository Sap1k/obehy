//! The vehicle book's side of the store (`emit/vehicles.py`'s SQL, `reference/registry.py`'s
//! `load_registry`): what it reads at start (the registry, stored pairings and live
//! attributes) and the rows it writes (live attributes, pairings, profiles), plus each
//! source's `[vehicles] physical` policy.
//!
//! The book itself (identity, pairing, profiles) is core state, `obehy_core::vehicles`; the
//! writer reaches it through [`Book`], so the store depends only on the rows.

use obehy_core::config::manifest::Source;
use obehy_core::model::{Feed, FeedState, JourneyKey, ObservationResult};
use obehy_core::time::{Date, Instant};
use serde_json::{Value, json};

/// (source, source vehicle ID).
pub type Report = (String, String);

/// A live attribute to write: `(source, source_vehicle_id, attribute, value, observed_at)`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AttributeRow {
    pub source: String,
    pub source_vehicle_id: String,
    pub attribute: String,
    pub value: String,
    pub observed_at: Instant,
}

/// What a pairing of two reports rests on (`emit/pairing.py`'s `Evidence`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Evidence {
    pub first_seen: Instant,
    pub last_seen: Instant,
    pub fixes: i64,
    pub journey: JourneyKey,
    pub confirmed: bool,
}

/// What is known about one vehicle, as `rt.vehicle_profile` stores it (`precedence.Profile`).
#[derive(Debug, Clone, PartialEq)]
pub struct ProfileRow {
    pub vehicle_id: String,
    pub registered: bool,
    pub public_label: Option<String>,
    /// `low_floor`, `air_conditioning`, `wheelchair_accessible`, in that order.
    pub flags: Vec<(String, Option<bool>)>,
    /// `manufacturer`, `model`, `operator`, `licence_plate`, `fleet_number`, in that order.
    pub texts: Vec<(String, Option<String>)>,
    pub datasets: Vec<String>,
    pub sources: Vec<String>,
    /// Each attribute's JSON object (statements, the pick, conflicts).
    pub attributes: Vec<Value>,
    pub last_stated: Option<Instant>,
}

impl ProfileRow {
    pub fn flag(&self, name: &str) -> Option<bool> {
        self.flags.iter().find(|(n, _)| n == name).and_then(|f| f.1)
    }

    pub fn text(&self, name: &str) -> Option<&str> {
        self.texts
            .iter()
            .find(|(n, _)| n == name)
            .and_then(|t| t.1.as_deref())
    }
}

/// What one write sends of the book (`VehicleRows`).
#[derive(Debug, Clone, Default, PartialEq)]
pub struct VehicleRows {
    pub attributes: Vec<AttributeRow>,
    pub gone: Vec<(Report, Report)>,
    pub kept: Vec<((Report, Report), Evidence)>,
    /// Delete every stored profile first.
    pub rewrite: bool,
    pub profiles: Vec<ProfileRow>,
}

/// `ref.vehicle_key` joined with its dataset's priority.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RegistryKeyRow {
    pub vehicle_id: String,
    pub source: Option<String>,
    pub source_vehicle_id: Option<String>,
    pub scheme: Option<String>,
    pub identifier: Option<String>,
    pub valid_from: Date,
    pub valid_to: Option<Date>,
    pub priority: i64,
}

/// A registered vehicle: its best label and model, and the datasets describing it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EntryRow {
    pub vehicle_id: String,
    pub public_label: Option<String>,
    pub manufacturer: Option<String>,
    pub model: Option<String>,
    pub datasets: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CuratedValueRow {
    pub vehicle_id: String,
    pub attribute: String,
    pub value: String,
    pub dataset: String,
    pub priority: i64,
    /// `curated` or `model`.
    pub origin: String,
}

/// The registry as `ref.*` holds it, in `load_registry`'s order.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct RegistryRows {
    pub keys: Vec<RegistryKeyRow>,
    pub entries: Vec<EntryRow>,
    pub values: Vec<CuratedValueRow>,
}

/// What the database holds of the vehicle book when the writer opens (`VehicleBook.load`):
/// the registry, the stored pairings (by pair) and live attributes (by report, attribute).
#[derive(Debug, Clone, Default, PartialEq)]
pub struct StoredVehicles {
    pub registry: RegistryRows,
    pub pairings: Vec<((Report, Report), Evidence)>,
    /// `(report, attribute, value, last_seen)`.
    pub attributes: Vec<(Report, String, String, Instant)>,
}

/// What the writer asks of the vehicle book (`emit/vehicles.py`'s `VehicleBook`).
pub trait Book {
    /// The live attributes to write of these results.
    fn observe(&mut self, results: &[&ObservationResult]) -> Vec<AttributeRow>;
    /// The rows to send: `attributes`, the pairings changed since the last call, the changed
    /// profiles (all of them, the first time).
    fn rows(&mut self, attributes: Vec<AttributeRow>) -> VehicleRows;
    /// Which vehicle a report on a journey of `day` is in history.
    fn worked_by(&mut self, report: &Report, day: Date) -> String;
    /// Which vehicle a report is on `day`, and how it is bound (`identify`).
    fn identify(&mut self, report: &Report, day: Date) -> (String, Option<String>);
    /// This tick's reports on journeys (trains, pairing evidence), feeds sorted.
    fn track(&mut self, states: &[(Feed, &FeedState)]);
    /// The latest observation time the book has seen (its clock), if any.
    fn clock(&self) -> Option<Instant> {
        None
    }
    /// Forget the reports last observed before `before` (the book's eviction rule); how many.
    fn forget_unseen(&mut self, _before: Instant) -> usize {
        0
    }
    /// A new registry (`obehy ref import`).
    fn reload(&mut self, registry: RegistryRows);
}

/// Each source's `[vehicles] physical`: `rt.physical_vehicles`' rows, sorted.
pub fn physical_rows(sources: &[Source]) -> Vec<(String, String, bool)> {
    let mut by_source: Vec<&Source> = sources.iter().collect();
    by_source.sort_by(|a, b| a.source.cmp(&b.source));
    let mut rows = Vec::new();
    for source in by_source {
        let mut modes: Vec<(&String, &bool)> = source.physical_vehicles.iter().collect();
        modes.sort();
        rows.extend(
            modes
                .into_iter()
                .map(|(mode, physical)| (source.source.clone(), mode.clone(), *physical)),
        );
    }
    rows
}

/// A journey as `rt.vehicle_pairing.journey` stores it.
pub fn journey_json(journey: &JourneyKey) -> Value {
    json!({
        "feed": journey.feed.as_str(),
        "namespace": journey.namespace,
        "key": journey.key,
        "service_date": journey.service_date,
    })
}

/// A book that knows nothing and identifies every report by its source form: the writer's book
/// until `obehy_core::vehicles` is ported, and a stand-in for tests of the rows alone.
#[derive(Debug, Default)]
pub struct PlainBook;

impl Book for PlainBook {
    fn observe(&mut self, _: &[&ObservationResult]) -> Vec<AttributeRow> {
        Vec::new()
    }

    fn rows(&mut self, attributes: Vec<AttributeRow>) -> VehicleRows {
        VehicleRows {
            attributes,
            ..VehicleRows::default()
        }
    }

    fn worked_by(&mut self, report: &Report, _: Date) -> String {
        format!("{}:{}", report.0, report.1)
    }

    fn identify(&mut self, report: &Report, _: Date) -> (String, Option<String>) {
        (format!("{}:{}", report.0, report.1), None)
    }

    fn track(&mut self, _: &[(Feed, &FeedState)]) {}

    fn reload(&mut self, _: RegistryRows) {}
}

// --- the core's vehicle book ------------------------------------------------------------------

use obehy_core::vehicles::precedence::{Picked, Profile, Statement};
use obehy_core::vehicles::registry::{CuratedValue, Entry, RegistryKey};
use obehy_core::vehicles::{Registry, VehicleBook, Vocabulary};

/// The registry in memory of its rows (`load_registry`'s grouping).
pub fn registry(rows: RegistryRows) -> Registry {
    let mut registry = Registry::default();
    for key in rows.keys {
        let entry = RegistryKey {
            vehicle_id: key.vehicle_id,
            priority: key.priority,
            valid_from: key.valid_from,
            valid_to: key.valid_to,
        };
        match key.source {
            Some(source) => registry
                .by_source
                .entry((source, key.source_vehicle_id.unwrap_or_default()))
                .or_default()
                .push(entry),
            None => registry
                .by_identifier
                .entry((
                    key.scheme.unwrap_or_default(),
                    key.identifier.unwrap_or_default(),
                ))
                .or_default()
                .push(entry),
        }
    }
    for e in rows.entries {
        registry.entries.insert(
            e.vehicle_id.clone(),
            Entry {
                vehicle_id: e.vehicle_id,
                public_label: e.public_label,
                manufacturer: e.manufacturer,
                model: e.model,
                datasets: e.datasets,
            },
        );
    }
    for v in rows.values {
        registry
            .values
            .entry(v.vehicle_id)
            .or_default()
            .push(CuratedValue {
                attribute: v.attribute,
                value: v.value,
                dataset: v.dataset,
                priority: v.priority,
                origin: v.origin,
            });
    }
    registry
}

/// The vehicle book as the database holds it (`VehicleBook.load`).
pub fn book(stored: StoredVehicles, vocabulary: Vocabulary) -> VehicleBook {
    let attributes = stored
        .attributes
        .into_iter()
        .map(|((source, source_vehicle_id), attribute, value, at)| {
            obehy_core::vehicles::AttributeRow {
                source,
                source_vehicle_id,
                attribute,
                value,
                at,
            }
        })
        .collect();
    let pairs = stored
        .pairings
        .into_iter()
        .map(|(pair, e)| {
            (
                pair,
                obehy_core::vehicles::Evidence {
                    first_seen: e.first_seen,
                    last_seen: e.last_seen,
                    fixes: e.fixes,
                    journey: e.journey,
                    confirmed: e.confirmed,
                },
            )
        })
        .collect();
    VehicleBook::load(registry(stored.registry), vocabulary, pairs, attributes)
}

fn statement_json(s: &Statement) -> Value {
    json!({
        "value": s.value,
        "origin": s.origin.as_str(),
        "source": s.source,
        "observed_at": s.observed_at,
    })
}

fn attribute_json(attribute: &str, p: &Picked) -> Value {
    let mut out = statement_json(&p.winner);
    out["attribute"] = json!(attribute);
    out["conflict"] = json!(p.conflict);
    out["statements"] = p.statements.iter().map(statement_json).collect();
    out
}

/// A profile as its row (`precedence.Profile`, attributes as their JSON).
pub fn profile_row(p: Profile) -> ProfileRow {
    ProfileRow {
        attributes: p
            .attributes
            .iter()
            .map(|(a, picked)| attribute_json(a, picked))
            .collect(),
        vehicle_id: p.vehicle_id,
        registered: p.registered,
        public_label: p.public_label,
        flags: p
            .flags
            .into_iter()
            .map(|(k, v)| (k.to_owned(), v))
            .collect(),
        texts: p
            .texts
            .into_iter()
            .map(|(k, v)| (k.to_owned(), v))
            .collect(),
        datasets: p.datasets,
        sources: p.sources,
        last_stated: p.last_stated,
    }
}

impl Book for VehicleBook {
    fn clock(&self) -> Option<Instant> {
        VehicleBook::clock(self)
    }

    fn forget_unseen(&mut self, before: Instant) -> usize {
        VehicleBook::forget_unseen(self, before)
    }

    fn observe(&mut self, results: &[&ObservationResult]) -> Vec<AttributeRow> {
        let owned: Vec<ObservationResult> = results.iter().map(|r| (*r).clone()).collect();
        VehicleBook::observe(self, &owned)
            .into_iter()
            .map(|a| AttributeRow {
                source: a.source,
                source_vehicle_id: a.source_vehicle_id,
                attribute: a.attribute,
                value: a.value,
                observed_at: a.at,
            })
            .collect()
    }

    fn rows(&mut self, attributes: Vec<AttributeRow>) -> VehicleRows {
        let observed = attributes
            .into_iter()
            .map(|a| obehy_core::vehicles::AttributeRow {
                source: a.source,
                source_vehicle_id: a.source_vehicle_id,
                attribute: a.attribute,
                value: a.value,
                at: a.observed_at,
            })
            .collect();
        let rows = VehicleBook::rows(self, observed);
        VehicleRows {
            attributes: rows
                .attributes
                .into_iter()
                .map(|a| AttributeRow {
                    source: a.source,
                    source_vehicle_id: a.source_vehicle_id,
                    attribute: a.attribute,
                    value: a.value,
                    observed_at: a.at,
                })
                .collect(),
            gone: rows.gone,
            kept: rows
                .kept
                .into_iter()
                .map(|(pair, e)| {
                    (
                        pair,
                        Evidence {
                            first_seen: e.first_seen,
                            last_seen: e.last_seen,
                            fixes: e.fixes,
                            journey: e.journey,
                            confirmed: e.confirmed,
                        },
                    )
                })
                .collect(),
            rewrite: rows.rewrite,
            profiles: rows.profiles.into_iter().map(profile_row).collect(),
        }
    }

    fn worked_by(&mut self, report: &Report, day: Date) -> String {
        VehicleBook::worked_by(self, report, day)
    }

    fn identify(&mut self, report: &Report, day: Date) -> (String, Option<String>) {
        let (id, bound) = VehicleBook::identify(self, report, day);
        (id, bound.map(|b| b.as_str().to_owned()))
    }

    fn track(&mut self, states: &[(Feed, &FeedState)]) {
        VehicleBook::track(self, states.iter().map(|(_, s)| *s));
    }

    fn reload(&mut self, rows: RegistryRows) {
        VehicleBook::reload(self, registry(rows));
    }
}
