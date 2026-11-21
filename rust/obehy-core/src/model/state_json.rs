//! A `FeedState` read from the JSON Python's fixture generators write
//! (`rust/tools/gen_publish_fixtures.py`, `enc`): the parity tests rebuild the Python core's
//! state with it. Not a storage format: the worker never persists `FeedState` (warm replay
//! rebuilds it).
//!
//! The shape: a dataclass is an object of its fields; an instant integer microseconds since the
//! epoch; a date ISO text; a fact its `model_json` JSON; a non-finite float the text of its
//! repr; tuples lists. A `CallState` and an `Interval` are lists of their fields in order.
//! `FeedState.vehicles` is the list of states in insertion order; `instances` and `hops` lists
//! of `[key, value]` in insertion order; `dirty` a list of journeys.

use std::collections::VecDeque;
use std::sync::Arc;

use serde_json::Value;

use super::json::fact_from_json;
use super::*;
use crate::time::{Date, Instant};

/// State JSON this reader does not understand.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("state JSON: {0}")]
pub struct StateJsonError(pub String);

type Result<T> = std::result::Result<T, StateJsonError>;

fn bad<T>(what: &str, value: &Value) -> Result<T> {
    let mut text = value.to_string();
    text.truncate(120);
    Err(StateJsonError(format!("{what}: {text}")))
}

fn get<'a>(value: &'a Value, key: &str) -> Result<&'a Value> {
    match value.get(key) {
        Some(found) => Ok(found),
        None => bad(&format!("no field {key}"), value),
    }
}

fn opt<'a, T>(value: &'a Value, f: impl FnOnce(&'a Value) -> Result<T>) -> Result<Option<T>> {
    if value.is_null() {
        Ok(None)
    } else {
        f(value).map(Some)
    }
}

fn list<'a, T>(value: &'a Value, f: impl FnMut(&'a Value) -> Result<T>) -> Result<Vec<T>> {
    match value.as_array() {
        Some(items) => items.iter().map(f).collect(),
        None => bad("not a list", value),
    }
}

fn item(value: &Value, index: usize) -> Result<&Value> {
    match value.as_array().and_then(|a| a.get(index)) {
        Some(found) => Ok(found),
        None => bad(&format!("no item {index}"), value),
    }
}

pub fn text(value: &Value) -> Result<String> {
    match value.as_str() {
        Some(s) => Ok(s.to_owned()),
        None => bad("not a string", value),
    }
}

pub fn int(value: &Value) -> Result<i64> {
    match value.as_i64() {
        Some(n) => Ok(n),
        None => bad("not an integer", value),
    }
}

fn index(value: &Value) -> Result<usize> {
    usize::try_from(int(value)?).map_err(|_| StateJsonError(format!("negative index {value}")))
}

fn visit(value: &Value) -> Result<u32> {
    u32::try_from(int(value)?).map_err(|_| StateJsonError(format!("bad visit number {value}")))
}

/// A float: a JSON number, or `inf`, `-inf`, `nan`.
pub fn float(value: &Value) -> Result<f64> {
    match value {
        Value::Number(n) => n.as_f64().map_or_else(|| bad("not a float", value), Ok),
        Value::String(s) if s == "inf" => Ok(f64::INFINITY),
        Value::String(s) if s == "-inf" => Ok(f64::NEG_INFINITY),
        Value::String(s) if s == "nan" => Ok(f64::NAN),
        _ => bad("not a float", value),
    }
}

fn boolean(value: &Value) -> Result<bool> {
    match value.as_bool() {
        Some(b) => Ok(b),
        None => bad("not a bool", value),
    }
}

pub fn instant(value: &Value) -> Result<Instant> {
    int(value).map(Instant::from_micros)
}

pub fn date(value: &Value) -> Result<Date> {
    Date::from_isoformat(&text(value)?).map_err(|e| StateJsonError(e.to_string()))
}

fn literal<T>(value: &Value, parse: fn(&str) -> Option<T>) -> Result<T> {
    match value.as_str().and_then(parse) {
        Some(found) => Ok(found),
        None => bad("not a known literal", value),
    }
}

