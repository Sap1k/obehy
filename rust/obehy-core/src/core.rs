//! `step`: one observation through the realtime core (BASE_PLAN.md sections 19-20): binding
//! continuity, DUK-Q4/Q15/Q18/Q19, PID-Q8, rail runs, cancellations, keyless sources.
//!
//! Pure: no database, network or files. The runner loads the index for an observation's keys
//! before calling [`step`] and executes the returned effects. Every read of static data that
//! was not loaded is an [`IndexMiss`], fatal in the runner.
//!
//! `FeedState.instances` and `.vehicles` are ordered by key, so `refresh` and its effects
//! visit journeys in key order.

use std::collections::{BTreeSet, HashMap, HashSet};
use std::sync::Arc;

use crate::config::Policy;
use crate::index::{Index, IndexMiss, Trip};
use crate::infer::keyed::{self, Found, Match, in_window, running, span};
use crate::infer::keyless;
use crate::infer::keyless::episode::advance as advance_episode;
use crate::model::{
    AssignVehicle, Binding, BindingMethod, CallState, CallStatus, Effect, EventKind, EventMethod,
    Feed, FeedState, Freshness, Instance, Interval, JourneyKey, KeylessEpisode, Lifecycle, LineRef,
    Observation, ObservationResult, PlatformAssignment, Position, Reason, RecordCancellation,
    ServiceDay, SourceDeparture, SourceSemantics, SourceState, TripCancelled, TripKey, VehicleId,
    VehicleKey, VehicleState, VehicleStatus, WriteEvent,
};
use crate::platforms::apply_platform;
use crate::runs::{journey_spans, part_spans, snapshots};
use crate::time::{Instant, TimeDelta, local_wall};
use crate::timeline::commit::terminus;
use crate::timeline::estimate::{behind, estimate_journey_memo};
use crate::timeline::fusion::with_lead;
use crate::timeline::plan::PlanCache;
use crate::timeline::progress::{at_origin_or_moving, past_first_stop};
use crate::timeline::runtimes::{RatioMemo, forget, record};
use crate::timeline::{SourceProgress, advance};

pub const CORE_VERSION: &str = "r1.0";

/// Normalized source states; connectors map their own codes onto these.
pub const SOURCE_PRE_TRIP: &str = "pre_trip"; // DUK-Q5: no progress or events until it departs
pub const SOURCE_RUNNING: &str = "running";

/// How the core reads each channel's facts, by (source, channel); a channel without an entry
/// takes its facts at face value.
pub type Semantics = HashMap<(String, String), SourceSemantics>;

/// What `step` cannot continue from: static data that was not loaded, or an observation of
/// another feed.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum CoreError {
    #[error(transparent)]
    IndexMiss(#[from] IndexMiss),
    #[error("observation for {0} applied to {1} state")]
    WrongFeed(Feed, Feed),
}

/// The core's context for one feed: its index, plans and the per-source progress policies,
/// the policy and the channels' semantics.
#[derive(Debug)]
pub struct Context<'a> {
    pub index: &'a mut Index,
    pub plans: &'a mut PlanCache,
    pub progress: &'a mut SourceProgress,
    pub policy: &'a Policy,
    pub semantics: &'a Semantics,
}

impl Context<'_> {
    pub fn semantics_of(&self, observation: &Observation) -> SourceSemantics {
        semantics_of(self.semantics, observation)
    }
}

fn semantics_of(semantics: &Semantics, observation: &Observation) -> SourceSemantics {
    semantics
        .iter()
        .find(|((source, channel), _)| {
            *source == observation.source && *channel == observation.channel
        })
        .map(|(_, s)| *s)
        .unwrap_or_default()
}

pub fn vehicle_of(observation: &Observation) -> Option<VehicleId> {
    observation.first::<VehicleKey>().map(|key| VehicleId {
        source: observation.source.clone(),
        source_vehicle_id: key.source_vehicle_id.clone(),
    })
}

fn observation_result(
    observation: &Arc<Observation>,
    journey: Option<JourneyKey>,
    reason: Option<Reason>,
    public: Vec<JourneyKey>,
    state_since: Option<Instant>,
    method: BindingMethod,
) -> Effect {
    Effect::ObservationResult(ObservationResult {
        observation: observation.clone(),
        journey,
        reason,
        public,
        state_since,
        method,
    })
}

fn status_of(reason: Reason) -> VehicleStatus {
    if reason == Reason::NotInService {
        VehicleStatus::NotInService
    } else {
        VehicleStatus::Unmatched
    }
}

