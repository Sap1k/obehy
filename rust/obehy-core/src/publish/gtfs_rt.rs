//! Per-feed GTFS-RT from core state (BASE_PLAN.md section 30). Matched vehicles only.
//!
//! Each entity is encoded once (and cached while its inputs stay the same); the message is a
//! header field and the entity fields framed by hand ([`field`]). prost writes fields in tag
//! order and keeps proto2 presence through `Option`: a set field is written even at its
//! default. Coordinates and bearings are proto `float` (`f64 as f32`).
//!
//! What is published: a live journey's
//! TripUpdate per published part with its vehicle; a finished one keeps the calls it made, no
//! vehicle, for `lifecycle.publish_finished_s` after its last call; a lost one the calls it made
//! and `NO_DATA` for every call ahead; a cancelled one `CANCELED` without calls. Vehicle
//! positions for live, not stale journeys only: a rail run once, as `train:<number>` (or its
//! opaque ID), at its standing vehicle; a road journey its one source's vehicles.

use std::sync::Arc;

use prost::Message;

use super::represent::{
    Publication, is_live, is_made, one_source, publication, run_position, shown,
    vehicles_by_journey,
};
use crate::config::policy::LifecyclePolicy;
use crate::model::{
    CallState, CallStatus, FeedState, Instance, JourneyKey, PartSpan, VehicleId, VehicleState,
};
use crate::runs::{current_part, current_train, published_parts, run_vehicle_id};
use crate::time::{Date, Instant};

/// The `gtfs-realtime.proto` messages and fields this worker writes (proto2: every scalar
/// keeps presence, enums are their `int32` values).
pub mod proto {
    use prost::Message;

    #[derive(Clone, PartialEq, Message)]
    pub struct FeedMessage {
        #[prost(message, optional, tag = "1")]
        pub header: Option<FeedHeader>,
        #[prost(message, repeated, tag = "2")]
        pub entity: Vec<FeedEntity>,
    }

    #[derive(Clone, PartialEq, Message)]
    pub struct FeedHeader {
        #[prost(string, optional, tag = "1")]
        pub gtfs_realtime_version: Option<String>,
        #[prost(int32, optional, tag = "2")]
        pub incrementality: Option<i32>,
        #[prost(uint64, optional, tag = "3")]
        pub timestamp: Option<u64>,
    }

    #[derive(Clone, PartialEq, Message)]
    pub struct FeedEntity {
        #[prost(string, optional, tag = "1")]
        pub id: Option<String>,
        #[prost(message, optional, tag = "3")]
        pub trip_update: Option<TripUpdate>,
        #[prost(message, optional, tag = "4")]
        pub vehicle: Option<VehiclePosition>,
        #[prost(message, optional, tag = "5")]
        pub alert: Option<Alert>,
    }

    #[derive(Clone, PartialEq, Message)]
    pub struct TripDescriptor {
        #[prost(string, optional, tag = "1")]
        pub trip_id: Option<String>,
        #[prost(string, optional, tag = "3")]
        pub start_date: Option<String>,
        #[prost(int32, optional, tag = "4")]
        pub schedule_relationship: Option<i32>,
    }

    #[derive(Clone, PartialEq, Message)]
    pub struct VehicleDescriptor {
        #[prost(string, optional, tag = "1")]
        pub id: Option<String>,
        #[prost(string, optional, tag = "2")]
        pub label: Option<String>,
    }

    #[derive(Clone, PartialEq, Message)]
    pub struct TripUpdate {
        #[prost(message, optional, tag = "1")]
        pub trip: Option<TripDescriptor>,
        #[prost(message, repeated, tag = "2")]
        pub stop_time_update: Vec<StopTimeUpdate>,
        #[prost(message, optional, tag = "3")]
        pub vehicle: Option<VehicleDescriptor>,
        #[prost(uint64, optional, tag = "4")]
        pub timestamp: Option<u64>,
    }

    #[derive(Clone, PartialEq, Message)]
    pub struct StopTimeEvent {
        #[prost(int64, optional, tag = "2")]
        pub time: Option<i64>,
    }

    #[derive(Clone, PartialEq, Message)]
    pub struct StopTimeProperties {
        #[prost(string, optional, tag = "1")]
        pub assigned_stop_id: Option<String>,
    }