fn feed(value: &Value) -> Result<Feed> {
    text(value)?.parse().map_err(StateJsonError)
}

fn fact(value: &Value) -> Result<Fact> {
    fact_from_json(value).map_err(|e| StateJsonError(e.to_string()))
}

fn fact_of<T: FactKind + Clone>(value: &Value) -> Result<T> {
    let found = fact(value)?;
    match T::of(&found) {
        Some(inner) => Ok(inner.clone()),
        None => bad("another fact type", value),
    }
}

pub fn journey(value: &Value) -> Result<JourneyKey> {
    Ok(JourneyKey {
        feed: feed(get(value, "feed")?)?,
        namespace: text(get(value, "namespace")?)?,
        key: text(get(value, "key")?)?,
        service_date: date(get(value, "service_date")?)?,
    })
}

pub fn vehicle_id(value: &Value) -> Result<VehicleId> {
    Ok(VehicleId {
        source: text(get(value, "source")?)?,
        source_vehicle_id: text(get(value, "source_vehicle_id")?)?,
    })
}

fn interval(value: &Value) -> Result<Interval> {
    Ok(Interval::new(
        instant(item(value, 0)?)?,
        instant(item(value, 1)?)?,
    ))
}

fn lead(value: &Value) -> Result<Lead> {
    Ok(Lead {
        vehicle: vehicle_id(get(value, "vehicle")?)?,
        seen: instant(get(value, "seen")?)?,
    })
}

fn binding(value: &Value) -> Result<Binding> {
    Ok(Binding {
        vehicle: vehicle_id(get(value, "vehicle")?)?,
        journey: journey(get(value, "journey")?)?,
        trip_id: text(get(value, "trip_id")?)?,
        method: literal(get(value, "method")?, BindingMethod::parse)?,
        bound_at: instant(get(value, "bound_at")?)?,
        confirmed: boolean(get(value, "confirmed")?)?,
    })
}

fn episode(value: &Value) -> Result<KeylessEpisode> {
    Ok(KeylessEpisode {
        line: text(get(value, "line")?)?,
        destination: text(get(value, "destination")?)?,
        started_at: instant(get(value, "started_at")?)?,
        last_at: instant(get(value, "last_at")?)?,
        next_stop: opt(get(value, "next_stop")?, text)?,
        departures: list(get(value, "departures")?, |d| {
            Ok(Departure {
                stop: text(get(d, "stop")?)?,
                left_at: instant(get(d, "left_at")?)?,
                scheduled: opt(get(d, "scheduled")?, interval)?,
                position: opt(get(d, "position")?, fact_of::<Position>)?,
            })
        })?,
    })
}

pub fn vehicle_state(value: &Value) -> Result<VehicleState> {
    Ok(VehicleState {
        vehicle: vehicle_id(get(value, "vehicle")?)?,
        feed: feed(get(value, "feed")?)?,
        status: literal(get(value, "status")?, VehicleStatus::parse)?,
        last_seen: instant(get(value, "last_seen")?)?,
        trip_key: opt(get(value, "trip_key")?, fact_of::<TripKey>)?,
        binding: opt(get(value, "binding")?, binding)?,
        position: opt(get(value, "position")?, fact_of::<Position>)?,
        position_at: opt(get(value, "position_at")?, instant)?,
        reason: opt(get(value, "reason")?, |r| literal(r, Reason::parse))?,
        episode: opt(get(value, "episode")?, episode)?,
        facts: list(get(value, "facts")?, fact)?.into(),
        since: opt(get(value, "since")?, instant)?,
    })
}

fn platform(value: &Value) -> Result<Platform> {
    Ok(Platform {
        value: text(get(value, "value")?)?,
        label: literal(get(value, "label")?, PlatformLabel::parse)?,
        boarding_point_id: opt(get(value, "boarding_point_id")?, text)?,
        assigned_at: instant(get(value, "assigned_at")?)?,
        source: text(get(value, "source")?)?,
    })
}

