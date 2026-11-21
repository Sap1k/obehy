//! `timeline::advance`, `timeline/fusion.rs`, `timeline/estimate.rs` and `platforms.rs` against
//! Python (`tools/gen_estimate_fixtures.py`): every call the Python core made into them while
//! stepping the corpus journeys and the unit scenarios of `test_timeline.py`,
//! `test_rail_fusion.py`, `test_cancellation.py`, `test_boards.py` and `test_hot_path.py`,
//! replayed here with the same inputs; the instances and effects out must be identical (every
//! float bit for bit, every instant to the microsecond).
//!
//! An instance is compared as the canonical JSON this file writes of it, for the one Python
//! made (decoded) and the one Rust made; the writer destructures every struct, so no field is
//! left out of the comparison.

mod common;

use std::collections::HashMap;
use std::io::Read;
use std::sync::Arc;

use obehy_core::config::Policy;
use obehy_core::config::policy::{POLICY_FILE, Predictor};
use obehy_core::index::{Call, Index, Trip, TripPart};
use obehy_core::infer::keyed::Match;
use obehy_core::model::json::{facts_from_json, facts_json_text};
use obehy_core::model::{
    BindingMethod, CallState, CallStatus, DelayReference, Effect, EventKind, EventMethod, Fact,
    FeedState, Fix, Freshness, Hypothesis, Instance, Interval, JourneyKey, JourneySpan, Lead,
    Lifecycle, NextPoint, NextStopPrediction, ObservationResult, OffPath, PartSpan, Placement,
    Platform, PlatformLabel, Position, Progress, Published, Reason, RecordPlatform, SourceClass,
    SourceSemantics, SourceTrack, StopKind, StopSeen, Track, Unresolved, VehicleId, WriteEvent,
};
use obehy_core::model::{Feed, Observation};
use obehy_core::platforms::apply_platform;
use obehy_core::time::{Date, Instant, TimeDelta};
use obehy_core::timeline::estimate::{
    Lateness, dwell_credit, estimate_with, fresh, last_fix_at_stop, seen_leaving,
    waiting_is_not_early,
};
use obehy_core::timeline::fusion::apply_point;
use obehy_core::timeline::fusion::{anchor_prediction, current_lateness, note_source, with_lead};
use obehy_core::timeline::plan::PlanCache;
use obehy_core::timeline::{SourceProgress, advance, usable_delay};
use serde_json::{Map, Value, json};

fn policy() -> Policy {
    Policy::load(
        &common::repo()
            .join("src/obehy/data/realtime")
            .join(POLICY_FILE),
    )
    .unwrap()
}

fn fixture() -> Value {
    let file = std::fs::File::open(common::path("estimate.json.gz")).expect("estimate.json.gz");
    let mut text = String::new();
    flate2::read::GzDecoder::new(file)
        .read_to_string(&mut text)
        .unwrap();
    serde_json::from_str(&text).unwrap()
}

// --- reading ----------------------------------------------------------------------------------

fn fl(v: &Value) -> f64 {
    match v {
        Value::String(s) if s == "inf" => f64::INFINITY,
        Value::String(s) if s == "-inf" => f64::NEG_INFINITY,
        Value::String(s) if s == "nan" => f64::NAN,
        Value::String(s) if s == "-0.0" => -0.0,
        _ => v.as_f64().unwrap_or_else(|| panic!("a float: {v}")),
    }
}

fn opt_fl(v: &Value) -> Option<f64> {
    (!v.is_null()).then(|| fl(v))
}

fn int(v: &Value) -> i64 {
    v.as_i64().unwrap_or_else(|| panic!("an integer: {v}"))
}

fn opt_int(v: &Value) -> Option<i64> {
    (!v.is_null()).then(|| int(v))
}

fn s(v: &Value) -> String {
    v.as_str()
        .unwrap_or_else(|| panic!("a string: {v}"))
        .to_owned()
}

fn opt_s(v: &Value) -> Option<String> {
    (!v.is_null()).then(|| s(v))
}

fn b(v: &Value) -> bool {
    v.as_bool().unwrap_or_else(|| panic!("a bool: {v}"))
}

fn inst(v: &Value) -> Instant {
    Instant::from_micros(int(v))
}

fn opt_inst(v: &Value) -> Option<Instant> {
    (!v.is_null()).then(|| inst(v))
}

fn arr(v: &Value) -> &Vec<Value> {
    v.as_array().unwrap_or_else(|| panic!("an array: {v}"))
}