    #[derive(Clone, PartialEq, Message)]
    pub struct StopTimeUpdate {
        #[prost(uint32, optional, tag = "1")]
        pub stop_sequence: Option<u32>,
        #[prost(message, optional, tag = "2")]
        pub arrival: Option<StopTimeEvent>,
        #[prost(message, optional, tag = "3")]
        pub departure: Option<StopTimeEvent>,
        #[prost(int32, optional, tag = "5")]
        pub schedule_relationship: Option<i32>,
        #[prost(message, optional, tag = "6")]
        pub stop_time_properties: Option<StopTimeProperties>,
    }

    #[derive(Clone, PartialEq, Message)]
    pub struct Position {
        #[prost(float, optional, tag = "1")]
        pub latitude: Option<f32>,
        #[prost(float, optional, tag = "2")]
        pub longitude: Option<f32>,
        #[prost(float, optional, tag = "3")]
        pub bearing: Option<f32>,
    }

    #[derive(Clone, PartialEq, Message)]
    pub struct VehiclePosition {
        #[prost(message, optional, tag = "1")]
        pub trip: Option<TripDescriptor>,
        #[prost(message, optional, tag = "2")]
        pub position: Option<Position>,
        #[prost(uint64, optional, tag = "5")]
        pub timestamp: Option<u64>,
        #[prost(message, optional, tag = "8")]
        pub vehicle: Option<VehicleDescriptor>,
    }

    #[derive(Clone, PartialEq, Message)]
    pub struct TimeRange {
        #[prost(uint64, optional, tag = "1")]
        pub start: Option<u64>,
        #[prost(uint64, optional, tag = "2")]
        pub end: Option<u64>,
    }

    #[derive(Clone, PartialEq, Message)]
    pub struct EntitySelector {
        #[prost(string, optional, tag = "2")]
        pub route_id: Option<String>,
        #[prost(message, optional, tag = "4")]
        pub trip: Option<TripDescriptor>,
        #[prost(string, optional, tag = "5")]
        pub stop_id: Option<String>,
    }

    #[derive(Clone, PartialEq, Message)]
    pub struct Translation {
        #[prost(string, optional, tag = "1")]
        pub text: Option<String>,
        #[prost(string, optional, tag = "2")]
        pub language: Option<String>,
    }

    #[derive(Clone, PartialEq, Message)]
    pub struct TranslatedString {
        #[prost(message, repeated, tag = "1")]
        pub translation: Vec<Translation>,
    }

    #[derive(Clone, PartialEq, Message)]
    pub struct Alert {
        #[prost(message, repeated, tag = "1")]
        pub active_period: Vec<TimeRange>,
        #[prost(message, repeated, tag = "5")]
        pub informed_entity: Vec<EntitySelector>,
        #[prost(int32, optional, tag = "6")]
        pub cause: Option<i32>,
        #[prost(int32, optional, tag = "7")]
        pub effect: Option<i32>,
        #[prost(message, optional, tag = "8")]
        pub url: Option<TranslatedString>,
        #[prost(message, optional, tag = "10")]
        pub header_text: Option<TranslatedString>,
        #[prost(message, optional, tag = "11")]
        pub description_text: Option<TranslatedString>,
        #[prost(int32, optional, tag = "14")]
        pub severity_level: Option<i32>,
        #[prost(message, optional, tag = "17")]
        pub cause_detail: Option<TranslatedString>,
        #[prost(message, optional, tag = "18")]
        pub effect_detail: Option<TranslatedString>,
    }

    /// `FeedHeader.Incrementality.FULL_DATASET`.
    pub const FULL_DATASET: i32 = 0;
    /// `TripDescriptor.ScheduleRelationship`.
    pub const SCHEDULED: i32 = 0;
    pub const CANCELED: i32 = 3;
    /// `TripUpdate.StopTimeUpdate.ScheduleRelationship.NO_DATA`.
    pub const NO_DATA: i32 = 2;
}

/// Call statuses with a time published while live (`TIMED`).
fn is_timed(status: CallStatus) -> bool {
    matches!(
        status,
        CallStatus::Actual | CallStatus::Inferred | CallStatus::Predicted
    )
}

/// `int(value.timestamp())`.
fn epoch(value: Instant) -> i64 {
    value.unix_seconds()
}

/// A protobuf `uint64`; panics on a negative value.
fn unsigned(value: i64) -> u64 {
    u64::try_from(value).expect("a uint64 field takes no negative value")
}

