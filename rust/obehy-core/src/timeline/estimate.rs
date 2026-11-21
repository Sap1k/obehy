//! Per-call public model: estimate, status and source class (docs/R1_SLICE.md section 1).
//! Port of `realtime/timeline/estimate.py`, operation for operation (its docstring has the
//! rules), plus the per-journey body of `core.estimate_all` and the three helpers it uses
//! (`_seen_leaving`, `_fresh`, `_behind`), which read only one instance.
//!
//! Python semantics kept: `td * float` and `timedelta(seconds=float)` round half to even
//! (`TimeDelta::mul_f64`, `from_secs_f64`; the early lead `lateness * early_keep` is a negative
//! duration), the publish deadband holds while `abs(old - value) < hold`, and truthiness of
//! instants and intervals is only `None` (`est_dep or est_arr`).

use crate::config::Policy;
use crate::config::policy::PredictionPolicy;
use crate::index::{Index, Trip};
use crate::model::{CallState, CallStatus, Instance, Interval, Lifecycle, SourceClass};
use crate::py::num::{py_max, py_min};
use crate::time::{Instant, TimeDelta};
use crate::timeline::fusion::{anchor_prediction, current_lateness};
use crate::timeline::plan::{Plan, Scheduled, no_timetabled_dwell};
use crate::timeline::runtimes::{Hops, minimum_drives, running_extra};
use crate::timeline::stops::departure;

/// The current lateness and where it comes from (`estimate.Lateness`).
pub type Lateness = (Option<TimeDelta>, Option<SourceClass>);

fn with_estimates(
    state: &CallState,
    arrival: Option<Instant>,
    departure: Option<Instant>,
    status: CallStatus,
    source_class: Option<SourceClass>,
) -> CallState {
    let mut call = state.clone();
    call.estimated_arrival = arrival;
    call.estimated_departure = departure;
    call.status = status;
    call.source_class = source_class;
    call
}

/// The instance with `calls`; the same calls are kept as they were (emitters reuse them).
fn with_calls(mut instance: Instance, calls: Vec<CallState>) -> Instance {
    if instance.calls[..] != calls[..] {
        instance.calls = calls.into();
    }
    instance
}

/// The current lateness, with early running measured while the vehicle stands at a stop taken
/// as zero: ahead of the timetable there it waits, it is not seen running early.
pub fn waiting_is_not_early(instance: &Instance, plan: &Plan, current: Lateness) -> Lateness {
    let (lateness, source_class) = current;
    let Some(lateness) = lateness else {
        return current;
    };
    if lateness >= TimeDelta::ZERO || source_class != Some(SourceClass::Gps) {
        return current;
    }
    let best = instance.track.as_ref().and_then(|t| t.hypotheses.first());
    match best {
        Some(best) if plan.at_stop(best.along_m) => (Some(TimeDelta::ZERO), source_class),
        _ => current,
    }
}

/// The passenger call without a timetabled dwell where the best reading's last fix on the
/// path lies, and when it was taken.
pub fn last_fix_at_stop(instance: &Instance, plan: &Plan) -> Option<(usize, Instant)> {
    let track = instance.track.as_ref()?;
    if track.hypotheses.is_empty() {
        return None;
    }
    if let Some(stop) = &track.at_stop {
        let call = usize::try_from(stop.call).expect("a call index");
        return instance.calls[call].passenger.then_some((call, stop.last));
    }
    let best = &track.hypotheses[0];
    if best.off_path_since.is_some() {
        return None;
    }
    let fix = best.history.last()?;
    let call = plan.dwelling_call(fix.along_m)?;
    instance.calls[call].passenger.then_some((call, fix.at))
}

/// The lateness carried on from a vehicle standing at a call without a timetabled dwell:
/// standing up to `credit` beyond its arrival (or beyond the timetable, arriving early) is the
/// dwell the minute holds.
fn standing(lateness: TimeDelta, arrived: TimeDelta, credit: TimeDelta) -> TimeDelta {
    let growth = lateness - arrived.max(TimeDelta::ZERO);
    lateness - growth.max(TimeDelta::ZERO).min(credit)
}

