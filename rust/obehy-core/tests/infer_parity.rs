//! `runs`, `infer::keyed` and `infer::keyless` against Python (R5): every call
//! `rust/tools/gen_infer_fixtures.py` recorded while the Python core stepped the corpus journeys
//! and an Arriva Express window (plus probes at shifted times) is made again here, in order, on
//! the same trace's index, and its output encoded as the generator encodes Python's must be
//! equal (floats bit for bit).

mod common;

use std::collections::BTreeSet;
use std::sync::Arc;

use obehy_core::config::Policy;
use obehy_core::config::policy::POLICY_FILE;
use obehy_core::index::{Index, IndexMiss};
use obehy_core::infer::keyed::{self, Found, Match};
use obehy_core::infer::keyless::score::{self, Placed};
use obehy_core::infer::keyless::{self as keyless, Decision, candidates, episode};
use obehy_core::model::{
    Binding, BindingMethod, CallState, Departure, Effect, Feed, Freshness, Instance, Interval,
    JourneyKey, JourneySpan, KeylessEpisode, Lifecycle, LineRef, Observation, PartSpan, Position,
    Progress, SourceSemantics, TripKey, VehicleId, VehicleState, VehicleStatus,
};
use obehy_core::runs;
use obehy_core::time::{Date, Instant};
use obehy_core::timeline::plan::PlanCache;
use serde_json::{Value, json};

use common::trace::{from_json, observation, read_gz_json};

// --- decoding the fixture ------------------------------------------------------------------

fn s(v: &Value) -> &str {
    v.as_str().unwrap_or_else(|| panic!("not a string: {v}"))
}

fn at(v: &Value) -> Instant {
    Instant::from_micros(v.as_i64().expect("microseconds"))
}

fn date(v: &Value) -> Date {
    Date::from_isoformat(s(v)).expect("an ISO date")
}

fn float(v: &Value) -> f64 {
    common::bits(s(v))
}

fn opt_float(v: &Value) -> Option<f64> {
    v.as_str().map(common::bits)
}

fn feed(v: &Value) -> Feed {
    s(v).parse().expect("a feed")
}

fn journey(v: &Value) -> JourneyKey {
    JourneyKey {
        feed: feed(&v[0]),
        namespace: s(&v[1]).to_owned(),
        key: s(&v[2]).to_owned(),
        service_date: date(&v[3]),
    }
}

fn spans(v: &Value) -> Vec<JourneySpan> {
    v.as_array()
        .unwrap()
        .iter()
        .map(|x| JourneySpan {
            journey: journey(&x[0]),
            first: x[1].as_u64().unwrap() as usize,
            last: x[2].as_u64().unwrap() as usize,
        })
        .collect()
}

fn parts(v: &Value) -> Vec<PartSpan> {
    v.as_array()
        .unwrap()
        .iter()
        .map(|x| PartSpan {
            trip_id: s(&x[0]).to_owned(),
            first: x[1].as_u64().unwrap() as usize,
            last: x[2].as_u64().unwrap() as usize,
        })
        .collect()
}

fn binding(v: &Value) -> Option<Binding> {
    if v.is_null() {
        return None;
    }
    Some(Binding {
        vehicle: VehicleId {
            source: s(&v["v"][0]).to_owned(),
            source_vehicle_id: s(&v["v"][1]).to_owned(),
        },
        journey: journey(&v["j"]),
        trip_id: s(&v["t"]).to_owned(),
        method: BindingMethod::parse(s(&v["m"])).expect("a binding method"),
        bound_at: at(&v["b"]),
        confirmed: v["c"].as_bool().unwrap(),
    })
}