/// `date.strftime("%Y%m%d")`.
pub fn yyyymmdd(day: Date) -> String {
    format!("{:04}{:02}{:02}", day.year(), day.month(), day.day())
}

fn trip(trip_id: &str, journey: &JourneyKey, cancelled: bool) -> proto::TripDescriptor {
    proto::TripDescriptor {
        trip_id: Some(trip_id.to_owned()),
        start_date: Some(yyyymmdd(journey.service_date)),
        schedule_relationship: Some(if cancelled {
            proto::CANCELED
        } else {
            proto::SCHEDULED
        }),
    }
}

fn entity_id(instance: &Instance, part: &PartSpan) -> String {
    let journey = &instance.journey;
    let day = journey.service_date.isoformat();
    if instance.parts.is_empty() {
        format!("trip:{}:{}:{day}", journey.namespace, journey.key)
    } else {
        format!("trip:{}:{day}", part.trip_id)
    }
}

fn mapped(call: &CallState) -> Option<&str> {
    call.platform
        .as_ref()
        .and_then(|p| p.boarding_point_id.as_deref())
}

/// A length-delimited protobuf field: the tag, the varint length, the bytes.
pub fn field(number: u32, payload: &[u8], out: &mut Vec<u8>) {
    out.push(u8::try_from(number << 3 | 2).expect("a one-byte tag"));
    let mut length = payload.len();
    while length > 0x7F {
        out.push((length & 0x7F) as u8 | 0x80);
        length >>= 7;
    }
    out.push(length as u8);
    out.extend_from_slice(payload);
}

/// The serialized VehiclePosition entity of `current` standing for `instance`.
pub fn vehicle_position(instance: &Instance, current: &VehicleState, vehicle_id: &str) -> Vec<u8> {
    let place = current
        .position
        .as_ref()
        .expect("a vehicle position has a position");
    let label = if instance.parts.is_empty() {
        None
    } else {
        current_train(instance).map(str::to_owned)
    };
    let (lat, lon, bearing, at) = match shown(Some(instance), &current.vehicle) {
        Some(drawn) => (drawn.lat, drawn.lon, drawn.bearing, drawn.at),
        None => (
            place.lat,
            place.lon,
            place.bearing,
            current.position_at.unwrap_or(current.last_seen),
        ),
    };
    let entity = proto::FeedEntity {
        id: Some(format!("vehicle:{vehicle_id}")),
        vehicle: Some(proto::VehiclePosition {
            trip: Some(trip(&current_part(instance), &instance.journey, false)),
            position: Some(proto::Position {
                latitude: Some(lat as f32),
                longitude: Some(lon as f32),
                bearing: bearing.map(|b| b as f32),
            }),
            timestamp: Some(unsigned(epoch(at))),
            vehicle: Some(proto::VehicleDescriptor {
                id: Some(vehicle_id.to_owned()),
                label,
            }),
        }),
        ..Default::default()
    };
    entity.encode_to_vec()
}