/// The current lateness less the dwell credited at the call last left, if it had no
/// timetabled dwell, fading out along the leg to the next call's arrival trigger.
pub fn dwell_credit(
    instance: &Instance,
    trip: &Trip,
    plan: &Plan,
    current: Lateness,
    policy: &PredictionPolicy,
) -> Lateness {
    let (lateness, source_class) = current;
    let Some(lateness) = lateness else {
        return current;
    };
    let Some(track) = &instance.track else {
        return current;
    };
    if source_class != Some(SourceClass::Gps) || track.hypotheses.is_empty() {
        return current;
    }
    let Some(progress) = instance.progress else {
        return current;
    };
    if !(0 <= progress.call_index && progress.call_index < instance.calls.len() as i64 - 1) {
        return current;
    }
    let i = progress.call_index as usize;
    let state = &instance.calls[i];
    if !state.passenger || !no_timetabled_dwell(plan.scheduled[i]) {
        return current;
    }
    let scheduled = plan.scheduled[i].1;
    let left = passed(state.passed_departure, state.departure);
    let (Some(scheduled), Some(left)) = (scheduled, left) else {
        return current;
    };
    let arrived = if let Some(arrived_at) = passed(state.passed_arrival, state.arrival) {
        arrived_at - scheduled
    } else if !trip.calls[i].on_request && i > 0 {
        // It certainly stopped: the lateness it came with, at the previous call's latest time.
        let before = &instance.calls[i - 1];
        let (arrival_before, departure_before) = plan.scheduled[i - 1];
        let left_before = passed(before.passed_departure, before.departure);
        let reached_before = passed(before.passed_arrival, before.arrival);
        if let (Some(left_before), Some(departure_before)) = (left_before, departure_before) {
            left_before - departure_before
        } else if let (Some(reached_before), Some(arrival_before)) =
            (reached_before, arrival_before)
        {
            reached_before - arrival_before
        } else {
            return current;
        }
    } else {
        return current; // a request stop not seen happening may have been passed
    };
    let credit = policy.dwell_credit_s.delta(&instance.mode);
    let credited = (left - scheduled) - standing(left - scheduled, arrived, credit);
    if credited <= TimeDelta::ZERO {
        return current;
    }
    let (Some(start), Some(end)) = (plan.zones[i].departure_m, plan.zones[i + 1].arrival_m) else {
        return current;
    };
    if end <= start {
        return current;
    }
    let share = py_min(
        1.0,
        py_max(0.0, (track.hypotheses[0].along_m - start) / (end - start)),
    );
    (Some(lateness - credited.mul_f64(1.0 - share)), source_class)
}

/// What `estimate` is given besides the instance: the current lateness, and the optional
/// anchor, running extras, floor at now, last fix at a stop, seen leaving and least drives
/// (Python's keyword arguments).
#[derive(Debug, Clone, Copy, Default)]
pub struct Inputs<'a> {
    pub current: Lateness,
    pub anchor: Option<(usize, TimeDelta)>,
    pub extra: Option<&'a [TimeDelta]>,
    pub now: Option<Instant>,
    pub dwelt: Option<(usize, Instant)>,
    pub left: Option<(usize, Instant)>,
    pub drives: Option<&'a [TimeDelta]>,
}

/// The time at `i` of an optional `(call, time)` pair.
fn at_call(pair: Option<(usize, Instant)>, i: usize) -> Option<Instant> {
    pair.filter(|(call, _)| *call == i).map(|(_, at)| at)
}

