//! Where a road vehicle is seen at a stop: the fixes there, whatever the reading makes of them.
//!
//! The fixes within the departure margin of a call's point ([`StopSeen`]) bound its events: the
//! vehicle arrived by the first fix there; it left after the last fix there and before the
//! first fix away, a typical dwell after the last fix at most, never past the middle of the gap,
//! and early enough to have driven to the first fix away at the timetable's pace. At the first
//! and last call a fix within `terminal_radius_m` counts while the vehicle stands.

use crate::geo::{LonLat, distance_m};
use crate::model::{EventKind, StopSeen};
use crate::time::{Instant, TimeDelta};
use crate::timeline::path::point_at;
use crate::timeline::plan::Plan;

/// The first call whose departure trigger is not committed yet; past the last one, the
/// terminus while its arrival is not.
fn pending(plan: &Plan, committed_m: f64) -> Option<usize> {
    for (call, zone) in plan.zones.iter().enumerate() {
        if let Some(departure) = zone.departure_m
            && departure > committed_m
        {
            return Some(call);
        }
    }
    let last = plan.zones.len().checked_sub(1)?;
    match plan.zones[last].arrival_m {
        Some(arrival) if arrival > committed_m => Some(last),
        _ => None,
    }
}

/// Whether the call's last event (its departure; the terminus's arrival) is committed.
pub fn done_at(plan: &Plan, call: usize, committed_m: f64) -> bool {
    match plan.leaving_m(call) {
        None => true,
        Some(trigger) => trigger <= committed_m,
    }
}

fn last_at_point(plan: &Plan, mut call: usize) -> usize {
    let distances = &plan.path.call_distances_m;
    while ((call + 1) as i64) < distances.len() as i64 - 1 && distances[call + 1] == distances[call]
    {
        call += 1;
    }
    call
}

/// The call whose point the fix is within `radius_m` of, among the first `ahead` calls not yet
/// left (the last one at its point); at the first and last call within `terminal_m`. None
/// elsewhere.
pub fn at_call(
    plan: &Plan,
    committed_m: f64,
    here: LonLat,
    radius_m: f64,
    ahead: i64,
    terminal_m: f64,
) -> Option<usize> {
    let first = pending(plan, committed_m)?;
    let distances = &plan.path.call_distances_m;
    let last = distances.len() as i64 - 1;
    let end = (first as i64 + ahead).min(last + 1);
    for call in first as i64..end {
        let reach = if call == 0 || call == last {
            radius_m.max(terminal_m)
        } else {
            radius_m
        };
        let index = call as usize;
        if distance_m(here, point_at(&plan.path, distances[index])) <= reach {
            return Some(if call < last {
                last_at_point(plan, index)
            } else {
                index
            });
        }
    }
    None
}

/// The stop presence after a fix at `here`: kept, extended, ended (seen away) or begun at
/// another call. A presence whose call is done is dropped. `terminal_m` is the reach at the
/// first and last call while the vehicle stands (0: it moves, or the source cannot tell);
/// standing began at `still_since`, the previous fix, which a new presence starts from.
#[allow(clippy::too_many_arguments)]
pub fn seen(
    plan: &Plan,
    previous: Option<&StopSeen>,
    committed_m: f64,
    here: LonLat,
    at: Instant,
    radius_m: f64,
    ahead: i64,
    terminal_m: f64,
    still_since: Option<Instant>,
) -> Option<StopSeen> {
    let previous = previous.filter(|p| !done_at(plan, p.call as usize, committed_m));
    let Some(call) = at_call(plan, committed_m, here, radius_m, ahead, terminal_m) else {
        return match previous {
            Some(p) if p.away.is_none() => Some(StopSeen {
                call: p.call,
                first: p.first,
                last: p.last,
                away: Some(at),
                away_m: None,
            }),
            _ => previous.copied(),
        };
    };
    let call_i = call as i64;
    let fresh = |first: Instant| StopSeen {
        call: call_i,
        first,
        last: at,
        away: None,
        away_m: None,
    };
    if let Some(p) = previous
        && p.call == call_i
    {
        if p.away.is_some() && call + 1 == plan.zones.len() {
            return Some(fresh(still_since.unwrap_or(at))); // standing again short of it
        }
        return Some(fresh(p.first));
    }
    if let Some(p) = previous
        && call_i < p.call
    {
        return Some(*p); // back at a call already left: noise
    }
    Some(fresh(still_since.unwrap_or(at)))
}

/// The call whose departure waits: the vehicle's newest fix is still there.
pub fn holding(stop: Option<&StopSeen>) -> Option<usize> {
    stop.filter(|s| s.away.is_none()).map(|s| s.call as usize)
}

/// When the vehicle left a stop it was seen at until `last` and away from at `away`. Panics
/// without `away`.
pub fn departure(plan: &Plan, stop: &StopSeen, dwell_s: f64) -> Instant {
    let away = stop.away.expect("a stop seen away");
    let gap = away - stop.last;
    let mut drive = TimeDelta::ZERO;
    let leaves = plan.scheduled_time(stop.call as usize, EventKind::Departure);
    if let (Some(away_m), Some(leaves)) = (stop.away_m, leaves)
        && let Some(reached) = plan.timetable.planned(away, away_m)
        && reached > leaves
    {
        drive = reached - leaves;
    }
    let credit = TimeDelta::from_secs_f64(dwell_s)
        .min(gap.div_int(2))
        .min(gap - drive);
    stop.last + credit.max(TimeDelta::ZERO)
}

/// Progress short of the departure trigger of the call the vehicle is still at.
pub fn capped(plan: &Plan, stop: Option<&StopSeen>, along_m: f64) -> f64 {
    let Some(call) = holding(stop) else {
        return along_m;
    };
    match plan.zones[call].departure_m {
        Some(trigger) if along_m >= trigger => trigger - 1.0,
        _ => along_m,
    }
}