/// Apply one observation to `state` (updated in place) and return its effects.
pub fn step(
    state: &mut FeedState,
    observation: &Arc<Observation>,
    ctx: &mut Context,
) -> Result<Vec<Effect>, CoreError> {
    if observation.feed != state.feed {
        return Err(CoreError::WrongFeed(observation.feed, state.feed));
    }
    let mut effects: Vec<Effect> = Vec::new();
    let vehicle = vehicle_of(observation);
    if vehicle.is_none() && observation.first::<PlatformAssignment>().is_some() {
        // Call evidence (a station board row): binds, never creates or moves a vehicle.
        return platform(state, observation, ctx);
    }
    if vehicle.is_none() && observation.first::<TripCancelled>().is_some() {
        return cancel(state, observation, ctx);
    }
    let previous: Option<VehicleState> = vehicle
        .as_ref()
        .and_then(|v| state.vehicles.get(v))
        .cloned();
    if let Some(previous) = &previous
        && observation.observed_at.is_some()
        && Some(previous.last_seen) == observation.observed_at
        && previous.facts[..] == observation.facts[..]
    {
        // The same fix again (a payload that did not change, a vehicle listed twice): stepping
        // it would change nothing, and it is not stored again.
        return Ok(effects);
    }
    let key = observation.first::<TripKey>();
    let line = observation.first::<LineRef>();
    let mut episode: Option<KeylessEpisode> = None;
    if key.is_none() && line.is_some() && vehicle.is_some() {
        let prior = previous.as_ref().and_then(|p| p.episode.as_ref());
        let semantics = ctx.semantics_of(observation);
        episode = advance_episode(prior, observation, &semantics, &ctx.policy.keyless);
    }
    let vehicle = match vehicle {
        Some(vehicle) if key.is_some() || episode.is_some() => vehicle,
        vehicle => {
            let since = vehicle.map(|vehicle| {
                set_vehicle(
                    state,
                    observation,
                    &vehicle,
                    VehicleStatus::Unmatched,
                    None,
                    None,
                    Some(Reason::NoKey),
                    None,
                )
            });
            effects.push(observation_result(
                observation,
                None,
                Some(Reason::NoKey),
                Vec::new(),
                since,
                BindingMethod::Keyed,
            ));
            return Ok(effects);
        }
    };

    let binding: Option<Binding>;
    if let Some(key) = key {
        binding = continued(previous.as_ref(), key, observation, state, ctx)?;
    } else {
        let line = line.expect("a keyless observation has a line");
        let episode = episode
            .as_ref()
            .expect("a keyless observation has an episode");
        let mut found = keyless::continued(
            previous.as_ref(),
            episode,
            observation.at(),
            &state.instances,
            ctx.index,
            ctx.policy,
        )?;
        if found.is_none() {
            let decision =
                keyless::bind(line, episode, observation, ctx.index, ctx.policy, ctx.plans)?;
            let matched = match decision.result {
                Found::Match(matched) => matched,
                Found::Reason(reason) => {
                    let since = set_vehicle(
                        state,
                        observation,
                        &vehicle,
                        status_of(reason),
                        None,
                        None,
                        Some(reason),
                        Some(episode.clone()),
                    );
                    effects.push(observation_result(
                        observation,
                        None,
                        Some(reason),
                        Vec::new(),
                        Some(since),
                        BindingMethod::Keyed,
                    ));
                    return Ok(effects);
                }
            };
            found = Some(Binding {
                vehicle: vehicle.clone(),
                journey: matched.journey.clone(),
                trip_id: matched.trip.trip_id.to_string(),
                method: BindingMethod::Inferred,
                bound_at: observation.at(),
                confirmed: false,
            });
            effects.extend(open(state, &matched, ctx.index, observation)?);
            effects.extend(assignments(
                state,
                &vehicle,
                &matched.journey,
                observation,
                BindingMethod::Inferred,
            ));
        }
        let made = found.expect("bound by now");
        binding = Some(keyless::confirm(&made, episode, ctx.index, ctx.policy)?);
    }
    let binding = match binding {
        Some(binding) => binding,
        None => {
            let key = key.expect("only a keyed observation is bound here");
            let mut result = bind(observation, ctx, None)?;
            if let Found::Match(matched) = &result
                && stale(matched, observation, ctx)
            {
                drop_unstarted(state, &matched.journey, &vehicle);
                result = Found::Reason(Reason::StaleKey);
            }
            let matched = match result {
                Found::Match(matched) => matched,
                Found::Reason(reason) => {
                    let since = set_vehicle(
                        state,
                        observation,
                        &vehicle,
                        status_of(reason),
                        Some(key.clone()),
                        None,
                        Some(reason),
                        None,
                    );
                    effects.push(observation_result(
                        observation,
                        None,
                        Some(reason),
                        Vec::new(),
                        Some(since),
                        BindingMethod::Keyed,
                    ));
                    return Ok(effects);
                }
            };
            effects.extend(open(state, &matched, ctx.index, observation)?);
            effects.extend(assignments(
                state,
                &vehicle,
                &matched.journey,
                observation,
                BindingMethod::Keyed,
            ));
            Binding {
                vehicle: vehicle.clone(),
                journey: matched.journey,
                trip_id: matched.trip.trip_id.to_string(),
                method: BindingMethod::Keyed,
                bound_at: observation.at(),
                confirmed: true,
            }
        }
    };

    let journey = binding.journey.clone();
    let trip = ctx.index.trip(&binding.trip_id)?.clone();
    let current = &state.instances[&journey];
    if leads(current, &vehicle, observation, ctx.policy) {
        // Moved out and back in place (`insert` below keeps its position).
        let slot = state
            .instances
            .get_mut(&journey)
            .expect("the bound instance");
        let held = placeholder(slot);
        let mut instance = with_lead(std::mem::replace(slot, held), &vehicle, observation);
        let before = previous.as_ref().and_then(|p| p.position.as_ref());
        let cancelled = instance.lifecycle == Lifecycle::Cancelled;
        instance = lifecycle(instance, &trip, observation, &journey, before, ctx)?;
        if cancelled && instance.lifecycle != Lifecycle::Cancelled {
            // seen running after all
            instance.calls = call_states(&trip).into();
            effects.extend(instance.public().into_iter().map(|j| {
                Effect::RecordCancellation(RecordCancellation {
                    journey: j,
                    source: observation.source.clone(),
                    at: observation.at(),
                    cancelled: false,
                })
            }));
        }
        if instance.lifecycle == Lifecycle::Cancelled {
            // not seen running: no progress
            state.instances.insert(journey.clone(), instance);
        } else {
            let calls_before = instance.calls.clone();
            let semantics = ctx.semantics_of(observation);
            let (advanced, mut timeline_effects) = advance(
                instance,
                &trip,
                observation,
                ctx.index,
                ctx.policy,
                ctx.plans,
                ctx.progress,
                semantics,
                before,
            )?;
            instance = advanced;
            if binding.confirmed
                && !Arc::ptr_eq(&instance.calls, &calls_before)
                && instance.calls[..] != calls_before[..]
            {
                let plan = ctx
                    .plans
                    .plan(&trip, journey.service_date, ctx.index, ctx.policy)?;
                record(
                    &mut state.hops,
                    &instance.mode,
                    &instance.calls,
                    &calls_before,
                    &plan.scheduled,
                    &ctx.policy.prediction,
                );
            }
            if !binding.confirmed {
                // history waits until continuity confirms the inference
                let mut events: Vec<WriteEvent> = Vec::new();
                timeline_effects.retain(|e| match e {
                    Effect::WriteEvent(event) => {
                        events.push(event.clone());
                        false
                    }
                    _ => true,
                });
                instance.held.extend(events);
            } else if !instance.held.is_empty() {
                let held = std::mem::take(&mut instance.held);
                timeline_effects = held
                    .into_iter()
                    .map(Effect::WriteEvent)
                    .chain(timeline_effects)
                    .collect();
            }
            state.instances.insert(journey.clone(), instance);
            state.dirty.insert(journey.clone());
            effects.extend(timeline_effects);
        }
    }
    let instance = &state.instances[&journey];
    let status = match instance.lifecycle {
        Lifecycle::Forecast | Lifecycle::PreTrip | Lifecycle::Cancelled => {
            VehicleStatus::Positioning
        }
        Lifecycle::Finished => VehicleStatus::Layover,
        Lifecycle::Running => VehicleStatus::Running,
    };
    let public = instance.public();
    let method = binding.method;
    let since = set_vehicle(
        state,
        observation,
        &vehicle,
        status,
        key.cloned(),
        Some(binding),
        None,
        episode,
    );
    effects.push(observation_result(
        observation,
        Some(journey),
        None,
        public,
        Some(since),
        method,
    ));
    Ok(effects)
}

