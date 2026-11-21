//! The timeline engine: progress, events and estimates of one journey instance. Port of
//! `realtime/timeline/__init__.py` (its docstring has the model): [`advance`] applies one bound
//! observation to its instance.
//!
//! GPS progress (map matching over the trip's path, [`progress`]) drives actual events,
//! committed once unambiguous, and the lateness the tracker measures drives predictions
//! ([`estimate`]); the source's own delay is the fallback. Several sources on one journey are
//! fused by [`fusion`].
//!
//! Float comparisons that decide (R4 hazard list): `observation.at <= seen` (repeated),
//! `sigma_m <= gps_sigma_m` (precise), the nearest projection `min(key=lateral_m)` (the first
//! segment wins ties), `along < best - sigma` (behind), `max(along, held)` on a shape and
//! `along < held - jitter` off it, `fresh` against whole seconds.

pub mod commit;
pub mod estimate;
pub mod fusion;
pub mod path;
pub mod plan;
pub mod progress;
pub mod runtimes;
pub mod stops;

use std::sync::Arc;

use crate::config::Policy;
use crate::config::policy::ProgressPolicy;
use crate::index::{Index, IndexMiss, Trip};
use crate::model::{
    CallState, Delay, Effect, EventKind, EventMethod, FLOORED_MINUTE_MIDDLE_S, Freshness, Instance,
    Interval, Lifecycle, Observation, PointEvent, Position, Published, SourceSemantics,
    SourceState, Unresolved, UnresolvedKind, WriteEvent,
};
use crate::py::num::py_max;
use crate::time::Instant;
use crate::timeline::commit::Crossing;
use crate::timeline::fusion::{
    apply_point, merge, note_source, position_leads, tracks_with, with_track,
};
use crate::timeline::path::{Path, Projection, heading_at, point_at, project_all};
use crate::timeline::plan::{Plan, PlanCache};
use crate::timeline::progress::{Step, update};

/// The progress policy with a source's position error, one per sigma (Python's
/// `_source_progress`, an `lru_cache` of 64).
///
/// Eviction: all entries when the base policy changes or a 65th sigma arrives (sigmas are per
/// source, a handful).
#[derive(Debug, Default)]
pub struct SourceProgress {
    base: Option<ProgressPolicy>,
    by_sigma: Vec<(u64, ProgressPolicy)>,
}

const SOURCE_PROGRESS_SIZE: usize = 64;

impl SourceProgress {
    pub fn new() -> SourceProgress {
        SourceProgress::default()
    }

    /// `replace(policy, gps_sigma_m=sigma_m)`, cached.
    pub fn get(&mut self, policy: &ProgressPolicy, sigma_m: f64) -> &ProgressPolicy {
        if self.base.as_ref() != Some(policy) {
            self.base = Some(policy.clone());
            self.by_sigma.clear();
        }
        let bits = sigma_m.to_bits();
        let found = self.by_sigma.iter().position(|(b, _)| *b == bits);
        let at = match found {
            Some(at) => at,
            None => {
                if self.by_sigma.len() >= SOURCE_PROGRESS_SIZE {
                    self.by_sigma.clear();
                }
                let mut with_sigma = policy.clone();
                with_sigma.gps_sigma_m = sigma_m;
                self.by_sigma.push((bits, with_sigma));
                self.by_sigma.len() - 1
            }
        };
        &self.by_sigma[at].1
    }
}

