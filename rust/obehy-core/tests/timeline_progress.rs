//! `timeline/{plan,stops,commit,progress}.rs` against Python (`tools/gen_progress_fixtures.py`):
//! every call the Python core made into the four modules while stepping the corpus journeys and
//! the unit scenarios of `test_progress.py`, `test_stops.py` and `test_timeline.py`, replayed
//! here with the same inputs and compared bit for bit; and every plan those calls read, rebuilt
//! from its path and schedule and queried at probe points.

mod common;

use std::collections::HashMap;
use std::io::Read;
use std::sync::Arc;

use obehy_core::config::Policy;
use obehy_core::config::policy::{POLICY_FILE, ProgressPolicy};
use obehy_core::index::{Call, Trip};
use obehy_core::model::{
    EventKind, Fix, Hypothesis, Interval, OffPath, Placement, Position, StopSeen, Track,
};
use obehy_core::time::{Date, Instant};
use obehy_core::timeline::commit::{Committed, Crossing, commit, terminus};
use obehy_core::timeline::path::{Path, make_path};
use obehy_core::timeline::plan::{Plan, build_plan, scheduled_instants};
use obehy_core::timeline::progress::{Step, at_origin_or_moving, past_first_stop, update};
use obehy_core::timeline::stops::{capped, departure, done_at, seen};
use serde_json::{Value, json};

fn policy() -> Policy {
    Policy::load(
        &common::repo()
            .join("src/obehy/data/realtime")
            .join(POLICY_FILE),
    )
    .unwrap()
}

fn fixture() -> Value {
    let file = std::fs::File::open(common::path("progress.json.gz")).expect("progress.json.gz");
    let mut text = String::new();
    flate2::read::GzDecoder::new(file)
        .read_to_string(&mut text)
        .unwrap();
    serde_json::from_str(&text).unwrap()
}

// --- reading the fixture ----------------------------------------------------------------------

fn fl(v: &Value) -> f64 {
    match v {
        Value::String(s) if s == "inf" => f64::INFINITY,
        Value::String(s) if s == "-inf" => f64::NEG_INFINITY,
        Value::String(s) if s == "nan" => f64::NAN,
        _ => v.as_f64().unwrap_or_else(|| panic!("a float: {v}")),
    }
}

fn opt_fl(v: &Value) -> Option<f64> {
    (!v.is_null()).then(|| fl(v))
}

fn int(v: &Value) -> i64 {
    v.as_i64().unwrap_or_else(|| panic!("an integer: {v}"))
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

fn placement_in(v: &Value) -> Placement {
    Placement {
        along_m: fl(&v[0]),
        at: inst(&v[1]),
    }
}

fn hypothesis_in(v: &Value) -> Hypothesis {
    Hypothesis {
        along_m: fl(&v[0]),
        at: inst(&v[1]),
        lateness_s: fl(&v[2]),
        log_p: fl(&v[3]),
        history: arr(&v[4]).iter().map(placement_in).collect(),
        at_us: int(&v[5]),
        off_path_since: opt_inst(&v[6]),
        reckoned_m: fl(&v[7]),
    }
}

fn stop_in(v: &Value) -> Option<StopSeen> {
    (!v.is_null()).then(|| StopSeen {
        call: int(&v[0]),
        first: inst(&v[1]),
        last: inst(&v[2]),
        away: opt_inst(&v[3]),
        away_m: opt_fl(&v[4]),
    })
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
            source: v[6][0].as_str().unwrap().to_owned(),
            lon: fl(&v[6][1]),
            lat: fl(&v[6][2]),
        }),
        at_stop: stop_in(&v[7]),
    })
}

fn position_in(v: &Value) -> Option<Position> {
    (!v.is_null()).then(|| Position {
        lat: fl(&v[0]),
        lon: fl(&v[1]),
        bearing: opt_fl(&v[2]),
    })
}

fn date(text: &str) -> Date {
    Date::from_isoformat(text).unwrap()
}

// --- writing Rust's results in the fixture's layout -------------------------------------------

