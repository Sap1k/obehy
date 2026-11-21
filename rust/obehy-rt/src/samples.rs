//! The diagnostic records of the output stream (`compare --diagnostics|--quality`). What the replay
//! diagnostics read of the core's state, so they read the stream and never the core; the shapes are
//! the contract in [`crate::compare`]'s module doc.
//!
//! - `predictor_sample` (`--diagnostics`, the rail feed only, every
//!   [`PREDICTOR_SAMPLE_EVERY_S`] on round multiples): each running rail run's arrival events and
//!   every predictor's arrival at the source's predicted next stop;
//! - `journey_sample` (`--quality`, every emit tick, per feed): each journey's lifecycle,
//!   progress, tracker reading, distances from its lead fix, and its calls and plan, the calls
//!   and plan only where they changed since the journey's previous sample (floats exact).
//!
//! Python skips unchanged calls and plans by object identity; values are compared here, which
//! gives the same records (identity implies equal values).

use std::collections::HashMap;
use std::sync::Arc;

use obehy_core::config::policy::Predictor;
use obehy_core::core::Context;
use obehy_core::geo::distance_m;
use obehy_core::index::{Index, IndexMiss};
use obehy_core::model::{CallState, FeedState, Instance, JourneyKey};
use obehy_core::time::{Instant, ServiceTime};
use obehy_core::timeline::fusion::{anchor_prediction, current_lateness};
use obehy_core::timeline::path::point_at;
use obehy_core::timeline::plan::Plan;

use crate::canonical::{obj, text, time};
use crate::store::recording::journey_plain;
use serde_json::Value;

pub const PREDICTOR_SAMPLE_EVERY_S: i64 = 300;

fn opt_float(value: Option<f64>) -> Value {
    value.map_or(Value::Null, Value::from)
}

/// The `predictor_sample` record of the rail feed at `at`.
pub fn predictor_sample(state: &FeedState, index: &Index, at: Instant) -> Result<Value, IndexMiss> {
    let mut journeys = Vec::new();
    for (journey, instance) in &state.instances {
        if instance.parts.is_empty() || instance.lifecycle.as_str() != "running" {
            continue;
        }
        let trip = index.trip(&instance.trip_id)?;
        let arrivals: Vec<Value> = instance
            .calls
            .iter()
            .enumerate()
            .filter_map(|(i, call)| {
                call.arrival
                    .map(|a| Value::Array(vec![Value::from(i as i64), time(Some(a.mid()))]))
            })
            .collect();
        let (own, _) = current_lateness(instance);
        let mut predicted = Vec::new();
        for (name, predictor) in Predictor::ALL {
            let found = if predictor == Predictor::Propagate {
                match (anchor_prediction(instance, trip, index, Predictor::Sz), own) {
                    (Some((i, _)), Some(own)) => Some((i, own)),
                    _ => None,
                }
            } else {
                anchor_prediction(instance, trip, index, predictor)
            };
            let Some((i, lateness)) = found else {
                continue;
            };
            let Some(scheduled) = trip.calls[i].arrival else {
                continue;
            };
            let when = ServiceTime::new(journey.service_date, scheduled).instant() + lateness;
            predicted.push(Value::Array(vec![
                text(name),
                Value::from(i as i64),
                time(Some(when)),
            ]));
        }
        if !arrivals.is_empty() || !predicted.is_empty() {
            journeys.push(obj([
                ("journey", journey_plain(journey)),
                ("arrivals", Value::Array(arrivals)),
                ("predicted", Value::Array(predicted)),
            ]));
        }
    }
    Ok(obj([
        ("kind", text("predictor_sample")),
        ("at", time(Some(at))),
        ("feed", text(state.feed.as_str())),
        ("journeys", Value::Array(journeys)),
    ]))
}

/// A call's sampled values (`samples._call_values`).
type CallValues = (
    Option<Instant>,
    Option<Instant>,
    Option<Instant>,
    Option<Instant>,
    Option<Instant>,
    Option<Instant>,
    bool,
    &'static str,
);