/// Apply one bound observation to its instance; `before` is the vehicle's previous fix (a stand
/// it waited on before the trip started, which the timeline never saw). Errors where Python
/// raises `IndexMiss` building the trip's plan.
#[allow(clippy::too_many_arguments)]
pub fn advance(
    instance: Instance,
    trip: &Trip,
    observation: &Arc<Observation>,
    index: &Index,
    policy: &Policy,
    plans: &mut PlanCache,
    progress_policies: &mut SourceProgress,
    semantics: SourceSemantics,
    before: Option<&Position>,
) -> Result<(Instance, Vec<Effect>), IndexMiss> {
    let usable = usable_delay(&instance, observation, policy, semantics);
    let (mut track, previous, news) = note_source(&instance, observation, semantics, usable);
    if !news {
        return Ok((instance, Vec::new())); // SZ-Q6: an unchanged entry says nothing new
    }
    let position = observation.first::<Position>();
    let seen = instance.track.as_ref().and_then(|t| t.seen_at);
    // A fix no newer than the last one used is no new information: it never moves progress
    // and does not keep the journey fresh.
    let repeated = position.is_some() && seen.is_some_and(|s| observation.at() <= s);
    let freshness = heard(&instance, observation, !repeated);
    let delay_s = usable.or(instance.reported_delay_s);
    let mut instance = instance;
    let mut effects: Vec<Effect> = Vec::new();
    if observation.first::<PointEvent>().is_some() && instance.lifecycle != Lifecycle::Finished {
        instance = with_track(instance, track.clone());
        instance.freshness = freshness;
        instance.reported_delay_s = delay_s;
        let plan = plans.plan(trip, instance.journey.service_date, index, policy)?;
        (instance, effects) = apply_point(
            instance,
            trip,
            &plan,
            observation,
            previous.as_ref(),
            index,
            policy,
        );
        if let Some(placed) = instance.source_track(&observation.source) {
            track = placed.clone();
        }
    }
    let Some(position) = position.filter(|_| instance.lifecycle == Lifecycle::Running && !repeated)
    else {
        instance = with_track(instance, track);
        instance.freshness = freshness;
        instance.reported_delay_s = delay_s;
        return Ok((instance, effects));
    };
    let source = observation.source.as_str();
    // Every vehicle is tracked alike; only how far a source's fixes may be off differs.
    let sigma_m = policy.rail.position_sigma_m.get(source);
    let progress_policy = progress_policies.get(&policy.progress, sigma_m);
    let plan = plans.plan(trip, instance.journey.service_date, index, policy)?;
    if is_behind(&instance, &plan, position, sigma_m)
        && !position_leads(&instance, source, observation.at(), policy)
    {
        // A lagging fix (the SŽ map's, a feed's late copy) while a better source is fresh.
        instance = with_track(instance, track);
        instance.freshness = freshness;
        instance.reported_delay_s = delay_s;
        effects.push(Effect::Unresolved(Unresolved {
            observation: observation.clone(),
            kind: UnresolvedKind::Behind,
            name: source.to_owned(),
        }));
        return Ok((instance, effects));
    }
    let mode: &str = &trip.mode;
    let dwell_s = policy.prediction.dwell_credit_s.get(mode);
    let precise = sigma_m <= policy.progress.gps_sigma_m;
    let radius_m = if precise {
        policy.lifecycle.departure_margin_m
    } else {
        0.0
    };
    let terminal_m = if precise {
        policy.lifecycle.terminal_radius_m
    } else {
        0.0
    };
    let step = update(
        &plan,
        instance.track.as_ref(),
        position,
        observation.at(),
        mode,
        source,
        progress_policy,
        dwell_s,
        radius_m,
        terminal_m,
        before,
    );
    track.position_at = Some(observation.at());
    let (calls, crossed) = cross(&instance, &step.crossed, source, policy);
    let mut progress = step.progress;
    let held = instance.progress;
    let anchored = instance.sources.iter().any(|t| t.point.is_some());
    if anchored
        && let Some(held) = held
        && progress.is_none_or(|p| p.distance_m < held.distance_m)
    {
        progress = Some(held); // a source's point anchors progress; fixes never take it back
    }
    if let Some(calls) = calls {
        instance.calls = calls.into();
    }
    instance.progress = progress;
    instance.off_route_since = step.off_route_since;
    instance.off_route = step.off_route;
    instance.track_source = Some(source.to_owned());
    instance.freshness = freshness;
    instance.reported_delay_s = delay_s;
    tracks_with(&mut instance.sources, track);
    instance.lifecycle = instance.settled_lifecycle(false);
    let shown_source = shown(&instance, source, observation.at(), policy);
    let mut published = instance.published;
    if source == shown_source || published.is_none() {
        published = Some(publish(
            &plan,
            &step,
            position,
            observation.at(),
            published,
            &policy.progress,
        ));
    }
    instance.shown_source = Some(shown_source);
    instance.published = published;
    instance.track = Some(step.track);
    effects.extend(crossed);
    Ok((instance, effects))
}

/// The projection nearest the point (`min(key=lateral_m)`: the first of equal ones).
fn nearest(path: &Path, lon: f64, lat: f64) -> Projection {
    let mut best: Option<Projection> = None;
    for p in project_all(path, lon, lat) {
        if best.is_none_or(|b| p.lateral_m < b.lateral_m) {
            best = Some(p);
        }
    }
    best.expect("a path of two points or more")
}

/// Whether the fix lies on the path behind the tracker's reading by more than its error.
fn is_behind(instance: &Instance, plan: &Plan, position: &Position, sigma_m: f64) -> bool {
    let best = instance.track.as_ref().and_then(|t| t.hypotheses.first());
    let Some(best) = best else {
        return false;
    };
    if plan.path.points.len() < 2 {
        return false;
    }
    nearest(&plan.path, position.lon, position.lat).along_m < best.along_m - sigma_m
}

/// The source whose vehicle stands for the journey: kept while its fixes stay fresh.
fn shown(instance: &Instance, source: &str, at: Instant, policy: &Policy) -> String {
    let held = instance
        .shown_source
        .as_deref()
        .and_then(|shown| instance.source_track(shown));
    let fresh = policy.rail.position_fresh_s as f64;
    match held {
        Some(held)
            if held
                .position_at
                .is_some_and(|p| (at - p).total_seconds() <= fresh) =>
        {
            held.source.clone()
        }
        _ => source.to_owned(),
    }
}

