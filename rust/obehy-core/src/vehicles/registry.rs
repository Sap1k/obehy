//! The vehicle registry in memory (BASE_PLAN.md section 27): the part of
//! `reference/registry.py` the vehicle book asks (`Registry.bound`). The store fills it from
//! `ref.*` (`load_registry`'s three queries).
//!
//! A report is a registered vehicle when a key valid on the day binds it: its source's vehicle
//! ID first, else an identifier it states (licence plate, EVN, series number: any source), in
//! the vocabulary's order. Where two datasets' keys claim it, the better dataset (lower
//! priority) wins.

use std::collections::{BTreeMap, HashMap};

use super::vocabulary::Vocabulary;
use crate::time::Date;

/// How a report is bound to the vehicle it is (`BoundBy`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum BoundBy {
    Source,
    Identifier,
    CoLocated,
    Train,
}

impl BoundBy {
    pub const fn as_str(self) -> &'static str {
        match self {
            BoundBy::Source => "source",
            BoundBy::Identifier => "identifier",
            BoundBy::CoLocated => "co_located",
            BoundBy::Train => "train",
        }
    }

    pub fn parse(text: &str) -> Option<BoundBy> {
        [
            BoundBy::Source,
            BoundBy::Identifier,
            BoundBy::CoLocated,
            BoundBy::Train,
        ]
        .into_iter()
        .find(|b| b.as_str() == text)
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RegistryKey {
    pub vehicle_id: String,
    pub priority: i64,
    pub valid_from: Date,
    pub valid_to: Option<Date>,
}

impl RegistryKey {
    pub fn valid(&self, day: Date) -> bool {
        self.valid_from <= day && self.valid_to.is_none_or(|to| day <= to)
    }
}

/// A curated statement about a registered vehicle (`CuratedValue`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CuratedValue {
    pub attribute: String,
    pub value: String,
    pub dataset: String,
    pub priority: i64,
    /// `curated` or `model`.
    pub origin: String,
}

/// A registered vehicle: its best label and model, and the datasets describing it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Entry {
    pub vehicle_id: String,
    pub public_label: Option<String>,
    pub manufacturer: Option<String>,
    pub model: Option<String>,
    pub datasets: Vec<String>,
}

#[derive(Debug, Clone, Default, PartialEq)]
pub struct Registry {
    pub by_source: HashMap<(String, String), Vec<RegistryKey>>,
    /// (scheme, normalised identifier) -> keys
    pub by_identifier: HashMap<(String, String), Vec<RegistryKey>>,
    pub entries: BTreeMap<String, Entry>,
    pub values: HashMap<String, Vec<CuratedValue>>,
}

fn best(keys: &[RegistryKey], day: Date) -> Option<&str> {
    keys.iter()
        .filter(|k| k.valid(day))
        .min_by(|a, b| {
            (a.priority, a.vehicle_id.as_str()).cmp(&(b.priority, b.vehicle_id.as_str()))
        })
        .map(|k| k.vehicle_id.as_str())
}

impl Registry {
    /// The registered vehicle a report is on `day`, by its source key or by an identifier it
    /// states (`stated`: scheme -> value as stated), and how; None when no key binds it.
    pub fn bound(
        &self,
        vocabulary: &Vocabulary,
        source: &str,
        source_vehicle_id: &str,
        stated: &BTreeMap<&str, &str>,
        day: Date,
    ) -> Option<(String, BoundBy)> {
        let by_source = self
            .by_source
            .get(&(source.to_owned(), source_vehicle_id.to_owned()));
        if let Some(found) = by_source.and_then(|keys| best(keys, day)) {
            return Some((found.to_owned(), BoundBy::Source));
        }
        for scheme in vocabulary.identifiers.keys() {
            let Some(value) = stated.get(scheme.as_str()) else {
                continue;
            };
            let key = (scheme.clone(), vocabulary.normalise(scheme, value));
            if let Some(found) = self.by_identifier.get(&key).and_then(|k| best(k, day)) {
                return Some((found.to_owned(), BoundBy::Identifier));
            }
        }
        None
    }
}