fn interval_in(v: &Value) -> Option<Interval> {
    (!v.is_null()).then(|| Interval::new(inst(&v[0]), inst(&v[1])))
}

fn journey_in(v: &Value) -> JourneyKey {
    JourneyKey {
        feed: s(&v[0]).parse::<Feed>().unwrap(),
        namespace: s(&v[1]),
        key: s(&v[2]),
        service_date: Date::from_isoformat(&s(&v[3])).unwrap(),
    }
}

fn platform_in(v: &Value) -> Option<Platform> {
    (!v.is_null()).then(|| Platform {
        value: s(&v[0]),
        label: PlatformLabel::parse(&s(&v[1])).unwrap(),
        boarding_point_id: opt_s(&v[2]),
        assigned_at: inst(&v[3]),
        source: s(&v[4]),
    })
}

fn call_in(v: &Value) -> CallState {
    CallState {
        sequence: int(&v[0]),
        location_id: s(&v[1]),
        visit_n: int(&v[2]) as u32,
        arrival: interval_in(&v[3]),
        departure: interval_in(&v[4]),
        estimated_arrival: opt_inst(&v[5]),
        estimated_departure: opt_inst(&v[6]),
        status: CallStatus::parse(&s(&v[7])).unwrap(),
        source_class: opt_s(&v[8]).map(|c| SourceClass::parse(&c).unwrap()),
        passed_arrival: opt_inst(&v[9]),
        passed_departure: opt_inst(&v[10]),
        passenger: b(&v[11]),
        platform: platform_in(&v[12]),
    }
}

fn hypothesis_in(v: &Value) -> Hypothesis {
    Hypothesis {
        along_m: fl(&v[0]),
        at: inst(&v[1]),
        lateness_s: fl(&v[2]),
        log_p: fl(&v[3]),
        history: arr(&v[4])
            .iter()
            .map(|p| Placement {
                along_m: fl(&p[0]),
                at: inst(&p[1]),
            })
            .collect(),
        at_us: int(&v[5]),
        off_path_since: opt_inst(&v[6]),
        reckoned_m: fl(&v[7]),
    }
}

fn track_in(v: &Value) -> Option<Track> {
    if v.is_null() {
        return None;
    }
    Some(Track {
        hypotheses: arr(&v[0]).iter().map(hypothesis_in).collect(),
        committed_m: fl(&v[1]),
        unmatched_since: opt_inst(&v[2]),
        seen_at: opt_inst(&v[3]),
        crossed_at: opt_inst(&v[4]),
        off_path: (!v[5].is_null()).then(|| OffPath {
            along_m: fl(&v[5][0]),
            at: inst(&v[5][1]),
            lateness_s: fl(&v[5][2]),
        }),
        fix: (!v[6].is_null()).then(|| Fix {
            source: s(&v[6][0]),
            lon: fl(&v[6][1]),
            lat: fl(&v[6][2]),
        }),
        at_stop: (!v[7].is_null()).then(|| StopSeen {
            call: int(&v[7][0]),
            first: inst(&v[7][1]),
            last: inst(&v[7][2]),
            away: opt_inst(&v[7][3]),
            away_m: opt_fl(&v[7][4]),
        }),
    })
}

fn lead_in(v: &Value) -> Option<Lead> {
    (!v.is_null()).then(|| Lead {
        vehicle: VehicleId {
            source: s(&v[0][0]),
            source_vehicle_id: s(&v[0][1]),
        },
        seen: inst(&v[1]),
    })
}

fn source_track_in(v: &Value, observations: &[Arc<Observation>]) -> SourceTrack {
    let facts: Option<Arc<[Fact]>> = match &v[2] {
        Value::Null => None,
        Value::Array(r) => Some(Arc::from(&observations[int(&r[1]) as usize].facts[..])),
        other => Some(Arc::from(facts_from_json(other).unwrap())),
    };
    SourceTrack {
        source: s(&v[0]),
        heard_at: inst(&v[1]),
        facts,
        next_point: (!v[3].is_null()).then(|| NextPoint {
            name: s(&v[3][0]),
            sr70: opt_s(&v[3][1]),
        }),
        delay_s: opt_int(&v[4]),
        delay_at: opt_inst(&v[5]),
        delay_reference: opt_s(&v[6]).map(|r| DelayReference::parse(&r).unwrap()),
        prediction: (!v[7].is_null()).then(|| NextStopPrediction {
            sr70: s(&v[7][0]),
            scheduled: opt_inst(&v[7][1]),
            predicted: opt_inst(&v[7][2]),
        }),
        point: opt_int(&v[8]),
        position_at: opt_inst(&v[9]),
        lead: lead_in(&v[10]),
    }
}

