//! Each feed's GTFS-RT `Alert` entities (BASE_PLAN.md section 25): port of the GTFS-RT part of
//! `realtime/emit/alerts.py` (`alert_entity`, `_over`, `AlertFeed` without its query). Writing
//! `rt.alert` and reading `alert_feed.sql` are the store's.
//!
//! A feed carries the alerts not withdrawn and not over with at least one entity resolved in
//! that feed (`rt.alert_target`), informing only those entities, under our IDs.

use std::collections::{BTreeMap, HashMap, HashSet};
use std::sync::Arc;

use prost::Message;

use super::gtfs_rt::proto;
use crate::model::Feed;
use crate::time::Instant;

/// One period of an alert row: ISO times as `rt.alert.periods` holds them (`None`: open).
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct AlertPeriod {
    pub start: Option<String>,
    pub end: Option<String>,
}

/// An entity an alert informs in a feed, resolved (`rt.alert_target`).
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct AlertTarget {
    /// `route`, `stop`, or anything else: a trip.
    pub kind: String,
    pub id: String,
    /// A trip's service date, ISO.
    pub service_date: Option<String>,
}

/// One row of `alert_feed.sql`: an alert in one feed. `entities` and `details` take no part in
/// the entity; they are kept because a change of any column re-encodes it, as in Python.
#[derive(Debug, Clone, PartialEq)]
pub struct AlertRow {
    pub source: String,
    pub alert_id: String,
    pub cause: String,
    pub effect: String,
    pub severity: String,
    pub periods: Vec<AlertPeriod>,
    pub header: BTreeMap<String, String>,
    pub description: BTreeMap<String, String>,
    pub url: BTreeMap<String, String>,
    pub cause_detail: BTreeMap<String, String>,
    pub effect_detail: BTreeMap<String, String>,
    pub entities: serde_json::Value,
    pub details: serde_json::Value,
    pub targets: Vec<AlertTarget>,
}

/// An alert row Python's protobuf would refuse (`ValueError` there).
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("alert {source_id}: {message}")]
pub struct AlertError {
    pub source_id: String,
    pub message: String,
}

const CAUSES: [&str; 13] = [
    "UNKNOWN_CAUSE",
    "OTHER_CAUSE",
    "TECHNICAL_PROBLEM",
    "STRIKE",
    "DEMONSTRATION",
    "ACCIDENT",
    "HOLIDAY",
    "WEATHER",
    "MAINTENANCE",
    "CONSTRUCTION",
    "POLICE_ACTIVITY",
    "MEDICAL_EMERGENCY",
    "SPECIAL_EVENT",
];
const EFFECTS: [&str; 11] = [
    "NO_SERVICE",
    "REDUCED_SERVICE",
    "SIGNIFICANT_DELAYS",
    "DETOUR",
    "ADDITIONAL_SERVICE",
    "MODIFIED_SERVICE",
    "OTHER_EFFECT",
    "UNKNOWN_EFFECT",
    "STOP_MOVED",
    "NO_EFFECT",
    "ACCESSIBILITY_ISSUE",
];
const SEVERITIES: [&str; 4] = ["UNKNOWN_SEVERITY", "INFO", "WARNING", "SEVERE"];

impl AlertRow {
    fn error(&self, message: String) -> AlertError {
        AlertError {
            source_id: format!("{}:{}", self.source, self.alert_id),
            message,
        }
    }

    /// An enum value by name; the GTFS-RT alert enums are numbered from 1.
    fn number(&self, names: &[&str], name: &str) -> Result<i32, AlertError> {
        names
            .iter()
            .position(|n| *n == name)
            .map(|i| i32::try_from(i + 1).expect("a small enum"))
            .ok_or_else(|| self.error(format!("unknown enum value {name:?}")))
    }

    fn epoch(&self, value: &str) -> Result<i64, AlertError> {
        Instant::from_isoformat(value)
            .map(Instant::timestamp_int)
            .map_err(|e| self.error(e.to_string()))
    }
}

fn translated(value: &BTreeMap<String, String>) -> Option<proto::TranslatedString> {
    // An untouched sub-message is not set: an alert without the text has no field.
    (!value.is_empty()).then(|| proto::TranslatedString {
        translation: value
            .iter()
            .map(|(language, text)| proto::Translation {
                text: Some(text.clone()),
                language: Some(language.clone()),
            })
            .collect(),
    })
}

