//! Rail runs and their public journeys (docs/R2_SLICE.md section 2). Pure.
//!
//! The core follows a CZPTT path (PA) as one run instance keyed `czptt:pa`; the public and
//! history identity is the train number on its service date. A run whose train number changes
//! is two journeys sharing the call where it changes, linked `continues_as`. A road trip is its
//! own journey and its own part: nothing here applies to it.

use sha2::{Digest, Sha256};

use crate::index::{Index, IndexMiss, Trip};
use crate::model::{
    Effect, Feed, Instance, JourneyKey, JourneyLinkKind, JourneySpan, LinkJourneys, PartSpan,
    ScheduledCall, SnapshotJourney, visit_numbers,
};
use crate::time::{Date, Instant};

pub const RUN_NAMESPACE: &str = "czptt:pa";
pub const TRAIN_NAMESPACE: &str = "czptt:train_number";
/// A train's vehicle ID: the train its number names.
pub const TRAIN_PREFIX: &str = "train:";

/// The run instance's key: the path (PA) on its service date. Panics on a trip outside a run,
///.
pub fn run_journey(feed: Feed, run: &Trip, day: Date) -> JourneyKey {
    let run_key = run.run_key.as_ref().expect("a run trip has a run key");
    JourneyKey {
        feed,
        namespace: RUN_NAMESPACE.to_owned(),
        key: run_key.to_string(),
        service_date: day,
    }
}

/// One public journey per train number, over consecutive parts that carry it.
pub fn journey_spans(feed: Feed, run: &Trip, day: Date) -> Vec<JourneySpan> {
    let mut spans: Vec<JourneySpan> = Vec::new();
    for part in &run.parts {
        let number = part.train_number.as_deref();
        if let (Some(last), Some(number)) = (spans.last_mut(), number)
            && last.journey.key == number
        {
            last.last = part.last;
            continue;
        }
        let journey = match number {
            Some(number) => JourneyKey {
                feed,
                namespace: TRAIN_NAMESPACE.to_owned(),
                key: number.to_owned(),
                service_date: day,
            },
            None => run_journey(feed, run, day),
        };
        spans.push(JourneySpan {
            journey,
            first: part.first,
            last: part.last,
        });
    }
    spans
}

pub fn part_spans(run: &Trip) -> Vec<PartSpan> {
    run.parts
        .iter()
        .map(|part| PartSpan {
            trip_id: part.trip_id.to_string(),
            first: part.first,
            last: part.last,
        })
        .collect()
}

/// The feed trips an instance publishes over its calls: a run's parts, or a road trip as its
/// own single part.
pub fn published_parts(instance: &Instance) -> Vec<PartSpan> {
    if instance.parts.is_empty() {
        vec![PartSpan {
            trip_id: instance.trip_id.clone(),
            first: 0,
            last: instance.calls.len() - 1,
        }]
    } else {
        instance.parts.clone()
    }
}

/// The trip part the vehicle is on: the one holding the next call to reach.
pub fn current_part(instance: &Instance) -> String {
    if instance.parts.is_empty() {
        // The one published part is the trip itself.
        return instance.trip_id.clone();
    }
    part_holding(&instance.parts, instance.left_index()).to_owned()
}

/// `current_part` over the published parts and the instance's `left_index()`.
pub fn part_holding(parts: &[PartSpan], left_index: i64) -> &str {
    parts
        .iter()
        .find(|part| left_index < part.last as i64)
        .unwrap_or(&parts[parts.len() - 1])
        .trip_id
        .as_str()
}

/// The train number a run carries now: its public journey holding the next call to reach (at
/// the call where the number changes, the next one); None off a run or without a number.
pub fn current_train(instance: &Instance) -> Option<&str> {
    train_holding(&instance.journeys, instance.left_index())
}

/// `current_train` over the instance's journeys and its `left_index()`.
pub fn train_holding(journeys: &[JourneySpan], left_index: i64) -> Option<&str> {
    let last = journeys.last()?;
    let span = journeys
        .iter()
        .find(|s| left_index < s.last as i64)
        .unwrap_or(last);
    (span.journey.namespace == TRAIN_NAMESPACE).then_some(span.journey.key.as_str())
}

/// A rail run's vehicle ID, whichever unit or source reports it, in the API and GTFS-RT alike:
/// the train its current number names, else an opaque ID of the run's day.
pub fn run_vehicle_id(instance: &Instance) -> String {
    vehicle_id_of(&instance.journey, &instance.journeys, instance.left_index())
}