fn write_event_in(v: &[Value]) -> WriteEvent {
    WriteEvent {
        journey: journey_in(&v[0]),
        location_id: s(&v[1]),
        visit_n: int(&v[2]) as u32,
        kind: EventKind::parse(&s(&v[3])).unwrap(),
        interval: interval_in(&v[4]).unwrap(),
        method: EventMethod::parse(&s(&v[5])).unwrap(),
        source: s(&v[6]),
    }
}

fn instance_in(m: &Map<String, Value>, observations: &[Arc<Observation>]) -> Instance {
    let fresh = &m["fresh"];
    Instance {
        journey: journey_in(&m["journey"]),
        release_id: s(&m["release"]),
        trip_id: s(&m["trip"]),
        mode: s(&m["mode"]),
        lifecycle: Lifecycle::parse(&s(&m["life"])).unwrap(),
        calls: arr(&m["calls"]).iter().map(call_in).collect(),
        freshness: Freshness {
            updated_at: inst(&fresh[0]),
            heard_at: inst(&fresh[1]),
            stale: b(&fresh[2]),
            lost: b(&fresh[3]),
        },
        progress: (!m["prog"].is_null()).then(|| Progress {
            distance_m: fl(&m["prog"][0]),
            call_index: int(&m["prog"][1]),
            at: inst(&m["prog"][2]),
        }),
        track: track_in(&m["track"]),
        reported_delay_s: opt_int(&m["rep"]),
        lateness_s: opt_int(&m["late"]),
        off_route_since: opt_inst(&m["ors"]),
        off_route: b(&m["or"]),
        lead: lead_in(&m["lead"]),
        journeys: arr(&m["journeys"])
            .iter()
            .map(|j| JourneySpan {
                journey: journey_in(&j[0]),
                first: int(&j[1]) as usize,
                last: int(&j[2]) as usize,
            })
            .collect(),
        parts: arr(&m["parts"])
            .iter()
            .map(|p| PartSpan {
                trip_id: s(&p[0]),
                first: int(&p[1]) as usize,
                last: int(&p[2]) as usize,
            })
            .collect(),
        sources: arr(&m["sources"])
            .iter()
            .map(|t| source_track_in(t, observations))
            .collect(),
        track_source: opt_s(&m["ts"]),
        shown_source: opt_s(&m["ss"]),
        published: (!m["pub"].is_null()).then(|| {
            let p = &m["pub"];
            Published {
                lon: fl(&p[0]),
                lat: fl(&p[1]),
                bearing: opt_fl(&p[2]),
                at: inst(&p[3]),
                along_m: opt_fl(&p[4]),
                snapped: b(&p[5]),
            }
        }),
        held: arr(&m["held"])
            .iter()
            .map(|e| write_event_in(arr(e)))
            .collect(),
    }
}

fn trip_in(v: &Value) -> Arc<Trip> {
    let id = |x: &Value| -> Arc<str> { Arc::from(x.as_str().unwrap()) };
    let opt_id = |x: &Value| -> Option<Arc<str>> { x.as_str().map(Arc::from) };
    let calls: Vec<Call> = arr(&v["calls"])
        .iter()
        .map(|c| Call {
            sequence: int(&c[0]) as i32,
            location_id: id(&c[1]),
            visit_n: int(&c[2]) as u32,
            passenger_service: b(&c[3]),
            arrival: c[4].as_i64().map(|x| x as i32),
            departure: c[5].as_i64().map(|x| x as i32),
            on_request: b(&c[6]),
            point_id: opt_id(&c[7]),
        })
        .collect();
    let parts: Vec<TripPart> = arr(&v["parts"])
        .iter()
        .map(|p| TripPart {
            trip_id: id(&p[0]),
            train_number: opt_id(&p[1]),
            first: int(&p[2]) as usize,
            last: int(&p[3]) as usize,
        })
        .collect();
    Arc::new(Trip {
        trip_id: id(&v["trip_id"]),
        route_id: id(&v["route_id"]),
        route_name: id(&v["route_name"]),
        mode: id(&v["mode"]),
        service_id: id(&v["service_id"]),
        headsign: opt_id(&v["headsign"]),
        shape_id: opt_id(&v["shape_id"]),
        calls: calls.into(),
        run_key: opt_id(&v["run_key"]),
        run_part: v["run_part"].as_i64().map(|p| p as i16),
        train_number: opt_id(&v["train_number"]),
        parts: parts.into(),
        agency_id: opt_id(&v["agency_id"]),
    })
}