/// The serialized TripUpdate entities of a journey, one per published part. A finished journey
/// publishes the calls it made; a lost one those and `NO_DATA` for every call ahead. Either
/// leaves out a part without a call made.
pub fn trip_updates(
    instance: &Instance,
    vehicle_id: Option<&str>,
    published: Publication,
) -> Vec<Vec<u8>> {
    let journey = &instance.journey;
    let parts = published_parts(instance);
    if published == Publication::Cancelled {
        return parts.iter().map(|p| cancelled(instance, p)).collect();
    }
    let live = published == Publication::Live;
    let lost = published == Publication::Lost;
    let timed_status = |s: CallStatus| if live { is_timed(s) } else { is_made(s) };
    let mut out = Vec::new();
    for (n, part) in parts.iter().enumerate() {
        let mut updates: Vec<(&CallState, bool, bool)> = Vec::new();
        let mut made = false;
        for i in part.first..=part.last {
            let call = &instance.calls[i];
            if !call.passenger {
                continue;
            }
            let timed = timed_status(call.status);
            made = made || is_made(call.status);
            if !timed && mapped(call).is_none() && !lost {
                continue;
            }
            // A junction shared with the neighbouring part: arrival before, departure after.
            let arrival = !(n > 0 && i == part.first);
            let departure = !(n + 1 < parts.len() && i == part.last);
            updates.push((call, arrival && timed, departure && timed));
        }
        if updates.is_empty() || (!live && !made) {
            continue;
        }
        let stop_time_update = updates
            .into_iter()
            .map(|(call, arrival, departure)| {
                let mut stop = proto::StopTimeUpdate {
                    stop_sequence: Some(
                        u32::try_from(call.sequence).expect("a stop_sequence is a uint32"),
                    ),
                    ..Default::default()
                };
                if lost && !is_made(call.status) {
                    stop.schedule_relationship = Some(proto::NO_DATA);
                    return stop;
                }
                if arrival && let Some(t) = call.estimated_arrival {
                    stop.arrival = Some(proto::StopTimeEvent {
                        time: Some(epoch(t)),
                    });
                }
                if departure && let Some(t) = call.estimated_departure {
                    stop.departure = Some(proto::StopTimeEvent {
                        time: Some(epoch(t)),
                    });
                }
                if let Some(assigned) = mapped(call) {
                    stop.stop_time_properties = Some(proto::StopTimeProperties {
                        assigned_stop_id: Some(assigned.to_owned()),
                    });
                }
                stop
            })
            .collect();
        let entity = proto::FeedEntity {
            id: Some(entity_id(instance, part)),
            trip_update: Some(proto::TripUpdate {
                trip: Some(trip(&part.trip_id, journey, false)),
                stop_time_update,
                vehicle: vehicle_id.map(|id| proto::VehicleDescriptor {
                    id: Some(id.to_owned()),
                    label: None,
                }),
                timestamp: Some(unsigned(epoch(instance.freshness.updated_at))),
            }),
            ..Default::default()
        };
        out.push(entity.encode_to_vec());
    }
    out
}

fn cancelled(instance: &Instance, part: &PartSpan) -> Vec<u8> {
    proto::FeedEntity {
        id: Some(entity_id(instance, part)),
        trip_update: Some(proto::TripUpdate {
            trip: Some(trip(&part.trip_id, &instance.journey, true)),
            timestamp: Some(unsigned(epoch(instance.freshness.updated_at))),
            ..Default::default()
        }),
        ..Default::default()
    }
    .encode_to_vec()
}

/// What a journey's TripUpdate entities are made of; equal inputs give equal bytes. The calls
/// compare by pointer (they are shared and replaced on change,, and the
/// cache holds its `Arc`, so a pointer cannot be reused while cached): a miss only recomputes.
#[derive(Debug)]
struct TripInputs {
    calls: Arc<[CallState]>,
    parts: Vec<PartSpan>,
    trip_id: String,
    updated_at: Instant,
    vehicle_id: Option<String>,
    publication: Publication,
}

impl TripInputs {
    fn of(instance: &Instance, vehicle_id: Option<String>, publication: Publication) -> Self {
        TripInputs {
            calls: Arc::clone(&instance.calls),
            parts: instance.parts.clone(),
            trip_id: instance.trip_id.clone(),
            updated_at: instance.freshness.updated_at,
            vehicle_id,
            publication,
        }
    }

    fn same(&self, other: &TripInputs) -> bool {
        Arc::ptr_eq(&self.calls, &other.calls)
            && self.parts == other.parts
            && self.trip_id == other.trip_id
            && self.updated_at == other.updated_at
            && self.vehicle_id == other.vehicle_id
            && self.publication == other.publication
    }
}

/// How the API names a road vehicle (`VehicleBook::vehicle_id`); [`source_form`] without a
/// registry.
pub type Identity<'a> = &'a dyn Fn(&VehicleId) -> String;

/// A vehicle by its source form, `<source>:<source_vehicle_id>`.
pub fn source_form(vehicle: &VehicleId) -> String {
    vehicle.to_string()
}

/// Serializes a feed's GTFS-RT from core state, keeping each journey's TripUpdate bytes while
/// their inputs are unchanged.
///
/// Eviction: the cache holds exactly the journeys published by the last `serialize` (rebuilt
/// each call. Vehicle positions are not cached: an entity is a few fields
/// and building it costs what checking its inputs would.
#[derive(Debug)]
pub struct FeedEmitter {
    pub policy: LifecyclePolicy,
    trips: std::collections::HashMap<JourneyKey, (TripInputs, Vec<Vec<u8>>)>,
}

impl FeedEmitter {
    pub fn new(policy: LifecyclePolicy) -> FeedEmitter {
        FeedEmitter {
            policy,
            trips: std::collections::HashMap::new(),
        }
    }