fn episode_of(v: &Value) -> Option<KeylessEpisode> {
    if v.is_null() {
        return None;
    }
    let departures = v["deps"]
        .as_array()
        .unwrap()
        .iter()
        .map(|d| Departure {
            stop: s(&d["stop"]).to_owned(),
            left_at: at(&d["left"]),
            scheduled: d["sched"]
                .as_array()
                .map(|i| Interval::new(at(&i[0]), at(&i[1]))),
            position: d["pos"].as_array().map(|p| Position {
                lat: float(&p[0]),
                lon: float(&p[1]),
                bearing: opt_float(&p[2]),
            }),
        })
        .collect();
    Some(KeylessEpisode {
        line: s(&v["line"]).to_owned(),
        destination: s(&v["dest"]).to_owned(),
        started_at: at(&v["start"]),
        last_at: at(&v["last"]),
        next_stop: v["next"].as_str().map(str::to_owned),
        departures,
    })
}

fn line(v: &Value) -> LineRef {
    LineRef {
        namespace: s(&v[0]).to_owned(),
        value: s(&v[1]).to_owned(),
    }
}

fn the_match(v: &Value, index: &Index) -> Match {
    Match {
        journey: journey(&v["j"]),
        trip: index.trip(s(&v["t"])).expect("a loaded trip").clone(),
        start: at(&v["s"]),
        end: at(&v["e"]),
    }
}

fn placed(v: &Value) -> Placed {
    Placed {
        residuals: v["r"].as_array().unwrap().iter().map(opt_float).collect(),
        next_stop: v["n"].as_u64().map(|n| n as usize),
    }
}

// --- encoding Rust's outputs as the generator encodes Python's -----------------------------

fn bits(x: Option<f64>) -> Value {
    x.map_or(Value::Null, |x| json!(format!("{:016x}", x.to_bits())))
}

fn enc_journey(j: &JourneyKey) -> Value {
    json!([
        j.feed.as_str(),
        j.namespace,
        j.key,
        j.service_date.isoformat()
    ])
}

fn enc_match(m: &Match) -> Value {
    json!({"j": enc_journey(&m.journey), "t": &*m.trip.trip_id, "s": m.start.micros(), "e": m.end.micros()})
}

fn enc_found(f: &Found) -> Value {
    match f {
        Found::Match(m) => json!({"m": enc_match(m)}),
        Found::Reason(r) => json!({"r": r.as_str()}),
    }
}

fn enc_binding(b: Option<&Binding>) -> Value {
    let Some(b) = b else { return Value::Null };
    json!({
        "v": [b.vehicle.source, b.vehicle.source_vehicle_id],
        "j": enc_journey(&b.journey),
        "t": b.trip_id,
        "m": b.method.as_str(),
        "b": b.bound_at.micros(),
        "c": b.confirmed,
    })
}

fn enc_episode(e: Option<&KeylessEpisode>) -> Value {
    let Some(e) = e else { return Value::Null };
    let deps: Vec<Value> = e
        .departures
        .iter()
        .map(|d| {
            json!({
                "stop": d.stop,
                "left": d.left_at.micros(),
                "sched": d.scheduled.map_or(Value::Null, |i| json!([i.lo.micros(), i.hi.micros()])),
                "pos": d.position.map_or(Value::Null, |p| json!([bits(Some(p.lat)), bits(Some(p.lon)), bits(p.bearing)])),
            })
        })
        .collect();
    json!({
        "line": e.line,
        "dest": e.destination,
        "start": e.started_at.micros(),
        "last": e.last_at.micros(),
        "next": e.next_stop,
        "deps": deps,
    })
}

fn enc_spans(items: &[JourneySpan]) -> Value {
    items
        .iter()
        .map(|x| json!([enc_journey(&x.journey), x.first, x.last]))
        .collect()
}

fn enc_parts(items: &[PartSpan]) -> Value {
    items
        .iter()
        .map(|x| json!([x.trip_id, x.first, x.last]))
        .collect()
}