/// The vehicle's assignment to each public journey of the instance just opened or joined.
fn assignments(
    state: &FeedState,
    vehicle: &VehicleId,
    journey: &JourneyKey,
    observation: &Observation,
    method: BindingMethod,
) -> Vec<Effect> {
    state.instances[journey]
        .public()
        .into_iter()
        .map(|j| {
            Effect::AssignVehicle(AssignVehicle {
                vehicle: vehicle.clone(),
                journey: j,
                at: observation.at(),
                method,
            })
        })
        .collect()
}

/// A station board row: `platforms.apply_platform` with the core's binder and opener. The
/// closures cannot return the index's error, so it is carried out beside them.
fn platform(
    state: &mut FeedState,
    observation: &Arc<Observation>,
    ctx: &mut Context,
) -> Result<Vec<Effect>, CoreError> {
    let scheduled = observation
        .first::<PlatformAssignment>()
        .expect("a platform assignment")
        .scheduled;
    let policy = ctx.policy;
    let mut missed: Option<IndexMiss> = None;
    let missed_bind = &mut missed;
    let mut missed_open: Option<IndexMiss> = None;
    let missed_opening = &mut missed_open;
    let effects = apply_platform(
        state,
        observation,
        ctx.index,
        |index, observation| match bind_with(observation, index, policy, Some(scheduled)) {
            Ok(Found::Match(matched)) => Ok(matched),
            Ok(Found::Reason(reason)) => Err(reason),
            Err(miss) => {
                *missed_bind = Some(miss);
                Err(Reason::NoKey)
            }
        },
        |state, index, matched| match open(state, matched, index, observation) {
            Ok(effects) => effects,
            Err(miss) => {
                *missed_opening = Some(miss);
                Vec::new()
            }
        },
    );
    if let Some(miss) = missed.or(missed_open) {
        return Err(miss.into());
    }
    Ok(effects)
}

/// A source cancelling the trip its keys name (see the module).
fn cancel(
    state: &mut FeedState,
    observation: &Arc<Observation>,
    ctx: &mut Context,
) -> Result<Vec<Effect>, CoreError> {
    let matched = match bind(observation, ctx, None)? {
        Found::Match(matched) => matched,
        Found::Reason(reason) => {
            return Ok(vec![observation_result(
                observation,
                None,
                Some(reason),
                Vec::new(),
                None,
                BindingMethod::Keyed,
            )]);
        }
    };
    let mut effects = open(state, &matched, ctx.index, observation)?;
    let instance = &state.instances[&matched.journey];
    let public = instance.public();
    if matches!(
        instance.lifecycle,
        Lifecycle::Forecast | Lifecycle::PreTrip | Lifecycle::Cancelled
    ) && public.len() == 1
    {
        let mut cancelled = instance.clone();
        cancelled.freshness.updated_at = observation.at();
        cancelled.freshness.heard_at = observation.received_at;
        cancelled.lifecycle = Lifecycle::Cancelled;
        cancelled.calls = instance
            .calls
            .iter()
            .map(|c| {
                let mut call = c.clone();
                call.estimated_arrival = None;
                call.estimated_departure = None;
                call.status = CallStatus::Cancelled;
                call.source_class = None;
                call
            })
            .collect();
        state.instances.insert(matched.journey.clone(), cancelled);
        state.dirty.insert(matched.journey.clone());
        effects.extend(public.iter().map(|j| {
            Effect::RecordCancellation(RecordCancellation {
                journey: j.clone(),
                source: observation.source.clone(),
                at: observation.at(),
                cancelled: true,
            })
        }));
    }
    // A journey bound by a cancellation is read back on a warm start like a station board's.
    let since = observation.received_at;
    effects.push(observation_result(
        observation,
        Some(matched.journey),
        None,
        public,
        Some(since),
        BindingMethod::Keyed,
    ));
    Ok(effects)
}