    /// How many journeys' entities are cached (the periodic summary's container size).
    pub fn cached(&self) -> usize {
        self.trips.len()
    }

    /// The feed's message: its trip updates, its vehicle positions, then `alerts` (the feed's
    /// serialized `Alert` entities, [`super::alerts`]). `identity` names a road vehicle as the
    /// API does.
    pub fn serialize<A: AsRef<[u8]>>(
        &mut self,
        state: &FeedState,
        now: Instant,
        alerts: &[A],
        identity: Identity<'_>,
    ) -> Vec<u8> {
        let header = proto::FeedHeader {
            gtfs_realtime_version: Some("2.0".to_owned()),
            incrementality: Some(proto::FULL_DATASET),
            timestamp: Some(unsigned(epoch(now))),
        };
        let mut out = Vec::new();
        field(1, &header.encode_to_vec(), &mut out);

        let mut vehicles_of = vehicles_by_journey(state);
        for (journey, vehicles) in &mut vehicles_of {
            if let Some(instance) = state.instances.get(*journey)
                && instance.parts.is_empty()
            {
                *vehicles = one_source(instance, vehicles, state);
            }
        }

        let mut journeys: Vec<(&JourneyKey, &Instance)> = state.instances.iter().collect();
        journeys.sort_by(|a, b| a.0.cmp(b.0));
        let mut trips = std::collections::HashMap::with_capacity(journeys.len());
        for (journey, instance) in journeys {
            // A stale journey keeps its predictions through a reception gap.
            let Some(published) = publication(instance, now, &self.policy) else {
                continue;
            };
            // Not live: the vehicle may be on its next trip.
            let vehicle_id = if published != Publication::Live {
                None
            } else if !instance.parts.is_empty() {
                Some(run_vehicle_id(instance))
            } else {
                vehicles_of
                    .get(journey)
                    .and_then(|v| v.first())
                    .map(|v| identity(v))
            };
            let inputs = TripInputs::of(instance, vehicle_id, published);
            let entities = match self.trips.remove(journey) {
                Some((cached, entities)) if cached.same(&inputs) => entities,
                _ => trip_updates(instance, inputs.vehicle_id.as_deref(), published),
            };
            for entity in &entities {
                field(2, entity, &mut out);
            }
            trips.insert(journey.clone(), (inputs, entities));
        }
        self.trips = trips;

        let mut chosen: Vec<(String, &Instance, &VehicleState)> = Vec::new();
        for (journey, vehicles) in &vehicles_of {
            let Some(instance) = state.instances.get(*journey) else {
                continue;
            };
            if !is_live(instance.lifecycle) || instance.freshness.stale {
                continue;
            }
            let bound: Vec<&VehicleState> = vehicles.iter().map(|v| &state.vehicles[*v]).collect();
            if !instance.parts.is_empty() {
                if let Some(lead) = run_position(instance, &bound) {
                    chosen.push((run_vehicle_id(instance), instance, lead));
                }
                continue;
            }
            chosen.extend(
                bound
                    .into_iter()
                    .filter(|v| v.position.is_some())
                    .map(|v| (identity(&v.vehicle), instance, v)),
            );
        }
        chosen.sort_by(|a, b| a.0.cmp(&b.0));
        for (vehicle_id, instance, current) in chosen {
            field(
                2,
                &vehicle_position(instance, current, &vehicle_id),
                &mut out,
            );
        }
        for entity in alerts {
            field(2, entity.as_ref(), &mut out);
        }
        out
    }
}

