//! Several sources on one rail run (docs/R2_SLICE.md section 5, BASE_PLAN.md section 20.6).
//! Port of `realtime/timeline/fusion.py`, operation for operation (its docstring has the model).
//!
//! No source owns a run; each keeps a [`SourceTrack`] on the instance (sorted by source name),
//! and each capability is decided on its own: positions feed the one tracker (a lower-ranked
//! source's fix may only move it forward while a better one is fresh, [`position_leads`]); a
//! source's point event commits a call and anchors progress ([`apply_point`]); the current delay
//! is the freshest point delay while it is newer than the last GPS fix, else the measured
//! lateness ([`current_lateness`]); prediction follows `rail.predictor`
//! ([`anchor_prediction`]).
//!
//! Decisions on Python's tie rules: `max(key=delay_at or heard_at)` and `max(key=heard_at)`
//! keep the first source seen (the lowest name) on ties; `round(lateness_s)` is half to even.

use std::sync::Arc;

use crate::config::Policy;
use crate::config::policy::Predictor;
use crate::index::{Index, Trip, calls_at_station};
use crate::model::{
    CallState, Delay, DelayReference, Effect, EventKind, EventMethod, Instance, Interval, Lead,
    NextPoint, NextStopPrediction, Observation, PointEvent, Progress, SourceClass, SourceSemantics,
    SourceTrack, Track, Unresolved, UnresolvedKind, VehicleId, WriteEvent,
};
use crate::py::num::{py_max, py_round_int};
use crate::time::{Instant, TimeDelta};
use crate::timeline::estimate::Lateness;
use crate::timeline::plan::Plan;

/// A source track with only its source and reception time (`SourceTrack(source, heard_at)`).
pub fn source_track(source: &str, heard_at: Instant) -> SourceTrack {
    SourceTrack {
        source: source.to_owned(),
        heard_at,
        facts: None,
        next_point: None,
        delay_s: None,
        delay_at: None,
        delay_reference: None,
        prediction: None,
        point: None,
        position_at: None,
        lead: None,
    }
}

/// The instance with `track` as its source's track.
pub fn with_track(mut instance: Instance, track: SourceTrack) -> Instance {
    tracks_with(&mut instance.sources, track);
    instance
}

/// `sources` (sorted by source) with `track` in its source's place.
pub fn tracks_with(sources: &mut Vec<SourceTrack>, track: SourceTrack) {
    for i in 0..sources.len() {
        if sources[i].source == track.source {
            sources[i] = track;
            return;
        }
        if sources[i].source > track.source {
            sources.insert(i, track);
            return;
        }
    }
    sources.push(track);
}

/// The vehicle `vehicle` leads the journey from `observation` on: the instance's own lead for
/// the first source (or the same one), else that source's track's.
pub fn with_lead(
    mut instance: Instance,
    vehicle: &VehicleId,
    observation: &Observation,
) -> Instance {
    let lead = Lead {
        vehicle: vehicle.clone(),
        seen: observation.at(),
    };
    if instance
        .lead
        .as_ref()
        .is_none_or(|l| l.vehicle.source == vehicle.source)
    {
        instance.lead = Some(lead);
        return instance;
    }
    let mut track = instance
        .source_track(&vehicle.source)
        .cloned()
        .unwrap_or_else(|| source_track(&vehicle.source, observation.received_at));
    track.lead = Some(lead);
    with_track(instance, track)
}

/// What the source says: its new track, its previous one, and false for an unchanged entry of
/// a source where that is no news (SZ-Q6). `delay_s` is the observation's usable delay
/// (`timeline::usable_delay`).
pub fn note_source(
    instance: &Instance,
    observation: &Observation,
    semantics: SourceSemantics,
    delay_s: Option<i64>,
) -> (SourceTrack, Option<SourceTrack>, bool) {
    let previous = instance.source_track(&observation.source);
    let no_news = semantics.unchanged_entry_is_no_news;
    if no_news
        && let Some(previous) = previous
        && previous.facts.as_deref() == Some(&observation.facts[..])
    {
        return (previous.clone(), Some(previous.clone()), false);
    }
    let mut track = previous
        .cloned()
        .unwrap_or_else(|| source_track(&observation.source, observation.received_at));
    let delay = if delay_s.is_some() {
        observation.first::<Delay>()
    } else {
        None
    };
    track.heard_at = observation.received_at;
    track.facts = no_news.then(|| Arc::from(&observation.facts[..]));
    if let Some(next) = observation.first::<NextPoint>() {
        track.next_point = Some(next.clone());
    }
    if let Some(prediction) = observation.first::<NextStopPrediction>() {
        track.prediction = Some(prediction.clone());
    }
    if let Some(delay_s) = delay_s {
        track.delay_s = Some(delay_s);
        track.delay_at = Some(observation.at());
    }
    if delay.is_some_and(|d| d.reference == DelayReference::Point) {
        track.delay_reference = Some(DelayReference::Point);
    }
    (track, previous.cloned(), true)
}