fn semantics_in(v: &Value) -> SourceSemantics {
    SourceSemantics {
        pre_trip_delay_is_elapsed: b(&v[0]),
        pre_trip_after_end_is_stale: b(&v[1]),
        unchanged_entry_is_no_news: b(&v[2]),
        delay_floor_minute: b(&v[3]),
    }
}

fn position_in(v: &Value) -> Option<Position> {
    (!v.is_null()).then(|| Position {
        lat: fl(&v[0]),
        lon: fl(&v[1]),
        bearing: opt_fl(&v[2]),
    })
}

/// A list of durations; empty for all zero.
fn deltas_in(v: &Value, len: usize) -> Vec<TimeDelta> {
    let values = arr(v);
    if values.is_empty() {
        return vec![TimeDelta::ZERO; len];
    }
    values
        .iter()
        .map(|x| TimeDelta::from_micros(int(x)))
        .collect()
}

/// The instances of a group as written: whole, or the fields changed against the journey's
/// previous one.
#[derive(Default)]
struct Deltas {
    last: HashMap<String, Map<String, Value>>,
}

impl Deltas {
    fn decode(&mut self, v: &Value) -> Map<String, Value> {
        let m = v.as_object().expect("an instance");
        let key = m["journey"].to_string();
        let full = if m.contains_key("~") {
            let mut full = self.last[&key].clone();
            for (name, value) in m {
                match name.as_str() {
                    "~" => {}
                    "calls~" => {
                        let calls = full.get_mut("calls").and_then(Value::as_array_mut).unwrap();
                        for (i, call) in arr(value).iter().enumerate() {
                            if !call.is_null() {
                                calls[i] = call.clone();
                            }
                        }
                    }
                    _ => {
                        full.insert(name.clone(), value.clone());
                    }
                }
            }
            full
        } else {
            m.clone()
        };
        self.last.insert(key, full.clone());
        full
    }
}

// --- writing (canonical, for comparison) ------------------------------------------------------

fn f(x: f64) -> Value {
    if x.is_nan() {
        json!("nan")
    } else if x.is_infinite() {
        json!(if x > 0.0 { "inf" } else { "-inf" })
    } else if x == 0.0 && x.is_sign_negative() {
        json!("-0.0")
    } else {
        json!(x)
    }
}

fn of(x: Option<f64>) -> Value {
    x.map_or(Value::Null, f)
}

fn t(at: Option<Instant>) -> Value {
    at.map_or(Value::Null, |a| json!(a.micros()))
}

fn ti(at: Instant) -> Value {
    json!(at.micros())
}

fn iv(i: Option<Interval>) -> Value {
    i.map_or(Value::Null, |i| json!([ti(i.lo), ti(i.hi)]))
}

fn journey_out(j: &JourneyKey) -> Value {
    json!([
        j.feed.as_str(),
        j.namespace,
        j.key,
        j.service_date.isoformat()
    ])
}

fn platform_out(p: Option<&Platform>) -> Value {
    p.map_or(Value::Null, |p| {
        let Platform {
            value,
            label,
            boarding_point_id,
            assigned_at,
            source,
        } = p;
        json!([
            value,
            label.as_str(),
            boarding_point_id,
            ti(*assigned_at),
            source
        ])
    })
}

fn call_out(c: &CallState) -> Value {
    let CallState {
        sequence,
        location_id,
        visit_n,
        arrival,
        departure,
        estimated_arrival,
        estimated_departure,
        status,
        source_class,
        passed_arrival,
        passed_departure,
        passenger,
        platform,
    } = c;
    json!([
        sequence,
        location_id,
        visit_n,
        iv(*arrival),
        iv(*departure),
        t(*estimated_arrival),
        t(*estimated_departure),
        status.as_str(),
        source_class.map(SourceClass::as_str),
        t(*passed_arrival),
        t(*passed_departure),
        passenger,
        platform_out(platform.as_ref()),
    ])
}

