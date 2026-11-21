//! `timeline/runtimes.rs`: the unit tests of `tests/unit/realtime/test_runtimes.py`, and a log
//! of `record`, `ratio`, `forget`, `running_extra` and `minimum_drives` over random journeys
//! sharing hops, replayed against Python's results (`tools/gen_timeline_fixtures.py`).

mod common;

use std::collections::VecDeque;
use std::path::PathBuf;

use obehy_core::config::Policy;
use obehy_core::config::policy::{POLICY_FILE, PredictionPolicy};
use obehy_core::model::{CallState, HopKey, Interval, Progress, Traversal};
use obehy_core::time::{Instant, TimeDelta};
use obehy_core::timeline::runtimes::{
    HopPlan, Hops, Scheduled, forget, minimum_drives, ratio, record, running_extra,
};
use serde_json::Value;

fn policy() -> Policy {
    let path: PathBuf = common::repo()
        .join("src/obehy/data/realtime")
        .join(POLICY_FILE);
    Policy::load(&path).unwrap()
}

// --- tests/unit/realtime/test_runtimes.py -----------------------------------------------------

/// 2026-10-08 08:00 Prague (06:00 UTC).
const NOW: Instant = Instant::from_unix_seconds(1_791_439_200);

fn hop() -> HopKey {
    HopKey {
        mode: "bus".into(),
        from: "jdf:A".into(),
        to: "jdf:B".into(),
        from_departure: false,
    }
}

/// Traversals ending `minutes` after 08:00 (negative: before).
fn hops(traversals: &[(i64, f64, f64)]) -> Hops {
    let mut hops = Hops::new();
    hops.insert(
        hop(),
        traversals
            .iter()
            .map(|&(minutes, taken, planned)| Traversal {
                end: NOW + TimeDelta::from_minutes(minutes),
                taken_s: taken,
                scheduled_s: planned,
            })
            .collect(),
    );
    hops
}

#[test]
fn a_hop_no_one_drove_has_no_ratio() {
    let policy = policy();
    assert_eq!(ratio(&Hops::new(), &hop(), NOW, &policy.prediction), None);
    assert_eq!(ratio(&hops(&[]), &hop(), NOW, &policy.prediction), None);
}

#[test]
fn one_traversal_moves_the_ratio_only_part_of_the_way() {
    let p = policy().prediction;
    let value = ratio(&hops(&[(0, 120.0, 60.0)]), &hop(), NOW, &p);
    let prior = p.runtime_prior_weight;
    assert_eq!(value, Some((prior + 2.0) / (prior + 1.0)));
}

#[test]
fn older_traversals_weigh_less() {
    let p = policy().prediction;
    let value = ratio(
        &hops(&[(-30, 120.0, 60.0), (0, 60.0, 60.0)]),
        &hop(),
        NOW,
        &p,
    )
    .unwrap();
    let old = (-1800.0 / p.runtime_decay_s as f64).exp();
    let prior = p.runtime_prior_weight;
    assert!((value - (prior + old * 2.0 + 1.0) / (prior + old + 1.0)).abs() < 1e-9);
}

#[test]
fn a_wild_traversal_is_clipped_and_a_future_one_ignored() {
    let p = policy().prediction;
    let value = ratio(
        &hops(&[(0, 6000.0, 60.0), (5, 60.0, 60.0)]),
        &hop(),
        NOW,
        &p,
    );
    let (prior, most) = (p.runtime_prior_weight, p.runtime_ratio_max);
    assert_eq!(value, Some((prior + most) / (prior + 1.0)));
}

#[test]
fn hops_not_driven_for_a_while_are_forgotten() {
    let p = policy().prediction;
    let mut hops = hops(&[(-120, 60.0, 60.0)]);
    forget(&mut hops, NOW, &p);
    assert!(hops.is_empty());
    assert!(p.runtime_keep_decays * p.runtime_decay_s < 7200);
}

#[test]
fn forget_reads_the_last_appended_not_the_latest() {
    // Ends are not sorted: the last appended decides, as `v[-1]` does in Python.
    let p = policy().prediction;
    let mut hops = hops(&[(0, 60.0, 60.0), (-120, 60.0, 60.0)]);
    forget(&mut hops, NOW, &p);
    assert!(hops.is_empty());
}

// --- the replayed log --------------------------------------------------------------------------

fn instant(v: &Value) -> Option<Instant> {
    v.as_i64().map(Instant::from_micros)
}

fn interval(v: &Value) -> Option<Interval> {
    (!v.is_null()).then(|| Interval::new(instant(&v[0]).unwrap(), instant(&v[1]).unwrap()))
}

fn calls(v: &Value) -> Vec<CallState> {
    v.as_array()
        .unwrap()
        .iter()
        .enumerate()
        .map(|(i, c)| {
            let mut call = CallState::new(i as i64, c[0].as_str().unwrap(), 1);
            call.passenger = c[1].as_bool().unwrap();
            call.arrival = interval(&c[2]);
            call.departure = interval(&c[3]);
            call
        })
        .collect()
}

fn scheduled(v: &Value) -> Vec<Scheduled> {
    v.as_array()
        .unwrap()
        .iter()
        .map(|s| (instant(&s[0]), instant(&s[1])))
        .collect()
}

fn key(v: &Value) -> HopKey {
    HopKey {
        mode: v[0].as_str().unwrap().into(),
        from: v[1].as_str().unwrap().into(),
        to: v[2].as_str().unwrap().into(),
        from_departure: v[3].as_bool().unwrap(),
    }
}