/// Bind by the first key; while that is ambiguous, by each further key in turn (SZ-Q5). `at`
/// is the time to bind at (a board row: its scheduled time), else the observation's.
fn bind(
    observation: &Observation,
    ctx: &mut Context,
    at: Option<Instant>,
) -> Result<Found, IndexMiss> {
    bind_with(observation, ctx.index, ctx.policy, at)
}

fn bind_with(
    observation: &Observation,
    index: &mut Index,
    policy: &Policy,
    at: Option<Instant>,
) -> Result<Found, IndexMiss> {
    let day = observation.first::<ServiceDay>().map(|d| d.day);
    let at = at.unwrap_or_else(|| observation.at());
    let mut result = Found::Reason(Reason::NoKey);
    for (n, key) in observation.all::<TripKey>().enumerate() {
        let found = keyed::bind(key, at, index, policy, day)?;
        if n == 0 || matches!(found, Found::Match(_)) {
            result = found;
        }
        if result != Found::Reason(Reason::Ambiguous) {
            break;
        }
    }
    let shared = &policy.journey.shared;
    let Found::Match(matched) = &result else {
        return Ok(result);
    };
    if shared.contains(&matched.journey.namespace) {
        return Ok(result);
    }
    for key in observation.all::<TripKey>() {
        if !shared.contains(&key.namespace) {
            continue;
        }
        if let Found::Match(found) = keyed::bind(key, at, index, policy, day)?
            && found.trip.trip_id == matched.trip.trip_id
            && found.journey.service_date == matched.journey.service_date
        {
            return Ok(Found::Match(Match {
                journey: found.journey,
                ..matched.clone()
            }));
        }
    }
    Ok(result)
}

/// Bring per-call estimates up to date as of `now` (before emitting state): of changed
/// journeys, and of journeys whose next predicted time has fallen behind `now`.
pub fn estimate_all(
    state: &mut FeedState,
    ctx: &mut Context,
    now: Instant,
) -> Result<(), IndexMiss> {
    estimate_all_with(state, ctx, now, 1)
}

/// Fewer journeys than this are estimated on the calling thread alone.
const PARALLEL_FROM: usize = 64;
/// Journeys a thread takes at a time.
const BLOCK: usize = 16;

/// One journey due for its estimate: the instance moved out of its entry (a placeholder holds
/// the entry meanwhile), and once looked up its trip
/// and plan, or why the trip is missing.
struct Due {
    journey: JourneyKey,
    day: crate::time::Date,
    instance: Option<Instance>,
    trip: Option<Arc<Trip>>,
    plan: Option<Arc<crate::timeline::plan::Plan>>,
    missing: Option<IndexMiss>,
}

/// [`estimate_all`] on up to `threads` threads (the caller's among them). A journey's estimate
/// reads only its own instance, its trip and plan, the index, the policy and the hops, none of
/// which an estimate changes, and every instance goes back to its own slot, so the result is
/// the same on any number of threads. Plans already cached are read on the threads; the rest
/// are built afterwards in journey order, and those journeys estimated then.
pub fn estimate_all_with(
    state: &mut FeedState,
    ctx: &mut Context,
    now: Instant,
    threads: usize,
) -> Result<(), IndexMiss> {
    let policy = &ctx.policy.prediction;
    let mut due: BTreeSet<JourneyKey> = std::mem::take(&mut state.dirty);
    due.extend(
        state
            .instances
            .iter()
            .filter(|(_, instance)| behind(instance, now, policy))
            .map(|(journey, _)| journey.clone()),
    );
    let FeedState {
        instances, hops, ..
    } = state;
    let mut jobs: Vec<Due> = Vec::with_capacity(due.len());
    let no_calls: Arc<[CallState]> = Arc::from(Vec::new());
    for journey in due {
        let Some(instance) = instances.get_mut(&journey) else {
            continue;
        };
        if instance.lifecycle == Lifecycle::Cancelled {
            continue;
        }
        let held = vacant(instance, &no_calls);
        jobs.push(Due {
            day: journey.service_date,
            journey,
            instance: Some(std::mem::replace(instance, held)),
            trip: None,
            plan: None,
            missing: None,
        });
    }
    let (index, plans, policy, hops) = (&*ctx.index, &*ctx.plans, ctx.policy, &*hops);
    // One estimate for every vehicle: buses, trams and trains alike (core.py).
    let estimate_block = |block: &mut [Due], memo: &mut RatioMemo| {
        for job in block {
            let instance = job.instance.as_ref().expect("not estimated yet");
            let trip = match index.trip(&instance.trip_id) {
                Ok(trip) => trip,
                Err(missing) => {
                    job.missing = Some(missing);
                    continue;
                }
            };
            job.trip = Some(trip.clone());
            let Some(plan) = plans.cached(trip, job.day, index) else {
                continue; // built afterwards
            };
            let instance = job.instance.take().expect("not estimated yet");
            job.instance = Some(estimate_journey_memo(
                instance, trip, &plan, index, policy, hops, now, memo,
            ));
            job.plan = Some(plan);
        }
    };
    let threads = threads.min(jobs.len().div_ceil(BLOCK));
    if threads <= 1 || jobs.len() < PARALLEL_FROM {
        estimate_block(&mut jobs, &mut RatioMemo::default());
    } else {
        let blocks = std::sync::Mutex::new(jobs.chunks_mut(BLOCK));
        let work = || {
            let mut memo = RatioMemo::default(); // the hops do not change here
            loop {
                let block = blocks.lock().expect("no worker panicked").next();
                let Some(block) = block else { break };
                estimate_block(block, &mut memo);
            }
        };
        std::thread::scope(|scope| {
            for _ in 1..threads {
                scope.spawn(work);
            }
            work();
        });
    }
    // In journey order: the first missing trip fails the estimate as it would have alone, and
    // plans not cached yet are built in the order they would have been.
    let mut memo = RatioMemo::default();
    for job in &mut jobs {
        if let Some(missing) = job.missing.take() {
            return Err(missing);
        }
        if job.plan.is_some() {
            continue;
        }
        let trip = job.trip.take().expect("the trip was found");
        let instance = job.instance.take().expect("not estimated yet");
        let plan = ctx.plans.plan(&trip, job.day, ctx.index, ctx.policy)?;
        job.instance = Some(estimate_journey_memo(
            instance, &trip, &plan, ctx.index, ctx.policy, hops, now, &mut memo,
        ));
    }
    for job in jobs {
        *instances.get_mut(&job.journey).expect("a due journey") = job.instance.expect("estimated");
    }
    state.dirty.clear();
    Ok(())
}

