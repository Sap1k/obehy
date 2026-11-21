//! Committing crossings of trigger points once the tracker's readings agree. Port of
//! `realtime/timeline/commit.py`, operation for operation.
//!
//! `BASE_PLAN.md` section 20.4 ("Commit when all agree") and `docs/R1_SLICE.md` section 9,
//! step 6. A crossing is committed in path order when every reading that is not negligible has
//! passed the trigger between the same two fixes. Its time is placed between those fixes by
//! timetable time; it is recorded as an event (history) when the fixes are close enough to say
//! when it happened, or, further apart, as an inferred one when the vehicle was about as late at
//! both (then the timetable says when it passed); realtime gets its time either way.
//!
//! Float comparisons that decide: `log_p >= -agree_within` (log_p relative to the best, from
//! `progress`), `before.along < x <= after.along` in brackets, `along_m >= reached`,
//! `placement.along_m <= frontier`, the share clamp, `most - least > max_inferred_spread_s` and
//! `total_seconds() > max_extrapolate_s`. Each compares values computed by the same IEEE
//! operations in the same order as Python, so a decision can only differ if an input does; the
//! fixtures check every input and output bit for bit.

use std::sync::Arc;

use crate::config::policy::ProgressPolicy;
use crate::model::{EventKind, EventMethod, Hypothesis, Interval, Placement, StopSeen};
use crate::py::num::{py_max, py_min};
use crate::time::{Instant, TimeDelta};
use crate::timeline::plan::{Plan, Trigger};
use crate::timeline::stops::{departure, holding};

/// A committed crossing of a call's trigger point.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Crossing {
    pub call: usize,
    pub kind: EventKind,
    /// The fixes either side.
    pub fixes: Interval,
    /// When, placed between them.
    pub at: Instant,
    /// An event for history too: observed between close enough fixes (`Progress`, its interval
    /// the fixes), or inferred across a gap the lateness agrees on either side (`Inferred`, its
    /// interval the times that lateness allows); None: realtime only.
    pub method: Option<EventMethod>,
    pub interval: Option<Interval>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Committed {
    pub beam: Vec<Hypothesis>,
    pub committed_m: f64,
    pub crossed: Vec<Crossing>,
    pub crossed_at: Option<Instant>,
}

/// The fixes either side of where a reading crossed a point.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Bracket {
    pub before: Placement,
    pub after: Placement,
}

