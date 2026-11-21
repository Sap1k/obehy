//! Time evidence for one keyless candidate (R5): port of `realtime/infer/keyless/score.py`
//! (BASE_PLAN.md section 19.2, step 3).
//!
//! - **Departures.** Each departure of the episode is placed on the candidate: the stop left is
//!   the first passenger call with that name after the previous departure's. The residual is how
//!   far the candidate's scheduled departure there lies outside the interval the source's delay
//!   implies. A name the candidate does not have is no evidence (ARRIVA-Q8); one the candidate
//!   has only before an earlier departure contradicts it. A departure seen further than
//!   `departure_radius_m` from the stop left keeps its place in the order but gives no time
//!   (ARRIVA-Q9).
//! - **Position.** The fix projected onto the candidate's path, within `max_path_distance_m`
//!   and (with a next stop) between the call before it and the next stop, else anywhere on the
//!   path (ARRIVA-Q9): the residual is the timetable's lateness there minus the source's delay.
//!   The smallest over the projections counts.

use crate::config::Policy;
use crate::geo::distance_m;
use crate::index::{Call, Index, IndexMiss, Trip};
use crate::infer::keyed::Match;
use crate::model::{Delay, Departure, Interval, KeylessEpisode, Observation, Position};
use crate::names::normal;
use crate::py::num::py_min;
use crate::time::{Instant, ServiceTime};
use crate::timeline::path::project_all;
use crate::timeline::plan::PlanCache;

/// The episode laid on a candidate: per departure its residual (None without a delay), and the
/// index into `trip.calls` of the current next stop (None if the episode has none).
#[derive(Debug, Clone, PartialEq)]
pub struct Placed {
    pub residuals: Vec<Option<f64>>,
    pub next_stop: Option<usize>,
}

/// The passenger calls: (index into `trip.calls`, normalised stop name).
fn stops(trip: &Trip, index: &Index) -> Result<Vec<(usize, String)>, IndexMiss> {
    let mut found = Vec::new();
    for (i, call) in trip.calls.iter().enumerate() {
        if call.passenger_service {
            found.push((i, normal(&index.location(&call.location_id)?.name)));
        }
    }
    Ok(found)
}

fn outside(value: Instant, interval: &Interval) -> f64 {
    if value < interval.lo {
        (interval.lo - value).total_seconds()
    } else if value > interval.hi {
        (value - interval.hi).total_seconds()
    } else {
        0.0
    }
}

/// The episode on the candidate, or None if it contradicts the candidate's calls.
pub fn place(
    found: &Match,
    episode: &KeylessEpisode,
    index: &Index,
    policy: &Policy,
) -> Result<Option<Placed>, IndexMiss> {
    let stops = stops(&found.trip, index)?;
    let day = found.journey.service_date;
    let mut residuals: Vec<Option<f64>> = Vec::with_capacity(episode.departures.len());
    let mut k = 0;
    for departure in &episode.departures {
        let name = normal(&departure.stop);
        let Some(at) = (k..stops.len()).find(|&n| stops[n].1 == name) else {
            if stops[..k].iter().any(|(_, n)| *n == name) {
                return Ok(None); // a stop the candidate has already passed: wrong order
            }
            continue; // not a stop of the candidate (ARRIVA-Q8): no evidence either way
        };
        let call = &found.trip.calls[stops[at].0];
        let seconds = call.departure.or(call.arrival);
        match (&departure.scheduled, seconds) {
            (Some(interval), Some(seconds)) if !away(departure, call, index, policy)? => {
                let scheduled = ServiceTime::new(day, seconds).instant();
                residuals.push(Some(outside(scheduled, interval)));
            }
            _ => residuals.push(None),
        }
        k = at + 1;
    }
    let current = match &episode.next_stop {
        Some(next) => {
            let name = normal(next);
            (k..stops.len())
                .find(|&n| stops[n].1 == name)
                .map(|n| stops[n].0)
        }
        None => None,
    };
    Ok(Some(Placed {
        residuals,
        next_stop: current,
    }))
}

/// Whether the vehicle was far from the stop when its name changed (ARRIVA-Q9). Python checks
/// it only for a departure with a scheduled interval and a call with a time (`or`
/// short-circuits), as here.
fn away(
    departure: &Departure,
    call: &Call,
    index: &Index,
    policy: &Policy,
) -> Result<bool, IndexMiss> {
    let Some(where_) = departure.position else {
        return Ok(false);
    };
    let location = index.location(call.placed_at())?;
    let (Some(lon), Some(lat)) = (location.lon, location.lat) else {
        return Ok(false);
    };
    let gap = distance_m((where_.lon, where_.lat), (lon, lat));
    Ok(gap > policy.keyless.departure_radius_m)
}

/// Seconds between the candidate's timetable at the fix and the source's delay, or None when
/// the fix is untimed, absent or off the candidate's path.
pub fn position_residual(
    found: &Match,
    placed: &Placed,
    observation: &Observation,
    index: &Index,
    policy: &Policy,
    plans: &mut PlanCache,
) -> Result<Option<f64>, IndexMiss> {
    let Some(position) = observation.first::<Position>() else {
        return Ok(None);
    };
    let Some(at) = observation.observed_at else {
        return Ok(None);
    };
    let plan = plans.plan(&found.trip, found.journey.service_date, index, policy)?;
    let distances = &plan.path.call_distances_m;
    let (mut lo, mut hi) = (f64::NEG_INFINITY, f64::INFINITY);
    if let Some(next_stop) = placed.next_stop {
        let margin = policy.lifecycle.arrival_radius_m;
        lo = distances[next_stop.saturating_sub(1)] - margin;
        hi = distances[next_stop] + margin;
    }
    let expected = observation
        .first::<Delay>()
        .map_or(0.0, |delay| delay.seconds as f64);
    let near: Vec<_> = project_all(&plan.path, position.lon, position.lat)
        .into_iter()
        .filter(|p| p.lateral_m <= policy.keyless.max_path_distance_m)
        .collect();
    let ranged: Vec<_> = near
        .iter()
        .filter(|p| lo <= p.along_m && p.along_m <= hi)
        .collect();
    let ranged = if ranged.is_empty() {
        near.iter().collect()
    } else {
        ranged
    };
    let residuals = ranged
        .iter()
        .map(|p| (plan.timetable.lateness_s(at, p.along_m) - expected).abs());
    Ok(residuals.reduce(py_min))
}
