//! Effects and core state as rows (`emit/db.py`'s row functions), in the column order of
//! `store`'s `*_COLUMNS` and state tables.

use std::sync::Arc;

use obehy_core::model::json::facts_to_json;
use obehy_core::model::{
    CallState, Feed, Instance, JourneyKey, LinkJourneys, ObservationResult, PartSpan,
    RecordCancellation, RecordPlatform, VehicleKey, WriteEvent,
};
use obehy_core::py::json::PyJson;
use obehy_core::time::Instant;

use super::cells::{Cell, Row};

fn key_cells(journey: &JourneyKey) -> [Cell; 4] {
    [
        Cell::text(journey.feed.as_str()),
        Cell::text(&journey.namespace),
        Cell::text(&journey.key),
        Cell::Date(journey.service_date),
    ]
}

/// The `rt.observation` row of an observation's result (`observation_row`).
pub fn observation_row(result: &ObservationResult, release_id: &str) -> Row {
    let obs = &result.observation;
    let journey = result.journey.as_ref();
    let vehicle = obs.first::<VehicleKey>();
    vec![
        Cell::Time(obs.received_at),
        Cell::text(&obs.source),
        Cell::text(&obs.channel),
        Cell::text(obs.feed.as_str()),
        Cell::text(&obs.raw.sha256),
        Cell::Int(obs.raw.item),
        Cell::opt_time(obs.observed_at),
        Cell::Int(obs.decoder_version),
        Cell::Int(obehy_core::model::FACT_SCHEMA_VERSION),
        Cell::json(facts_to_json(&obs.facts)),
        Cell::opt_text(journey.map(|j| j.namespace.as_str())),
        Cell::opt_text(journey.map(|j| j.key.as_str())),
        Cell::opt_date(journey.map(|j| j.service_date)),
        Cell::opt_text(result.reason.map(|r| r.as_str())),
        Cell::text(release_id),
        Cell::opt_text(vehicle.map(|v| v.source_vehicle_id.as_str())),
        // A row without a vehicle (a board row) starts its own state.
        Cell::Time(result.state_since.unwrap_or(obs.received_at)),
    ]
}

pub fn event_row(e: &WriteEvent) -> Row {
    let mut row: Row = key_cells(&e.journey).into();
    row.extend([
        Cell::text(&e.location_id),
        Cell::Int(i64::from(e.visit_n)),
        Cell::text(e.kind.as_str()),
        Cell::Time(e.interval.mid()),
        Cell::Time(e.interval.lo),
        Cell::Time(e.interval.hi),
        Cell::text(e.method.as_str()),
        Cell::text(&e.source),
    ]);
    row
}

pub fn link_row(link: &LinkJourneys) -> Row {
    vec![
        Cell::text(link.source.feed.as_str()),
        Cell::text(&link.source.namespace),
        Cell::text(&link.source.key),
        Cell::text(&link.target.namespace),
        Cell::text(&link.target.key),
        Cell::Date(link.source.service_date),
        Cell::text(link.kind.as_str()),
    ]
}

pub fn platform_row(e: &RecordPlatform) -> Row {
    let mut row: Row = key_cells(&e.journey).into();
    row.extend([
        Cell::text(&e.location_id),
        Cell::Int(i64::from(e.visit_n)),
        Cell::text(e.kind.as_str()),
        Cell::text(&e.platform.value),
        Cell::text(e.platform.label.as_str()),
        Cell::opt_text(e.platform.boarding_point_id.as_deref()),
        Cell::text(&e.platform.source),
        Cell::Time(e.platform.assigned_at),
        Cell::Time(e.platform.assigned_at),
    ]);
    row
}

pub fn cancellation_row(c: &RecordCancellation) -> Row {
    let mut row: Row = key_cells(&c.journey).into();
    row.extend([
        Cell::text(&c.source),
        Cell::Time(c.at),
        Cell::Bool(c.cancelled),
    ]);
    row
}

pub fn part_row(i: &Instance, part: &PartSpan, release_id: &str) -> Row {
    let mut row: Row = key_cells(&i.journey).into();
    row.extend([
        Cell::text(&part.trip_id),
        Cell::Int(part.first as i64),
        Cell::Int(part.last as i64),
        Cell::text(release_id),
    ]);
    row
}

/// The calls' JSON (`json.dumps([_call_json(c, i <= left) ...])`), as a value.
pub fn calls_json(instance: &Instance) -> PyJson {
    let left = instance.left_index();
    PyJson::List(
        instance
            .calls
            .iter()
            .enumerate()
            .map(|(i, c)| call_json(c, (i as i64) <= left))
            .collect(),
    )
}

