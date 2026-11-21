//! A keyless vehicle's episode (R5). What it has shown since its line or destination last changed.
//!
//! An episode lasts while the line and destination stay the same and the vehicle is heard at
//! least every `episode_gap_s`. Each change of its next-stop name is a departure from the stop
//! named until then (ARRIVA-Q2), at the observation's time; with the source's delay it implies
//! the scheduled departure, an interval when the delay is whole minutes rounded down
//! (ARRIVA-Q3). The names are the source's; some are not stops of the timetable (a motorway
//! junction, ARRIVA-Q8).

use crate::config::policy::KeylessPolicy;
use crate::model::{
    Delay, Departure, Destination, FLOORED_MINUTE, Interval, KeylessEpisode, LineRef, NextStopName,
    Observation, Position, SourceSemantics,
};
use crate::names::same_name;
use crate::time::TimeDelta;

/// Longer than any trip's stop list; older departures no longer decide anything.
pub const MAX_DEPARTURES: usize = 40;

/// The episode after `observation`; None when it names no line or destination.
pub fn advance(
    previous: Option<&KeylessEpisode>,
    observation: &Observation,
    semantics: &SourceSemantics,
    policy: &KeylessPolicy,
) -> Option<KeylessEpisode> {
    let line = observation.first::<LineRef>()?;
    let destination = observation.first::<Destination>()?;
    let at = observation.at();
    let name = observation
        .first::<NextStopName>()
        .map(|following| following.name.clone());
    let Some(previous) = previous.filter(|previous| {
        previous.line == line.value
            && same_name(&previous.destination, &destination.name)
            && at - previous.last_at <= TimeDelta::from_seconds(policy.episode_gap_s)
    }) else {
        return Some(KeylessEpisode {
            line: line.value.clone(),
            destination: destination.name.clone(),
            started_at: at,
            last_at: at,
            next_stop: name,
            departures: Vec::new(),
        });
    };
    if at < previous.last_at {
        return Some(previous.clone()); // an older fix arriving late tells nothing new
    }
    let mut departures = previous.departures.clone();
    if let (Some(name), Some(next_stop)) = (&name, &previous.next_stop)
        && !same_name(name, next_stop)
        && observation.observed_at.is_some()
    {
        departures.push(Departure {
            stop: next_stop.clone(),
            left_at: at,
            scheduled: scheduled(observation, semantics),
            position: observation.first::<Position>().copied(),
        });
        if departures.len() > MAX_DEPARTURES {
            departures.drain(..departures.len() - MAX_DEPARTURES);
        }
    }
    Some(KeylessEpisode {
        line: previous.line.clone(),
        destination: previous.destination.clone(),
        started_at: previous.started_at,
        last_at: at,
        next_stop: name.or_else(|| previous.next_stop.clone()),
        departures,
    })
}

/// When the timetable had the departure the vehicle just made: its time minus the delay.
fn scheduled(observation: &Observation, semantics: &SourceSemantics) -> Option<Interval> {
    let delay = observation.first::<Delay>()?;
    let hi = observation.at() - TimeDelta::from_seconds(delay.seconds);
    if semantics.delay_floor_minute {
        // d minutes means d to d + 59 s late
        return Some(Interval::new(hi - FLOORED_MINUTE, hi));
    }
    Some(Interval::new(hi, hi))
}