/// Whether no better-ranked source has a fresh fix: then this source's fix moves the tracker
/// even back, else only forward.
pub fn position_leads(instance: &Instance, source: &str, at: Instant, policy: &Policy) -> bool {
    let rail = &policy.rail;
    let rank = rail.rank(source);
    let fresh = TimeDelta::from_seconds(rail.position_fresh_s);
    for other in &instance.sources {
        let Some(position_at) = other.position_at else {
            continue;
        };
        if other.source == source {
            continue;
        }
        if rail.rank(&other.source) < rank && at - position_at <= fresh {
            return false;
        }
    }
    true
}

/// One event from two: a measured interval inside the point's minute (widened by the
/// tolerance) narrows it; one outside loses to the point.
pub fn merge(
    existing: Option<Interval>,
    new: Interval,
    new_is_point: bool,
    policy: &Policy,
) -> Interval {
    let Some(existing) = existing else {
        return new;
    };
    let (point, measured) = if new_is_point {
        (new, existing)
    } else {
        (existing, new)
    };
    let slack = TimeDelta::from_seconds(policy.rail.event_tolerance_s);
    let lo = measured.lo.max(point.lo - slack);
    let hi = measured.hi.min(point.hi + slack);
    if lo <= hi {
        Interval::new(lo, hi)
    } else {
        point
    }
}

/// Where a source's point may lie: from the call it placed the train at last, else one call
/// behind the run's frontier.
fn start(instance: &Instance, previous: Option<&SourceTrack>) -> i64 {
    if let Some(point) = previous.and_then(|p| p.point) {
        return point;
    }
    (instance.reached_index() - 1).max(0)
}

/// The first call from `start` with one of the codes that fits the run's other events in
/// time; with codes found but none fitting, (None, true).
fn resolve(
    instance: &Instance,
    trip: &Trip,
    start: i64,
    codes: &[&str],
    actual: Interval,
    index: &Index,
    policy: &Policy,
) -> (Option<usize>, bool) {
    let mut found = false;
    for i in calls_at_station(trip, index, codes, start, false) {
        found = true;
        if consistent(instance, i, actual, policy) {
            return (Some(i), false);
        }
    }
    (None, found)
}

/// Whether a point event at call `i` fits the run's other events in time (within the
/// tolerance): never before an earlier call's, never after a later call's.
fn consistent(instance: &Instance, i: usize, actual: Interval, policy: &Policy) -> bool {
    let slack = TimeDelta::from_seconds(policy.rail.event_tolerance_s);
    for (j, call) in instance.calls.iter().enumerate() {
        let times: Vec<Instant> = [call.passed_arrival, call.passed_departure]
            .into_iter()
            .flatten()
            .collect();
        if times.is_empty() || j == i {
            continue;
        }
        let latest = *times.iter().max().expect("not empty");
        let earliest = *times.iter().min().expect("not empty");
        if j < i && actual.hi + slack < latest {
            return false;
        }
        if j > i && actual.lo - slack > earliest {
            return false;
        }
    }
    true
}

/// Readings behind a source's point contradict it and are dropped; nothing before it is
/// committed again.
pub fn anchor(track: Option<Track>, along_m: f64, policy: &Policy) -> Option<Track> {
    let mut track = track?;
    let jitter = policy.progress.jitter_m;
    track.hypotheses.retain(|h| h.along_m >= along_m - jitter);
    track.committed_m = py_max(track.committed_m, along_m);
    Some(track)
}