/// Commit, in path order, every crossing the deciding readings agree on (commit.py `commit`,
/// whose docstring has the rules). `stop` is the vehicle's presence at a stop
/// (`stops::seen`); `returning`: the first fix back on the path after a stretch off it.
#[allow(clippy::too_many_arguments)]
pub fn commit(
    plan: &Plan,
    beam: Vec<Hypothesis>,
    committed_m: f64,
    mut crossed_at: Option<Instant>,
    at: Instant,
    policy: &ProgressPolicy,
    dwell_s: f64,
    stop: Option<&StopSeen>,
    returning: bool,
) -> Committed {
    if beam.is_empty() {
        return Committed {
            beam,
            committed_m,
            crossed: Vec::new(),
            crossed_at,
        };
    }
    let deciding = deciding(&beam, at, policy);
    let frontier = fold_min(deciding.iter().map(|h| h.along_m));
    let longest = TimeDelta::from_seconds(policy.max_event_interval_s);
    let mut crossed: Vec<Crossing> = Vec::new();
    let mut reached = committed_m;
    for trigger in plan.triggers_between(committed_m, frontier) {
        let brackets: Vec<Option<Bracket>> = deciding
            .iter()
            .map(|h| bracket(&h.history, trigger.along_m))
            .collect();
        let times = fix_times(brackets[0]);
        if brackets.iter().any(|b| fix_times(*b) != times) {
            break; // the readings disagree on when: undecided
        }
        let bracket = brackets[0];
        if bracket.is_none() && deciding.iter().any(|h| before(&h.history, trigger.along_m)) {
            break; // passed by a reading held off the path: no fix past it yet
        }
        if trigger.kind == EventKind::Departure {
            if holding(stop) == Some(trigger.call) {
                break; // still at the stop
            }
            if returning && bracket.is_some_and(|b| b.after.at == at) {
                break; // back on the path past a stop it may pull back to
            }
        }
        reached = trigger.along_m;
        let Some(bracket) = bracket else {
            // Crossed before anything was observed: nothing, except the origin's departure:
            // timed by the fixes on its stand if it was seen there, else inferred from the
            // lateness at the first fix just past it.
            let first = deciding[0].history[0];
            let origin = trigger.call == 0 && trigger.kind == EventKind::Departure;
            if origin
                && let Some(stop) = stop
                && stop.call == 0
                && let Some(away) = stop.away
            {
                let mut when = departure(plan, stop, dwell_s);
                if let Some(c) = crossed_at
                    && when < c
                {
                    when = c;
                }
                crossed_at = Some(when);
                let stood = Interval::new(stop.last, away);
                let method = if stood.hi - stood.lo <= longest {
                    EventMethod::Progress
                } else {
                    EventMethod::Inferred
                };
                crossed.push(Crossing {
                    call: 0,
                    kind: EventKind::Departure,
                    fixes: stood,
                    at: when,
                    method: Some(method),
                    interval: Some(stood),
                });
                continue;
            }
            let inferred = if origin {
                before_first(first, trigger, plan, policy)
            } else {
                None
            };
            if let Some(inferred) = inferred {
                let mut when = inferred.mid();
                if let Some(c) = crossed_at
                    && when < c
                {
                    when = c;
                }
                crossed_at = Some(when);
                crossed.push(Crossing {
                    call: trigger.call,
                    kind: trigger.kind,
                    fixes: Interval::new(first.at, first.at),
                    at: when,
                    method: Some(EventMethod::Inferred),
                    interval: Some(inferred),
                });
            }
            continue;
        };
        let mut when = crossing_time(bracket, trigger, plan, dwell_s);
        let mut fixes = Interval::new(bracket.before.at, bracket.after.at);
        let mut seen_leaving = false;
        if let Some(stop) = stop
            && stop.call as usize == trigger.call
        {
            if trigger.kind == EventKind::Arrival {
                when = min_instant(when, stop.first);
            } else if let Some(away) = stop.away {
                // Between the last fix at the stop and the first one away.
                when = departure(plan, stop, dwell_s);
                fixes = Interval::new(stop.last, away);
                seen_leaving = true;
            }
        }
        if let Some(c) = crossed_at
            && when < c
        {
            when = c;
        }
        crossed_at = Some(when);
        let crossing = |method, interval| Crossing {
            call: trigger.call,
            kind: trigger.kind,
            fixes,
            at: when,
            method,
            interval,
        };
        if seen_leaving && fixes.hi - fixes.lo > longest {
            // Silent between: it left at some time in the gap, the fixes say no more.
            crossed.push(crossing(Some(EventMethod::Inferred), Some(fixes)));
            continue;
        }
        if fixes.hi - fixes.lo <= longest {
            crossed.push(crossing(Some(EventMethod::Progress), Some(fixes)));
            continue;
        }
        let inferred = inferred(bracket, trigger, when, plan, policy);
        let method = inferred.map(|_| EventMethod::Inferred);
        crossed.push(crossing(method, inferred));
    }
    if reached == committed_m {
        return Committed {
            beam,
            committed_m,
            crossed: Vec::new(),
            crossed_at,
        };
    }
    // `h in deciding`: identity, else value equality (Python's tuple containment).
    let kept: Vec<Hypothesis> = beam
        .iter()
        .filter(|h| {
            deciding.iter().any(|d| std::ptr::eq(*d, *h) || *d == *h) || h.along_m >= reached
        })
        .map(|h| cut(h, reached))
        .collect();
    Committed {
        beam: kept,
        committed_m: reached,
        crossed,
        crossed_at,
    }
}

/// `min(values)` over a non-empty iterator: the first of equal values. Panics when empty
/// (Python raises).
fn fold_min(mut values: impl Iterator<Item = f64>) -> f64 {
    let first = values.next().expect("min() of an empty sequence");
    values.fold(first, py_min)
}