pub fn trip_row(i: &Instance, calls: Arc<PyJson>) -> Row {
    let mut row: Row = key_cells(&i.journey).into();
    row.extend([
        Cell::text(&i.release_id),
        Cell::text(&i.trip_id),
        Cell::text(i.lifecycle.as_str()),
        Cell::opt_int(i.lateness_s),
        Cell::Bool(i.off_route),
        Cell::Bool(i.freshness.stale),
        Cell::Time(i.freshness.updated_at),
        Cell::Json(calls),
        Cell::json(sources_json(i)),
    ]);
    row
}

fn iso(value: Option<Instant>) -> PyJson {
    value.map_or(PyJson::Null, |at| PyJson::str(at.isoformat()))
}

/// Which sources the journey's realtime came from (the API's provenance), by source.
pub fn sources_json(i: &Instance) -> PyJson {
    let mut tracks: Vec<_> = i.sources.iter().collect();
    tracks.sort_by(|a, b| a.source.cmp(&b.source));
    PyJson::List(
        tracks
            .into_iter()
            .map(|t| {
                PyJson::object([
                    ("source", PyJson::str(&t.source)),
                    ("heard_at", iso(Some(t.heard_at))),
                    ("position_at", iso(t.position_at)),
                    ("delay_s", PyJson::opt(t.delay_s, PyJson::Int)),
                    (
                        "leads_position",
                        PyJson::Bool(i.track_source.as_deref() == Some(t.source.as_str())),
                    ),
                ])
            })
            .collect(),
    )
}

/// A call's current state; `passed`: the vehicle has left it (`Instance.left_index`).
pub fn call_json(call: &CallState, passed: bool) -> PyJson {
    PyJson::object([
        ("sequence", PyJson::Int(call.sequence)),
        ("location_id", PyJson::str(&call.location_id)),
        ("visit_n", PyJson::Int(i64::from(call.visit_n))),
        ("status", PyJson::lit(call.status.as_str())),
        ("passed", PyJson::Bool(passed)),
        ("estimated_arrival", iso(call.estimated_arrival)),
        ("estimated_departure", iso(call.estimated_departure)),
        (
            "source_class",
            PyJson::opt(call.source_class, |c| PyJson::lit(c.as_str())),
        ),
        ("passenger", PyJson::Bool(call.passenger)),
        (
            "platform",
            PyJson::opt(call.platform.as_ref(), |p| {
                PyJson::object([
                    ("value", PyJson::str(&p.value)),
                    ("label", PyJson::lit(p.label.as_str())),
                    (
                        "boarding_point_id",
                        PyJson::opt(p.boarding_point_id.as_deref(), PyJson::str),
                    ),
                    ("source", PyJson::str(&p.source)),
                ])
            }),
        ),
    ])
}

/// Where a vehicle row draws the vehicle: its own fix, or the journey's (`represents`).
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Drawn {
    pub lat: Option<f64>,
    pub lon: Option<f64>,
    pub bearing: Option<f64>,
    pub at: Option<Instant>,
}

/// A `vehicle_state_current` row (`_vehicle_row`), from what the writer resolved: the trip it is
/// on, where it is drawn and which vehicle it is.
#[allow(clippy::too_many_arguments)]
pub fn vehicle_row(
    feed: Feed,
    v: &obehy_core::model::VehicleState,
    trip_id: Option<String>,
    drawn: Drawn,
    release_id: &str,
    identity: (String, Option<String>),
    represents: bool,
) -> Row {
    let b = v.binding.as_ref();
    vec![
        Cell::text(feed.as_str()),
        Cell::text(&v.vehicle.source),
        Cell::text(&v.vehicle.source_vehicle_id),
        Cell::text(v.status.as_str()),
        Cell::Time(v.last_seen),
        Cell::opt_text(b.map(|b| b.journey.namespace.as_str())),
        Cell::opt_text(b.map(|b| b.journey.key.as_str())),
        Cell::opt_date(b.map(|b| b.journey.service_date)),
        Cell::opt_text(trip_id),
        Cell::opt_float(drawn.lat),
        Cell::opt_float(drawn.lon),
        Cell::opt_float(drawn.bearing),
        Cell::opt_text(v.reason.map(|r| r.as_str())),
        Cell::text(release_id),
        Cell::Text(identity.0),
        Cell::opt_text(identity.1),
        Cell::Bool(represents),
        Cell::opt_time(drawn.at),
    ]
}
