//! Hop running times learned live (BASE_PLAN.md section 20.5). Port of
//! `realtime/timeline/runtimes.py`.
//!
//! A hop is two consecutive passenger calls, keyed by mode and the two locations, so every line
//! over the same stops shares it. Each journey that records an arrival at its end adds a
//! traversal: the time from its recorded arrival at the first call (its departure at the
//! origin, which has no arrival) against the timetable's time for the same span. The running
//! time of a hop now is the timetable's times a ratio: the traversals' ratios weighted by
//! recency (decaying by e every `runtime_decay_s`), shrunk toward the timetable's own (1) with
//! `runtime_prior_weight`.
//!
//! Each hop's traversals are a deque in append order. Ends are not sorted (a late recorder can
//! append an older end), so it is trimmed from the front only while the front is out of the
//! window, and `forget` looks at the last appended, not the latest end, exactly as Python does.
//! Eviction: traversals leave on append (front older than the window), hops leave in `forget`
//! (empty, or the last appended older than the window).
//!
//! The functions take the journey's parts (mode, calls, progress) rather than an `Instance`,
//! and the plan through [`HopPlan`], so they stand without `plan.rs`.

use std::collections::VecDeque;

use indexmap::IndexMap;

use crate::config::policy::PredictionPolicy;
use crate::model::{CallState, HopKey, Progress, Traversal};
use crate::py::num::{py_max, py_min};
use crate::time::{Instant, TimeDelta};

/// `FeedState.hops`: each hop's recent traversals, oldest appended first; hops in insertion
/// order.
pub type Hops = IndexMap<HopKey, VecDeque<Traversal>>;

/// Per call: (scheduled arrival, scheduled departure) (`plan.Scheduled`).
pub type Scheduled = (Option<Instant>, Option<Instant>);

/// What the estimates read of a plan (`Plan.scheduled`, `Plan.zones`,
/// `Plan.path.call_distances_m`).
pub trait HopPlan {
    fn scheduled(&self, call: usize) -> Scheduled;
    /// `plan.zones[call].arrival_m`
    fn arrival_m(&self, call: usize) -> Option<f64>;
    /// `plan.zones[call].departure_m`
    fn departure_m(&self, call: usize) -> Option<f64>;
    /// `plan.path.call_distances_m[call]`
    fn call_distance_m(&self, call: usize) -> f64;
}

fn previous_passenger(calls: &[CallState], i: usize) -> Option<usize> {
    (0..i).rev().find(|&k| calls[k].passenger)
}

fn key(mode: &str, calls: &[CallState], a: usize, b: usize) -> HopKey {
    HopKey {
        mode: mode.to_owned(),
        from: calls[a].location_id.clone(),
        to: calls[b].location_id.clone(),
        from_departure: a == 0,
    }
}

/// `timedelta(seconds=runtime_keep_decays * runtime_decay_s)` (an int).
fn window(policy: &PredictionPolicy) -> TimeDelta {
    TimeDelta::from_seconds(policy.runtime_keep_decays * policy.runtime_decay_s)
}

/// Add the hops a journey (`mode`, its `calls` now) completed since its calls were `before`:
/// each passenger call with a newly recorded arrival, timed from the previous passenger call's
/// recorded event. Panics if `before` and `calls` differ in length (`zip(strict=True)`).
pub fn record(
    hops: &mut Hops,
    mode: &str,
    calls: &[CallState],
    before: &[CallState],
    scheduled: &[Scheduled],
    policy: &PredictionPolicy,
) {
    let window = window(policy);
    assert_eq!(before.len(), calls.len(), "zip(strict=True)");
    for (b, (old, new)) in before.iter().zip(calls).enumerate() {
        let Some(arrival) = new.arrival else {
            continue;
        };
        if !new.passenger || old.arrival.is_some() {
            continue;
        }
        let Some(a) = previous_passenger(calls, b) else {
            continue;
        };
        let start = if a == 0 {
            calls[a].departure
        } else {
            calls[a].arrival
        };
        let planned_start = if a == 0 {
            scheduled[a].1
        } else {
            scheduled[a].0.or(scheduled[a].1)
        };
        let planned_end = scheduled[b].0.or(scheduled[b].1);
        let (Some(start), Some(planned_start), Some(planned_end)) =
            (start, planned_start, planned_end)
        else {
            continue;
        };
        let end = arrival.mid();
        let taken = (end - start.mid()).total_seconds();
        let planned = (planned_end - planned_start).total_seconds();
        if taken <= 0.0 || planned <= 0.0 {
            continue;
        }
        let traversals = hops.entry(key(mode, calls, a, b)).or_default();
        traversals.push_back(Traversal {
            end,
            taken_s: taken,
            scheduled_s: planned,
        });
        while traversals.front().is_some_and(|t| t.end < end - window) {
            traversals.pop_front();
        }
    }
}