/// `min(a, b)` of instants: `b` only if it is earlier.
fn min_instant(a: Instant, b: Instant) -> Instant {
    if b < a { b } else { a }
}

/// `max(a, b)` of instants: `b` only if it is later.
fn max_instant(a: Instant, b: Instant) -> Instant {
    if b > a { b } else { a }
}

/// The readings a commit needs to agree: all but the negligible ones, or only the most likely
/// once a decision has been open longer than `max_commit_lag_s`.
fn deciding<'a>(
    beam: &'a [Hypothesis],
    at: Instant,
    policy: &ProgressPolicy,
) -> Vec<&'a Hypothesis> {
    let deciding: Vec<&Hypothesis> = beam
        .iter()
        .filter(|h| h.log_p >= -policy.agree_within)
        .collect();
    let oldest = deciding
        .iter()
        .map(|h| h.history[0].at)
        .reduce(min_instant)
        .expect("min() of an empty sequence");
    if deciding.len() > 1 && at - oldest > TimeDelta::from_seconds(policy.max_commit_lag_s) {
        return deciding[..1].to_vec();
    }
    deciding
}

fn fix_times(bracket: Option<Bracket>) -> Option<(Instant, Instant)> {
    bracket.map(|b| (b.before.at, b.after.at))
}

/// The fixes either side of where the history crossed `along_m`; None if it was crossed before
/// the history starts (never observed).
pub fn bracket(history: &[Placement], along_m: f64) -> Option<Bracket> {
    history
        .windows(2)
        .find(|w| w[0].along_m < along_m && along_m <= w[1].along_m)
        .map(|w| Bracket {
            before: w[0],
            after: w[1],
        })
}

/// When the vehicle crossed `trigger` between two fixes (commit.py `_crossing_time`): placed
/// in proportion to timetable time (else distance), with a typical dwell `dwell_s` at a stop
/// the timetable gives none.
fn crossing_time(bracket: Bracket, trigger: &Trigger, plan: &Plan, dwell_s: f64) -> Instant {
    let Bracket { before, after } = bracket;
    let timetable = &plan.timetable;
    let planned = plan.scheduled_time(trigger.call, trigger.kind);
    let s0 = timetable.planned(before.at, before.along_m);
    let s1 = timetable.planned(after.at, after.along_m);
    let share = match (planned, s0, s1) {
        (Some(planned), Some(s0), Some(s1)) if s1 > s0 => (planned - s0).div_delta(s1 - s0),
        _ => (trigger.along_m - before.along_m) / (after.along_m - before.along_m),
    };
    let when = before.at + (after.at - before.at).mul_f64(py_min(1.0, py_max(0.0, share)));
    let (Some(s0), Some(s1)) = (s0, s1) else {
        return when;
    };
    if dwell_s <= 0.0 {
        return when;
    }
    let left = trigger.kind == EventKind::Departure
        && plan.dwelling_call(before.along_m) == Some(trigger.call);
    let reached = trigger.kind == EventKind::Arrival
        && plan.dwelling_call(after.along_m) == Some(trigger.call);
    if !(left || reached) {
        return when;
    }
    let other = if left { after.along_m } else { before.along_m };
    let ends = 1 + i64::from(plan.dwelling_call(other).is_some());
    let unexplained = (after.at - before.at) - (s1 - s0);
    let credit = TimeDelta::from_secs_f64(dwell_s).min(unexplained.div_int(ends));
    if credit <= TimeDelta::ZERO {
        return when;
    }
    if left {
        return max_instant(when, before.at + credit);
    }
    min_instant(when, after.at - credit)
}

/// When a crossing between fixes too far apart to observe it happened, if the vehicle was about
/// as late at both (commit.py `_inferred`).
fn inferred(
    bracket: Bracket,
    trigger: &Trigger,
    when: Instant,
    plan: &Plan,
    policy: &ProgressPolicy,
) -> Option<Interval> {
    let planned = plan.scheduled_time(trigger.call, trigger.kind)?;
    let timetable = &plan.timetable;
    let Bracket { before, after } = bracket;
    let a = timetable.lateness_s(before.at, before.along_m);
    let b = timetable.lateness_s(after.at, after.along_m);
    // sorted((a, b)): stable, so equal values keep their order.
    let (least, most) = if b < a { (b, a) } else { (a, b) };
    if most - least > policy.max_inferred_spread_s as f64 {
        return None;
    }
    let lo = max_instant(
        before.at,
        min_instant(when, planned + TimeDelta::from_secs_f64(least)),
    );
    let hi = min_instant(
        after.at,
        max_instant(when, planned + TimeDelta::from_secs_f64(most)),
    );
    Some(if lo <= hi {
        Interval::new(lo, hi)
    } else {
        Interval::new(when, when)
    })
}

