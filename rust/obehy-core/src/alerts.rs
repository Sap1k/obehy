//! Alerts from any source, in one shape: port of `realtime/alerts.py` (BASE_PLAN.md section
//! 25). Decoded by the alert channels' decoders (obehy-sources), written by the store, never
//! passed through the core. Pure.

use crate::time::{Date, Instant};

/// What an alert informs, as the source names it.
#[derive(Debug, Clone, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct AlertEntity {
    /// `route`, `stop` or `trip`.
    pub kind: String,
    /// A source key namespace (`pid:gtfs_route_id`).
    pub namespace: String,
    pub identifier: String,
    /// A trip's service date.
    pub service_date: Option<Date>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct Period {
    pub start: Option<Instant>,
    /// None: until further notice.
    pub end: Option<Instant>,
}

/// `(language, text)`, sorted by language.
pub type Texts = Vec<(String, String)>;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Alert {
    pub source: String,
    pub channel: String,
    /// The source's, unique within the source.
    pub alert_id: String,
    /// GTFS-RT `Alert.Cause`, `Alert.Effect` and `Alert.SeverityLevel` names.
    pub cause: String,
    pub effect: String,
    pub severity: String,
    pub periods: Vec<Period>,
    pub header: Texts,
    pub description: Texts,
    pub url: Texts,
    pub cause_detail: Texts,
    pub effect_detail: Texts,
    pub entities: Vec<AlertEntity>,
    /// Source-specific, kept for the API (PID infotexts: how a board shows it).
    pub details: Vec<(String, String)>,
}

/// `texts(**by_language)`: the non-empty texts, sorted by language.
pub fn texts(by_language: &[(&str, Option<&str>)]) -> Texts {
    let mut found: Texts = by_language
        .iter()
        .filter_map(|(lang, text)| {
            text.filter(|t| !t.is_empty())
                .map(|t| ((*lang).to_owned(), t.to_owned()))
        })
        .collect();
    found.sort();
    found
}