/// An instance holding a slot while its own is estimated: nothing reads it, and nothing is
/// allocated for it.
fn vacant(like: &Instance, no_calls: &Arc<[CallState]>) -> Instance {
    Instance {
        journey: JourneyKey {
            feed: like.journey.feed,
            namespace: String::new(),
            key: String::new(),
            service_date: like.journey.service_date,
        },
        release_id: String::new(),
        trip_id: String::new(),
        mode: String::new(),
        lifecycle: like.lifecycle,
        calls: Arc::clone(no_calls),
        freshness: like.freshness,
        progress: None,
        track: None,
        reported_delay_s: None,
        lateness_s: None,
        off_route_since: None,
        off_route: false,
        lead: None,
        journeys: Vec::new(),
        parts: Vec::new(),
        sources: Vec::new(),
        track_source: None,
        shown_source: None,
        published: None,
        held: Vec::new(),
    }
}

/// A cheap instance to hold a slot while its own is stepped.
fn placeholder(like: &Instance) -> Instance {
    Instance {
        journey: like.journey.clone(),
        release_id: String::new(),
        trip_id: String::new(),
        mode: String::new(),
        lifecycle: like.lifecycle,
        calls: like.calls.clone(),
        freshness: like.freshness,
        progress: None,
        track: None,
        reported_delay_s: None,
        lateness_s: None,
        off_route_since: None,
        off_route: false,
        lead: None,
        journeys: Vec::new(),
        parts: Vec::new(),
        sources: Vec::new(),
        track_source: None,
        shown_source: None,
        published: None,
        held: Vec::new(),
    }
}

/// Emit-tick housekeeping: flag stale and lost journeys, forget long-unseen vehicles and
/// journeys. A journey going stale just short of its terminus is taken to have arrived there
/// (`arrived_unseen`); its inferred arrival is the history effect returned.
pub fn refresh(
    state: &mut FeedState,
    now: Instant,
    ctx: &mut Context,
) -> Result<Vec<Effect>, IndexMiss> {
    let policy = ctx.policy;
    let mut effects: Vec<Effect> = Vec::new();
    forget(&mut state.hops, now, &policy.prediction);
    // Each entry is decided on its own, so iteration order cannot change the result.
    let unseen = TimeDelta::from_seconds(policy.lifecycle.forget_after_s);
    state
        .vehicles
        .retain(|_, current| now - current.last_seen <= unseen);
    let mut bound: HashSet<&JourneyKey> = HashSet::new();
    let mut unconfirmed: HashSet<JourneyKey> = HashSet::new();
    for binding in state.vehicles.values().filter_map(|v| v.binding.as_ref()) {
        bound.insert(&binding.journey);
        if !binding.confirmed {
            unconfirmed.insert(binding.journey.clone());
        }
    }
    let FeedState {
        instances, dirty, ..
    } = state;
    instances.retain(|journey, instance| {
        !(now - instance.freshness.heard_at > unseen && !bound.contains(journey))
    });
    drop(bound);
    let mut limits: HashMap<String, (TimeDelta, TimeDelta)> = HashMap::new();
    for (journey, instance) in instances.iter_mut() {
        let idle = now - instance.freshness.heard_at;
        let (stale_after, lost_after) = *limits.entry(instance.mode.clone()).or_insert_with(|| {
            (
                TimeDelta::from_secs_f64(policy.lifecycle.stale_after_s.get(&instance.mode)),
                TimeDelta::from_secs_f64(
                    policy.lifecycle.predict_without_data_s.get(&instance.mode),
                ),
            )
        });
        let live = !matches!(
            instance.lifecycle,
            Lifecycle::Finished | Lifecycle::Cancelled
        );
        let (stale, lost) = (live && idle > stale_after, live && idle > lost_after);
        if (stale, lost) == (instance.freshness.stale, instance.freshness.lost) {
            continue;
        }
        instance.freshness.stale = stale;
        instance.freshness.lost = lost;
        if stale
            && !unconfirmed.contains(journey)
            && instance.held.is_empty()
            && let Some((arrived, event)) = arrived_unseen(instance, ctx)?
        {
            *instance = arrived;
            effects.push(Effect::WriteEvent(event));
            dirty.insert(journey.clone());
        }
    }
    Ok(effects)
}

