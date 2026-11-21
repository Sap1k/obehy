//! The diagnostic records of the output stream (`compare --diagnostics|--quality`): port of
//! `emit/samples.py`. What the replay diagnostics read of the core's state, so they read the
//! stream and never the core; the shapes are the contract in `compare.py`'s docstring.
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

use crate::canonical::C;
use crate::store::recording::journey_plain;

pub const PREDICTOR_SAMPLE_EVERY_S: i64 = 300;

fn time(at: Option<Instant>) -> C {
    C::time(at)
}

fn opt_float(value: Option<f64>) -> C {
    value.map_or(C::Null, C::Float)
}

/// The `predictor_sample` record of the rail feed at `at`.
pub fn predictor_sample(state: &FeedState, index: &Index, at: Instant) -> Result<C, IndexMiss> {
    let mut journeys = Vec::new();
    for (journey, instance) in &state.instances {
        if instance.parts.is_empty() || instance.lifecycle.as_str() != "running" {
            continue;
        }
        let trip = index.trip(&instance.trip_id)?;
        let arrivals: Vec<C> = instance
            .calls
            .iter()
            .enumerate()
            .filter_map(|(i, call)| {
                call.arrival
                    .map(|a| C::List(vec![C::Int(i as i128), time(Some(a.mid()))]))
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
            predicted.push(C::List(vec![
                C::str(name),
                C::Int(i as i128),
                time(Some(when)),
            ]));
        }
        if !arrivals.is_empty() || !predicted.is_empty() {
            journeys.push(C::obj([
                ("journey", journey_plain(journey)),
                ("arrivals", C::List(arrivals)),
                ("predicted", C::List(predicted)),
            ]));
        }
    }
    Ok(C::obj([
        ("kind", C::str("predictor_sample")),
        ("at", time(Some(at))),
        ("feed", C::str(state.feed.as_str())),
        ("journeys", C::List(journeys)),
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

fn call_record(v: &CallValues) -> C {
    C::obj([
        ("arrival", time(v.0)),
        ("departure", time(v.1)),
        ("estimated_arrival", time(v.2)),
        ("estimated_departure", time(v.3)),
        ("passed_arrival", time(v.4)),
        ("passed_departure", time(v.5)),
        ("passenger", C::Bool(v.6)),
        ("status", C::str(v.7)),
    ])
}

fn plan_values(plan: &Plan) -> C {
    let path = &plan.path;
    C::obj([
        (
            "scheduled",
            C::List(
                plan.scheduled
                    .iter()
                    .map(|(a, d)| C::List(vec![time(*a), time(*d)]))
                    .collect(),
            ),
        ),
        (
            "zones",
            C::List(
                plan.zones
                    .iter()
                    .map(|z| C::List(vec![opt_float(z.arrival_m), opt_float(z.departure_m)]))
                    .collect(),
            ),
        ),
        (
            "calls_m",
            C::List(path.call_distances_m.iter().map(|d| C::Float(*d)).collect()),
        ),
        ("shaped", C::Bool(path.shaped)),
        ("points", C::Int(path.points.len() as i128)),
    ])
}

/// What a journey's previous sample carried.
#[derive(Debug)]
struct Seen {
    calls: Arc<[CallState]>,
    values: Vec<CallValues>,
    plan: Arc<Plan>,
    plan_values: C,
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
    ) -> Result<C, IndexMiss> {
        let mut journeys = Vec::with_capacity(state.instances.len());
        for (journey, instance) in &state.instances {
            journeys.push(self.journey(journey, instance, state, ctx)?);
        }
        self.seen
            .retain(|j, _| j.feed != state.feed || state.instances.contains_key(j));
        Ok(C::obj([
            ("kind", C::str("journey_sample")),
            ("at", time(Some(at))),
            ("feed", C::str(state.feed.as_str())),
            ("journeys", C::List(journeys)),
        ]))
    }

    fn journey(
        &mut self,
        journey: &JourneyKey,
        instance: &Instance,
        state: &FeedState,
        ctx: &mut Context,
    ) -> Result<C, IndexMiss> {
        let trip = ctx.index.trip(&instance.trip_id)?.clone();
        let plan = ctx
            .plans
            .plan(&trip, journey.service_date, ctx.index, ctx.policy)?;
        let (progress, track) = (instance.progress, instance.track.as_ref());
        let reached = progress.map_or(-1, |p| p.call_index);
        let track_record = track.map_or(C::Null, |track| {
            let best = track.hypotheses.first();
            C::obj([
                ("seen_at", time(track.seen_at)),
                (
                    "at_stop",
                    C::Bool(track.at_stop.is_some_and(|s| s.away.is_none())),
                ),
                ("lateness_s", opt_float(track.lateness_s())),
                ("hypotheses", C::Bool(best.is_some())),
                (
                    "off_path",
                    C::Bool(best.is_some_and(|b| b.off_path_since.is_some())),
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
        Ok(C::obj([
            ("journey", journey_plain(journey)),
            ("trip_id", C::str(&instance.trip_id)),
            ("mode", C::str(&instance.mode)),
            ("lifecycle", C::str(instance.lifecycle.as_str())),
            ("stale", C::Bool(instance.freshness.stale)),
            ("lost", C::Bool(instance.freshness.lost)),
            ("rail", C::Bool(!instance.parts.is_empty())),
            ("lead", C::Bool(instance.lead.is_some())),
            (
                "progress",
                progress.map_or(C::Null, |p| {
                    C::obj([
                        ("call", C::Int(i128::from(p.call_index))),
                        ("along_m", C::Float(p.distance_m)),
                    ])
                }),
            ),
            ("track", track_record),
            ("fix", fix),
            ("n", C::Int(instance.calls.len() as i128)),
            ("calls", C::List(calls)),
            ("plan", shown.unwrap_or(C::Null)),
        ]))
    }

    /// The calls that changed since the journey's last sample (all on its first, or when their
    /// number changed) and its plan when that changed.
    fn changes(
        &mut self,
        journey: &JourneyKey,
        instance: &Instance,
        plan: &Arc<Plan>,
    ) -> (Vec<C>, Option<C>) {
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
            .map(|(i, v)| C::List(vec![C::Int(*i as i128), call_record(v)]))
            .collect();
        (records, shown)
    }
}

/// Distances from the lead vehicle's position along a path of two points or more.
fn fix(instance: &Instance, plan: &Plan, state: &FeedState, reached: i64, margin: f64) -> C {
    let (Some(track), Some(lead)) = (&instance.track, &instance.lead) else {
        return C::Null;
    };
    let path = &plan.path;
    if track.seen_at.is_none() || path.points.len() < 2 {
        return C::Null;
    }
    let Some(position) = state
        .vehicles
        .get(&lead.vehicle)
        .and_then(|v| v.position.as_ref())
    else {
        return C::Null;
    };
    let here = (position.lon, position.lat);
    let calls = &path.call_distances_m;
    let best = track.hypotheses.first();
    let from = usize::try_from((reached - 1).max(0)).expect("non-negative");
    let to = usize::try_from((reached + 3).max(0))
        .expect("non-negative")
        .min(calls.len());
    let near: Vec<C> = (from..to)
        .filter(|&i| distance_m(here, point_at(path, calls[i])) <= margin)
        .map(|i| C::Int(i as i128))
        .collect();
    C::obj([
        (
            "best_m",
            best.map_or(C::Null, |b| {
                C::Float(distance_m(here, point_at(path, b.along_m)))
            }),
        ),
        (
            "first_m",
            C::Float(distance_m(here, point_at(path, calls[0]))),
        ),
        (
            "last_m",
            C::Float(distance_m(here, point_at(path, calls[calls.len() - 1]))),
        ),
        ("near", C::List(near)),
    ])
}