/// A call: the list of its fields in order.
pub fn call(value: &Value) -> Result<CallState> {
    let at = |i: usize| item(value, i);
    Ok(CallState {
        sequence: int(at(0)?)?,
        location_id: text(at(1)?)?,
        visit_n: visit(at(2)?)?,
        arrival: opt(at(3)?, interval)?,
        departure: opt(at(4)?, interval)?,
        estimated_arrival: opt(at(5)?, instant)?,
        estimated_departure: opt(at(6)?, instant)?,
        status: literal(at(7)?, CallStatus::parse)?,
        source_class: opt(at(8)?, |s| literal(s, SourceClass::parse))?,
        passed_arrival: opt(at(9)?, instant)?,
        passed_departure: opt(at(10)?, instant)?,
        passenger: boolean(at(11)?)?,
        platform: opt(at(12)?, platform)?,
    })
}

fn hypothesis(value: &Value) -> Result<Hypothesis> {
    let history: Vec<Placement> = list(get(value, "history")?, |p| {
        Ok(Placement {
            along_m: float(item(p, 0)?)?,
            at: instant(item(p, 1)?)?,
        })
    })?;
    Ok(Hypothesis {
        along_m: float(get(value, "along_m")?)?,
        at: instant(get(value, "at")?)?,
        lateness_s: float(get(value, "lateness_s")?)?,
        log_p: float(get(value, "log_p")?)?,
        history: history.into(),
        at_us: int(get(value, "at_us")?)?,
        off_path_since: opt(get(value, "off_path_since")?, instant)?,
        reckoned_m: float(get(value, "reckoned_m")?)?,
    })
}

fn track(value: &Value) -> Result<Track> {
    Ok(Track {
        hypotheses: list(get(value, "hypotheses")?, hypothesis)?,
        committed_m: float(get(value, "committed_m")?)?,
        unmatched_since: opt(get(value, "unmatched_since")?, instant)?,
        seen_at: opt(get(value, "seen_at")?, instant)?,
        crossed_at: opt(get(value, "crossed_at")?, instant)?,
        off_path: opt(get(value, "off_path")?, |o| {
            Ok(OffPath {
                along_m: float(get(o, "along_m")?)?,
                at: instant(get(o, "at")?)?,
                lateness_s: float(get(o, "lateness_s")?)?,
            })
        })?,
        fix: opt(get(value, "fix")?, |f| {
            Ok(super::Fix {
                source: text(item(f, 0)?)?,
                lon: float(item(f, 1)?)?,
                lat: float(item(f, 2)?)?,
            })
        })?,
        at_stop: opt(get(value, "at_stop")?, |s| {
            Ok(StopSeen {
                call: int(get(s, "call")?)?,
                first: instant(get(s, "first")?)?,
                last: instant(get(s, "last")?)?,
                away: opt(get(s, "away")?, instant)?,
                away_m: opt(get(s, "away_m")?, float)?,
            })
        })?,
    })
}

fn source_track(value: &Value) -> Result<SourceTrack> {
    Ok(SourceTrack {
        source: text(get(value, "source")?)?,
        heard_at: instant(get(value, "heard_at")?)?,
        facts: opt(get(value, "facts")?, |f| list(f, fact))?.map(Arc::from),
        next_point: opt(get(value, "next_point")?, fact_of::<NextPoint>)?,
        delay_s: opt(get(value, "delay_s")?, int)?,
        delay_at: opt(get(value, "delay_at")?, instant)?,
        delay_reference: opt(get(value, "delay_reference")?, |r| {
            literal(r, DelayReference::parse)
        })?,
        prediction: opt(get(value, "prediction")?, fact_of::<NextStopPrediction>)?,
        point: opt(get(value, "point")?, int)?,
        position_at: opt(get(value, "position_at")?, instant)?,
        lead: opt(get(value, "lead")?, lead)?,
    })
}

pub fn write_event(value: &Value) -> Result<WriteEvent> {
    Ok(WriteEvent {
        journey: journey(get(value, "journey")?)?,
        location_id: text(get(value, "location_id")?)?,
        visit_n: visit(get(value, "visit_n")?)?,
        kind: literal(get(value, "kind")?, EventKind::parse)?,
        interval: interval(get(value, "interval")?)?,
        method: literal(get(value, "method")?, EventMethod::parse)?,
        source: text(get(value, "source")?)?,
    })
}