/// Per-call estimates (estimate.py `estimate`; its docstring has the rules). Panics where
/// `scheduled` does not have one entry per call (Python's `zip(strict=True)`).
pub fn estimate(
    instance: Instance,
    scheduled: &[Scheduled],
    policy: &PredictionPolicy,
    inputs: &Inputs<'_>,
) -> Instance {
    assert_eq!(
        instance.calls.len(),
        scheduled.len(),
        "zip() arguments differ in length"
    );
    let mode = instance.mode.as_str();
    let (mut lateness, delay_class) = inputs.current;
    if instance.lifecycle == Lifecycle::PreTrip
        && let Some(l) = lateness
        && l < TimeDelta::ZERO
    {
        lateness = Some(TimeDelta::ZERO); // not departed: an early departure is observed
    }
    let reached = instance.left_index();
    let latest_event = instance
        .calls
        .iter()
        .enumerate()
        .filter(|(_, c)| {
            c.arrival.is_some()
                || c.departure.is_some()
                || c.passed_arrival.is_some()
                || c.passed_departure.is_some()
        })
        .map(|(i, _)| i as i64)
        .max()
        .unwrap_or(-1);
    let mut floor: Option<Instant> = instance.progress.map(|p| p.at);
    if let Some(now) = inputs.now
        && floor.is_none_or(|f| now > f)
    {
        floor = Some(now); // a call not yet reached is not reached before now
    }
    let hold = policy.publish_deadband_s.delta(mode);
    let mut before = floor;
    let finished = instance.lifecycle == Lifecycle::Finished;
    let last = instance.calls.len() as i64 - 1;
    let mut calls: Vec<CallState> = Vec::with_capacity(instance.calls.len());
    let mut ahead = 0; // passenger calls predicted so far
    let road = inputs.drives.is_some();
    let mut credit = policy.dwell_credit_s.delta(mode);
    let mut leave: Option<Instant> = None; // the previous passenger call's departure
    for (i, (state, &(sched_arr, sched_dep))) in instance.calls.iter().zip(scheduled).enumerate() {
        let index = i as i64;
        let recorded = state.arrival.is_some() || state.departure.is_some();
        if recorded || state.passed_arrival.is_some() || state.passed_departure.is_some() {
            let est_arr = passed(state.passed_arrival, state.arrival);
            let mut est_dep = passed(state.passed_departure, state.departure);
            let status = if recorded {
                CallStatus::Actual
            } else {
                CallStatus::Inferred
            };
            // A call with an event after it was left, departure unseen: not a prediction.
            let waiting = index >= latest_event;
            let predicted_departure =
                est_dep.is_none() && index < last && waiting && !finished && lateness.is_some();
            let mut chain: Option<Instant> = None;
            if predicted_departure && let Some(mut l) = lateness {
                let leave_floor = latest(floor, est_arr);
                if state.passenger
                    && delay_class == Some(SourceClass::Gps)
                    && let (Some(arr), Some(s_arr)) = (est_arr, sched_arr)
                    && no_timetabled_dwell((sched_arr, sched_dep))
                {
                    credit = policy.dwell_credit_s.delta(mode);
                    l = standing(l, arr - s_arr, credit);
                }
                let (dep, carried) = departure_of(
                    est_arr,
                    sched_arr,
                    sched_dep,
                    l,
                    leave_floor,
                    mode,
                    policy,
                    state.passenger,
                );
                est_dep = dep;
                l = carried;
                if let (Some(seen), Some(dep)) = (at_call(inputs.dwelt, i), est_dep) {
                    est_dep = Some(dwelling_until(dep, est_arr, seen, mode, policy));
                }
                if let (Some(gone), Some(_)) = (at_call(inputs.left, i), est_dep) {
                    est_dep = Some(gone);
                }
                if inputs.drives.is_some() && state.passenger {
                    // Where the chain starts: when it leaves by the fixes, not by `now`.
                    chain = leaves_by_fixes(
                        est_arr,
                        sched_arr,
                        sched_dep,
                        l,
                        mode,
                        policy,
                        state.passenger,
                        at_call(inputs.dwelt, i),
                        at_call(inputs.left, i),
                    );
                }
                lateness = Some(l);
            }
            calls.push(with_estimates(
                state,
                est_arr,
                est_dep,
                status,
                Some(SourceClass::Gps),
            ));
            // Only a predicted departure starts the chain.
            leave = if state.passenger && predicted_departure {
                chain
            } else {
                None
            };
            floor = latest(floor, est_dep.or(est_arr));
        } else if index <= reached || finished {
            calls.push(with_estimates(
                state,
                None,
                None,
                CallStatus::NoRealtime,
                None,
            ));
        } else if let Some(mut l) = lateness {
            match inputs.anchor {
                Some((call, value)) if call == i => l = value,
                _ => {
                    if state.passenger && l < TimeDelta::ZERO && ahead > 0 {
                        // An early lead holds to the next call and fades beyond it.
                        l = l.mul_f64(policy.early_keep.get(mode));
                    }
                }
            }
            if let Some(extra) = inputs.extra
                && inputs.anchor.is_none_or(|(call, _)| call != i)
            {
                l = l + extra[i];
            }
            if state.passenger {
                ahead += 1;
            }
            // The chain's arrival: by the lateness, no sooner than the drive from the previous
            // departure allows; then floored like any prediction.
            let mut reached_at = shifted(sched_arr, l, None);
            if let (Some(drives), Some(leave)) = (inputs.drives, leave)
                && state.passenger
                && let Some((held_at, held_lateness)) =
                    held_back(reached_at, sched_arr, leave + drives[i])
            {
                reached_at = Some(held_at);
                l = held_lateness; // held back by the drive: later from here
            }
            let est_arr = shifted(reached_at, TimeDelta::ZERO, floor);
            if est_arr.is_some() {
                floor = est_arr;
            }
            let (mut est_dep, carried) = departure_of(
                est_arr,
                sched_arr,
                sched_dep,
                l,
                floor,
                mode,
                policy,
                state.passenger,
            );
            l = carried;
            if road
                && state.passenger
                && let (Some(arr), Some(dep)) = (est_arr, est_dep)
                && no_timetabled_dwell((sched_arr, sched_dep))
                && index < last
            {
                est_dep = Some(dep.max(arr + credit));
            }
            if est_dep.is_some() {
                floor = est_dep;
            }
            if state.passenger && inputs.drives.is_some() {
                leave = leaves_by_fixes(
                    reached_at,
                    sched_arr,
                    sched_dep,
                    l,
                    mode,
                    policy,
                    index < last,
                    None,
                    None,
                )
                .or(reached_at);
            }
            let (mut arr, mut dep) = (est_arr, est_dep);
            if state.status == CallStatus::Predicted
                && within(
                    (state.estimated_arrival, state.estimated_departure),
                    (est_arr, est_dep),
                    hold,
                    before,
                )
            {
                (arr, dep) = (state.estimated_arrival, state.estimated_departure);
            }
            before = dep.or(arr).or(before);
            calls.push(with_estimates(
                state,
                arr,
                dep,
                CallStatus::Predicted,
                delay_class,
            ));
            lateness = Some(l);
        } else {
            calls.push(with_estimates(
                state,
                None,
                None,
                CallStatus::Scheduled,
                None,
            ));
        }
    }
    with_calls(instance, calls)
}