fn enc_effect(e: &Effect) -> Value {
    match e {
        Effect::LinkJourneys(l) => json!({"link": [
            enc_journey(&l.source),
            enc_journey(&l.target),
            l.kind.as_str()
        ]}),
        Effect::SnapshotJourney(n) => {
            let calls: Vec<Value> = n
                .calls
                .iter()
                .map(|c| {
                    json!([
                        c.ordinal,
                        c.location_id,
                        c.visit_n,
                        c.passenger_service,
                        c.scheduled_arrival,
                        c.scheduled_departure,
                        c.name
                    ])
                })
                .collect();
            json!({
                "j": enc_journey(&n.journey),
                "at": n.at.micros(),
                "rel": n.release_id,
                "t": n.trip_id,
                "rn": n.route_name,
                "hs": n.headsign,
                "calls": calls,
                "rk": n.run_key,
                "rid": n.route_id,
                "mode": n.mode,
                "ag": n.agency_id,
            })
        }
        other => panic!("runs.snapshots made {other:?}"),
    }
}

fn enc_placed(p: Option<&Placed>) -> Value {
    let Some(p) = p else { return Value::Null };
    let residuals: Vec<Value> = p.residuals.iter().map(|r| bits(*r)).collect();
    json!({"r": residuals, "n": p.next_stop})
}

fn enc_decision(d: &Decision) -> Value {
    json!({
        "res": enc_found(&d.result),
        "n": d.candidates,
        "ev": d.evidence.map(|e| e.as_str()),
        "best": bits(d.best_s),
        "ru": bits(d.runner_up_s),
    })
}

fn or_miss(result: Result<Value, IndexMiss>) -> Value {
    result.unwrap_or_else(|_| json!({"miss": true}))
}

// --- the instance runs.py reads --------------------------------------------------------------

/// An instance whose `left_index()` is `left`, with the recorded journeys and parts.
fn instance(r: &Value) -> Instance {
    let j = journey(&r["j"]);
    let t0 = Instant::from_micros(0);
    let n = r["n"].as_u64().unwrap() as usize;
    let left = r["left"].as_i64().unwrap();
    let calls: Vec<CallState> = (0..n)
        .map(|i| CallState::new(i as i64, format!("c{i}"), 1))
        .collect();
    Instance {
        journey: j,
        release_id: "r".into(),
        trip_id: s(&r["t"]).to_owned(),
        mode: "bus".into(),
        lifecycle: Lifecycle::Running,
        calls: calls.into(),
        freshness: Freshness {
            updated_at: t0,
            heard_at: t0,
            stale: false,
            lost: false,
        },
        progress: (left >= 0).then_some(Progress {
            distance_m: 0.0,
            call_index: left,
            at: t0,
        }),
        track: None,
        reported_delay_s: None,
        lateness_s: None,
        off_route_since: None,
        off_route: false,
        lead: None,
        journeys: spans(&r["spans"]),
        parts: parts(&r["parts"]),
        sources: Vec::new(),
        track_source: None,
        shown_source: None,
        published: None,
        held: Vec::new(),
    }
}

fn vehicle_state(prev: &Value) -> Option<VehicleState> {
    if prev.is_null() {
        return None;
    }
    let b = binding(&prev["b"]);
    let vehicle = b.as_ref().map_or_else(
        || VehicleId {
            source: "x".into(),
            source_vehicle_id: "x".into(),
        },
        |b| b.vehicle.clone(),
    );
    Some(VehicleState {
        vehicle,
        feed: Feed::Jdf,
        status: VehicleStatus::Running,
        last_seen: Instant::from_micros(0),
        trip_key: None,
        binding: b,
        position: None,
        position_at: None,
        reason: None,
        episode: episode_of(&prev["e"]),
        facts: Arc::from(Vec::new()),
        since: None,
    })
}

// --- replay ------------------------------------------------------------------------------------

struct Replay<'a> {
    index: Index,
    observations: Vec<Observation>,
    policy: &'a Policy,
    plans: PlanCache,
}