pub fn instance(value: &Value) -> Result<Instance> {
    Ok(Instance {
        journey: journey(get(value, "journey")?)?,
        release_id: text(get(value, "release_id")?)?,
        trip_id: text(get(value, "trip_id")?)?,
        mode: text(get(value, "mode")?)?,
        lifecycle: literal(get(value, "lifecycle")?, Lifecycle::parse)?,
        calls: list(get(value, "calls")?, call)?.into(),
        freshness: {
            let f = get(value, "freshness")?;
            Freshness {
                updated_at: instant(get(f, "updated_at")?)?,
                heard_at: instant(get(f, "heard_at")?)?,
                stale: boolean(get(f, "stale")?)?,
                lost: boolean(get(f, "lost")?)?,
            }
        },
        progress: opt(get(value, "progress")?, |p| {
            Ok(Progress {
                distance_m: float(get(p, "distance_m")?)?,
                call_index: int(get(p, "call_index")?)?,
                at: instant(get(p, "at")?)?,
            })
        })?,
        track: opt(get(value, "track")?, track)?,
        reported_delay_s: opt(get(value, "reported_delay_s")?, int)?,
        lateness_s: opt(get(value, "lateness_s")?, int)?,
        off_route_since: opt(get(value, "off_route_since")?, instant)?,
        off_route: boolean(get(value, "off_route")?)?,
        lead: opt(get(value, "lead")?, lead)?,
        journeys: list(get(value, "journeys")?, |s| {
            Ok(JourneySpan {
                journey: journey(get(s, "journey")?)?,
                first: index(get(s, "first")?)?,
                last: index(get(s, "last")?)?,
            })
        })?,
        parts: list(get(value, "parts")?, |p| {
            Ok(PartSpan {
                trip_id: text(get(p, "trip_id")?)?,
                first: index(get(p, "first")?)?,
                last: index(get(p, "last")?)?,
            })
        })?,
        sources: list(get(value, "sources")?, source_track)?,
        track_source: opt(get(value, "track_source")?, text)?,
        shown_source: opt(get(value, "shown_source")?, text)?,
        published: opt(get(value, "published")?, |p| {
            Ok(Published {
                lon: float(get(p, "lon")?)?,
                lat: float(get(p, "lat")?)?,
                bearing: opt(get(p, "bearing")?, float)?,
                at: instant(get(p, "at")?)?,
                along_m: opt(get(p, "along_m")?, float)?,
                snapped: boolean(get(p, "snapped")?)?,
            })
        })?,
        held: list(get(value, "held")?, write_event)?,
    })
}

/// A whole feed state.
pub fn feed_state(value: &Value) -> Result<FeedState> {
    let mut state = FeedState::new(feed(get(value, "feed")?)?);
    for v in list(get(value, "vehicles")?, vehicle_state)? {
        state.vehicles.insert(v.vehicle.clone(), v);
    }
    let instances: FastIndexMap<JourneyKey, Instance> = list(get(value, "instances")?, |kv| {
        Ok((journey(item(kv, 0)?)?, instance(item(kv, 1)?)?))
    })?
    .into_iter()
    .collect();
    state.instances = instances;
    state.dirty = list(get(value, "dirty")?, journey)?.into_iter().collect();
    for kv in list(get(value, "hops")?, Ok)? {
        let key = item(kv, 0)?;
        let hop = HopKey {
            mode: text(item(key, 0)?)?,
            from: text(item(key, 1)?)?,
            to: text(item(key, 2)?)?,
            from_departure: boolean(item(key, 3)?)?,
        };
        let traversals: VecDeque<Traversal> = list(item(kv, 1)?, |t| {
            Ok(Traversal {
                end: instant(item(t, 0)?)?,
                taken_s: float(item(t, 1)?)?,
                scheduled_s: float(item(t, 2)?)?,
            })
        })?
        .into_iter()
        .collect();
        state.hops.insert(hop, traversals);
    }
    Ok(state)
}