fn track_out(tr: Option<&Track>) -> Value {
    let Some(tr) = tr else {
        return Value::Null;
    };
    let Track {
        hypotheses,
        committed_m,
        unmatched_since,
        seen_at,
        crossed_at,
        off_path,
        fix,
        at_stop,
    } = tr;
    let hyps: Vec<Value> = hypotheses
        .iter()
        .map(|h| {
            let Hypothesis {
                along_m,
                at,
                lateness_s,
                log_p,
                history,
                at_us,
                off_path_since,
                reckoned_m,
            } = h;
            json!([
                f(*along_m),
                ti(*at),
                f(*lateness_s),
                f(*log_p),
                history
                    .iter()
                    .map(|p| json!([f(p.along_m), ti(p.at)]))
                    .collect::<Vec<_>>(),
                at_us,
                t(*off_path_since),
                f(*reckoned_m),
            ])
        })
        .collect();
    json!([
        hyps,
        f(*committed_m),
        t(*unmatched_since),
        t(*seen_at),
        t(*crossed_at),
        off_path.map_or(Value::Null, |o| json!([
            f(o.along_m),
            ti(o.at),
            f(o.lateness_s)
        ])),
        fix.as_ref()
            .map_or(Value::Null, |x| json!([x.source, f(x.lon), f(x.lat)])),
        at_stop.map_or(Value::Null, |st| json!([
            st.call,
            ti(st.first),
            ti(st.last),
            t(st.away),
            of(st.away_m)
        ])),
    ])
}

fn lead_out(l: Option<&Lead>) -> Value {
    l.map_or(Value::Null, |l| {
        json!([[l.vehicle.source, l.vehicle.source_vehicle_id], ti(l.seen)])
    })
}

fn source_track_out(st: &SourceTrack) -> Value {
    let SourceTrack {
        source,
        heard_at,
        facts,
        next_point,
        delay_s,
        delay_at,
        delay_reference,
        prediction,
        point,
        position_at,
        lead,
    } = st;
    json!([
        source,
        ti(*heard_at),
        facts.as_ref().map(|x| facts_json_text(x)),
        next_point.as_ref().map(|n| json!([n.name, n.sr70])),
        delay_s,
        t(*delay_at),
        delay_reference.map(DelayReference::as_str),
        prediction
            .as_ref()
            .map(|p| json!([p.sr70, t(p.scheduled), t(p.predicted)])),
        point,
        t(*position_at),
        lead_out(lead.as_ref()),
    ])
}

fn write_event_out(e: &WriteEvent) -> Vec<Value> {
    let WriteEvent {
        journey,
        location_id,
        visit_n,
        kind,
        interval,
        method,
        source,
    } = e;
    vec![
        journey_out(journey),
        json!(location_id),
        json!(visit_n),
        json!(kind.as_str()),
        iv(Some(*interval)),
        json!(method.as_str()),
        json!(source),
    ]
}

fn instance_out(i: &Instance) -> Value {
    let Instance {
        journey,
        release_id,
        trip_id,
        mode,
        lifecycle,
        calls,
        freshness,
        progress,
        track,
        reported_delay_s,
        lateness_s,
        off_route_since,
        off_route,
        lead,
        journeys,
        parts,
        sources,
        track_source,
        shown_source,
        published,
        held,
    } = i;
    json!({
        "journey": journey_out(journey),
        "release": release_id,
        "trip": trip_id,
        "mode": mode,
        "life": lifecycle.as_str(),
        "calls": calls.iter().map(call_out).collect::<Vec<_>>(),
        "fresh": [ti(freshness.updated_at), ti(freshness.heard_at), freshness.stale, freshness.lost],
        "prog": progress.map_or(Value::Null, |p| json!([f(p.distance_m), p.call_index, ti(p.at)])),
        "track": track_out(track.as_ref()),
        "rep": reported_delay_s,
        "late": lateness_s,
        "ors": t(*off_route_since),
        "or": off_route,
        "lead": lead_out(lead.as_ref()),
        "journeys": journeys.iter().map(|s| json!([journey_out(&s.journey), s.first, s.last])).collect::<Vec<_>>(),
        "parts": parts.iter().map(|s| json!([s.trip_id, s.first, s.last])).collect::<Vec<_>>(),
        "sources": sources.iter().map(source_track_out).collect::<Vec<_>>(),
        "ts": track_source,
        "ss": shown_source,
        "pub": published.map_or(Value::Null, |p| json!([f(p.lon), f(p.lat), of(p.bearing), ti(p.at), of(p.along_m), p.snapped])),
        "held": held.iter().map(write_event_out).collect::<Vec<_>>(),
    })
}