/// The later of two times, either possibly unknown.
fn latest(a: Option<Instant>, b: Option<Instant>) -> Option<Instant> {
    match (a, b) {
        (None, b) => b,
        (Some(a), None) => Some(a),
        (Some(a), Some(b)) => Some(if a >= b { a } else { b }),
    }
}

/// The arrival no sooner than `reachable`, and the lateness that makes, if it holds back.
fn held_back(
    reached: Option<Instant>,
    scheduled: Option<Instant>,
    reachable: Instant,
) -> Option<(Instant, TimeDelta)> {
    let (reached, scheduled) = (reached?, scheduled?);
    if reachable <= reached {
        return None;
    }
    Some((reachable, reachable - scheduled))
}

/// When a road vehicle leaves a call by its fixes and the lateness alone, never by the floor
/// at `now`.
#[allow(clippy::too_many_arguments)]
fn leaves_by_fixes(
    arrival: Option<Instant>,
    sched_arr: Option<Instant>,
    sched_dep: Option<Instant>,
    lateness: TimeDelta,
    mode: &str,
    policy: &PredictionPolicy,
    passenger: bool,
    seen: Option<Instant>,
    left: Option<Instant>,
) -> Option<Instant> {
    if left.is_some() {
        return left;
    }
    let (leaves, _) = departure_of(
        arrival, sched_arr, sched_dep, lateness, None, mode, policy, passenger,
    );
    let mut leaves = leaves?;
    if passenger
        && let Some(arrival) = arrival
        && no_timetabled_dwell((sched_arr, sched_dep))
    {
        leaves = leaves.max(arrival + policy.dwell_credit_s.delta(mode));
    }
    Some(match seen {
        None => leaves,
        Some(seen) => leaves.max(seen),
    })
}