/// A running journey gone quiet at or just short of its terminus (its vehicle lost, or on to
/// its next trip without the arrival seen): finished, its arrival the start of its final
/// standstill there if it was seen standing at the terminus (`timeline.stops`), else inferred
/// from the lateness at the last fixes (`timeline.commit.terminus`).
fn arrived_unseen(
    instance: &Instance,
    ctx: &mut Context,
) -> Result<Option<(Instance, WriteEvent)>, IndexMiss> {
    let last = instance.calls.len() - 1;
    let call = &instance.calls[last];
    if instance.lifecycle != Lifecycle::Running
        || call.arrival.is_some()
        || call.passed_arrival.is_some()
    {
        return Ok(None);
    }
    let Some(track) = &instance.track else {
        return Ok(None);
    };
    let interval = match &track.at_stop {
        // Seen standing at the terminus (its stand may lie off the path): arrived when the
        // final standstill began.
        Some(stop) if stop.call == last as i64 => Some(Interval::new(stop.first, stop.first)),
        _ if !track.hypotheses.is_empty() => {
            let trip = ctx.index.trip(&instance.trip_id)?.clone();
            let plan =
                ctx.plans
                    .plan(&trip, instance.journey.service_date, ctx.index, ctx.policy)?;
            terminus(&plan, &track.hypotheses[0].history, &ctx.policy.progress)
        }
        _ => None,
    };
    let Some(interval) = interval else {
        return Ok(None);
    };
    let at = interval.mid();
    let (journey, visit_n) = instance.public_call(last, EventKind::Arrival);
    let event = WriteEvent {
        journey,
        location_id: call.location_id.clone(),
        visit_n,
        kind: EventKind::Arrival,
        interval,
        method: EventMethod::Inferred,
        source: instance.track_source.clone().unwrap_or_default(),
    };
    let mut arrived = instance.clone();
    let mut calls = arrived.calls.to_vec();
    calls[last].passed_arrival = Some(at);
    arrived.calls = calls.into();
    arrived.lifecycle = arrived.settled_lifecycle(false);
    Ok(Some((arrived, event)))
}

/// Whether this vehicle drives its source's contribution to the journey: it is that source's
/// lead, there is none, or the lead has not been seen for `rail.position_fresh_s` (DUK-Q11;
/// different sources are fused, `timeline.fusion`).
fn leads(
    instance: &Instance,
    vehicle: &VehicleId,
    observation: &Observation,
    policy: &Policy,
) -> bool {
    match instance.lead_for(&vehicle.source) {
        None => true,
        Some(lead) if lead.vehicle == *vehicle => true,
        Some(lead) => {
            observation.at() - lead.seen > TimeDelta::from_seconds(policy.rail.position_fresh_s)
        }
    }
}

fn continued(
    previous: Option<&VehicleState>,
    key: &TripKey,
    observation: &Observation,
    state: &FeedState,
    ctx: &mut Context,
) -> Result<Option<Binding>, IndexMiss> {
    let Some(previous) = previous else {
        return Ok(None);
    };
    let Some(binding) = &previous.binding else {
        return Ok(None);
    };
    if previous.trip_key.as_ref() != Some(key) {
        return Ok(None);
    }
    if !state.instances.contains_key(&binding.journey) {
        return Ok(None);
    }
    // `admissible` and `_stale` read one match of the bound trip.
    let trip = ctx.index.trip(&binding.trip_id)?.clone();
    let matched = Match::of(binding.journey.clone(), trip)?;
    if !in_window(&matched, observation.at(), ctx.policy) {
        return Ok(None);
    }
    if stale(&matched, observation, ctx) {
        return Ok(None);
    }
    Ok(Some(binding.clone()))
}

/// A pre-departure report after the trip's scheduled end, from a source whose such keys are
/// left over (DUK-Q15).
fn stale(matched: &Match, observation: &Observation, ctx: &Context) -> bool {
    if !ctx.semantics_of(observation).pre_trip_after_end_is_stale {
        return false;
    }
    observation
        .first::<SourceState>()
        .is_some_and(|s| s.code == SOURCE_PRE_TRIP && observation.at() > matched.end)
}

/// Forget a journey that never started once its only vehicle's key turns out stale.
fn drop_unstarted(state: &mut FeedState, journey: &JourneyKey, vehicle: &VehicleId) {
    let Some(instance) = state.instances.get(journey) else {
        return;
    };
    if instance.progress.is_some() || instance.lifecycle == Lifecycle::Finished {
        return;
    }
    let others = state.vehicles.iter().any(|(v, current)| {
        v != vehicle
            && current
                .binding
                .as_ref()
                .is_some_and(|b| b.journey == *journey)
    });
    if !others {
        state.instances.remove(journey);
        state.dirty.remove(journey);
    }
}