fn call_values(call: &CallState) -> CallValues {
    (
        call.arrival.map(|i| i.mid()),
        call.departure.map(|i| i.mid()),
        call.estimated_arrival,
        call.estimated_departure,
        call.passed_arrival,
        call.passed_departure,
        call.passenger,
        call.status.as_str(),
    )
}

fn call_record(v: &CallValues) -> Value {
    obj([
        ("arrival", time(v.0)),
        ("departure", time(v.1)),
        ("estimated_arrival", time(v.2)),
        ("estimated_departure", time(v.3)),
        ("passed_arrival", time(v.4)),
        ("passed_departure", time(v.5)),
        ("passenger", Value::Bool(v.6)),
        ("status", text(v.7)),
    ])
}

fn plan_values(plan: &Plan) -> Value {
    let path = &plan.path;
    obj([
        (
            "scheduled",
            Value::Array(
                plan.scheduled
                    .iter()
                    .map(|(a, d)| Value::Array(vec![time(*a), time(*d)]))
                    .collect(),
            ),
        ),
        (
            "zones",
            Value::Array(
                plan.zones
                    .iter()
                    .map(|z| Value::Array(vec![opt_float(z.arrival_m), opt_float(z.departure_m)]))
                    .collect(),
            ),
        ),
        (
            "calls_m",
            Value::Array(
                path.call_distances_m
                    .iter()
                    .map(|d| Value::from(*d))
                    .collect(),
            ),
        ),
        ("shaped", Value::Bool(path.shaped)),
        ("points", Value::from(path.points.len())),
    ])
}

/// What a journey's previous sample carried.
#[derive(Debug)]
struct Seen {
    calls: Arc<[CallState]>,
    values: Vec<CallValues>,
    plan: Arc<Plan>,
    plan_values: Value,
}

/// Writes `journey_sample` records; remembers each journey's last calls and plan, and forgets a
/// journey absent from a sample of its feed (as the reader of the records does).
///
/// Eviction: a journey leaves with the first sample of its feed it is missing from.
#[derive(Debug, Default)]
pub struct JourneySampler {
    seen: HashMap<JourneyKey, Seen>,
}

impl JourneySampler {
    pub fn new() -> JourneySampler {
        JourneySampler::default()
    }

    pub fn sample(
        &mut self,
        state: &FeedState,
        ctx: &mut Context,
        at: Instant,
    ) -> Result<Value, IndexMiss> {
        let mut journeys = Vec::with_capacity(state.instances.len());
        for (journey, instance) in &state.instances {
            journeys.push(self.journey(journey, instance, state, ctx)?);
        }
        self.seen
            .retain(|j, _| j.feed != state.feed || state.instances.contains_key(j));
        Ok(obj([
            ("kind", text("journey_sample")),
            ("at", time(Some(at))),
            ("feed", text(state.feed.as_str())),
            ("journeys", Value::Array(journeys)),
        ]))
    }

    fn journey(
        &mut self,
        journey: &JourneyKey,
        instance: &Instance,
        state: &FeedState,
        ctx: &mut Context,
    ) -> Result<Value, IndexMiss> {
        let trip = ctx.index.trip(&instance.trip_id)?.clone();
        let plan = ctx
            .plans
            .plan(&trip, journey.service_date, ctx.index, ctx.policy)?;
        let (progress, track) = (instance.progress, instance.track.as_ref());
        let reached = progress.map_or(-1, |p| p.call_index);
        let track_record = track.map_or(Value::Null, |track| {
            let best = track.hypotheses.first();
            obj([
                ("seen_at", time(track.seen_at)),
                (
                    "at_stop",
                    Value::Bool(track.at_stop.is_some_and(|s| s.away.is_none())),
                ),
                ("lateness_s", opt_float(track.lateness_s())),
                ("hypotheses", Value::Bool(best.is_some())),
                (
                    "off_path",
                    Value::Bool(best.is_some_and(|b| b.off_path_since.is_some())),
                ),
            ])
        });
        let fix = fix(
            instance,
            &plan,
            state,
            reached,
            ctx.policy.lifecycle.departure_margin_m,
        );
        let (calls, shown) = self.changes(journey, instance, &plan);
        Ok(obj([
            ("journey", journey_plain(journey)),
            ("trip_id", text(&instance.trip_id)),
            ("mode", text(&instance.mode)),
            ("lifecycle", text(instance.lifecycle.as_str())),
            ("stale", Value::Bool(instance.freshness.stale)),
            ("lost", Value::Bool(instance.freshness.lost)),
            ("rail", Value::Bool(!instance.parts.is_empty())),
            ("lead", Value::Bool(instance.lead.is_some())),
            (
                "progress",
                progress.map_or(Value::Null, |p| {
                    obj([
                        ("call", Value::from(p.call_index)),
                        ("along_m", Value::from(p.distance_m)),
                    ])
                }),
            ),
            ("track", track_record),
            ("fix", fix),
            ("n", Value::from(instance.calls.len())),
            ("calls", Value::Array(calls)),
            ("plan", shown.unwrap_or(Value::Null)),
        ]))
    }