/// The final monotone repair (BASE_PLAN.md 20.6): emitted times never decrease along the trip.
pub fn monotone(instance: Instance) -> Instance {
    let mut latest: Option<Instant> = None;
    let mut calls: Vec<CallState> = Vec::with_capacity(instance.calls.len());
    for state in instance.calls.iter() {
        let (mut arrival, mut departure) = (state.estimated_arrival, state.estimated_departure);
        if let Some(a) = arrival {
            if let Some(l) = latest
                && a < l
            {
                arrival = Some(l);
            }
            latest = arrival;
        }
        if let Some(d) = departure {
            if let Some(l) = latest
                && d < l
            {
                departure = Some(l);
            }
            latest = departure;
        }
        let mut state = state.clone();
        state.estimated_arrival = arrival;
        state.estimated_departure = departure;
        calls.push(state);
    }
    with_calls(instance, calls)
}

/// The predicted departure and the lateness carried on from it (estimate.py `_departure`).
#[allow(clippy::too_many_arguments)]
fn departure_of(
    arrival: Option<Instant>,
    sched_arr: Option<Instant>,
    sched_dep: Option<Instant>,
    lateness: TimeDelta,
    floor: Option<Instant>,
    mode: &str,
    policy: &PredictionPolicy,
    passenger: bool,
) -> (Option<Instant>, TimeDelta) {
    let Some(sched_dep) = sched_dep else {
        return (None, lateness);
    };
    let mut lateness = lateness;
    if passenger {
        let early = -policy.max_early_departure_s.delta(mode);
        lateness = lateness.max(early);
    }
    let dwell = sched_arr.map_or(TimeDelta::ZERO, |a| sched_dep - a);
    let Some(arrival) = arrival else {
        return (shifted(Some(sched_dep), lateness, floor), lateness);
    };
    if lateness <= TimeDelta::ZERO || dwell <= TimeDelta::ZERO {
        return (shifted(Some(sched_dep), lateness, floor), lateness);
    }
    let long = dwell >= policy.long_dwell_s.delta(mode);
    let minimum = if long {
        policy.min_long_dwell_s.get(mode)
    } else {
        policy.min_dwell_s.get(mode)
    };
    let mut leaves = sched_dep.max(arrival + dwell.min(TimeDelta::from_secs_f64(minimum)));
    if let Some(floor) = floor
        && leaves < floor
    {
        leaves = floor;
    }
    (Some(leaves), leaves - sched_dep)
}

/// The predicted departure from a call without a timetabled dwell where the vehicle was last
/// seen at `seen`: no sooner, and no sooner than a typical dwell after it arrived.
fn dwelling_until(
    departure: Instant,
    arrival: Option<Instant>,
    seen: Instant,
    mode: &str,
    policy: &PredictionPolicy,
) -> Instant {
    let mut leaves = departure.max(seen);
    if let Some(arrival) = arrival {
        leaves = leaves.max(arrival + policy.dwell_credit_s.delta(mode));
    }
    leaves
}

/// Whether the published times `kept` may stay: each within `hold` of the new one, and not
/// before `before`.
fn within(
    kept: (Option<Instant>, Option<Instant>),
    new: (Option<Instant>, Option<Instant>),
    hold: TimeDelta,
    before: Option<Instant>,
) -> bool {
    if hold <= TimeDelta::ZERO {
        return false;
    }
    for (old, value) in [(kept.0, new.0), (kept.1, new.1)] {
        match (old, value) {
            (None, None) => {}
            (Some(old), Some(value)) => {
                if (old - value).abs() >= hold || before.is_some_and(|b| old < b) {
                    return false;
                }
            }
            _ => return false,
        }
    }
    true
}

