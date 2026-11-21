//! Platform assignments from station boards (docs/R2_SLICE.md section 6). Pure.
//!
//! A board row binds by train number and the date of its scheduled time like any keyed
//! observation, but carries no vehicle: it never creates or moves one. It attaches to the run's
//! call at the station (`sr70` key) whose scheduled time of that kind matches (exact instant
//! equality); a run without an instance gets one (`open_instance`). The value is the call's
//! shown platform; a newer assignment replaces an older one, compared by (value, label).

use std::sync::Arc;

use crate::index::{Index, Trip, calls_at_station};
use crate::infer::keyed::Match;
use crate::model::{
    BindingMethod, Effect, EventKind, FeedState, Observation, ObservationResult, Platform,
    PlatformAssignment, Reason, RecordPlatform, StopKind, Unresolved, UnresolvedKind,
};
use crate::time::{Date, ServiceTime};

fn call_of(trip: &Trip, day: Date, fact: &PlatformAssignment, index: &Index) -> Option<usize> {
    for i in calls_at_station(trip, index, &[fact.station.as_str()], 0, true) {
        let call = &trip.calls[i];
        let seconds = if fact.kind == StopKind::Arrival {
            call.arrival
        } else {
            call.departure
        };
        let Some(seconds) = seconds else {
            continue;
        };
        if ServiceTime::new(day, seconds).instant() == fact.scheduled {
            return Some(i);
        }
    }
    None
}

/// Nothing is mapped for now: boards number tracks for passengers, CZPTT by internal track
/// numbers, so an equal designation is often a different track (2026-10-10). Kept as the hook
/// for board-numbered platform stops (RUST_PORT.md R0 item 2).
fn boarding_point(_fact: &PlatformAssignment, _index: &Index) -> Option<String> {
    None
}

fn event_kind(kind: StopKind) -> EventKind {
    match kind {
        StopKind::Arrival => EventKind::Arrival,
        StopKind::Departure => EventKind::Departure,
    }
}

fn result(
    observation: &Arc<Observation>,
    journey: Option<crate::model::JourneyKey>,
    reason: Option<Reason>,
    public: Vec<crate::model::JourneyKey>,
) -> Effect {
    Effect::ObservationResult(ObservationResult {
        observation: observation.clone(),
        journey,
        reason,
        public,
        state_since: None,
        method: BindingMethod::Keyed,
    })
}

/// Apply a board row (an observation with a `PlatformAssignment` and no vehicle) to `state`.
///
/// `bind` binds it as `core._bind` does (it may build run trips in the index);
/// `open_instance` opens the matched journey's instance in `state` if it has none
/// (`core._open`) and returns its effects. Panics on an observation without a platform
/// assignment, or when `open_instance` leaves no instance.
pub fn apply_platform(
    state: &mut FeedState,
    observation: &Arc<Observation>,
    index: &mut Index,
    bind: impl FnOnce(&mut Index, &Observation) -> Result<Match, Reason>,
    open_instance: impl FnOnce(&mut FeedState, &mut Index, &Match) -> Vec<Effect>,
) -> Vec<Effect> {
    let fact = observation
        .first::<PlatformAssignment>()
        .expect("a platform assignment");
    let found = match bind(index, observation) {
        Ok(found) => found,
        Err(reason) => return vec![result(observation, None, Some(reason), Vec::new())],
    };
    let mut effects = open_instance(state, index, &found);
    let journey = found.journey.clone();
    let instance = state
        .instances
        .get(&journey)
        .expect("the matched journey's instance");
    let Some(i) = call_of(&found.trip, journey.service_date, fact, index) else {
        let name = format!(
            "{} {} {}",
            fact.station,
            fact.kind,
            fact.scheduled.isoformat()
        );
        let public = instance.public();
        effects.push(Effect::Unresolved(Unresolved {
            observation: observation.clone(),
            kind: UnresolvedKind::Platform,
            name,
        }));
        effects.push(result(observation, Some(journey), None, public));
        return effects;
    };
    let platform = Platform {
        value: fact.value.clone(),
        label: fact.label,
        boarding_point_id: boarding_point(fact, index),
        assigned_at: observation.received_at,
        source: observation.source.clone(),
    };
    let current = instance.calls[i].platform.as_ref();
    let changed = current.is_none_or(|c| (&c.value, c.label) != (&platform.value, platform.label));
    let kept = if changed {
        platform.clone()
    } else {
        current.expect("unchanged").clone()
    };
    let (public_journey, visit_n) = instance.public_call(i, event_kind(fact.kind));
    let public = instance.public();
    let mut updated = instance.clone();
    let mut calls = updated.calls.to_vec();
    calls[i].platform = Some(kept);
    let location_id = calls[i].location_id.clone();
    updated.calls = calls.into();
    updated.freshness.heard_at = observation.received_at;
    state.instances.insert(journey.clone(), updated);
    state.dirty.insert(journey.clone());
    effects.push(Effect::RecordPlatform(RecordPlatform {
        journey: public_journey,
        location_id,
        visit_n,
        kind: fact.kind,
        platform,
    }));
    effects.push(result(observation, Some(journey), None, public));
    effects
}
