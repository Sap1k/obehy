//! Keyless inference (R5). A vehicle without a trip key bound by what it shows (BASE_PLAN.md
//! section 19.2; the first source is Arriva Express, docs/sources/arriva-express.md).
//!
//! A scheduled time at the next stop that exactly one of the line's trips has binds that trip
//! outright, whatever the destination says, and ends a binding to another trip (MPVNET-Q3,
//! MPVNET-Q11: a city bus names its line's terminal, not its trip's). Otherwise candidates come
//! from the line and are hard-filtered by destination and time ([`candidates`]). The episode's
//! departures, else the current fix, give each a time residual
//! ([`score`]). The best candidate binds only if its residual is at most `accept_s`
//! (`gps_accept_s` for a fix alone) and the runner-up's is at least `margin_s` larger;
//! otherwise the observation is `ambiguous`. The core calls it only for an observation with a
//! `LineRef` and no `TripKey`.

pub mod candidates;
pub mod episode;
pub mod score;

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::hash::BuildHasher;

use indexmap::IndexMap;

use crate::config::Policy;
use crate::index::{Index, IndexMiss};
use crate::infer::keyed::{Found, Match, admissible};
use crate::model::{
    Binding, BindingMethod, JourneyKey, KeylessEpisode, LineRef, NextStopName, Observation, Reason,
    VehicleState,
};
use crate::time::Instant;
use crate::timeline::plan::PlanCache;

use self::candidates::{at_next_stop, candidates};
use self::score::{place, position_residual};

/// What told the candidates apart.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Evidence {
    Departures,
    Position,
    /// The scheduled time at the next stop (MPVNET-Q3).
    NextStopTime,
}

impl Evidence {
    pub const fn as_str(self) -> &'static str {
        match self {
            Evidence::Departures => "departures",
            Evidence::Position => "position",
            Evidence::NextStopTime => "next_stop_time",
        }
    }
}

/// The outcome and its explanation: how many candidates passed the hard filters, the evidence
/// used, and the best and runner-up residuals (seconds).
#[derive(Debug, Clone, PartialEq)]
pub struct Decision {
    pub result: Found,
    pub candidates: usize,
    pub evidence: Option<Evidence>,
    pub best_s: Option<f64>,
    pub runner_up_s: Option<f64>,
}

impl Decision {
    fn reason(reason: Reason, candidates: usize) -> Decision {
        Decision {
            result: Found::Reason(reason),
            candidates,
            evidence: None,
            best_s: None,
            runner_up_s: None,
        }
    }
}

/// The journeys an instance exists for: Python's `Container[JourneyKey]`.
pub trait Journeys {
    fn holds(&self, journey: &JourneyKey) -> bool;
}

impl<V, S: BuildHasher> Journeys for IndexMap<JourneyKey, V, S> {
    fn holds(&self, journey: &JourneyKey) -> bool {
        self.contains_key(journey)
    }
}

impl<V, S: BuildHasher> Journeys for HashMap<JourneyKey, V, S> {
    fn holds(&self, journey: &JourneyKey) -> bool {
        self.contains_key(journey)
    }
}

impl<V> Journeys for BTreeMap<JourneyKey, V> {
    fn holds(&self, journey: &JourneyKey) -> bool {
        self.contains_key(journey)
    }
}

impl<S: BuildHasher> Journeys for HashSet<JourneyKey, S> {
    fn holds(&self, journey: &JourneyKey) -> bool {
        self.contains(journey)
    }
}

impl Journeys for BTreeSet<JourneyKey> {
    fn holds(&self, journey: &JourneyKey) -> bool {
        self.contains(journey)
    }
}

/// Python's `_median`: sorted, the middle one or the mean of the two middle ones.
fn median(values: &mut [f64]) -> f64 {
    values.sort_by(|a, b| a.partial_cmp(b).expect("residuals are not NaN"));
    let middle = values.len() / 2;
    if values.len() % 2 == 1 {
        values[middle]
    } else {
        (values[middle - 1] + values[middle]) / 2.0
    }
}

/// The next stop and its scheduled time, when the source gives them (MPVNET-Q3).
fn next_stop_due(observation: &Observation) -> Option<(&NextStopName, Instant)> {
    observation
        .first::<NextStopName>()
        .and_then(|next| next.scheduled.map(|due| (next, due)))
}

/// The one trip of the line with a call at the next stop at its scheduled time, whatever the
/// destination (MPVNET-Q11), with the gap in seconds and how many trips were looked at; None
/// without such a time, or unless exactly one trip has it.
fn named_by_next_stop(
    line: &LineRef,
    episode: &KeylessEpisode,
    observation: &Observation,
    index: &Index,
    policy: &Policy,
) -> Result<Option<(f64, usize, Match)>, IndexMiss> {
    let Some((next, due)) = next_stop_due(observation) else {
        return Ok(None);
    };
    let found = match candidates(
        line,
        None,
        episode.townless,
        observation.at(),
        index,
        &policy.keyless,
    )? {
        Ok(found) => found,
        Err(_) => return Ok(None),
    };
    let timed = at_next_stop(
        &found,
        &next.name,
        due,
        episode.townless,
        index,
        &policy.keyless,
    )?;
    Ok(match timed[..] {
        [(gap, chosen)] => Some((gap, found.len(), chosen.clone())),
        _ => None,
    })
}