/// When a vehicle last seen just short of its terminus arrived there: the scheduled arrival
/// shifted by the lateness at its last fixes (their range), if the last is at most
/// `max_extrapolate_s` of timetable time before it and the lateness over its fixes on the final
/// leg kept (at most `max_inferred_spread_s` apart). From the last fix on; None otherwise.
/// Panics on a plan without calls (Python's `scheduled[-1]` raises).
pub fn terminus(plan: &Plan, history: &[Placement], policy: &ProgressPolicy) -> Option<Interval> {
    assert!(!plan.scheduled.is_empty(), "tuple index out of range");
    let last = plan.scheduled.len() - 1;
    let planned = plan.scheduled_time(last, EventKind::Arrival)?;
    let fix = *history.last()?;
    let reached = plan.timetable.planned(fix.at, fix.along_m)?;
    if (planned - reached).total_seconds() > policy.max_extrapolate_s as f64 {
        return None;
    }
    // The lateness on the final leg only: standing at the call before it is not a change of
    // pace on the way in.
    let leg = if last > 0 {
        plan.zones[last - 1].departure_m
    } else {
        None
    };
    let mut driven: Vec<Placement> = history
        .iter()
        .filter(|p| leg.is_none_or(|leg| p.along_m >= leg))
        .copied()
        .collect();
    if driven.is_empty() {
        driven.push(fix);
    }
    let lateness: Vec<f64> = driven
        .iter()
        .map(|p| plan.timetable.lateness_s(p.at, p.along_m))
        .collect();
    let most = lateness[1..].iter().copied().fold(lateness[0], py_max);
    let least = lateness[1..].iter().copied().fold(lateness[0], py_min);
    if most - least > policy.max_inferred_spread_s as f64 {
        return None;
    }
    let lo = max_instant(fix.at, planned + TimeDelta::from_secs_f64(least));
    let hi = max_instant(fix.at, planned + TimeDelta::from_secs_f64(most));
    Some(Interval::new(lo, hi))
}

/// When the vehicle left its origin, first seen just past it (commit.py `_before_first`).
fn before_first(
    first: Placement,
    trigger: &Trigger,
    plan: &Plan,
    policy: &ProgressPolicy,
) -> Option<Interval> {
    let planned = plan.scheduled_time(trigger.call, trigger.kind);
    let reached = plan.timetable.planned(first.at, first.along_m);
    let (planned, reached) = (planned?, reached?);
    if (reached - planned).total_seconds() > policy.max_extrapolate_s as f64 {
        return None;
    }
    let lateness = TimeDelta::from_secs_f64(plan.timetable.lateness_s(first.at, first.along_m));
    let lo = min_instant(planned, planned + lateness);
    let hi = min_instant(first.at, planned + lateness.max(TimeDelta::ZERO));
    Some(Interval::new(min_instant(lo, hi), hi))
}

/// Whether the history was observed before `along_m` but not yet past it.
fn before(history: &[Placement], along_m: f64) -> bool {
    match (history.first(), history.last()) {
        (Some(first), Some(last)) => first.along_m < along_m && last.along_m < along_m,
        _ => false,
    }
}

/// Drop history before the last fix at or behind the commit point.
fn cut(h: &Hypothesis, frontier: f64) -> Hypothesis {
    let mut start = 0;
    for (i, placement) in h.history.iter().enumerate() {
        if placement.along_m <= frontier {
            start = i;
        }
    }
    if start == 0 {
        return h.clone();
    }
    Hypothesis {
        history: Arc::from(&h.history[start..]),
        ..h.clone()
    }
}