fn check_hops(hops: &Hops, expected: &Value, step: usize) {
    let expected = expected.as_array().unwrap();
    assert_eq!(hops.len(), expected.len(), "step {step}: hop count");
    for ((k, traversals), e) in hops.iter().zip(expected) {
        assert_eq!(*k, key(&e[0]), "step {step}: hop order");
        let want: VecDeque<Traversal> = e[1]
            .as_array()
            .unwrap()
            .iter()
            .map(|t| Traversal {
                end: instant(&t[0]).unwrap(),
                taken_s: t[1].as_f64().unwrap(),
                scheduled_s: t[2].as_f64().unwrap(),
            })
            .collect();
        assert_eq!(*traversals, want, "step {step}: {k:?}");
    }
}

struct TestPlan {
    scheduled: Vec<Scheduled>,
    zones: Vec<(Option<f64>, Option<f64>)>,
    distances: Vec<f64>,
}

impl HopPlan for TestPlan {
    fn scheduled(&self, call: usize) -> Scheduled {
        self.scheduled[call]
    }
    fn arrival_m(&self, call: usize) -> Option<f64> {
        self.zones[call].0
    }
    fn departure_m(&self, call: usize) -> Option<f64> {
        self.zones[call].1
    }
    fn call_distance_m(&self, call: usize) -> f64 {
        self.distances[call]
    }
}

fn micros(values: &[TimeDelta]) -> Vec<i64> {
    values.iter().map(|d| d.micros()).collect()
}

fn ints(v: &Value) -> Vec<i64> {
    v.as_array().unwrap().iter().map(common::int).collect()
}

fn check_policy(p: &PredictionPolicy, expected: &Value) {
    assert_eq!(p.runtime_decay_s, common::int(&expected["runtime_decay_s"]));
    assert_eq!(
        p.runtime_keep_decays,
        common::int(&expected["runtime_keep_decays"])
    );
    assert_eq!(
        p.runtime_prior_weight.to_bits(),
        expected["runtime_prior_weight"].as_f64().unwrap().to_bits()
    );
    assert_eq!(
        p.runtime_ratio_max.to_bits(),
        expected["runtime_ratio_max"].as_f64().unwrap().to_bits()
    );
}

#[test]
fn a_log_of_journeys_learns_the_same_runtimes_as_python() {
    let fixture = common::json("runtimes.json");
    let policy = policy();
    let p = &policy.prediction;
    check_policy(p, &fixture["policy"]);
    let mut hops = Hops::new();
    let mut counts = [0usize; 4];
    for (step, entry) in fixture["log"].as_array().unwrap().iter().enumerate() {
        match entry["op"].as_str().unwrap() {
            "record" => {
                record(
                    &mut hops,
                    entry["mode"].as_str().unwrap(),
                    &calls(&entry["calls"]),
                    &calls(&entry["before"]),
                    &scheduled(&entry["scheduled"]),
                    p,
                );
                counts[0] += 1;
            }
            "ratios" => {
                let now = instant(&entry["now"]).unwrap();
                let expected = entry["ratios"].as_array().unwrap();
                assert_eq!(expected.len(), hops.len(), "step {step}");
                for e in expected {
                    let actual = ratio(&hops, &key(&e[0]), now, p);
                    let want = e[1].as_f64();
                    assert_eq!(
                        actual.map(f64::to_bits),
                        want.map(f64::to_bits),
                        "step {step}: {:?}",
                        e[0]
                    );
                }
                if !entry["hops"].is_null() {
                    check_hops(&hops, &entry["hops"], step);
                }
                counts[1] += 1;
            }
            "forget" => {
                forget(&mut hops, instant(&entry["now"]).unwrap(), p);
                check_hops(&hops, &entry["hops"], step);
                counts[2] += 1;
            }
            "plan" => {
                let now = instant(&entry["now"]).unwrap();
                let mode = entry["mode"].as_str().unwrap();
                let calls = calls(&entry["calls"]);
                let progress = (!entry["progress"].is_null()).then(|| Progress {
                    distance_m: entry["progress"][1].as_f64().unwrap(),
                    call_index: common::int(&entry["progress"][0]),
                    at: now,
                });
                let plan = TestPlan {
                    scheduled: scheduled(&entry["scheduled"]),
                    zones: entry["zones"]
                        .as_array()
                        .unwrap()
                        .iter()
                        .map(|z| (z[0].as_f64(), z[1].as_f64()))
                        .collect(),
                    distances: entry["call_distances"]
                        .as_array()
                        .unwrap()
                        .iter()
                        .map(|d| d.as_f64().unwrap())
                        .collect(),
                };
                let extra = running_extra(mode, &calls, progress.as_ref(), &plan, &hops, now, p);
                assert_eq!(micros(&extra), ints(&entry["extra"]), "step {step}: extra");
                let speed = entry["speed_mps"].as_f64().unwrap();
                let drives = minimum_drives(mode, &calls, &plan, &hops, now, p, speed);
                assert_eq!(
                    micros(&drives),
                    ints(&entry["drives"]),
                    "step {step}: drives"
                );
                counts[3] += 1;
            }
            other => panic!("unknown op {other}"),
        }
    }
    assert!(counts.iter().all(|&c| c > 0), "{counts:?}");
    assert!(
        fixture["log"]
            .as_array()
            .unwrap()
            .iter()
            .any(|e| e["op"] == "plan" && ints(&e["extra"]).iter().any(|&x| x != 0)),
        "some hop runs off the timetable"
    );
}
