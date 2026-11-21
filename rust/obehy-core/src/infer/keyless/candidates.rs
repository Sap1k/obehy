//! Candidate journeys of a keyless observation (R5) (BASE_PLAN.md section 19.2, steps 1-2).
//!
//! The line's trips ([`Index::line_keys`]) on the candidate service dates, hard-filtered: the
//! trip runs that day, its last passenger stop is the destination (matched only against the
//! trip's own calls, section 19.1), and its schedule widened by `candidate_window_s` covers the
//! observation. A journey is keyed by the trip's own literal key, so the same trip bound by a
//! keyed source is the same journey.

use crate::config::policy::KeylessPolicy;
use crate::index::{Index, IndexMiss, Trip, child_namespace};
use crate::infer::keyed::{Match, running, span};
use crate::model::{JourneyKey, LineRef, Reason};
use crate::names::same_name;
use crate::time::{Instant, TimeDelta, candidate_service_dates};

/// The name of the trip's last passenger stop.
pub fn destination_of<'a>(trip: &Trip, index: &'a Index) -> Result<Option<&'a str>, IndexMiss> {
    match trip.calls.iter().rev().find(|call| call.passenger_service) {
        Some(last) => Ok(Some(&*index.location(&last.location_id)?.name)),
        None => Ok(None),
    }
}

/// The line's journeys that can be the vehicle's, sorted by journey; or why there are none.
/// In loop order before the sort (day, then the line's keys, then each key's entries): one key
/// on one day can give two trips, and the stable sort keeps them in that order.
pub fn candidates(
    line: &LineRef,
    destination: &str,
    at: Instant,
    index: &Index,
    policy: &KeylessPolicy,
) -> Result<Result<Vec<Match>, Reason>, IndexMiss> {
    let Some(namespace) = child_namespace(&line.namespace) else {
        return Ok(Err(Reason::NoLine));
    };
    if index.keys(&line.namespace, &line.value)?.is_empty() {
        return Ok(Err(Reason::NoLine));
    }
    let keys = index.line_keys(&line.namespace, &line.value)?;
    if keys.is_empty() {
        return Ok(Err(Reason::NoTrip));
    }
    let window = TimeDelta::from_seconds(policy.candidate_window_s);
    let mut any_running = false;
    let mut found: Vec<Match> = Vec::new();
    for day in candidate_service_dates(at) {
        for key in keys {
            for (_, trip) in running(index, namespace, key, &[day])? {
                if trip.run_key.is_some() {
                    continue;
                }
                any_running = true;
                match destination_of(&trip, index)? {
                    Some(last) if same_name(last, destination) => {}
                    _ => continue,
                }
                let journey = JourneyKey {
                    feed: index.feed,
                    namespace: namespace.to_owned(),
                    key: key.to_string(),
                    service_date: day,
                };
                let (start, end) = span(&trip, &journey)?;
                if start - window <= at && at <= end + window {
                    found.push(Match {
                        journey,
                        trip,
                        start,
                        end,
                    });
                }
            }
        }
    }
    if found.is_empty() {
        return Ok(Err(if any_running {
            Reason::NotInService
        } else {
            Reason::NotActive
        }));
    }
    found.sort_by(|a, b| a.journey.cmp(&b.journey));
    Ok(Ok(found))
}