/// Create the journey's instance on first binding, with its schedule snapshot.
pub fn open(
    state: &mut FeedState,
    matched: &Match,
    index: &Index,
    observation: &Observation,
) -> Result<Vec<Effect>, IndexMiss> {
    if state.instances.contains_key(&matched.journey) {
        return Ok(Vec::new()); // a second vehicle on the same journey (DUK-Q11)
    }
    let trip = &matched.trip;
    let spans = journey_spans(index.feed, trip, matched.journey.service_date);
    state.instances.insert(
        matched.journey.clone(),
        Instance {
            journey: matched.journey.clone(),
            release_id: index.release_id.clone(),
            trip_id: trip.trip_id.to_string(),
            mode: trip.mode.to_string(),
            lifecycle: Lifecycle::PreTrip,
            calls: call_states(trip).into(),
            freshness: Freshness {
                updated_at: observation.at(),
                heard_at: observation.received_at,
                stale: false,
                lost: false,
            },
            progress: None,
            track: None,
            reported_delay_s: None,
            lateness_s: None,
            off_route_since: None,
            off_route: false,
            lead: None,
            journeys: spans.clone(),
            parts: part_spans(trip),
            sources: Vec::new(),
            track_source: None,
            shown_source: None,
            published: None,
            held: Vec::new(),
        },
    );
    snapshots(&matched.journey, &spans, trip, index, observation.at())
}

fn call_states(trip: &Trip) -> Vec<CallState> {
    trip.calls
        .iter()
        .map(|c| {
            let mut call =
                CallState::new(i64::from(c.sequence), c.location_id.to_string(), c.visit_n);
            call.passenger = c.passenger_service;
            call
        })
        .collect()
}

/// The schedule snapshots (and journey links) of an instance's trip or run.
pub fn snapshot(
    journey: &JourneyKey,
    index: &Index,
    trip_id: &str,
    at: Instant,
) -> Result<Vec<Effect>, IndexMiss> {
    let trip = index.trip(trip_id)?;
    let spans = journey_spans(index.feed, trip, journey.service_date);
    snapshots(journey, &spans, trip, index, at)
}

/// `pre_trip` (or `cancelled`: seen running after all) becomes `running` when the source says
/// so, or, for a source without trip states, once the scheduled start has passed (core.py's
/// `_lifecycle`: DUK-Q5, Q18, Q19, PID-Q8).
fn lifecycle(
    instance: Instance,
    trip: &Trip,
    observation: &Observation,
    journey: &JourneyKey,
    previous: Option<&Position>,
    ctx: &mut Context,
) -> Result<Instance, IndexMiss> {
    if !matches!(
        instance.lifecycle,
        Lifecycle::PreTrip | Lifecycle::Cancelled
    ) {
        return Ok(instance);
    }
    // The scheduled start: Python reads it before the call; it is pure.
    let start = span(trip, journey)?.0;
    let started = match observation.first::<SourceState>() {
        None => observation.at() >= start,
        Some(source) => {
            let running = source.code == SOURCE_RUNNING
                && seen_starting(&instance, trip, observation, previous, ctx)?;
            running || missed_departure(&instance, trip, observation, start, ctx)?
        }
    };
    let mut instance = instance;
    if started {
        instance.lifecycle = Lifecycle::Running;
    }
    Ok(instance)
}

/// DUK-Q19, PID-Q8: a vehicle saying it runs has started only at the first stop or seen moving
/// along the trip's path past it.
fn seen_starting(
    instance: &Instance,
    trip: &Trip,
    observation: &Observation,
    previous: Option<&Position>,
    ctx: &mut Context,
) -> Result<bool, IndexMiss> {
    let Some(position) = observation.first::<Position>() else {
        return Ok(true); // nothing to judge by: the source's state stands
    };
    let plan = ctx
        .plans
        .plan(trip, instance.journey.service_date, ctx.index, ctx.policy)?;
    Ok(at_origin_or_moving(
        &plan,
        previous,
        position,
        observation.at(),
        &ctx.policy.progress,
        ctx.policy.lifecycle.terminal_radius_m,
    ))
}

/// DUK-Q18: after the scheduled start, the source plans the same departure on a later day and
/// the vehicle is on the trip's path past its first stop.
fn missed_departure(
    instance: &Instance,
    trip: &Trip,
    observation: &Observation,
    start: Instant,
    ctx: &mut Context,
) -> Result<bool, IndexMiss> {
    let (Some(planned), Some(position)) = (
        observation.first::<SourceDeparture>(),
        observation.first::<Position>(),
    ) else {
        return Ok(false);
    };
    if observation.at() < start {
        return Ok(false);
    }
    let (later, scheduled) = (local_wall(planned.at), local_wall(start));
    if later.date() <= scheduled.date() || later.micros_of_day() != scheduled.micros_of_day() {
        return Ok(false);
    }
    let plan = ctx
        .plans
        .plan(trip, instance.journey.service_date, ctx.index, ctx.policy)?;
    Ok(past_first_stop(&plan, position, &ctx.policy.progress))
}

/// Record the vehicle's state after `observation`; returns its `since`.
#[allow(clippy::too_many_arguments)]
fn set_vehicle(
    state: &mut FeedState,
    observation: &Observation,
    vehicle: &VehicleId,
    status: VehicleStatus,
    key: Option<TripKey>,
    binding: Option<Binding>,
    reason: Option<Reason>,
    episode: Option<KeylessEpisode>,
) -> Instant {
    let previous = state.vehicles.get(vehicle);
    let (position, position_at) = fix(previous, observation.first::<Position>(), observation.at());
    let since = since(previous, observation, binding.as_ref(), episode.as_ref());
    let current = VehicleState {
        vehicle: vehicle.clone(),
        feed: state.feed,
        status,
        last_seen: observation.at(),
        trip_key: key,
        binding,
        position,
        position_at,
        reason,
        episode,
        facts: Arc::from(&observation.facts[..]),
        since: Some(since),
    };
    match state.vehicles.get_mut(vehicle) {
        Some(slot) => *slot = current,
        None => {
            state.vehicles.insert(vehicle.clone(), current);
        }
    }
    since
}

