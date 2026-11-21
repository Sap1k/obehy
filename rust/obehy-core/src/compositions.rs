//! Compositions and the lookups that answer them (BASE_PLAN.md sections 18.2 and 28). A lookup is
//! `plan(subject, view)` (pure), a fetch by the broker, and `decode(bytes, request)` (pure);
//! compositions are not core state. Pure.

use std::collections::BTreeMap;

use crate::time::{Date, Instant};

/// What the caller asks about: a trip on a date, from a call onward (None: from the next
/// passenger call).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Subject {
    pub feed: String,
    pub trip_id: String,
    pub service_date: Date,
    pub from_location: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ViewCall {
    /// 0-based along the trip.
    pub ordinal: i64,
    pub location_id: String,
    pub visit_n: i64,
    pub passenger: bool,
    /// Live state says the train has left it.
    pub left: bool,
    /// namespace -> identifier (sr70).
    pub keys: BTreeMap<String, String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TripView {
    pub feed: String,
    pub trip_id: String,
    pub service_date: Date,
    pub now: Instant,
    /// namespace -> identifier: czptt:train_number, czptt:tr.
    pub keys: BTreeMap<String, String>,
    pub calls: Vec<ViewCall>,
}

/// One upstream call: `cache_key` names the answer, `params` fill the manifest URL's
/// `{name}` placeholders, the answer holds from the call `start` onward.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UpstreamRequest {
    pub cache_key: String,
    pub params: Vec<(String, String)>,
    pub start: ViewCall,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct NotAvailable {
    pub reason: String,
}

impl NotAvailable {
    pub fn new(reason: impl Into<String>) -> NotAvailable {
        NotAvailable {
            reason: reason.into(),
        }
    }
}

/// One vehicle of a train as a source states it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Unit {
    pub sequence: i64,
    /// `locomotive`, `coach` or `unit`.
    pub kind: String,
    pub role: String,
    pub vehicle_type: Option<String>,
    pub coach_number: Option<String>,
    pub classes: Vec<i64>,
    /// Vocabulary attributes or `raw:<source>:<code>`.
    pub features: Vec<String>,
    /// (code, the source's text).
    pub texts: Vec<(String, String)>,
    pub destination: Option<String>,
    pub details: Vec<(String, String)>,
}

/// The call an answer is asked from: the subject's, else the next passenger call the train
/// has not left; never the last passenger call (`compositions.start_call`).
pub fn start_call(subject: &Subject, view: &TripView) -> Result<ViewCall, NotAvailable> {
    let passenger: Vec<&ViewCall> = view.calls.iter().filter(|c| c.passenger).collect();
    if passenger.len() < 2 {
        return Err(NotAvailable::new(
            "the trip has no passenger calls to depart from",
        ));
    }
    let departing = &passenger[..passenger.len() - 1];
    let last_departing = (*departing.last().expect("two passenger calls")).clone();
    if let Some(from) = &subject.from_location {
        let named: Vec<&&ViewCall> = departing
            .iter()
            .filter(|c| &c.location_id == from)
            .collect();
        if from == &passenger[passenger.len() - 1].location_id {
            return Ok(last_departing);
        }
        if named.is_empty() {
            return Err(NotAvailable::new(format!(
                "{from} is no departing call of the trip"
            )));
        }
        return Ok(named
            .iter()
            .find(|c| !c.left)
            .copied()
            .copied()
            .unwrap_or(named[named.len() - 1])
            .clone());
    }
    let last_left = view
        .calls
        .iter()
        .filter(|c| c.left)
        .map(|c| c.ordinal)
        .max()
        .unwrap_or(-1);
    Ok(departing
        .iter()
        .find(|c| c.ordinal > last_left)
        .map_or(last_departing, |c| (*c).clone()))
}
