//! Keyed binding (R5). A literal source key plus the observation time choose one journey.
//!
//! The date rule of BASE_PLAN.md section 19.3: candidate service dates are local yesterday,
//! today and tomorrow; a date survives when the trip runs on it and the observation lies in
//! `[start - pre_trip(mode), end + max_delay(mode)]`. The closest survivor wins; a tie is
//! ambiguous. Keys are never reinterpreted (DUK-Q3): a key whose trip does not run is
//! unmatched.
//!
//! Rail (docs/R2_SLICE.md section 2): a matched trip part is lifted to its run on that date, and
//! the instance is the run (`czptt:pa`), whichever key found it. A source that names the
//! operating date (`ServiceDay`, SŽ) makes it the only candidate.
//!
//! Python caches `_span` and `_admissible` (lru, pure); they are recomputed here. Every function
//! that reads the index returns its [`IndexMiss`] (fatal in the runner).

use std::collections::HashSet;
use std::sync::Arc;

use crate::config::Policy;
use crate::config::policy::TimePolicy;
use crate::index::{Index, IndexMiss, KeyRef, Trip, parent_ref};
use crate::model::{Binding, JourneyKey, Reason, TripKey};
use crate::runs::run_journey;
use crate::time::{Date, Instant, ServiceTime, TimeDelta, candidate_service_dates};

#[derive(Debug, Clone, PartialEq)]
pub struct Match {
    pub journey: JourneyKey,
    pub trip: Arc<Trip>,
    pub start: Instant,
    pub end: Instant,
}

impl Match {
    /// A match of `trip` as `journey`, its span from the schedule (`Match(j, t, *span(t, j))`).
    pub fn of(journey: JourneyKey, trip: Arc<Trip>) -> Result<Match, IndexMiss> {
        let (start, end) = span(&trip, &journey)?;
        Ok(Match {
            journey,
            trip,
            start,
            end,
        })
    }
}

/// A match, or why there is none: Python's `Match | Reason`.
#[derive(Debug, Clone, PartialEq)]
pub enum Found {
    Match(Match),
    Reason(Reason),
}

impl Found {
    pub fn reason(&self) -> Option<Reason> {
        match self {
            Found::Match(_) => None,
            Found::Reason(reason) => Some(*reason),
        }
    }
}

/// The journey's scheduled start and end instants.
pub fn span(trip: &Trip, journey: &JourneyKey) -> Result<(Instant, Instant), IndexMiss> {
    let day = journey.service_date;
    Ok((
        ServiceTime::new(day, trip.start()?).instant(),
        ServiceTime::new(day, trip.end()?).instant(),
    ))
}

/// `[start - pre_trip(mode), end + max_delay(mode)]`, each a `timedelta(seconds=<float>)`.
pub fn admissible_window(
    start: Instant,
    end: Instant,
    mode: &str,
    time: &TimePolicy,
) -> (Instant, Instant) {
    (
        start - TimeDelta::from_secs_f64(time.pre_trip_s.get(mode)),
        end + TimeDelta::from_secs_f64(time.max_delay_s.get(mode)),
    )
}

/// Whether `at` lies in the journey's admissible window (section 19.3, rule 2).
pub fn in_window(found: &Match, at: Instant, policy: &Policy) -> bool {
    let (lo, hi) = admissible_window(found.start, found.end, &found.trip.mode, &policy.time);
    lo <= at && at <= hi
}

/// Whether `at` is still in the window of the journey `binding` holds (section 19.5): what keeps
/// a binding, keyed or inferred, from one observation to the next.
pub fn admissible(
    binding: &Binding,
    at: Instant,
    index: &Index,
    policy: &Policy,
) -> Result<bool, IndexMiss> {
    let trip = index.trip(&binding.trip_id)?.clone();
    Ok(in_window(
        &Match::of(binding.journey.clone(), trip)?,
        at,
        policy,
    ))
}

/// The feed trips a key names that run, on each of `days` in turn: the key valid on the day and
/// the trip's service running it. A rail part is yielded as itself ([`Index::run`] joins it
/// into its run). Eager, where Python's is a generator: every caller but [`bind`] consumes it
/// whole, and [`bind`] walks the same loop itself.
pub fn running(
    index: &Index,
    namespace: &str,
    key: &str,
    days: &[Date],
) -> Result<Vec<(Date, Arc<Trip>)>, IndexMiss> {
    let entries = index.keys(namespace, key)?;
    let mut found = Vec::new();
    for &day in days {
        for entry in entries {
            if !entry.valid_on(day) {
                continue;
            }
            if let Some(trip) = index.running_trip(&entry.public_id, day)? {
                found.push((day, trip.clone()));
            }
        }
    }
    Ok(found)
}

fn distance(found: &Match, at: Instant) -> TimeDelta {
    if at < found.start {
        found.start - at
    } else if at > found.end {
        at - found.end
    } else {
        TimeDelta::from_micros(0)
    }
}

/// The journey `key` names at `at` (or on the operating date `day` a source gave), or why none.
/// Mutable for [`Index::run`], which caches the run trips it builds.
pub fn bind(
    key: &TripKey,
    at: Instant,
    index: &mut Index,
    policy: &Policy,
    day: Option<Date>,
) -> Result<Found, IndexMiss> {
    let entries = index.keys(&key.namespace, &key.key)?.to_vec();
    if entries.is_empty() {
        if let Some(parent) = parent_ref(&KeyRef::new(&key.namespace, &key.key))
            && index
                .keys(&parent.namespace, &parent.identifier)?
                .is_empty()
        {
            return Ok(Found::Reason(Reason::NoLine));
        }
        return Ok(Found::Reason(Reason::NoTrip));
    }
    let mut found: Vec<Match> = Vec::new();
    let mut seen: HashSet<JourneyKey> = HashSet::new();
    let days: Vec<Date> = match day {
        Some(day) => vec![day],
        None => candidate_service_dates(at).to_vec(),
    };
    // `running`, walked lazily as Python's generator is: an early AMBIGUOUS reads no further.
    for candidate in days {
        for entry in &entries {
            if !entry.valid_on(candidate) {
                continue;
            }
            let Some(trip) = index.running_trip(&entry.public_id, candidate)?.cloned() else {
                continue;
            };
            let (trip, journey) = if trip.run_key.is_some() {
                let run = index.run(&trip, candidate)?;
                if run.parts.is_empty() {
                    return Ok(Found::Reason(Reason::Ambiguous)); // two versions of a part run
                }
                let journey = run_journey(index.feed, &run, candidate);
                if !seen.insert(journey.clone()) {
                    continue; // another part of the same run
                }
                (run, journey)
            } else {
                let journey = JourneyKey {
                    feed: index.feed,
                    namespace: key.namespace.clone(),
                    key: key.key.clone(),
                    service_date: candidate,
                };
                (trip, journey)
            };
            found.push(Match::of(journey, trip)?);
        }
    }
    if found.is_empty() {
        return Ok(Found::Reason(Reason::NotActive));
    }
    let mut window: Vec<Match> = found
        .into_iter()
        .filter(|m| in_window(m, at, policy))
        .collect();
    if window.is_empty() {
        return Ok(Found::Reason(Reason::NotInService));
    }
    window.sort_by(|a, b| {
        (distance(a, at), a.journey.service_date).cmp(&(distance(b, at), b.journey.service_date))
    });
    if window.len() > 1 && distance(&window[0], at) == distance(&window[1], at) {
        return Ok(Found::Reason(Reason::Ambiguous));
    }
    Ok(Found::Match(window.swap_remove(0)))
}