fn effects_out(effects: &[Effect], observation: &Arc<Observation>) -> Value {
    let mut out = Vec::new();
    for e in effects {
        out.push(match e {
            Effect::WriteEvent(w) => {
                let mut v = vec![json!("W")];
                v.extend(write_event_out(w));
                Value::Array(v)
            }
            Effect::Unresolved(Unresolved {
                observation: o,
                kind,
                name,
            }) => {
                assert!(Arc::ptr_eq(o, observation), "the observation stepped");
                json!(["U", kind.as_str(), name])
            }
            Effect::RecordPlatform(RecordPlatform {
                journey,
                location_id,
                visit_n,
                kind,
                platform,
            }) => json!([
                "P",
                journey_out(journey),
                location_id,
                visit_n,
                kind.as_str(),
                platform_out(Some(platform))
            ]),
            Effect::ObservationResult(ObservationResult {
                observation: o,
                journey,
                reason,
                public,
                state_since,
                method,
            }) => {
                assert!(Arc::ptr_eq(o, observation), "the observation stepped");
                json!([
                    "R",
                    journey.as_ref().map(journey_out),
                    reason.map(Reason::as_str),
                    public.iter().map(journey_out).collect::<Vec<_>>(),
                    t(*state_since),
                    method.as_str()
                ])
            }
            other => panic!("unexpected effect {other:?}"),
        });
    }
    Value::Array(out)
}

fn lateness_out(l: Lateness) -> Value {
    json!([l.0.map(TimeDelta::micros), l.1.map(SourceClass::as_str)])
}

fn pair_out(p: Option<(usize, Instant)>) -> Value {
    p.map_or(Value::Null, |(i, at)| json!([i, ti(at)]))
}

// --- the replay -------------------------------------------------------------------------------

struct Group<'a> {
    name: &'a str,
    indexes: Vec<Index>,
    trips: Vec<Arc<Trip>>,
    observations: Vec<Arc<Observation>>,
    plans: HashMap<usize, PlanCache>,
    progress: SourceProgress,
    deltas: Deltas,
}

#[derive(Default)]
struct Tally {
    checked: HashMap<&'static str, usize>,
    failed: Vec<String>,
}

impl Tally {
    fn check(
        &mut self,
        kind: &'static str,
        at: &str,
        what: &str,
        expected: &Value,
        actual: &Value,
    ) {
        *self.checked.entry(kind).or_default() += 1;
        let (e, a) = (expected.to_string(), actual.to_string());
        if e != a {
            if self.failed.len() < 12 {
                let cut = |x: &str| x.chars().take(3000).collect::<String>();
                self.failed.push(format!(
                    "{at} {kind} {what}:\n  python {}\n  rust   {}",
                    cut(&e),
                    cut(&a)
                ));
            } else {
                self.failed.push(String::new());
            }
        }
    }
}

impl Group<'_> {
    fn instance(&mut self, v: &Value) -> Instance {
        let full = self.deltas.decode(v);
        instance_in(&full, &self.observations)
    }
}

/// The first differing field of two instance encodings, for the failure message.
fn diff(expected: &Value, actual: &Value) -> String {
    if let (Some(e), Some(a)) = (expected.as_object(), actual.as_object()) {
        for (k, v) in e {
            if a.get(k) != Some(v) {
                if k == "calls" {
                    for (i, (x, y)) in arr(v).iter().zip(arr(&a[k])).enumerate() {
                        if x != y {
                            return format!("calls[{i}]");
                        }
                    }
                }
                return k.clone();
            }
        }
    }
    "-".into()
}