/// A point event: the call it names at or after the frontier gets its event (SZ-Q3: the
/// arrival while standing, else the departure, or a passage at a railway point).
pub fn apply_point(
    instance: Instance,
    trip: &Trip,
    plan: &Plan,
    observation: &Arc<Observation>,
    previous: Option<&SourceTrack>,
    index: &Index,
    policy: &Policy,
) -> (Instance, Vec<Effect>) {
    let Some(event) = observation.first::<PointEvent>() else {
        return (instance, Vec::new());
    };
    let mut codes: Vec<&str> = event.codes.iter().map(String::as_str).collect();
    if let Some(hint) = previous.and_then(|p| p.next_point.as_ref())
        && hint.name == event.name
        && let Some(sr70) = &hint.sr70
    {
        codes.push(sr70);
    }
    let from = start(&instance, previous);
    let (found, misfit) = resolve(&instance, trip, from, &codes, event.actual, index, policy);
    let Some(i) = found else {
        let kind = if misfit {
            UnresolvedKind::Inconsistent
        } else {
            UnresolvedKind::Point
        };
        let unresolved = Unresolved {
            observation: observation.clone(),
            kind,
            name: event.name.clone(),
        };
        return (instance, vec![Effect::Unresolved(unresolved)]); // moves nothing
    };
    let last = trip.calls.len() - 1;
    if i == 0 && event.standing {
        return (instance, Vec::new()); // waiting at the origin: nothing has happened yet
    }
    let call = &trip.calls[i];
    let kind = if event.standing {
        EventKind::Arrival
    } else if call.passenger_service || i == 0 || i == last {
        EventKind::Departure
    } else {
        EventKind::Passage
    };
    let mut calls: Vec<CallState> = instance.calls.to_vec();
    let mut state = calls[i].clone();
    let before = if kind == EventKind::Arrival {
        state.arrival
    } else {
        state.departure
    };
    let merged;
    if kind == EventKind::Arrival {
        merged = merge(state.arrival, event.actual, true, policy);
        state.arrival = Some(merged);
        state.passed_arrival = Some(merged.mid());
    } else {
        merged = merge(state.departure, event.actual, true, policy);
        state.departure = Some(merged);
        state.passed_departure = Some(merged.mid());
        if kind == EventKind::Passage && state.arrival.is_none() {
            state.passed_arrival = Some(merged.mid());
        }
    }
    calls[i] = state;
    let zone = plan.zones[i];
    let along = if kind == EventKind::Arrival {
        zone.arrival_m
    } else {
        zone.departure_m
    }
    .unwrap_or(plan.path.call_distances_m[i]);
    let mut progress = instance.progress;
    if progress.is_none_or(|p| along > p.distance_m) {
        progress = Some(Progress {
            distance_m: along,
            call_index: plan.call_index_at(along),
            at: event.actual.lo,
        });
    }
    let track = instance.source_track(&observation.source).cloned();
    let mut instance = instance;
    instance.calls = calls.into();
    instance.progress = progress;
    instance.track = anchor(instance.track.take(), along, policy);
    instance.lifecycle = instance.settled_lifecycle(true);
    if let Some(mut track) = track {
        track.point = Some(i as i64);
        instance = with_track(instance, track);
    }
    if Some(merged) == before {
        return (instance, Vec::new()); // a repeat of the point already recorded
    }
    let (journey, visit_n) = instance.public_call(i, kind);
    let effect = WriteEvent {
        journey,
        location_id: call.location_id.to_string(),
        visit_n,
        kind,
        interval: merged,
        method: EventMethod::Source,
        source: observation.source.clone(),
    };
    (instance, vec![Effect::WriteEvent(effect)])
}

// --- delay and prediction --------------------------------------------------------------------

/// The time a source's delay is from: when it gave it, else when it was last heard.
fn delay_time(track: &SourceTrack) -> Instant {
    track.delay_at.unwrap_or(track.heard_at)
}

/// The freshest point delay while it is newer than the last fix of a source without point
/// delays (a GPS), else the measured lateness; else R1's rule (tracker, then the source's
/// delay).
pub fn current_lateness(instance: &Instance) -> Lateness {
    let mut point: Option<&SourceTrack> = None;
    for t in &instance.sources {
        if t.delay_reference == Some(DelayReference::Point) && t.delay_s.is_some() {
            // max(key=...): the first of equal keys stays.
            if point.is_none_or(|p| delay_time(t) > delay_time(p)) {
                point = Some(t);
            }
        }
    }
    let gps_at = instance
        .sources
        .iter()
        .filter(|t| t.delay_reference != Some(DelayReference::Point))
        .filter_map(|t| t.position_at)
        .max();
    let lateness_s = instance.track.as_ref().and_then(Track::lateness_s);
    let by_point = point.is_some_and(|p| gps_at.is_none_or(|g| delay_time(p) > g));
    if let Some(lateness_s) = lateness_s
        && !by_point
    {
        return (
            Some(TimeDelta::from_seconds(py_round_int(lateness_s))),
            Some(SourceClass::Gps),
        );
    }
    if let Some(delay_s) = point.and_then(|p| p.delay_s) {
        return (
            Some(TimeDelta::from_seconds(delay_s)),
            Some(SourceClass::Source),
        );
    }
    if let Some(reported) = instance.reported_delay_s {
        return (
            Some(TimeDelta::from_seconds(reported)),
            Some(SourceClass::Source),
        );
    }
    (None, None)
}

/// The lateness at the source's predicted next stop, by `predictor`; None for plain
/// propagation or without a usable source prediction.
pub fn anchor_prediction(
    instance: &Instance,
    trip: &Trip,
    index: &Index,
    predictor: Predictor,
) -> Option<(usize, TimeDelta)> {
    if predictor == Predictor::Propagate {
        return None;
    }
    let mut source: Option<&SourceTrack> = None;
    for t in instance.sources.iter().filter(|t| t.prediction.is_some()) {
        if source.is_none_or(|s| t.heard_at > s.heard_at) {
            source = Some(t);
        }
    }
    let source = source?;
    let prediction = source.prediction.as_ref()?;
    let (Some(scheduled), Some(predicted)) = (prediction.scheduled, prediction.predicted) else {
        return None;
    };
    let reached = instance.progress.map_or(-1, |p| p.call_index);
    let target =
        calls_at_station(trip, index, &[prediction.sr70.as_str()], reached + 1, true).next()?;
    let predicted_delay = predicted - scheduled;
    if predictor == Predictor::Sz {
        return Some((target, predicted_delay));
    }
    let (own, _) = current_lateness(instance);
    match (own, source.delay_s) {
        (Some(own), Some(delay_s)) => Some((
            target,
            own + (predicted_delay - TimeDelta::from_seconds(delay_s)),
        )),
        _ => Some((target, predicted_delay)),
    }
}