/// A GTFS-RT `Alert` entity of one alert row.
pub fn alert_entity(row: &AlertRow) -> Result<Vec<u8>, AlertError> {
    let mut active_period = Vec::with_capacity(row.periods.len());
    for period in &row.periods {
        let bound = |value: &Option<String>| -> Result<Option<u64>, AlertError> {
            value
                .as_deref()
                .map(|v| {
                    let seconds = row.epoch(v)?;
                    u64::try_from(seconds).map_err(|_| row.error(format!("{v} is before 1970")))
                })
                .transpose()
        };
        active_period.push(proto::TimeRange {
            start: bound(&period.start)?,
            end: bound(&period.end)?,
        });
    }
    let informed_entity = row
        .targets
        .iter()
        .map(|target| match target.kind.as_str() {
            "route" => proto::EntitySelector {
                route_id: Some(target.id.clone()),
                ..Default::default()
            },
            "stop" => proto::EntitySelector {
                stop_id: Some(target.id.clone()),
                ..Default::default()
            },
            _ => proto::EntitySelector {
                trip: Some(proto::TripDescriptor {
                    trip_id: Some(target.id.clone()),
                    start_date: target.service_date.as_ref().map(|d| d.replace('-', "")),
                    schedule_relationship: None,
                }),
                ..Default::default()
            },
        })
        .collect();
    let entity = proto::FeedEntity {
        id: Some(format!("alert:{}:{}", row.source, row.alert_id)),
        alert: Some(proto::Alert {
            active_period,
            informed_entity,
            cause: Some(row.number(&CAUSES, &row.cause)?),
            effect: Some(row.number(&EFFECTS, &row.effect)?),
            url: translated(&row.url),
            header_text: translated(&row.header),
            description_text: translated(&row.description),
            severity_level: Some(row.number(&SEVERITIES, &row.severity)?),
            cause_detail: translated(&row.cause_detail),
            effect_detail: translated(&row.effect_detail),
        }),
        ..Default::default()
    };
    Ok(entity.encode_to_vec())
}

/// Every period of the alert has ended by `now` (an alert without periods never is over).
pub fn over(row: &AlertRow, now: Instant) -> Result<bool, AlertError> {
    if row.periods.is_empty() {
        return Ok(false);
    }
    for period in &row.periods {
        let Some(end) = &period.end else {
            return Ok(false);
        };
        // `end <= now.timestamp()`: whole seconds against exact microseconds.
        if row.epoch(end)? * 1_000_000 > now.micros() {
            return Ok(false);
        }
    }
    Ok(true)
}

/// An alert in a feed: (feed, source, alert ID).
type AlertKey = (Feed, String, String);

/// Each feed's GTFS-RT alert entities (`AlertFeed`): the rows the store last read
/// ([`AlertEntities::load`], after an alert poll or a release switch), each alert's entity cached
/// while its row is unchanged.
///
/// Eviction: `load` keeps only the cached entities of alerts in the new rows; `rows` is replaced
/// whole by each load.
#[derive(Debug, Default)]
pub struct AlertEntities {
    /// Set when the rows must be read again (an alert poll, a release switch).
    pub dirty: bool,
    rows: BTreeMap<Feed, Vec<AlertRow>>,
    cache: HashMap<AlertKey, (AlertRow, Arc<[u8]>)>,
}

impl AlertEntities {
    pub fn new() -> AlertEntities {
        AlertEntities {
            dirty: true,
            ..Default::default()
        }
    }

    /// The rows of `alert_feed.sql`, per feed in its order.
    pub fn load(&mut self, rows: BTreeMap<Feed, Vec<AlertRow>>) {
        let live: HashSet<(Feed, &str, &str)> = rows
            .iter()
            .flat_map(|(feed, list)| {
                list.iter()
                    .map(move |r| (*feed, r.source.as_str(), r.alert_id.as_str()))
            })
            .collect();
        self.cache
            .retain(|(feed, source, id), _| live.contains(&(*feed, source.as_str(), id.as_str())));
        self.rows = rows;
        self.dirty = false;
    }

    /// The feed's alert entities at `now`, in row order, those over left out.
    pub fn entities(&mut self, feed: Feed, now: Instant) -> Result<Vec<Arc<[u8]>>, AlertError> {
        let mut out = Vec::new();
        for row in self.rows.get(&feed).map_or(&[][..], Vec::as_slice) {
            if over(row, now)? {
                continue;
            }
            let key = (feed, row.source.clone(), row.alert_id.clone());
            match self.cache.get(&key) {
                Some((cached, bytes)) if cached == row => out.push(Arc::clone(bytes)),
                _ => {
                    let bytes: Arc<[u8]> = alert_entity(row)?.into();
                    out.push(Arc::clone(&bytes));
                    self.cache.insert(key, (row.clone(), bytes));
                }
            }
        }
        Ok(out)
    }

    /// How many entities are cached.
    pub fn cached(&self) -> usize {
        self.cache.len()
    }
}