    /// The calls that changed since the journey's last sample (all on its first, or when their
    /// number changed) and its plan when that changed.
    fn changes(
        &mut self,
        journey: &JourneyKey,
        instance: &Instance,
        plan: &Arc<Plan>,
    ) -> (Vec<Value>, Option<Value>) {
        let calls = &instance.calls;
        let before = self.seen.remove(journey);
        let mut changed: Vec<(usize, CallValues)> = Vec::new();
        let values = match &before {
            Some(b) if Arc::ptr_eq(&b.calls, calls) => b.values.clone(),
            Some(b) if b.values.len() == calls.len() => {
                let mut values = b.values.clone();
                for (i, call) in calls.iter().enumerate() {
                    let value = call_values(call);
                    if value != values[i] {
                        values[i] = value;
                        changed.push((i, value));
                    }
                }
                values
            }
            _ => {
                let values: Vec<CallValues> = calls.iter().map(call_values).collect();
                changed = values.iter().copied().enumerate().collect();
                values
            }
        };
        let mut shown = None;
        let plan_values = match &before {
            Some(b) if Arc::ptr_eq(&b.plan, plan) => b.plan_values.clone(),
            _ => {
                let made = plan_values(plan);
                if before.as_ref().is_none_or(|b| b.plan_values != made) {
                    shown = Some(made.clone());
                }
                made
            }
        };
        self.seen.insert(
            journey.clone(),
            Seen {
                calls: calls.clone(),
                values,
                plan: plan.clone(),
                plan_values,
            },
        );
        let records = changed
            .iter()
            .map(|(i, v)| Value::Array(vec![Value::from(*i as i64), call_record(v)]))
            .collect();
        (records, shown)
    }
}

/// Distances from the lead vehicle's position along a path of two points or more.
fn fix(instance: &Instance, plan: &Plan, state: &FeedState, reached: i64, margin: f64) -> Value {
    let (Some(track), Some(lead)) = (&instance.track, &instance.lead) else {
        return Value::Null;
    };
    let path = &plan.path;
    if track.seen_at.is_none() || path.points.len() < 2 {
        return Value::Null;
    }
    let Some(position) = state
        .vehicles
        .get(&lead.vehicle)
        .and_then(|v| v.position.as_ref())
    else {
        return Value::Null;
    };
    let here = (position.lon, position.lat);
    let calls = &path.call_distances_m;
    let best = track.hypotheses.first();
    let from = usize::try_from((reached - 1).max(0)).expect("non-negative");
    let to = usize::try_from((reached + 3).max(0))
        .expect("non-negative")
        .min(calls.len());
    let near: Vec<Value> = (from..to)
        .filter(|&i| distance_m(here, point_at(path, calls[i])) <= margin)
        .map(|i| Value::from(i as i64))
        .collect();
    obj([
        (
            "best_m",
            best.map_or(Value::Null, |b| {
                Value::from(distance_m(here, point_at(path, b.along_m)))
            }),
        ),
        (
            "first_m",
            Value::from(distance_m(here, point_at(path, calls[0]))),
        ),
        (
            "last_m",
            Value::from(distance_m(here, point_at(path, calls[calls.len() - 1]))),
        ),
        ("near", Value::Array(near)),
    ])
}