/// The hop's running time against the timetable now: the traversals' ratios weighted by
/// recency (`runtime_decay_s`), with the timetable's (1) weighing `runtime_prior_weight`; None
/// for a hop no one has driven lately. Panics where Python divides by zero (no prior weight and
/// no traversal in the window).
pub fn ratio(hops: &Hops, key: &HopKey, now: Instant, policy: &PredictionPolicy) -> Option<f64> {
    let traversals = hops.get(key).filter(|t| !t.is_empty())?;
    let decay = policy.runtime_decay_s;
    let most = policy.runtime_ratio_max;
    // Python compares the float age with this int exactly; it is far below 2^53.
    let cutoff = (policy.runtime_keep_decays * decay) as f64;
    let (mut total, mut weight) = (policy.runtime_prior_weight, policy.runtime_prior_weight);
    for traversal in traversals {
        let age = (now - traversal.end).total_seconds();
        if age < 0.0 || age > cutoff {
            continue;
        }
        let w = (-age / decay as f64).exp();
        total += w * py_min(
            most,
            py_max(1.0 / most, traversal.taken_s / traversal.scheduled_s),
        );
        weight += w;
    }
    assert!(weight != 0.0, "float division by zero");
    Some(total / weight)
}

/// Drop hops with no traversal in the window (insertion order of the rest kept).
pub fn forget(hops: &mut Hops, now: Instant, policy: &PredictionPolicy) {
    let since = now - window(policy);
    hops.retain(|_, v| v.back().is_some_and(|t| !(t.end < since)));
}

/// Per call, the time the hop into it takes now beyond the timetable's running time (from the
/// previous passenger call's scheduled departure); on the hop the vehicle is driving, only for
/// the share of it left. Zero for calls behind progress and where there is no ratio.
pub fn running_extra(
    mode: &str,
    calls: &[CallState],
    progress: Option<&Progress>,
    plan: &impl HopPlan,
    hops: &Hops,
    now: Instant,
    policy: &PredictionPolicy,
) -> Vec<TimeDelta> {
    let mut extra = vec![TimeDelta::ZERO; calls.len()];
    let reached = progress.map_or(-1, |p| p.call_index);
    let first = usize::try_from(reached.max(0) + 1).expect("non-negative");
    for b in first..calls.len() {
        if !calls[b].passenger {
            continue;
        }
        let Some(a) = previous_passenger(calls, b) else {
            continue;
        };
        let r = ratio(hops, &key(mode, calls, a, b), now, policy);
        let start = plan.scheduled(a).1.or(plan.scheduled(a).0);
        let end = plan.scheduled(b).0.or(plan.scheduled(b).1);
        let (Some(r), Some(start), Some(end)) = (r, start, end) else {
            continue;
        };
        if end <= start {
            continue;
        }
        let mut share = 1.0;
        if let Some(progress) = progress
            && (a as i64) <= reached
        {
            // Driving the hop: the share of it left.
            let (left, reach) = (plan.departure_m(a), plan.arrival_m(b));
            if let (Some(left), Some(reach)) = (left, reach)
                && reach > left
            {
                share = py_min(
                    1.0,
                    py_max(0.0, (reach - progress.distance_m) / (reach - left)),
                );
            }
        }
        extra[b] = (end - start).mul_f64((r - 1.0) * share);
    }
    extra
}

/// Per passenger call, the least time from the previous passenger call's departure to its
/// arrival: the hop's running time now (timetable times the live ratio) less the typical dwell
/// the timetable's minute holds (`dwell_credit_s`), and never less than the hop's length at the
/// mode's top speed. Zero for the first call and non-passenger calls. Panics on a zero
/// `speed_mps` (Python raises).
pub fn minimum_drives(
    mode: &str,
    calls: &[CallState],
    plan: &impl HopPlan,
    hops: &Hops,
    now: Instant,
    policy: &PredictionPolicy,
    speed_mps: f64,
) -> Vec<TimeDelta> {
    let credit = TimeDelta::from_secs_f64(policy.dwell_credit_s.get(mode));
    let mut drives = vec![TimeDelta::ZERO; calls.len()];
    for b in 1..calls.len() {
        if !calls[b].passenger {
            continue;
        }
        let Some(a) = previous_passenger(calls, b) else {
            continue;
        };
        let start = plan.scheduled(a).1.or(plan.scheduled(a).0);
        let end = plan.scheduled(b).0.or(plan.scheduled(b).1);
        let mut running = match (start, end) {
            (Some(start), Some(end)) => end - start,
            _ => TimeDelta::ZERO,
        };
        if let Some(r) = ratio(hops, &key(mode, calls, a, b), now, policy) {
            running = running.mul_f64(r);
        }
        assert!(speed_mps != 0.0, "float division by zero");
        let fastest = TimeDelta::from_secs_f64(
            py_max(0.0, plan.call_distance_m(b) - plan.call_distance_m(a)) / speed_mps,
        );
        drives[b] = (running - credit).max(fastest).max(TimeDelta::ZERO);
    }
    drives
}
