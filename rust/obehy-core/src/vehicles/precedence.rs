//! Which statement about a vehicle's attribute counts, and the vehicle's profile (BASE_PLAN.md
//! section 27). Pure.
//!
//! The registry's datasets (curated, the lower priority first) beat what sources state live,
//! which beats a model's default. Among live statements the vocabulary ranks the sources (per
//! attribute, else `live_sources`), the most recent first among equals. A winner that another
//! curated or live statement contradicts is flagged; every statement is kept.

use std::cmp::Ordering;
use std::collections::{BTreeMap, BTreeSet};

use super::registry::{CuratedValue, Entry};
use super::vocabulary::Vocabulary;
use crate::time::Instant;

/// The attributes a profile also carries as columns, for boards and lists.
pub const FLAGS: [&str; 3] = ["low_floor", "air_conditioning", "wheelchair_accessible"];
pub const TEXTS: [&str; 5] = [
    "manufacturer",
    "model",
    "operator",
    "licence_plate",
    "fleet_number",
];

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Origin {
    Curated,
    Live,
    Model,
}

impl Origin {
    pub const fn as_str(self) -> &'static str {
        match self {
            Origin::Curated => "curated",
            Origin::Live => "live",
            Origin::Model => "model",
        }
    }

    const fn rank(self) -> i64 {
        match self {
            Origin::Curated => 0,
            Origin::Live => 1,
            Origin::Model => 2,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Statement {
    pub attribute: String,
    pub value: String,
    pub origin: Origin,
    /// The dataset (curated, model) or the realtime source (live).
    pub source: String,
    /// A dataset's.
    pub priority: Option<i64>,
    /// A live value's last_seen.
    pub observed_at: Option<Instant>,
}

impl Statement {
    pub fn curated(value: &CuratedValue) -> Statement {
        Statement {
            attribute: value.attribute.clone(),
            value: value.value.clone(),
            origin: if value.origin == "model" {
                Origin::Model
            } else {
                Origin::Curated
            },
            source: value.dataset.clone(),
            priority: Some(value.priority),
            observed_at: None,
        }
    }

    pub fn live(attribute: &str, value: &str, source: &str, at: Instant) -> Statement {
        Statement {
            attribute: attribute.to_owned(),
            value: value.to_owned(),
            origin: Origin::Live,
            source: source.to_owned(),
            priority: None,
            observed_at: Some(at),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Picked {
    pub winner: Statement,
    pub conflict: bool,
    pub statements: Vec<Statement>,
}

/// `_rank`: (origin, rank within it, recency, source, value); recency is minus the float
/// timestamp compares it.
fn rank_cmp(a: &Statement, b: &Statement, vocabulary: &Vocabulary) -> Ordering {
    let within = |s: &Statement| -> i64 {
        match s.origin {
            Origin::Live => {
                i64::try_from(vocabulary.source_rank(&s.attribute, &s.source)).expect("a rank")
            }
            _ => s.priority.unwrap_or(1 << 20),
        }
    };
    let recency = |s: &Statement| s.observed_at.map_or(0.0, |at| -(at.micros() as f64 / 1e6));
    a.origin
        .rank()
        .cmp(&b.origin.rank())
        .then_with(|| within(a).cmp(&within(b)))
        .then_with(|| {
            recency(a)
                .partial_cmp(&recency(b))
                .unwrap_or(Ordering::Equal)
        })
        .then_with(|| a.source.cmp(&b.source))
        .then_with(|| a.value.cmp(&b.value))
}

/// Per attribute (sorted), the statement that counts.
pub fn pick(statements: Vec<Statement>, vocabulary: &Vocabulary) -> BTreeMap<String, Picked> {
    let mut grouped: BTreeMap<String, Vec<Statement>> = BTreeMap::new();
    for s in statements {
        grouped.entry(s.attribute.clone()).or_default().push(s);
    }
    grouped
        .into_iter()
        .map(|(attribute, mut ordered)| {
            ordered.sort_by(|a, b| rank_cmp(a, b, vocabulary));
            let winner = ordered[0].clone();
            let conflict = ordered.iter().any(|s| {
                matches!(s.origin, Origin::Curated | Origin::Live) && s.value != winner.value
            });
            (
                attribute,
                Picked {
                    winner,
                    conflict,
                    statements: ordered,
                },
            )
        })
        .collect()
}

/// What is known about one vehicle, as `rt.vehicle_profile` stores it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Profile {
    pub vehicle_id: String,
    pub registered: bool,
    pub public_label: Option<String>,
    /// [`FLAGS`] in order.
    pub flags: Vec<(&'static str, Option<bool>)>,
    /// [`TEXTS`] in order.
    pub texts: Vec<(&'static str, Option<String>)>,
    pub datasets: Vec<String>,
    pub sources: Vec<String>,
    /// Per attribute, sorted: the winner, the conflict flag and every statement
    /// (`rt.vehicle_profile.attributes`).
    pub attributes: Vec<(String, Picked)>,
    /// The latest live statement.
    pub last_stated: Option<Instant>,
}

impl Profile {
    pub fn flag(&self, name: &str) -> Option<bool> {
        self.flags
            .iter()
            .find(|(n, _)| *n == name)
            .and_then(|f| f.1)
    }

    pub fn text(&self, name: &str) -> Option<&str> {
        self.texts
            .iter()
            .find(|(n, _)| *n == name)
            .and_then(|t| t.1.as_deref())
    }
}

/// `profile(vehicle_id, entry, curated, live, vocabulary)`.
pub fn profile(
    vehicle_id: &str,
    entry: Option<&Entry>,
    curated: &[CuratedValue],
    live: Vec<Statement>,
    vocabulary: &Vocabulary,
) -> Profile {
    let sources: BTreeSet<String> = live.iter().map(|s| s.source.clone()).collect();
    let last_stated = live.iter().filter_map(|s| s.observed_at).max();
    let mut all: Vec<Statement> = curated.iter().map(Statement::curated).collect();
    all.extend(live);
    let picked = pick(all, vocabulary);
    Profile {
        vehicle_id: vehicle_id.to_owned(),
        registered: entry.is_some(),
        public_label: entry.and_then(|e| e.public_label.clone()),
        flags: FLAGS
            .iter()
            .map(|a| (*a, picked.get(*a).map(|p| p.winner.value == "true")))
            .collect(),
        texts: TEXTS
            .iter()
            .map(|a| (*a, picked.get(*a).map(|p| p.winner.value.clone())))
            .collect(),
        datasets: entry.map(|e| e.datasets.clone()).unwrap_or_default(),
        sources: sources.into_iter().collect(),
        attributes: picked.into_iter().collect(),
        last_stated,
    }
}