/// `run_vehicle_id` over the instance's journey, journeys and `left_index()`.
pub fn vehicle_id_of(journey: &JourneyKey, journeys: &[JourneySpan], left_index: i64) -> String {
    match train_holding(journeys, left_index) {
        Some(number) => format!("{TRAIN_PREFIX}{number}"),
        None => opaque(journey),
    }
}

/// `train-` and the first 16 hex digits of the SHA-256 of `feed|namespace|key|date`. Python
/// caches it (lru 8192); it is pure, so recomputing is the same.
pub fn opaque(journey: &JourneyKey) -> String {
    let identity = format!(
        "{}|{}|{}|{}",
        journey.feed.as_str(),
        journey.namespace,
        journey.key,
        journey.service_date.isoformat()
    );
    let digest = Sha256::digest(identity.as_bytes());
    let mut out = String::from("train-");
    for byte in &digest[..8] {
        out.push_str(&format!("{byte:02x}"));
    }
    out
}

/// The schedule snapshot of each public journey of an instance, and their links.
pub fn snapshots(
    journey: &JourneyKey,
    spans: &[JourneySpan],
    trip: &Trip,
    index: &Index,
    at: Instant,
) -> Result<Vec<Effect>, IndexMiss> {
    if spans.is_empty() {
        let last = trip.calls.len() - 1;
        let only = snapshot(journey, trip, 0, last, &trip.trip_id, index, at, None)?;
        return Ok(vec![Effect::SnapshotJourney(only)]);
    }
    let mut effects = Vec::with_capacity(2 * spans.len() - 1);
    for span in spans {
        // The journey's first part: the one starting at its first call (a junction is also the
        // end of the part before).
        let part = trip
            .parts
            .iter()
            .find(|p| p.first == span.first)
            .or_else(|| {
                trip.parts
                    .iter()
                    .find(|p| p.first <= span.first && span.first <= p.last)
            })
            .map_or(&trip.trip_id, |p| &p.trip_id);
        let run_key = trip.run_key.as_deref();
        let made = snapshot(
            &span.journey,
            trip,
            span.first,
            span.last,
            part,
            index,
            at,
            run_key,
        )?;
        effects.push(Effect::SnapshotJourney(made));
    }
    for pair in spans.windows(2) {
        effects.push(Effect::LinkJourneys(LinkJourneys {
            source: pair[0].journey.clone(),
            target: pair[1].journey.clone(),
            kind: JourneyLinkKind::ContinuesAs,
        }));
    }
    Ok(effects)
}

#[allow(clippy::too_many_arguments)]
fn snapshot(
    journey: &JourneyKey,
    trip: &Trip,
    first: usize,
    last: usize,
    trip_id: &str,
    index: &Index,
    at: Instant,
    run_key: Option<&str>,
) -> Result<SnapshotJourney, IndexMiss> {
    let span = &trip.calls[first..=last];
    let visits = visit_numbers(span.iter().map(|call| &*call.location_id));
    let mut calls = Vec::with_capacity(span.len());
    for (ordinal, (call, visit)) in span.iter().zip(visits).enumerate() {
        calls.push(ScheduledCall {
            ordinal: ordinal as i64 + 1,
            location_id: call.location_id.to_string(),
            visit_n: visit,
            passenger_service: call.passenger_service,
            scheduled_arrival: call.arrival,
            scheduled_departure: call.departure,
            name: index.location(&call.location_id)?.name.to_string(),
        });
    }
    Ok(SnapshotJourney {
        journey: journey.clone(),
        at,
        release_id: index.release_id.clone(),
        trip_id: trip_id.to_owned(),
        route_name: trip.route_name.to_string(),
        headsign: trip.headsign.as_deref().map(str::to_owned),
        calls: calls.into(),
        run_key: run_key.map(str::to_owned),
        route_id: Some(trip.route_id.to_string()),
        mode: Some(trip.mode.to_string()),
        agency_id: trip.agency_id.as_deref().map(str::to_owned),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn opaque_is_sha256_of_the_journey() {
        // hashlib.sha256(b"czptt|czptt:pa|PA1|2026-10-08").hexdigest()[:16]
        let journey = JourneyKey {
            feed: Feed::Czptt,
            namespace: RUN_NAMESPACE.into(),
            key: "PA1".into(),
            service_date: Date::ymd(2026, 10, 8),
        };
        assert_eq!(opaque(&journey), "train-1b63fa0f86838ca3");
    }
}