impl Replay<'_> {
    fn obs(&self, v: &Value) -> Observation {
        match v.as_u64() {
            Some(n) => self.observations[n as usize].clone(),
            None => observation(v),
        }
    }

    fn call(&mut self, r: &Value) -> Value {
        let policy = self.policy;
        match s(&r["f"]) {
            "keyed.bind" => {
                let key = TripKey {
                    namespace: s(&r["key"][0]).to_owned(),
                    key: s(&r["key"][1]).to_owned(),
                };
                let day = r["day"].as_str().map(|d| Date::from_isoformat(d).unwrap());
                or_miss(
                    keyed::bind(&key, at(&r["at"]), &mut self.index, policy, day)
                        .map(|f| enc_found(&f)),
                )
            }
            "keyed.admissible" => {
                let b = binding(&r["b"]).unwrap();
                or_miss(keyed::admissible(&b, at(&r["at"]), &self.index, policy).map(|x| json!(x)))
            }
            "keyed.span" => {
                let trip = self.index.trip(s(&r["t"])).unwrap().clone();
                let (a, b) = keyed::span(&trip, &journey(&r["j"])).unwrap();
                json!([a.micros(), b.micros()])
            }
            "keyed.running" => {
                let days: Vec<Date> = r["days"].as_array().unwrap().iter().map(date).collect();
                let found = keyed::running(&self.index, s(&r["ns"]), s(&r["key"]), &days).unwrap();
                found
                    .iter()
                    .map(|(d, t)| json!([d.isoformat(), &*t.trip_id]))
                    .collect()
            }
            "runs.run_journey" => {
                let trip = self.index.trip(s(&r["t"])).unwrap().clone();
                enc_journey(&runs::run_journey(feed(&r["feed"]), &trip, date(&r["day"])))
            }
            "runs.journey_spans" => {
                let trip = self.index.trip(s(&r["t"])).unwrap().clone();
                enc_spans(&runs::journey_spans(
                    feed(&r["feed"]),
                    &trip,
                    date(&r["day"]),
                ))
            }
            "runs.part_spans" => {
                let trip = self.index.trip(s(&r["t"])).unwrap().clone();
                enc_parts(&runs::part_spans(&trip))
            }
            "runs.snapshots" => {
                let trip = self.index.trip(s(&r["t"])).unwrap().clone();
                let made = runs::snapshots(
                    &journey(&r["j"]),
                    &spans(&r["spans"]),
                    &trip,
                    &self.index,
                    at(&r["at"]),
                )
                .unwrap();
                made.iter().map(enc_effect).collect()
            }
            "runs.instance" => {
                let inst = instance(r);
                json!({
                    "parts": enc_parts(&runs::published_parts(&inst)),
                    "part": runs::current_part(&inst),
                    "train": runs::current_train(&inst),
                    "vid": runs::run_vehicle_id(&inst),
                })
            }
            "episode.advance" => {
                let prev = episode_of(&r["prev"]);
                let o = self.obs(&r["o"]);
                let sem = &r["sem"];
                let semantics = SourceSemantics {
                    pre_trip_delay_is_elapsed: sem[0].as_bool().unwrap(),
                    pre_trip_after_end_is_stale: sem[1].as_bool().unwrap(),
                    unchanged_entry_is_no_news: sem[2].as_bool().unwrap(),
                    delay_floor_minute: sem[3].as_bool().unwrap(),
                };
                let out = episode::advance(prev.as_ref(), &o, &semantics, &policy.keyless);
                enc_episode(out.as_ref())
            }
            "keyless.bind" => {
                let e = episode_of(&r["ep"]).unwrap();
                let o = self.obs(&r["o"]);
                or_miss(
                    keyless::bind(
                        &line(&r["line"]),
                        &e,
                        &o,
                        &self.index,
                        policy,
                        &mut self.plans,
                    )
                    .map(|d| enc_decision(&d)),
                )
            }
            "keyless.continued" => {
                let prev = vehicle_state(&r["prev"]);
                let mut held: BTreeSet<JourneyKey> = BTreeSet::new();
                if let Some(b) = prev.as_ref().and_then(|p| p.binding.as_ref())
                    && r["prev"]["held"].as_bool().unwrap()
                {
                    held.insert(b.journey.clone());
                }
                let e = episode_of(&r["ep"]).unwrap();
                let out =
                    keyless::continued(prev.as_ref(), &e, at(&r["at"]), &held, &self.index, policy)
                        .unwrap();
                enc_binding(out.as_ref())
            }
            "keyless.confirm" => {
                let b = binding(&r["b"]).unwrap();
                let e = episode_of(&r["ep"]).unwrap();
                let out = keyless::confirm(&b, &e, &self.index, policy).unwrap();
                enc_binding(Some(&out))
            }
            "keyless.candidates" => {
                let out = candidates::candidates(
                    &line(&r["line"]),
                    s(&r["dest"]),
                    at(&r["at"]),
                    &self.index,
                    &policy.keyless,
                );
                match out {
                    Ok(Ok(found)) => json!({"ms": found.iter().map(enc_match).collect::<Vec<_>>()}),
                    Ok(Err(reason)) => json!({"r": reason.as_str()}),
                    Err(_) => json!({"miss": true}),
                }
            }
            "score.place" => {
                let m = the_match(&r["m"], &self.index);
                let e = episode_of(&r["ep"]).unwrap();
                let out = score::place(&m, &e, &self.index, policy).unwrap();
                enc_placed(out.as_ref())
            }
            "score.position_residual" => {
                let m = the_match(&r["m"], &self.index);
                let p = placed(&r["placed"]);
                let o = self.obs(&r["o"]);
                let out =
                    score::position_residual(&m, &p, &o, &self.index, policy, &mut self.plans)
                        .unwrap();
                bits(out)
            }
            other => panic!("unknown record {other}"),
        }
    }
}