pub fn bind(
    line: &LineRef,
    episode: &KeylessEpisode,
    observation: &Observation,
    index: &Index,
    policy: &Policy,
    plans: &mut PlanCache,
) -> Result<Decision, IndexMiss> {
    if let Some((gap, looked_at, chosen)) =
        named_by_next_stop(line, episode, observation, index, policy)?
    {
        return Ok(Decision {
            result: Found::Match(chosen),
            candidates: looked_at,
            evidence: Some(Evidence::NextStopTime),
            best_s: Some(gap),
            runner_up_s: None,
        });
    }
    let found = match candidates(
        line,
        Some(&episode.destination),
        episode.townless,
        observation.at(),
        index,
        &policy.keyless,
    )? {
        Ok(found) => found,
        Err(reason) => return Ok(Decision::reason(reason, 0)),
    };
    let mut by_departures: Vec<(f64, &Match)> = Vec::new();
    let mut by_position: Vec<(f64, &Match)> = Vec::new();
    let mut survivors = 0;
    for candidate in &found {
        let Some(placed) = place(candidate, episode, index, policy)? else {
            continue; // its calls contradict the episode
        };
        survivors += 1;
        let mut timed: Vec<f64> = placed.residuals.iter().flatten().copied().collect();
        if !timed.is_empty() {
            by_departures.push((median(&mut timed), candidate));
            continue;
        }
        if let Some(residual) =
            position_residual(candidate, &placed, observation, index, policy, plans)?
        {
            by_position.push((residual, candidate));
        }
    }
    if survivors == 0 {
        return Ok(Decision::reason(Reason::NotInService, found.len()));
    }
    let keyless = &policy.keyless;
    let (mut scored, accept, evidence) = if !by_departures.is_empty() {
        (by_departures, keyless.accept_s, Evidence::Departures)
    } else if !by_position.is_empty() {
        (by_position, keyless.gps_accept_s, Evidence::Position)
    } else {
        // nothing to tell them apart yet
        return Ok(Decision::reason(Reason::Ambiguous, survivors));
    };
    scored.sort_by(|a, b| {
        a.0.partial_cmp(&b.0)
            .expect("residuals are not NaN")
            .then_with(|| a.1.journey.cmp(&b.1.journey))
    });
    let (best, chosen) = scored[0];
    let runner_up = scored.get(1).map(|(residual, _)| *residual);
    let refused = best > accept as f64
        || runner_up.is_some_and(|runner_up| runner_up - best < keyless.margin_s as f64);
    Ok(Decision {
        result: if refused {
            Found::Reason(Reason::Ambiguous)
        } else {
            Found::Match(chosen.clone())
        },
        candidates: survivors,
        evidence: Some(evidence),
        best_s: Some(best),
        runner_up_s: runner_up,
    })
}

/// An inferred binding continues while its episode does (same line and destination, no long
/// silence) and the time stays in the journey's window (section 19.5); otherwise None, and the
/// vehicle is inferred again. A bound trip is never re-scored on the way: the source's delay is
/// stale between stops (ARRIVA-Q10), and the delay comes from GPS progress. Only a next stop's
/// scheduled time naming another trip ends it early (MPVNET-Q11: the destination of a city
/// bus stays its line's terminal from one trip to the next).
pub fn continued(
    previous: Option<&VehicleState>,
    line: &LineRef,
    episode: &KeylessEpisode,
    observation: &Observation,
    instances: &impl Journeys,
    index: &Index,
    policy: &Policy,
) -> Result<Option<Binding>, IndexMiss> {
    let at = observation.at();
    let Some(previous) = previous else {
        return Ok(None);
    };
    let Some(binding) = previous
        .binding
        .as_ref()
        .filter(|b| b.method == BindingMethod::Inferred)
    else {
        return Ok(None);
    };
    match &previous.episode {
        Some(prior) if prior.started_at == episode.started_at => {}
        _ => return Ok(None), // line or destination changed, or the vehicle was gone too long
    }
    if !instances.holds(&binding.journey) {
        return Ok(None);
    }
    if !admissible(binding, at, index, policy)? {
        return Ok(None);
    }
    if let Some((next, due)) = next_stop_due(observation) {
        let bound = [Match::of(
            binding.journey.clone(),
            index.trip(&binding.trip_id)?.clone(),
        )?];
        let keeps = !at_next_stop(
            &bound,
            &next.name,
            due,
            episode.townless,
            index,
            &policy.keyless,
        )?
        .is_empty();
        if !keeps
            && named_by_next_stop(line, episode, observation, index, policy)?
                .is_some_and(|(_, _, named)| named.journey != binding.journey)
        {
            return Ok(None);
        }
    }
    Ok(Some(binding.clone()))
}

/// An inferred binding is confirmed once the vehicle has left `confirm_departures` stops of the
/// trip, in its order; times are not compared (ARRIVA-Q10).
pub fn confirm(
    binding: &Binding,
    episode: &KeylessEpisode,
    index: &Index,
    policy: &Policy,
) -> Result<Binding, IndexMiss> {
    if binding.confirmed {
        return Ok(binding.clone());
    }
    let trip = index.trip(&binding.trip_id)?.clone();
    let found = Match::of(binding.journey.clone(), trip)?;
    let placed = place(&found, episode, index, policy)?;
    if placed.is_some_and(|p| p.residuals.len() as i64 >= policy.keyless.confirm_departures) {
        return Ok(Binding {
            confirmed: true,
            ..binding.clone()
        });
    }
    Ok(binding.clone())
}