fn f(x: f64) -> Value {
    if x.is_finite() {
        json!(x)
    } else if x.is_nan() {
        json!("nan")
    } else if x > 0.0 {
        json!("inf")
    } else {
        json!("-inf")
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

fn interval(i: Option<Interval>) -> Value {
    i.map_or(Value::Null, |i| json!([ti(i.lo), ti(i.hi)]))
}

fn hypothesis(h: &Hypothesis) -> Value {
    json!([
        f(h.along_m),
        ti(h.at),
        f(h.lateness_s),
        f(h.log_p),
        h.history
            .iter()
            .map(|p| json!([f(p.along_m), ti(p.at)]))
            .collect::<Vec<_>>(),
        h.at_us,
        t(h.off_path_since),
        f(h.reckoned_m),
    ])
}

fn stop(s: Option<&StopSeen>) -> Value {
    s.map_or(Value::Null, |s| {
        json!([s.call, ti(s.first), ti(s.last), t(s.away), of(s.away_m)])
    })
}

fn track(tr: &Track) -> Value {
    json!([
        tr.hypotheses.iter().map(hypothesis).collect::<Vec<_>>(),
        f(tr.committed_m),
        t(tr.unmatched_since),
        t(tr.seen_at),
        t(tr.crossed_at),
        tr.off_path.map_or(Value::Null, |o| json!([
            f(o.along_m),
            ti(o.at),
            f(o.lateness_s)
        ])),
        tr.fix
            .as_ref()
            .map_or(Value::Null, |x| json!([x.source, f(x.lon), f(x.lat)])),
        stop(tr.at_stop.as_ref()),
    ])
}

fn crossing(c: &Crossing) -> Value {
    json!([
        c.call,
        c.kind.as_str(),
        interval(Some(c.fixes)),
        ti(c.at),
        c.method.map(|m| m.as_str()),
        interval(c.interval),
    ])
}

fn step(s: &Step) -> Value {
    json!([
        track(&s.track),
        s.progress.map_or(Value::Null, |p| json!([
            f(p.distance_m),
            p.call_index,
            ti(p.at)
        ])),
        s.crossed.iter().map(crossing).collect::<Vec<_>>(),
        t(s.off_route_since),
        s.off_route,
    ])
}

fn committed(c: &Committed) -> Value {
    json!([
        c.beam.iter().map(hypothesis).collect::<Vec<_>>(),
        f(c.committed_m),
        c.crossed.iter().map(crossing).collect::<Vec<_>>(),
        t(c.crossed_at),
    ])
}

/// Bit-exact comparison: floats by their IEEE bits, integers exactly, everything else equal.
/// Returns the path of the first difference.
fn differs(actual: &Value, expected: &Value, at: &str) -> Option<String> {
    match (actual, expected) {
        (Value::Number(a), Value::Number(e)) => {
            let same = if e.is_f64() {
                a.is_f64() && a.as_f64().unwrap().to_bits() == e.as_f64().unwrap().to_bits()
            } else {
                !a.is_f64() && a.as_i64() == e.as_i64()
            };
            (!same).then(|| format!("{at}: {actual} != {expected}"))
        }
        (Value::Array(a), Value::Array(e)) => {
            if a.len() != e.len() {
                return Some(format!("{at}: length {} != {}", a.len(), e.len()));
            }
            a.iter()
                .zip(e)
                .enumerate()
                .find_map(|(i, (x, y))| differs(x, y, &format!("{at}[{i}]")))
        }
        _ => (actual != expected).then(|| format!("{at}: {actual} != {expected}")),
    }
}

fn check(actual: &Value, expected: &Value, what: &str) {
    if let Some(difference) = differs(actual, expected, "") {
        panic!("{what}: {difference}");
    }
}

// --- plans -------------------------------------------------------------------------------------

fn path_in(v: &Value) -> Path {
    let points = arr(&v[0]).iter().map(|p| (fl(&p[0]), fl(&p[1]))).collect();
    let distances = arr(&v[1]).iter().map(fl).collect();
    let calls = arr(&v[2]).iter().map(fl).collect();
    make_path(points, distances, calls, v[3].as_bool().unwrap())
}

fn trip_of(trip_id: &str, seconds: &[Value]) -> Trip {
    let id = |s: &str| Arc::<str>::from(s);
    let opt = |v: &Value| v.as_i64().map(|s| s as i32);
    Trip {
        trip_id: id(trip_id),
        route_id: id("r"),
        route_name: id("r"),
        mode: id("bus"),
        service_id: id("s"),
        headsign: None,
        shape_id: None,
        calls: seconds
            .iter()
            .enumerate()
            .map(|(i, s)| Call {
                sequence: i as i32,
                location_id: id("l"),
                point_id: None,
                arrival: opt(&s[0]),
                departure: opt(&s[1]),
                visit_n: 1,
                passenger_service: true,
                on_request: false,
            })
            .collect(),
        run_key: None,
        run_part: None,
        train_number: None,
        parts: Box::new([]),
        agency_id: None,
    }
}

fn kind_str(kind: EventKind) -> &'static str {
    kind.as_str()
}

/// Rebuild one plan and check it against Python's: zones, triggers and the queries at the
/// probe points.
fn plan_of(v: &Value, policy: &Policy, what: &str) -> Arc<Plan> {
    let path = Arc::new(path_in(&v["path"]));
    let scheduled: Vec<_> = arr(&v["scheduled"])
        .iter()
        .map(|s| (opt_inst(&s[0]), opt_inst(&s[1])))
        .collect();
    if !v["trip"].is_null() {
        let trip = trip_of(v["trip"][0].as_str().unwrap(), arr(&v["trip"][2]));
        let day = date(v["trip"][1].as_str().unwrap());
        assert_eq!(
            scheduled_instants(&trip, day),
            scheduled,
            "{what}: scheduled_instants"
        );
    }
    let plan = build_plan(path, scheduled, policy);
    let zones = Value::Array(
        plan.zones
            .iter()
            .map(|z| json!([of(z.arrival_m), of(z.departure_m)]))
            .collect(),
    );
    check(&zones, &v["zones"], &format!("{what}: zones"));
    let triggers = Value::Array(
        plan.triggers
            .iter()
            .map(|x| json!([f(x.along_m), x.call, kind_str(x.kind)]))
            .collect(),
    );
    check(&triggers, &v["triggers"], &format!("{what}: triggers"));

    let probes = &v["probes"];
    let moments: Vec<Instant> = arr(&probes["moments"]).iter().map(inst).collect();
    let tt = &plan.timetable;
    for (r, row) in arr(&probes["rows"]).iter().enumerate() {
        let along = fl(&row[0]);
        let window = tt.window(along);
        let actual = json!([
            f(along),
            window.map_or(Value::Null, |w| json!([t(w.earliest), t(w.latest)])),
            moments
                .iter()
                .map(|&m| f(tt.lateness_s(m, along)))
                .collect::<Vec<_>>(),
            moments
                .iter()
                .map(|&m| t(tt.planned(m, along)))
                .collect::<Vec<_>>(),
            plan.call_index_at(along),
            plan.at_stop(along),
            plan.dwelling_call(along),
        ]);
        check(&actual, row, &format!("{what}: probe {r}"));
    }
    for (b, row) in arr(&probes["between"]).iter().enumerate() {
        let (lo, hi) = (fl(&row[0]), fl(&row[1]));
        let found = plan.triggers_between(lo, hi);
        let first = (!found.is_empty()).then(|| {
            plan.triggers
                .iter()
                .position(|x| x == &found[0])
                .expect("a trigger of the plan")
        });
        let actual = json!([row[0], row[1], found.len(), first]);
        check(&actual, row, &format!("{what}: triggers_between {b}"));
    }
    if !probes["departure_m"].is_null() {
        check(
            &f(plan.departure_m()),
            &probes["departure_m"],
            &format!("{what}: departure_m"),
        );
    }
    let leaving = Value::Array(
        (0..plan.zones.len())
            .map(|i| of(plan.leaving_m(i)))
            .collect(),
    );
    check(&leaving, &probes["leaving"], &format!("{what}: leaving_m"));
    let scheduled_time = Value::Array(
        (0..plan.zones.len())
            .map(|i| {
                json!([
                    t(plan.scheduled_time(i, EventKind::Arrival)),
                    t(plan.scheduled_time(i, EventKind::Departure))
                ])
            })
            .collect(),
    );
    check(
        &scheduled_time,
        &probes["scheduled_time"],
        &format!("{what}: scheduled_time"),
    );
    Arc::new(plan)
}

// --- the replay --------------------------------------------------------------------------------

struct Policies {
    base: ProgressPolicy,
    by_sigma: HashMap<u64, ProgressPolicy>,
}

impl Policies {
    fn get(&mut self, sigma: &Value) -> &ProgressPolicy {
        let sigma = fl(sigma);
        let base = &self.base;
        self.by_sigma
            .entry(sigma.to_bits())
            .or_insert_with(|| ProgressPolicy {
                gps_sigma_m: sigma,
                ..base.clone()
            })
    }
}

#[test]
fn the_tracker_matches_python_call_for_call() {
    let fixture = fixture();
    let policy = policy();
    assert_eq!(
        policy.progress.gps_sigma_m.to_bits(),
        fl(&fixture["policy"]["gps_sigma_m"]).to_bits(),
        "the fixture's policy"
    );
    let mut policies = Policies {
        base: policy.progress.clone(),
        by_sigma: HashMap::new(),
    };
    let mut counts: HashMap<String, usize> = HashMap::new();
    let mut plans_checked = 0;
    let groups = arr(&fixture["groups"]);
    for group in groups {
        let name = group["name"].as_str().unwrap();
        let plans: Vec<Arc<Plan>> = arr(&group["plans"])
            .iter()
            .enumerate()
            .map(|(i, p)| plan_of(p, &policy, &format!("{name} plan {i}")))
            .collect();
        plans_checked += plans.len();
        let mut outputs: HashMap<usize, Track> = HashMap::new();
        for (k, call) in arr(&group["calls"]).iter().enumerate() {
            let c = arr(call);
            let function = c[0].as_str().unwrap();
            *counts.entry(function.to_owned()).or_default() += 1;
            let plan = &plans[int(&c[1]) as usize];
            let what = format!("{name} call {k} ({function})");
            let expected = c.last().unwrap();
            match function {
                "update" => {
                    let previous = if c[2].get(0).and_then(Value::as_str) == Some("ref") {
                        let k = int(&c[2][1]) as usize;
                        Some(outputs.get(&k).expect("an earlier update's track").clone())
                    } else {
                        track_in(&c[2])
                    };
                    let position = position_in(&c[3]).unwrap();
                    let progress = policies.get(&c[7]).clone();
                    let out = update(
                        plan,
                        previous.as_ref(),
                        &position,
                        inst(&c[4]),
                        c[5].as_str().unwrap(),
                        c[6].as_str().unwrap(),
                        &progress,
                        fl(&c[8]),
                        fl(&c[9]),
                        fl(&c[10]),
                        position_in(&c[11]).as_ref(),
                    );
                    check(&step(&out), expected, &what);
                    outputs.insert(k, out.track);
                }
                "commit" => {
                    let beam = arr(&c[2]).iter().map(hypothesis_in).collect();
                    let progress = policies.get(&c[6]).clone();
                    let out = commit(
                        plan,
                        beam,
                        fl(&c[3]),
                        opt_inst(&c[4]),
                        inst(&c[5]),
                        &progress,
                        fl(&c[7]),
                        stop_in(&c[8]).as_ref(),
                        c[9].as_bool().unwrap(),
                    );
                    check(&committed(&out), expected, &what);
                }
                "seen" => {
                    let out = seen(
                        plan,
                        stop_in(&c[2]).as_ref(),
                        fl(&c[3]),
                        (fl(&c[4][0]), fl(&c[4][1])),
                        inst(&c[5]),
                        fl(&c[6]),
                        int(&c[7]),
                        fl(&c[8]),
                        opt_inst(&c[9]),
                    );
                    check(&stop(out.as_ref()), expected, &what);
                }
                "capped" => {
                    let out = capped(plan, stop_in(&c[2]).as_ref(), fl(&c[3]));
                    check(&f(out), expected, &what);
                }
                "done_at" => {
                    let out = done_at(plan, int(&c[2]) as usize, fl(&c[3]));
                    check(&json!(out), expected, &what);
                }
                "terminus" => {
                    let history: Vec<Placement> = arr(&c[2]).iter().map(placement_in).collect();
                    let out = terminus(plan, &history, policies.get(&c[3]));
                    check(&interval(out), expected, &what);
                }
                "departure" => {
                    let out = departure(plan, &stop_in(&c[2]).unwrap(), fl(&c[3]));
                    check(&ti(out), expected, &what);
                }
                "origin" => {
                    let progress = policies.get(&c[5]).clone();
                    let out = at_origin_or_moving(
                        plan,
                        position_in(&c[2]).as_ref(),
                        &position_in(&c[3]).unwrap(),
                        inst(&c[4]),
                        &progress,
                        fl(&c[6]),
                    );
                    check(&json!(out), expected, &what);
                }
                "past" => {
                    let progress = policies.get(&c[3]).clone();
                    let out = past_first_stop(plan, &position_in(&c[2]).unwrap(), &progress);
                    check(&json!(out), expected, &what);
                }
                other => panic!("{what}: unknown call {other}"),
            }
        }
    }
    let mut summary: Vec<_> = counts.into_iter().collect();
    summary.sort();
    println!(
        "{} groups, {plans_checked} plans, calls {summary:?}",
        groups.len()
    );
    for function in [
        "update",
        "commit",
        "seen",
        "capped",
        "done_at",
        "terminus",
        "departure",
        "origin",
    ] {
        assert!(
            summary.iter().any(|(f, n)| f == function && *n > 0),
            "no {function} call in the fixture"
        );
    }
    assert!(
        groups
            .iter()
            .any(|g| g["name"].as_str().unwrap().starts_with("corpus/"))
            && groups
                .iter()
                .any(|g| g["name"].as_str().unwrap().starts_with("test/")),
        "the corpus and the unit scenarios"
    );
}