/// The vehicle's position and its fix time after a report at `at`: a newer fix replaces it;
/// none, one no newer (channels of one source out of step) or the same place again keep it, so
/// the map never steps back to an older fix.
fn fix(
    previous: Option<&VehicleState>,
    position: Option<&Position>,
    at: Instant,
) -> (Option<Position>, Option<Instant>) {
    let held = previous.and_then(|p| match (&p.position, p.position_at) {
        (Some(position), Some(position_at)) => Some((position, position_at)),
        _ => None,
    });
    let Some((held, held_at)) = held else {
        return (position.copied(), position.map(|_| at));
    };
    match position {
        None => (Some(*held), Some(held_at)),
        Some(_) if at <= held_at => (Some(*held), Some(held_at)),
        Some(position) if (position.lat, position.lon) == (held.lat, held.lon) => {
            (Some(*position), Some(held_at))
        }
        Some(position) => (Some(*position), Some(at)),
    }
}

/// Where the vehicle's state begins: a keyless vehicle's at its episode's start (inference
/// reads the whole episode), a bound one's at its binding; otherwise this observation.
fn since(
    previous: Option<&VehicleState>,
    observation: &Observation,
    binding: Option<&Binding>,
    episode: Option<&KeylessEpisode>,
) -> Instant {
    if let Some(previous) = previous
        && let Some(since) = previous.since
    {
        if let Some(episode) = episode {
            if previous
                .episode
                .as_ref()
                .is_some_and(|p| p.started_at == episode.started_at)
            {
                return since;
            }
        } else if let (Some(binding), Some(before)) = (binding, &previous.binding)
            && before.journey == binding.journey
            && before.bound_at == binding.bound_at
        {
            return since;
        }
    }
    observation.received_at
}

/// Move live journeys onto a newly activated release (BASE_PLAN.md section 29): each journey
/// re-resolved through its own key and service date and re-snapshotted; calls keep their
/// observed events by `(location_id, visit_n)`; progress restarts from the next fix. A journey
/// whose key no longer runs is dropped and its vehicles unbound. The caller has loaded every
/// live journey's key into `index`.
pub fn rebase(state: &mut FeedState, index: &mut Index) -> Result<Vec<Effect>, IndexMiss> {
    let mut effects: Vec<Effect> = Vec::new();
    let mut moved: HashMap<JourneyKey, String> = HashMap::new();
    let mut journeys: Vec<JourneyKey> = state.instances.keys().cloned().collect();
    journeys.sort();
    for journey in journeys {
        let Some(trip) = resolve(&journey, index)? else {
            state.instances.remove(&journey);
            continue;
        };
        let instance = state.instances[&journey].clone();
        let old: HashMap<(&str, u32), &CallState> = instance
            .calls
            .iter()
            .map(|c| ((c.location_id.as_str(), c.visit_n), c))
            .collect();
        let fresh = call_states(&trip);
        assert_eq!(trip.calls.len(), fresh.len(), "zip(strict=True)");
        let calls: Vec<CallState> = trip
            .calls
            .iter()
            .zip(&fresh)
            .map(|(c, fresh)| {
                let mut call = old
                    .get(&(&*c.location_id, c.visit_n))
                    .map_or_else(|| fresh.clone(), |&kept| kept.clone());
                call.sequence = i64::from(c.sequence);
                call.passenger = fresh.passenger;
                call
            })
            .collect();
        let spans = journey_spans(index.feed, &trip, journey.service_date);
        let updated_at = instance.freshness.updated_at;
        let rebased = Instance {
            release_id: index.release_id.clone(),
            trip_id: trip.trip_id.to_string(),
            calls: calls.into(),
            progress: None,
            track: None,
            journeys: spans.clone(),
            parts: part_spans(&trip),
            ..instance
        };
        state.instances.insert(journey.clone(), rebased);
        moved.insert(journey.clone(), trip.trip_id.to_string());
        state.dirty.insert(journey.clone());
        effects.extend(snapshots(&journey, &spans, &trip, index, updated_at)?);
    }
    let mut vehicles: Vec<VehicleId> = state.vehicles.keys().cloned().collect();
    vehicles.sort();
    for vehicle in vehicles {
        let current = state.vehicles.get_mut(&vehicle).expect("a listed vehicle");
        let Some(binding) = &mut current.binding else {
            continue;
        };
        if let Some(trip_id) = moved.get(&binding.journey) {
            binding.trip_id.clone_from(trip_id);
        } else {
            current.binding = None;
            current.status = VehicleStatus::Unmatched;
            current.reason = Some(Reason::NoTrip);
        }
    }
    Ok(effects)
}

/// The (run) trip a journey's key names on its service date, if it runs.
pub fn resolve(journey: &JourneyKey, index: &mut Index) -> Result<Option<Arc<Trip>>, IndexMiss> {
    let found = running(
        index,
        &journey.namespace,
        &journey.key,
        &[journey.service_date],
    )?;
    match found.into_iter().next() {
        Some((day, trip)) => Ok(Some(index.run(&trip, day)?)),
        None => Ok(None),
    }
}