fn passed(when: Option<Instant>, recorded: Option<Interval>) -> Option<Instant> {
    when.or_else(|| recorded.map(Interval::mid))
}

fn shifted(
    scheduled: Option<Instant>,
    delay: TimeDelta,
    floor: Option<Instant>,
) -> Option<Instant> {
    let value = scheduled? + delay;
    Some(match floor {
        Some(floor) if value < floor => floor,
        _ => value,
    })
}

// --- the per-journey body of core.estimate_all ---------------------------------------------

/// The call a road vehicle has been seen away from, its departure not committed yet, and when
/// it left (`core._seen_leaving`).
pub fn seen_leaving(
    instance: &Instance,
    plan: &Plan,
    policy: &PredictionPolicy,
) -> Option<(usize, Instant)> {
    let stop = instance.track.as_ref()?.at_stop.as_ref()?;
    stop.away?;
    let call = usize::try_from(stop.call).expect("a call index");
    Some((
        call,
        departure(plan, stop, policy.dwell_credit_s.get(&instance.mode)),
    ))
}

/// `now` while the journey's last fix is recent enough that a call not seen reached has not
/// been (`core._fresh`); None in a reception gap.
pub fn fresh(instance: &Instance, now: Instant, policy: &PredictionPolicy) -> Option<Instant> {
    let fresh = policy.floor_fresh_s.get(&instance.mode);
    let progress = instance.progress?;
    if fresh <= 0.0 || now - progress.at > TimeDelta::from_secs_f64(fresh) {
        return None;
    }
    Some(now)
}

/// Whether a running journey's first predicted time is before `now`, for a mode floored at
/// now (`core._behind`).
pub fn behind(instance: &Instance, now: Instant, policy: &PredictionPolicy) -> bool {
    if instance.lifecycle != Lifecycle::Running || policy.floor_fresh_s.get(&instance.mode) <= 0.0 {
        return false;
    }
    for call in instance.calls.iter() {
        if call.status == CallStatus::Predicted {
            let first = call.estimated_arrival.or(call.estimated_departure);
            return first.is_some_and(|f| f < now);
        }
    }
    false
}

/// One journey's estimates as `core.estimate_all` makes them, the running extras and least
/// drives from `hops`. The caller skips cancelled instances and marks nothing.
pub fn estimate_journey(
    instance: Instance,
    trip: &Trip,
    plan: &Plan,
    index: &Index,
    policy: &Policy,
    hops: &Hops,
    now: Instant,
) -> Instance {
    let prediction = &policy.prediction;
    let mode = instance.mode.as_str();
    let extra = running_extra(
        mode,
        &instance.calls,
        instance.progress.as_ref(),
        plan,
        hops,
        now,
        prediction,
    );
    let drives = minimum_drives(
        mode,
        &instance.calls,
        plan,
        hops,
        now,
        prediction,
        policy.progress.max_speed_mps.get(mode),
    );
    estimate_with(instance, trip, plan, index, policy, now, &extra, &drives)
}

/// [`estimate_journey`] with the running extras and least drives given.
#[allow(clippy::too_many_arguments)]
pub fn estimate_with(
    instance: Instance,
    trip: &Trip,
    plan: &Plan,
    index: &Index,
    policy: &Policy,
    now: Instant,
    extra: &[TimeDelta],
    drives: &[TimeDelta],
) -> Instance {
    let prediction = &policy.prediction;
    let current = waiting_is_not_early(&instance, plan, current_lateness(&instance));
    let current = dwell_credit(&instance, trip, plan, current, prediction);
    let mut instance = instance;
    instance.lateness_s = current
        .0
        .map(|l| crate::py::num::py_round_int(l.total_seconds()));
    let inputs = Inputs {
        current,
        anchor: anchor_prediction(&instance, trip, index, policy.rail.predictor),
        extra: Some(extra),
        now: fresh(&instance, now, prediction),
        dwelt: last_fix_at_stop(&instance, plan),
        left: seen_leaving(&instance, plan, prediction),
        drives: Some(drives),
    };
    monotone(estimate(instance, &plan.scheduled, prediction, &inputs))
}