/// A feed's message from a fresh emitter (`feed_message`'s bytes), without alerts, vehicles by
/// their source form.
pub fn feed_bytes(state: &FeedState, now: Instant, policy: &LifecyclePolicy) -> Vec<u8> {
    FeedEmitter::new(policy.clone()).serialize::<&[u8]>(state, now, &[], &source_form)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::{Feed, Freshness, Lifecycle};

    fn instance(statuses: &[CallStatus], parts: Vec<PartSpan>) -> Instance {
        let at = Instant::from_unix_seconds(1_790_000_000);
        let calls: Vec<CallState> = statuses
            .iter()
            .enumerate()
            .map(|(i, status)| {
                let mut call = CallState::new(i as i64 + 1, format!("L{i}"), 1);
                call.status = *status;
                call.estimated_arrival = Some(at + crate::time::TimeDelta::from_minutes(i as i64));
                call.estimated_departure = call.estimated_arrival;
                call
            })
            .collect();
        Instance {
            journey: JourneyKey {
                feed: Feed::Czptt,
                namespace: "czptt:pa".into(),
                key: "PA".into(),
                service_date: Date::ymd(2026, 1, 5),
            },
            release_id: "r".into(),
            trip_id: "t".into(),
            mode: "rail".into(),
            lifecycle: Lifecycle::Running,
            calls: calls.into(),
            freshness: Freshness {
                updated_at: at,
                heard_at: at,
                stale: false,
                lost: false,
            },
            progress: None,
            track: None,
            reported_delay_s: None,
            lateness_s: None,
            off_route_since: None,
            off_route: false,
            lead: None,
            journeys: Vec::new(),
            parts,
            sources: Vec::new(),
            track_source: None,
            shown_source: None,
            published: None,
            held: Vec::new(),
        }
    }

    fn decode(bytes: &[u8]) -> proto::FeedEntity {
        proto::FeedEntity::decode(bytes).expect("an entity")
    }

    #[test]
    fn hand_framed_fields_are_protobufs_own() {
        for size in [0usize, 1, 127, 128, 16_383, 16_384, 300_000] {
            let entity = proto::FeedEntity {
                id: Some("x".repeat(size)),
                ..Default::default()
            };
            let message = proto::FeedMessage {
                header: None,
                entity: vec![entity.clone()],
            };
            let mut framed = Vec::new();
            field(2, &entity.encode_to_vec(), &mut framed);
            assert_eq!(framed, message.encode_to_vec(), "{size}");
        }
        assert_eq!(yyyymmdd(Date::ymd(26, 1, 5)), "00260105");
    }

    #[test]
    fn a_junction_carries_the_arrival_before_and_the_departure_after() {
        use CallStatus::{Actual, Predicted};
        let parts = vec![
            PartSpan {
                trip_id: "a".into(),
                first: 0,
                last: 1,
            },
            PartSpan {
                trip_id: "b".into(),
                first: 1,
                last: 2,
            },
        ];
        let run = instance(&[Actual, Predicted, Predicted], parts);
        let entities = trip_updates(&run, Some("train:1"), Publication::Live);
        let updates: Vec<proto::TripUpdate> = entities
            .iter()
            .map(|e| decode(e).trip_update.expect("a trip update"))
            .collect();
        assert_eq!(
            decode(&entities[1]).id.as_deref(),
            Some("trip:b:2026-01-05")
        );
        let junction_before = &updates[0].stop_time_update[1];
        let junction_after = &updates[1].stop_time_update[0];
        assert!(junction_before.arrival.is_some() && junction_before.departure.is_none());
        assert!(junction_after.arrival.is_none() && junction_after.departure.is_some());
        assert_eq!(
            updates[0].trip.as_ref().unwrap().schedule_relationship,
            Some(proto::SCHEDULED)
        );
    }

    #[test]
    fn lost_calls_ahead_are_no_data_and_a_cancelled_trip_has_none() {
        use CallStatus::{Actual, Predicted};
        let trip = instance(&[Actual, Predicted, Predicted], Vec::new());
        let (entity,) = match trip_updates(&trip, None, Publication::Lost).as_slice() {
            [one] => (decode(one),),
            other => panic!("{} entities", other.len()),
        };
        assert_eq!(entity.id.as_deref(), Some("trip:czptt:pa:PA:2026-01-05"));
        let stops = entity.trip_update.unwrap().stop_time_update;
        let no_data: Vec<bool> = stops
            .iter()
            .map(|s| s.schedule_relationship == Some(proto::NO_DATA))
            .collect();
        assert_eq!(no_data, vec![false, true, true]);
        assert!(stops[1].arrival.is_none() && stops[0].departure.is_some());
        let cancelled = decode(&trip_updates(&trip, None, Publication::Cancelled)[0]);
        let update = cancelled.trip_update.unwrap();
        assert!(update.stop_time_update.is_empty() && update.vehicle.is_none());
        assert_eq!(
            update.trip.unwrap().schedule_relationship,
            Some(proto::CANCELED)
        );
        // Finished without a call made: nothing.
        let unmade = instance(&[Predicted, Predicted], Vec::new());
        assert!(trip_updates(&unmade, None, Publication::Finished).is_empty());
    }
}