#[test]
fn infer_and_runs_match_python_on_the_corpus_and_a_keyless_window() {
    let policy = Policy::load(
        &common::repo()
            .join("src/obehy/data/realtime")
            .join(POLICY_FILE),
    )
    .unwrap();
    let fixture = read_gz_json(&common::path("infer/calls.json.gz"));
    assert_eq!(s(&fixture["policy"]), policy.version);
    let mut checked: std::collections::BTreeMap<String, usize> = Default::default();
    let mut failures: Vec<String> = Vec::new();
    let mut mismatches = 0;
    for trace in fixture["traces"].as_array().unwrap() {
        let name = s(&trace["trace"]);
        let data = read_gz_json(&common::repo().join(name));
        let (index, observations) = from_json(&data);
        let mut replay = Replay {
            index,
            observations,
            policy: &policy,
            plans: PlanCache::new(),
        };
        for (n, record) in trace["calls"].as_array().unwrap().iter().enumerate() {
            let f = s(&record["f"]).to_owned();
            let got = replay.call(record);
            *checked.entry(f.clone()).or_default() += 1;
            if got != record["out"] {
                mismatches += 1;
                if failures.len() < 20 {
                    failures.push(format!(
                        "{name} #{n} {f}:\n  record {record}\n  rust   {got}"
                    ));
                }
            }
        }
    }
    assert!(
        failures.is_empty(),
        "{mismatches} mismatches, the first:\n{}",
        failures.join("\n")
    );
    eprintln!("checked {checked:?}");
    for f in [
        "keyed.bind",
        "keyed.admissible",
        "keyed.span",
        "keyed.running",
        "runs.run_journey",
        "runs.journey_spans",
        "runs.part_spans",
        "runs.snapshots",
        "runs.instance",
        "episode.advance",
        "keyless.bind",
        "keyless.continued",
        "keyless.confirm",
        "keyless.candidates",
        "score.place",
        "score.position_residual",
    ] {
        assert!(checked.get(f).copied().unwrap_or(0) > 0, "no {f} record");
    }
}