fn replay(group: &mut Group<'_>, records: &[Value], policy: &Policy, tally: &mut Tally) {
    for (n, record) in records.iter().enumerate() {
        let at = format!("{} #{n}", group.name);
        let kind = record["k"].as_str().unwrap();
        let obs = record
            .get("obs")
            .map(|o| group.observations[int(o) as usize].clone());
        match kind {
            "adv" => {
                let input = group.instance(&record["in"]);
                let expected = group.instance(&record["out"]);
                let ix = int(&record["ix"]) as usize;
                let trip = group.trips[int(&record["trip"]) as usize].clone();
                let observation = obs.unwrap();
                let before = position_in(&record["before"]);
                let plans = group.plans.entry(ix).or_default();
                let (out, effects) = advance(
                    input,
                    &trip,
                    &observation,
                    &group.indexes[ix],
                    policy,
                    plans,
                    &mut group.progress,
                    semantics_in(&record["sem"]),
                    before.as_ref(),
                )
                .unwrap();
                let (e, a) = (instance_out(&expected), instance_out(&out));
                tally.check("adv", &at, &diff(&e, &a), &e, &a);
                tally.check(
                    "adv.fx",
                    &at,
                    "",
                    &record["fx"],
                    &effects_out(&effects, &observation),
                );
            }
            "point" => {
                let input = group.instance(&record["in"]);
                let expected = group.instance(&record["out"]);
                let ix = int(&record["ix"]) as usize;
                let trip = group.trips[int(&record["trip"]) as usize].clone();
                let observation = obs.unwrap();
                let previous = (!record["prev"].is_null())
                    .then(|| source_track_in(&record["prev"], &group.observations));
                let index = &group.indexes[ix];
                let plan = group
                    .plans
                    .entry(ix)
                    .or_default()
                    .plan(&trip, input.journey.service_date, index, policy)
                    .unwrap();
                let (out, effects) = apply_point(
                    input,
                    &trip,
                    &plan,
                    &observation,
                    previous.as_ref(),
                    index,
                    policy,
                );
                let (e, a) = (instance_out(&expected), instance_out(&out));
                tally.check("point", &at, &diff(&e, &a), &e, &a);
                tally.check(
                    "point.fx",
                    &at,
                    "",
                    &record["fx"],
                    &effects_out(&effects, &observation),
                );
            }
            "note" => {
                let input = group.instance(&record["in"]);
                let observation = obs.unwrap();
                let (track, previous, news) = note_source(
                    &input,
                    &observation,
                    semantics_in(&record["sem"]),
                    opt_int(&record["delay"]),
                );
                let expected = &record["out"];
                let expected = json!([
                    source_track_out(&source_track_in(&expected[0], &group.observations)),
                    if expected[1].is_null() {
                        Value::Null
                    } else {
                        source_track_out(&source_track_in(&expected[1], &group.observations))
                    },
                    expected[2],
                ]);
                let actual = json!([
                    source_track_out(&track),
                    previous.as_ref().map_or(Value::Null, source_track_out),
                    news
                ]);
                tally.check("note", &at, "", &expected, &actual);
            }
            "usable" => {
                let input = group.instance(&record["in"]);
                let observation = obs.unwrap();
                let out = usable_delay(&input, &observation, policy, semantics_in(&record["sem"]));
                tally.check("usable", &at, "", &record["out"], &json!(out));
            }
            "lead" => {
                let input = group.instance(&record["in"]);
                let expected = group.instance(&record["out"]);
                let observation = obs.unwrap();
                let vehicle = VehicleId {
                    source: s(&record["veh"][0]),
                    source_vehicle_id: s(&record["veh"][1]),
                };
                let out = with_lead(input, &vehicle, &observation);
                let (e, a) = (instance_out(&expected), instance_out(&out));
                tally.check("lead", &at, &diff(&e, &a), &e, &a);
            }
            "est" => replay_estimate(group, record, policy, tally, &at),
            "plat" => replay_platform(group, record, tally, &at, obs.unwrap()),
            other => panic!("unknown record kind {other}"),
        }
    }
}

fn replay_estimate(
    group: &mut Group<'_>,
    record: &Value,
    policy: &Policy,
    tally: &mut Tally,
    at: &str,
) {
    let input = group.instance(&record["in"]);
    let expected = group.instance(&record["out"]);
    let ix = int(&record["ix"]) as usize;
    let trip = group.trips[int(&record["trip"]) as usize].clone();
    let predictor = Predictor::ALL
        .iter()
        .find(|(name, _)| *name == record["pred"].as_str().unwrap())
        .map(|(_, p)| *p)
        .unwrap();
    let mut changed;
    let policy = if predictor == policy.rail.predictor {
        policy
    } else {
        changed = policy.clone();
        changed.rail.predictor = predictor;
        &changed
    };
    let index = &group.indexes[ix];
    let plan = group
        .plans
        .entry(ix)
        .or_default()
        .plan(&trip, input.journey.service_date, index, policy)
        .unwrap();
    let prediction = &policy.prediction;
    let now = inst(&record["now"]);
    let current0 = current_lateness(&input);
    tally.check(
        "est.current",
        at,
        "",
        &record["cur0"],
        &lateness_out(current0),
    );
    let current = waiting_is_not_early(&input, &plan, current0);
    let current = dwell_credit(&input, &trip, &plan, current, prediction);
    tally.check("est.credit", at, "", &record["cur"], &lateness_out(current));
    let anchor = anchor_prediction(&input, &trip, index, predictor);
    let anchor = anchor.map_or(Value::Null, |(i, d)| json!([i, d.micros()]));
    tally.check("est.anchor", at, "", &record["anchor"], &anchor);
    tally.check(
        "est.fresh",
        at,
        "",
        &record["fresh"],
        &t(fresh(&input, now, prediction)),
    );
    tally.check(
        "est.dwelt",
        at,
        "",
        &record["dwelt"],
        &pair_out(last_fix_at_stop(&input, &plan)),
    );
    tally.check(
        "est.left",
        at,
        "",
        &record["left"],
        &pair_out(seen_leaving(&input, &plan, prediction)),
    );
    let calls = input.calls.len();
    let extra = deltas_in(&record["extra"], calls);
    let drives = deltas_in(&record["drives"], calls);
    let out = estimate_with(input, &trip, &plan, index, policy, now, &extra, &drives);
    let (e, a) = (instance_out(&expected), instance_out(&out));
    tally.check("est", at, &diff(&e, &a), &e, &a);
}