/// Where the journey is shown after a fix of the shown source (`_published`): on a real shape
/// the tracker's reading, never behind the last one shown; else the fix itself, unless it lies
/// further back than the jitter.
fn publish(
    plan: &Plan,
    step: &Step,
    position: &Position,
    at: Instant,
    before: Option<Published>,
    policy: &ProgressPolicy,
) -> Published {
    let path = &plan.path;
    let best = step.track.hypotheses.first();
    let held = before.and_then(|b| b.along_m);
    let on_shape = path.shaped && step.track.off_path.is_none();
    if let Some(best) = best
        && on_shape
        && best.off_path_since.is_none()
    {
        let mut along = best.along_m;
        if let Some(before) = before
            && before.snapped
            && let Some(held) = held
        {
            along = py_max(along, held);
        }
        let (lon, lat) = point_at(path, along);
        return Published {
            lon,
            lat,
            bearing: Some(heading_at(path, along)),
            at,
            along_m: Some(along),
            snapped: true,
        };
    }
    let along = (path.points.len() >= 2).then(|| nearest(path, position.lon, position.lat).along_m);
    if let (Some(before), Some(held), Some(along)) = (before, held, along)
        && along < held - policy.jitter_m
    {
        return before;
    }
    Published {
        lon: position.lon,
        lat: position.lat,
        bearing: position.bearing,
        at,
        along_m: along,
        snapped: false,
    }
}

/// The delay the observation reports, if usable; the one rule for every source's delay.
///
/// Not at or below `delay.discard_below_s`; and a source whose pre-trip delay is the time since
/// the scheduled departure (DUK-Q14) has it ignored before departure and while it still
/// reports a pre-trip state (DUK-Q18).
pub fn usable_delay(
    instance: &Instance,
    observation: &Observation,
    policy: &Policy,
    semantics: SourceSemantics,
) -> Option<i64> {
    let delay = observation.first::<Delay>()?;
    if delay.seconds <= policy.delay.discard_below_s {
        return None;
    }
    let source = observation.first::<SourceState>();
    let before =
        instance.lifecycle == Lifecycle::PreTrip || source.is_some_and(|s| s.code == "pre_trip");
    if semantics.pre_trip_delay_is_elapsed && before {
        return None;
    }
    if semantics.delay_floor_minute {
        // Whole minutes rounded down: the minute's middle.
        return Some(delay.seconds + FLOORED_MINUTE_MIDDLE_S);
    }
    Some(delay.seconds)
}

/// The observation's time; only new information keeps the journey fresh.
fn heard(instance: &Instance, observation: &Observation, fresh: bool) -> Freshness {
    let old = instance.freshness;
    Freshness {
        updated_at: observation.at(),
        heard_at: if fresh {
            observation.received_at
        } else {
            old.heard_at
        },
        stale: old.stale,
        lost: old.lost,
    }
}

/// Set the calls' passed times and recorded events; recorded ones go to history (`_cross`).
/// None: the calls are unchanged.
fn cross(
    instance: &Instance,
    crossed: &[Crossing],
    source: &str,
    policy: &Policy,
) -> (Option<Vec<CallState>>, Vec<Effect>) {
    if crossed.is_empty() {
        return (None, Vec::new());
    }
    let mut calls: Vec<CallState> = instance.calls.to_vec();
    let mut effects: Vec<Effect> = Vec::new();
    for crossing in crossed {
        let state = &mut calls[crossing.call];
        let arrival = crossing.kind == EventKind::Arrival;
        let held = if arrival {
            state.arrival
        } else {
            state.departure
        };
        let mut fixes = crossing.fixes;
        let progress = crossing.method == Some(EventMethod::Progress);
        let recorded: Option<Interval> = if progress {
            if held.is_some() {
                fixes = merge(held, fixes, false, policy);
            }
            Some(fixes)
        } else {
            // Inferred: history only where nothing better holds the call; realtime unchanged.
            if held.is_none() {
                crossing.interval
            } else {
                None
            }
        };
        if arrival {
            state.passed_arrival = Some(crossing.at);
            if progress {
                state.arrival = Some(fixes);
            }
        } else {
            state.passed_departure = Some(crossing.at);
            if progress {
                state.departure = Some(fixes);
            }
        }
        if let (Some(recorded), Some(method)) = (recorded, crossing.method) {
            let location_id = state.location_id.clone();
            let (journey, visit_n) = instance.public_call(crossing.call, crossing.kind);
            effects.push(Effect::WriteEvent(WriteEvent {
                journey,
                location_id,
                visit_n,
                kind: crossing.kind,
                interval: recorded,
                method,
                source: source.to_owned(),
            }));
        }
    }
    (Some(calls), effects)
}