fn replay_platform(
    group: &mut Group<'_>,
    record: &Value,
    tally: &mut Tally,
    at: &str,
    observation: Arc<Observation>,
) {
    let ix = int(&record["ix"]) as usize;
    let mut state = FeedState::new(group.indexes[ix].feed);
    let bound: Result<Match, Reason> = match record["bind"].get("m") {
        Some(m) => Ok(Match {
            journey: journey_in(&m[0]),
            trip: group.trips[int(&m[1]) as usize].clone(),
            start: inst(&m[2]),
            end: inst(&m[3]),
        }),
        None => {
            let reason = s(&record["bind"]["r"]);
            Err(Reason::parse(&reason).unwrap())
        }
    };
    let opened = bound.as_ref().ok().map(|_| group.instance(&record["inst"]));
    let expected = bound.as_ref().ok().map(|_| group.instance(&record["out"]));
    let effects = apply_platform(
        &mut state,
        &observation,
        &mut group.indexes[ix],
        |_, _| bound.clone(),
        |state, _, found| {
            let instance = opened.expect("an instance to open");
            state.instances.insert(found.journey.clone(), instance);
            Vec::new()
        },
    );
    tally.check(
        "plat.fx",
        at,
        "",
        &record["fx"],
        &effects_out(&effects, &observation),
    );
    if let Some(expected) = expected {
        let actual = state.instances.get(&expected.journey).unwrap();
        let (e, a) = (instance_out(&expected), instance_out(actual));
        tally.check("plat", at, &diff(&e, &a), &e, &a);
        tally.check(
            "plat.dirty",
            at,
            "",
            &record["dirty"],
            &json!(state.dirty.contains(&expected.journey)),
        );
    }
}

#[test]
fn advance_fusion_estimate_and_platforms_match_python() {
    let policy = policy();
    let data = fixture();
    let mut tally = Tally::default();
    let mut corpus_groups = 0;
    for g in arr(&data["groups"]) {
        let name = g["name"].as_str().unwrap();
        let mut indexes: Vec<Index> = Vec::new();
        if let Some(corpus) = g["corpus"].as_str() {
            corpus_groups += 1;
            let path = common::repo()
                .join("tests/realtime/corpus")
                .join(format!("{corpus}.json.gz"));
            indexes.push(common::trace::from_json(&common::trace::read_gz_json(&path)).0);
        } else {
            for ix in arr(&g["indexes"]) {
                indexes.push(common::trace::from_json(ix).0);
            }
        }
        let mut group = Group {
            name,
            indexes,
            trips: arr(&g["trips"]).iter().map(trip_in).collect(),
            observations: arr(&g["observations"])
                .iter()
                .map(|o| Arc::new(common::trace::observation(o)))
                .collect(),
            plans: HashMap::new(),
            progress: SourceProgress::new(),
            deltas: Deltas::default(),
        };
        replay(&mut group, arr(&g["records"]), &policy, &mut tally);
    }
    let mut checked: Vec<_> = tally.checked.iter().collect();
    checked.sort();
    println!("{corpus_groups} corpus journeys; checked {checked:?}");
    assert!(
        corpus_groups >= 100,
        "the corpus journeys are in the fixture"
    );
    for kind in ["adv", "point", "lead", "est", "plat", "note", "usable"] {
        assert!(
            tally.checked.get(kind).copied().unwrap_or(0) > 0,
            "no {kind} records"
        );
    }
    let failures = tally.failed.len();
    let shown: Vec<&String> = tally.failed.iter().filter(|f| !f.is_empty()).collect();
    assert!(
        failures == 0,
        "{failures} mismatches; the first:\n{}",
        shown
            .iter()
            .map(|s| s.as_str())
            .collect::<Vec<_>>()
            .join("\n")
    );
}

// Keep the imports of model types used only in patterns or by name honest.
#[allow(dead_code)]
fn _types(_: BindingMethod, _: StopKind) {}
