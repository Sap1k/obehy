//! Binding, keyless inference and rail runs: the parts of `tests/unit/realtime/test_binding.py`,
//! `test_keyless.py` and `test_rail_runs.py` that `runs`, `infer::keyed` and `infer::keyless`
//! decide, over the same hand-made timetables (`common::builder`). What those tests check
//! through `core.step` beyond binding (lifecycle, events, GTFS-RT) waits for the core (R6); the
//! keyless scenarios run through a small driver doing what `core.step` does for a keyless
//! observation (advance, continued, else bind, then confirm).

mod common;

use std::collections::BTreeSet;
use std::sync::Arc;

use obehy_core::config::Policy;
use obehy_core::config::manifest::{RECORDER_FILTERS, load_sources};
use obehy_core::config::policy::POLICY_FILE;
use obehy_core::index::Index;
use obehy_core::infer::keyed::{self, Found, Match};
use obehy_core::infer::keyless::{self as keyless, Evidence, episode, score};
use obehy_core::model::{
    Binding, BindingMethod, CallState, Delay, DelayReference, Destination, Effect, Fact, Feed,
    Freshness, Instance, JourneyKey, JourneyLinkKind, Lifecycle, LineRef, NextStopName,
    Observation, PartSpan, Position, Progress, RawRef, Reason, SourceSemantics, TripKey, VehicleId,
    VehicleKey, VehicleState, VehicleStatus,
};
use obehy_core::runs;
use obehy_core::time::{Date, Instant};
use obehy_core::timeline::plan::PlanCache;

use common::builder::{
    ORIGIN, STEP_LON, Timetable, at, at2, days_between, local, timetable, with_unknown,
};

const D8: Date = Date::ymd(2026, 10, 8);
const D9: Date = Date::ymd(2026, 10, 9);
const NS: &str = "cis:line_trip";

fn policy() -> Policy {
    Policy::load(
        &common::repo()
            .join("src/obehy/data/realtime")
            .join(POLICY_FILE),
    )
    .unwrap()
}

fn jk(feed: Feed, namespace: &str, key: &str, day: Date) -> JourneyKey {
    JourneyKey {
        feed,
        namespace: namespace.into(),
        key: key.into(),
        service_date: day,
    }
}

fn duk(key: &str, day: Date) -> JourneyKey {
    jk(Feed::Jdf, NS, key, day)
}

fn bind_at(index: &mut Index, key: &str, when: &str) -> Found {
    let key = TripKey {
        namespace: NS.into(),
        key: key.into(),
    };
    keyed::bind(&key, local(when), index, &policy(), None).unwrap()
}

fn journey_of(found: &Found) -> Option<&JourneyKey> {
    match found {
        Found::Match(m) => Some(&m.journey),
        Found::Reason(_) => None,
    }
}

fn one_trip(key: &str, days: &[Date], calls: &[(&str, &str)]) -> Index {
    let mut tt = timetable(Feed::Jdf, NS);
    tt.trip(
        key,
        days,
        calls.iter().map(|(n, t)| at(n, t)).collect(),
        |s| s,
    );
    tt.index()
}

// --- test_binding.py -------------------------------------------------------------------------

#[test]
fn t2_night_trip_belongs_to_yesterdays_service_date() {
    let mut index = one_trip("582492:143", &[D8], &[("A", "23:50"), ("B", "24:30")]);
    let found = bind_at(&mut index, "582492:143", "2026-10-09 00:20");
    assert_eq!(journey_of(&found), Some(&duk("582492:143", D8)));
}

#[test]
fn t3_vehicle_waiting_for_tomorrows_first_trip() {
    let mut index = one_trip("582492:143", &[D9], &[("A", "00:05"), ("B", "00:30")]);
    let found = bind_at(&mut index, "582492:143", "2026-10-08 23:55");
    assert_eq!(journey_of(&found), Some(&duk("582492:143", D9)));
}

#[test]
fn t4_rollover_keeps_the_running_trips_service_date() {
    let days = days_between(D8, Date::ymd(2026, 10, 10));
    let mut index = one_trip("582492:143", &days, &[("A", "22:00"), ("B", "23:00")]);
    for when in ["2026-10-08 23:58", "2026-10-09 00:25"] {
        let found = bind_at(&mut index, "582492:143", when);
        assert_eq!(journey_of(&found), Some(&duk("582492:143", D8)), "{when}");
    }
}

#[test]
fn t5_duk_q4_yesterdays_key_in_the_morning_is_not_in_service() {
    let mut index = one_trip("582492:143", &[D8], &[("A", "22:00"), ("B", "22:40")]);
    let found = bind_at(&mut index, "582492:143", "2026-10-08 22:10");
    assert_eq!(journey_of(&found), Some(&duk("582492:143", D8)));
    let found = bind_at(&mut index, "582492:143", "2026-10-09 06:00");
    assert_eq!(found, Found::Reason(Reason::NotInService));
    // The binding made the evening before is no longer admissible either.
    let binding = Binding {
        vehicle: VehicleId {
            source: "duk".into(),
            source_vehicle_id: "1001".into(),
        },
        journey: duk("582492:143", D8),
        trip_id: "jdf:t1".into(),
        method: BindingMethod::Keyed,
        bound_at: local("2026-10-08 22:10"),
        confirmed: true,
    };
    let p = policy();
    assert!(keyed::admissible(&binding, local("2026-10-08 22:10"), &index, &p).unwrap());
    assert!(keyed::admissible(&binding, local("2026-10-09 00:40"), &index, &p).unwrap());
    assert!(!keyed::admissible(&binding, local("2026-10-09 00:41"), &index, &p).unwrap());
}

#[test]
fn duk_q3_off_calendar_key_is_taken_literally() {
    let weekend = Date::ymd(2026, 10, 10);
    let mut index = one_trip("300001:301", &[weekend], &[("A", "08:00"), ("B", "08:30")]);
    let found = bind_at(&mut index, "300001:301", "2026-10-08 08:05");
    assert_eq!(found, Found::Reason(Reason::NotActive));
}

#[test]
fn unknown_line_and_unknown_trip_are_told_apart() {
    let mut index = one_trip("582492:143", &[D8], &[("A", "08:00"), ("B", "08:30")]);
    with_unknown(&mut index, &[(NS, "582492:999"), (NS, "626:1")]);
    let found = bind_at(&mut index, "582492:999", "2026-10-08 08:05");
    assert_eq!(found, Found::Reason(Reason::NoTrip));
    let found = bind_at(&mut index, "626:1", "2026-10-08 08:05");
    assert_eq!(found, Found::Reason(Reason::NoLine));
    // A key never looked up is the caller's error, as Python's IndexMiss.
    let key = TripKey {
        namespace: NS.into(),
        key: "1:1".into(),
    };
    assert!(keyed::bind(&key, local("2026-10-08 08:05"), &mut index, &policy(), None).is_err());
}

#[test]
fn a_service_day_is_the_only_candidate() {
    // SŽ names the operating date: at 00:25 the evening trip of D8 still runs late, but on D9
    // only D9's trip is a candidate, 21:35 before its start: outside the window.
    let days = days_between(D8, Date::ymd(2026, 10, 10));
    let mut index = one_trip("582492:143", &days, &[("A", "22:00"), ("B", "23:00")]);
    let key = TripKey {
        namespace: NS.into(),
        key: "582492:143".into(),
    };
    let p = policy();
    let when = local("2026-10-09 00:25");
    let found = keyed::bind(&key, when, &mut index, &p, Some(D8)).unwrap();
    assert_eq!(journey_of(&found), Some(&duk("582492:143", D8)));
    let found = keyed::bind(&key, when, &mut index, &p, Some(D9)).unwrap();
    assert_eq!(found, Found::Reason(Reason::NotInService));
}

#[test]
fn two_trips_one_key_equally_close_are_ambiguous() {
    let mut tt = timetable(Feed::Jdf, NS);
    for _ in 0..2 {
        tt.trip(
            "582492:143",
            &[D8],
            vec![at("A", "08:00"), at("B", "08:30")],
            |s| s,
        );
    }
    let mut index = tt.index();
    let found = bind_at(&mut index, "582492:143", "2026-10-08 08:05");
    assert_eq!(found, Found::Reason(Reason::Ambiguous));
}

#[test]
fn the_closest_service_date_wins() {
    // At 00:05 on D9 both are in their windows: D8's 22:00-22:30 ended 95 minutes before, D9's
    // 00:30-01:00 starts in 25.
    let mut tt = timetable(Feed::Jdf, NS);
    tt.trip(
        "582492:143",
        &[D8],
        vec![at("A", "22:00"), at("B", "22:30")],
        |s| s,
    );
    tt.trip(
        "582492:143",
        &[D9],
        vec![at("A", "00:30"), at("B", "01:00")],
        |s| s,
    );
    let mut index = tt.index();
    let found = bind_at(&mut index, "582492:143", "2026-10-09 00:05");
    let Found::Match(m) = found else {
        panic!("{found:?}")
    };
    assert_eq!(m.journey, duk("582492:143", D9));
    assert_eq!(
        (m.start, m.end),
        (local("2026-10-09 00:30"), local("2026-10-09 01:00"))
    );
}

// --- test_rail_runs.py -----------------------------------------------------------------------

const TRAIN: &str = "czptt:train_number";

fn pa() -> JourneyKey {
    jk(Feed::Czptt, "czptt:pa", "PA1", D8)
}
fn j1() -> JourneyKey {
    jk(Feed::Czptt, TRAIN, "106006", D8)
}
fn j2() -> JourneyKey {
    jk(Feed::Czptt, TRAIN, "6006", D8)
}

/// A ~ X B | B C: part 1 (A..B, X a railway point) and part 2 (B..C), sharing B.
fn rail_run(number_change: bool, days: &[Date]) -> Timetable {
    let mut tt = timetable(Feed::Czptt, TRAIN);
    tt.trip(
        "106006",
        days,
        vec![
            at("A", "08:00"),
            at("~X", "08:05"),
            at2("B", "08:10", "08:10"),
        ],
        |s| {
            s.trip_id("czptt:trip:PA1:1")
                .run("PA1", 1)
                .train("106006")
                .mode("rail")
                .no_shape()
        },
    );
    let second = if number_change { "6006" } else { "106006" };
    tt.trip(
        second,
        days,
        vec![at2("B", "08:10", "08:12"), at("C", "08:20")],
        |s| {
            s.trip_id("czptt:trip:PA1:2")
                .run("PA1", 2)
                .train(second)
                .mode("rail")
                .no_shape()
                .first_sequence(3)
        },
    );
    tt.key(
        "czptt:tr",
        "Tr:1",
        &["czptt:trip:PA1:1", "czptt:trip:PA1:2"],
    );
    tt
}

fn bind_rail(index: &mut Index, namespace: &str, key: &str, when: &str) -> Found {
    let key = TripKey {
        namespace: namespace.into(),
        key: key.into(),
    };
    keyed::bind(&key, local(when), index, &policy(), None).unwrap()
}

/// An instance of `trip` (calls without events) whose vehicle has left call `left`.
fn instance_of(m: &Match, left: i64) -> Instance {
    let t0 = Instant::from_micros(0);
    Instance {
        journey: m.journey.clone(),
        release_id: "test-release".into(),
        trip_id: m.trip.trip_id.to_string(),
        mode: m.trip.mode.to_string(),
        lifecycle: Lifecycle::Running,
        calls: m
            .trip
            .calls
            .iter()
            .map(|c| CallState {
                passenger: c.passenger_service,
                ..CallState::new(i64::from(c.sequence), c.location_id.to_string(), c.visit_n)
            })
            .collect::<Vec<_>>()
            .into(),
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
        journeys: runs::journey_spans(Feed::Czptt, &m.trip, m.journey.service_date),
        parts: runs::part_spans(&m.trip),
        sources: Vec::new(),
        track_source: None,
        shown_source: None,
        published: None,
        held: Vec::new(),
    }
}

#[test]
fn r1_r2_a_run_is_one_instance_with_a_journey_per_train_number() {
    let mut index = rail_run(true, &[D8]).index();
    let found = bind_rail(&mut index, TRAIN, "6006", "2026-10-08 08:00");
    let Found::Match(m) = found else {
        panic!("{found:?}")
    };
    assert_eq!(m.journey, pa());
    // X is a railway point; B is shared by both parts and both journeys.
    let passenger: Vec<bool> = m.trip.calls.iter().map(|c| c.passenger_service).collect();
    assert_eq!(passenger, [true, false, true, true]);
    let parts: Vec<(usize, usize)> = m.trip.parts.iter().map(|p| (p.first, p.last)).collect();
    assert_eq!(parts, [(0, 2), (2, 3)]);
    let spans = runs::journey_spans(Feed::Czptt, &m.trip, D8);
    let got: Vec<(JourneyKey, usize, usize)> = spans
        .iter()
        .map(|s| (s.journey.clone(), s.first, s.last))
        .collect();
    assert_eq!(got, [(j1(), 0, 2), (j2(), 2, 3)]);
    let effects = runs::snapshots(
        &m.journey,
        &spans,
        &m.trip,
        &index,
        local("2026-10-08 08:00"),
    )
    .unwrap();
    let snapshots: Vec<(JourneyKey, String, Option<String>, usize)> = effects
        .iter()
        .filter_map(|e| match e {
            Effect::SnapshotJourney(s) => Some((
                s.journey.clone(),
                s.trip_id.clone(),
                s.run_key.clone(),
                s.calls.len(),
            )),
            _ => None,
        })
        .collect();
    assert_eq!(
        snapshots,
        [
            (j1(), "czptt:trip:PA1:1".into(), Some("PA1".into()), 3),
            (j2(), "czptt:trip:PA1:2".into(), Some("PA1".into()), 2),
        ]
    );
    let links: Vec<_> = effects
        .iter()
        .filter_map(|e| match e {
            Effect::LinkJourneys(l) => Some((l.source.clone(), l.target.clone(), l.kind)),
            _ => None,
        })
        .collect();
    assert_eq!(links, [(j1(), j2(), JourneyLinkKind::ContinuesAs)]);
}

#[test]
fn a_run_publishes_its_feed_trip_parts_and_a_road_trip_itself() {
    let mut index = rail_run(true, &[D8]).index();
    let Found::Match(m) = bind_rail(&mut index, TRAIN, "6006", "2026-10-08 08:00") else {
        panic!()
    };
    let instance = instance_of(&m, -1);
    let parts: Vec<(String, usize, usize)> = runs::published_parts(&instance)
        .into_iter()
        .map(|p| (p.trip_id, p.first, p.last))
        .collect();
    assert_eq!(
        parts,
        [
            ("czptt:trip:PA1:1".into(), 0, 2),
            ("czptt:trip:PA1:2".into(), 2, 3)
        ]
    );
    assert_eq!(runs::current_part(&instance), "czptt:trip:PA1:1"); // at A: next call in part 1
    // At the junction B (left the call before it) the next call is still B, part 1's last; once
    // B is left, part 2.
    assert_eq!(runs::current_part(&instance_of(&m, 1)), "czptt:trip:PA1:1");
    assert_eq!(runs::current_part(&instance_of(&m, 2)), "czptt:trip:PA1:2");
    // A road trip is its own single part.
    let mut road = instance_of(&m, -1);
    road.parts.clear();
    road.trip_id = "jdf:t1".into();
    assert_eq!(
        runs::published_parts(&road),
        [PartSpan {
            trip_id: "jdf:t1".into(),
            first: 0,
            last: 3
        }]
    );
}

#[test]
fn a_runs_vehicle_is_its_current_train() {
    let mut index = rail_run(true, &[D8]).index();
    let Found::Match(m) = bind_rail(&mut index, TRAIN, "6006", "2026-10-08 08:00") else {
        panic!()
    };
    assert_eq!(runs::run_vehicle_id(&instance_of(&m, -1)), "train:106006");
    assert_eq!(runs::current_train(&instance_of(&m, 1)), Some("106006"));
    assert_eq!(runs::run_vehicle_id(&instance_of(&m, 2)), "train:6006");
    // Off a run, or a run without train numbers: an opaque ID of the run's day.
    let mut plain = instance_of(&m, -1);
    plain.journeys.clear();
    assert_eq!(runs::current_train(&plain), None);
    assert_eq!(runs::run_vehicle_id(&plain), "train-1b63fa0f86838ca3");
}

#[test]
fn r1_one_train_number_is_one_journey() {
    let mut index = rail_run(false, &[D8]).index();
    let Found::Match(m) = bind_rail(&mut index, TRAIN, "106006", "2026-10-08 08:00") else {
        panic!()
    };
    let spans = runs::journey_spans(Feed::Czptt, &m.trip, D8);
    let got: Vec<JourneyKey> = spans.into_iter().map(|s| s.journey).collect();
    assert_eq!(got, [j1()]);
}

#[test]
fn r5_a_train_number_and_a_tr_bind_the_same_run() {
    let mut index = rail_run(true, &[D8]).index();
    let by_number = bind_rail(&mut index, TRAIN, "106006", "2026-10-08 08:00");
    let by_tr = bind_rail(&mut index, "czptt:tr", "Tr:1", "2026-10-08 08:01");
    assert_eq!(journey_of(&by_number), Some(&pa()));
    assert_eq!(journey_of(&by_tr), Some(&pa()));
    // The run trip is built once and cached among the trips.
    let (Found::Match(a), Found::Match(b)) = (by_number, by_tr) else {
        panic!()
    };
    assert!(Arc::ptr_eq(&a.trip, &b.trip));
    assert_eq!(&*a.trip.trip_id, "run:PA1:1,2");
}

#[test]
fn a_key_of_a_run_not_running_that_day_is_not_active() {
    let mut index = rail_run(true, &[D9]).index();
    let found = bind_rail(&mut index, TRAIN, "6006", "2026-10-07 12:00");
    assert_eq!(found, Found::Reason(Reason::NotActive));
}

#[test]
fn two_versions_of_one_part_running_the_same_day_are_ambiguous() {
    let mut index = rail_run(true, &[D8]).index();
    let second = index.trip("czptt:trip:PA1:2").unwrap().clone();
    let alias = index.pool.id("czptt:trip:PA1:2b");
    index.trips.insert(alias.clone(), second);
    let run = index.pool.id("PA1");
    let mut parts = index.runs[&run].to_vec();
    parts.push(alias);
    index.runs.insert(run, parts.into());
    let found = bind_rail(&mut index, TRAIN, "106006", "2026-10-08 08:00");
    assert_eq!(found, Found::Reason(Reason::Ambiguous));
}

// --- test_keyless.py -------------------------------------------------------------------------

const TEPLICE: &str = "Teplice,Celní";
const OUT: [&str; 6] = ["A", "B", "C", "D", "E", TEPLICE];

fn express(extra: &[(&str, Vec<(&str, String)>)]) -> Index {
    let mut tt = timetable(Feed::Jdf, NS);
    for (n, hour) in [(1, 10), (3, 11), (5, 12)] {
        let calls = OUT
            .iter()
            .enumerate()
            .map(|(i, stop)| at(stop, &format!("{hour}:{:02}", 10 * i)))
            .collect();
        tt.trip(&format!("580916:{n}"), &[D8], calls, |s| s);
    }
    for (n, hour) in [(2, 10), (4, 12)] {
        let calls = OUT
            .iter()
            .rev()
            .enumerate()
            .map(|(i, stop)| at(stop, &format!("{hour}:{:02}", 10 * i)))
            .collect();
        tt.trip(&format!("580916:{n}"), &[D8], calls, |s| s);
    }
    for (key, calls) in extra {
        let calls = calls.iter().map(|(stop, time)| at(stop, time)).collect();
        tt.trip(key, &[D8], calls, |s| s);
    }
    tt.index()
}

fn express_journey(n: u32, day: Date) -> JourneyKey {
    duk(&format!("580916:{n}"), day)
}

struct Seen<'a> {
    at: &'a str,
    following: Option<&'a str>,
    destination: &'a str,
    delay_min: Option<i64>,
    place: Option<f64>,
}

fn seen<'a>(at: &'a str, following: &'a str) -> Seen<'a> {
    Seen {
        at,
        following: Some(following),
        destination: TEPLICE,
        delay_min: Some(0),
        place: None,
    }
}

impl<'a> Seen<'a> {
    fn place(mut self, stops: f64) -> Self {
        self.place = Some(stops);
        self
    }
    fn delay(mut self, minutes: i64) -> Self {
        self.delay_min = Some(minutes);
        self
    }
    fn to(mut self, destination: &'a str) -> Self {
        self.destination = destination;
        self
    }
    fn observation(&self) -> Observation {
        let mut facts: Vec<Fact> = vec![
            Fact::VehicleKey(VehicleKey {
                source_vehicle_id: "6SA3614".into(),
            }),
            Fact::LineRef(LineRef {
                namespace: "cis:line".into(),
                value: "580916".into(),
            }),
            Fact::Destination(Destination {
                name: self.destination.into(),
            }),
        ];
        if let Some(name) = self.following {
            facts.push(Fact::NextStopName(NextStopName { name: name.into() }));
        }
        if let Some(i) = self.place {
            facts.push(Fact::Position(Position {
                lat: ORIGIN.1,
                lon: ORIGIN.0 + i * STEP_LON,
                bearing: None,
            }));
        }
        if let Some(minutes) = self.delay_min {
            facts.push(Fact::Delay(Delay {
                seconds: minutes * 60,
                reference: DelayReference::Departure,
            }));
        }
        let when = local(self.at);
        Observation {
            source: "arriva-express".into(),
            channel: "buses".into(),
            feed: Feed::Jdf,
            received_at: when,
            observed_at: Some(when),
            raw: RawRef {
                sha256: "0".repeat(64),
                item: 0,
            },
            decoder_version: 1,
            facts,
        }
    }
}

/// What `core.step` does with a keyless observation, without the timeline: advance the
/// episode, continue the binding or infer one, confirm it.
struct Driver {
    index: Index,
    policy: Policy,
    semantics: SourceSemantics,
    plans: PlanCache,
    vehicles: std::collections::BTreeMap<VehicleId, VehicleState>,
    instances: BTreeSet<JourneyKey>,
    decisions: Vec<keyless::Decision>,
}

impl Driver {
    fn new(index: Index) -> Driver {
        let sources = load_sources(
            &common::repo().join("src/obehy/data/realtime/sources"),
            &RECORDER_FILTERS,
        )
        .unwrap();
        let semantics = sources
            .iter()
            .flat_map(|s| &s.channels)
            .find(|c| c.source == "arriva-express" && c.channel == "buses")
            .expect("the arriva-express buses channel")
            .core_semantics;
        assert!(semantics.delay_floor_minute, "ARRIVA-Q3");
        Driver {
            index,
            policy: policy(),
            semantics,
            plans: PlanCache::new(),
            vehicles: Default::default(),
            instances: BTreeSet::new(),
            decisions: Vec::new(),
        }
    }

    /// The journey bound, or the reason; None for a repeated report (no news).
    fn step(&mut self, o: &Observation) -> Option<Result<JourneyKey, Reason>> {
        let vehicle = VehicleId {
            source: o.source.clone(),
            source_vehicle_id: o.first::<VehicleKey>().unwrap().source_vehicle_id.clone(),
        };
        let previous = self.vehicles.get(&vehicle).cloned();
        if let Some(p) = &previous
            && Some(p.last_seen) == o.observed_at
            && *p.facts == *o.facts
        {
            return None;
        }
        let line = o.first::<LineRef>().unwrap().clone();
        let episode = episode::advance(
            previous.as_ref().and_then(|p| p.episode.as_ref()),
            o,
            &self.semantics,
            &self.policy.keyless,
        )
        .unwrap();
        let mut binding = keyless::continued(
            previous.as_ref(),
            &episode,
            o.at(),
            &self.instances,
            &self.index,
            &self.policy,
        )
        .unwrap();
        if binding.is_none() {
            let decision = keyless::bind(
                &line,
                &episode,
                o,
                &self.index,
                &self.policy,
                &mut self.plans,
            )
            .unwrap();
            self.decisions.push(decision.clone());
            match decision.result {
                Found::Match(m) => {
                    self.instances.insert(m.journey.clone());
                    binding = Some(Binding {
                        vehicle: vehicle.clone(),
                        journey: m.journey,
                        trip_id: m.trip.trip_id.to_string(),
                        method: BindingMethod::Inferred,
                        bound_at: o.at(),
                        confirmed: false,
                    });
                }
                Found::Reason(reason) => {
                    self.set(vehicle, o, None, episode);
                    return Some(Err(reason));
                }
            }
        }
        let binding =
            keyless::confirm(&binding.unwrap(), &episode, &self.index, &self.policy).unwrap();
        let journey = binding.journey.clone();
        self.set(vehicle, o, Some(binding), episode);
        Some(Ok(journey))
    }

    fn set(
        &mut self,
        vehicle: VehicleId,
        o: &Observation,
        binding: Option<Binding>,
        episode: obehy_core::model::KeylessEpisode,
    ) {
        let state = VehicleState {
            vehicle: vehicle.clone(),
            feed: o.feed,
            status: VehicleStatus::Running,
            last_seen: o.at(),
            trip_key: None,
            binding,
            position: o.first::<Position>().copied(),
            position_at: None,
            reason: None,
            episode: Some(episode),
            facts: o.facts.clone().into(),
            since: None,
        };
        self.vehicles.insert(vehicle, state);
    }

    fn binding(&self) -> Option<&Binding> {
        self.vehicles.values().next()?.binding.as_ref()
    }

    fn run(&mut self, seen: &[Seen<'_>]) -> Vec<Result<JourneyKey, Reason>> {
        seen.iter()
            .map(|s| self.step(&s.observation()).expect("news"))
            .collect()
    }
}

fn bound(n: u32) -> Result<JourneyKey, Reason> {
    Ok(express_journey(n, D8))
}

#[test]
fn position_at_the_origin_binds_the_trip_about_to_leave() {
    let mut driver = Driver::new(express(&[]));
    let results = driver.run(&[seen("2026-10-08 10:58", "A").place(0.0)]);
    assert_eq!(results, [bound(3)]); // 10:00 is an hour late there; 12:00 too far
    let binding = driver.binding().unwrap();
    assert!(binding.method == BindingMethod::Inferred && !binding.confirmed);
    assert_eq!(driver.decisions[0].evidence, Some(Evidence::Position));
}

#[test]
fn position_alone_binds_mid_leg() {
    // Halfway C -> D at 11:25: on time for the 11:00 trip, 35 minutes late for the 10:00.
    let mut driver = Driver::new(express(&[]));
    assert_eq!(
        driver.run(&[seen("2026-10-08 11:25", "D").place(2.5)]),
        [bound(3)]
    );
}

#[test]
fn arriva_q6_destination_matches_without_commas_and_spaces() {
    let mut driver = Driver::new(express(&[]));
    let obs = seen("2026-10-08 11:25", "D")
        .place(2.5)
        .to("Teplice, Celní");
    assert_eq!(driver.run(&[obs]), [bound(3)]);
    // The opposite direction never is a candidate.
    let mut driver = Driver::new(express(&[]));
    let obs = seen("2026-10-08 11:25", "D").place(2.5).to("A");
    assert_eq!(driver.run(&[obs]), [Err(Reason::NotInService)]);
}

#[test]
fn without_position_or_departure_the_observation_is_ambiguous() {
    let mut driver = Driver::new(express(&[]));
    assert_eq!(
        driver.run(&[seen("2026-10-08 11:25", "D")]),
        [Err(Reason::Ambiguous)]
    );
    assert_eq!(driver.decisions[0].evidence, None);
}

#[test]
fn arriva_q2_q3_departures_bind_and_confirm() {
    // Untimed by position: the next-stop changes alone decide. Leaving A at 11:01 one minute
    // late (11:00 departure in [10:59:01, 11:00:00]), then B at 11:11.
    let mut driver = Driver::new(express(&[]));
    let results = driver.run(&[
        seen("2026-10-08 10:59", "A"),
        seen("2026-10-08 11:01", "B").delay(1),
        seen("2026-10-08 11:11", "C").delay(1),
    ]);
    assert_eq!(results, [Err(Reason::Ambiguous), bound(3), bound(3)]);
    assert_eq!(driver.decisions[1].evidence, Some(Evidence::Departures));
    assert_eq!(driver.decisions[1].best_s, Some(0.0));
    assert!(driver.binding().unwrap().confirmed); // two departures agree
}

#[test]
fn two_trips_minutes_apart_are_ambiguous() {
    let late: Vec<(&str, String)> = OUT
        .iter()
        .enumerate()
        .map(|(i, stop)| (*stop, format!("11:{:02}", 5 + 10 * i)))
        .collect();
    let mut driver = Driver::new(express(&[("580916:7", late)]));
    let results = driver.run(&[
        seen("2026-10-08 10:59", "A"),
        seen("2026-10-08 11:03", "B").delay(2),
    ]);
    assert_eq!(results[1], Err(Reason::Ambiguous)); // 11:00 and 11:05 both fit
}

#[test]
fn arriva_q8_names_the_trip_lacks_are_no_evidence() {
    // A motorway junction between B and C: leaving it is not a stop of the trip.
    let mut driver = Driver::new(express(&[]));
    let results = driver.run(&[
        seen("2026-10-08 10:59", "A").place(0.0),
        seen("2026-10-08 11:01", "B").delay(1).place(0.2),
        seen("2026-10-08 11:11", "křížení D8").delay(1).place(1.2),
        seen("2026-10-08 11:15", "C").delay(1).place(1.6),
    ]);
    assert_eq!(results, vec![bound(3); 4]);
    let state = driver.vehicles.values().next().unwrap();
    let episode = state.episode.as_ref().unwrap();
    let stops: Vec<&str> = episode.departures.iter().map(|d| d.stop.as_str()).collect();
    assert_eq!(stops, ["A", "B", "křížení D8"]);
    // On the trip, the junction gives no residual and keeps the order.
    let trip = driver.index.trip("jdf:t2").unwrap().clone();
    let m = Match::of(express_journey(3, D8), trip).unwrap();
    let placed = score::place(&m, episode, &driver.index, &driver.policy)
        .unwrap()
        .unwrap();
    assert_eq!(placed.residuals.len(), 2);
    assert_eq!(placed.next_stop, Some(2));
}

#[test]
fn a_stop_already_passed_contradicts_the_candidate() {
    // Left B, then A: on the outbound trip A comes before B.
    let mut driver = Driver::new(express(&[]));
    driver.run(&[
        seen("2026-10-08 11:08", "B"),
        seen("2026-10-08 11:11", "A"),
        seen("2026-10-08 11:13", "C"),
    ]);
    let state = driver.vehicles.values().next().unwrap();
    let episode = state.episode.as_ref().unwrap();
    let trip = driver.index.trip("jdf:t2").unwrap().clone();
    let m = Match::of(express_journey(3, D8), trip).unwrap();
    assert_eq!(
        score::place(&m, episode, &driver.index, &driver.policy).unwrap(),
        None
    );
}

#[test]
fn a_new_destination_infers_again() {
    let mut driver = Driver::new(express(&[]));
    let results = driver.run(&[
        seen("2026-10-08 11:25", "D").place(2.5),
        seen("2026-10-08 11:58", TEPLICE).to("A").place(5.0),
    ]);
    // Back from Teplice at 12:00.
    assert_eq!(results, [bound(3), bound(4)]);
    assert_eq!(driver.binding().unwrap().journey, express_journey(4, D8));
}

#[test]
fn a_bound_trip_continues_past_midnight_on_its_service_date() {
    let times: Vec<(&str, String)> = OUT
        .iter()
        .enumerate()
        .map(|(i, stop)| {
            let minutes = 50 + 10 * i;
            (*stop, format!("{}:{:02}", 23 + minutes / 60, minutes % 60))
        })
        .collect();
    let mut driver = Driver::new(express(&[("580916:9", times)]));
    let results = driver.run(&[
        seen("2026-10-08 23:55", "B").place(0.5),
        seen("2026-10-09 00:01", "C").place(1.1),
        seen("2026-10-09 00:11", "D").place(2.1),
        seen("2026-10-09 00:21", "E").place(3.1),
    ]);
    assert_eq!(results, vec![bound(9); 4]);
    assert_eq!(driver.decisions.len(), 1); // bound once, continued after
}

#[test]
fn arriva_q9_a_next_stop_stuck_on_the_origin_is_no_departure_time() {
    // The name stays on A while the bus drives the leg, then changes far from A: that change is
    // not when it left A. The position, anywhere on the path, places it on the 11:00 trip.
    let mut driver = Driver::new(express(&[]));
    let results = driver.run(&[
        seen("2026-10-08 11:25", "A").place(2.5),
        seen("2026-10-08 11:41", "E").place(4.1),
    ]);
    assert_eq!(results, vec![bound(3); 2]);
    // Inferred afresh, the departure from A is far from A: no time, the position decides.
    let state = driver.vehicles.values().next().unwrap();
    let episode = state.episode.as_ref().unwrap();
    let o = seen("2026-10-08 11:41", "E").place(4.1).observation();
    let line = o.first::<LineRef>().unwrap().clone();
    let decision = keyless::bind(
        &line,
        episode,
        &o,
        &driver.index,
        &driver.policy,
        &mut driver.plans,
    )
    .unwrap();
    assert_eq!(decision.evidence, Some(Evidence::Position));
    assert_eq!(journey_of(&decision.result), Some(&express_journey(3, D8)));
}

#[test]
fn arriva_q10_a_bound_trip_is_not_rescored_by_a_stale_delay() {
    // Bound on the 11:00 trip, the bus is stuck: it leaves C 25 minutes late claiming 3. The
    // departure fits no trip, but the binding holds.
    let mut driver = Driver::new(express(&[]));
    let results = driver.run(&[
        seen("2026-10-08 10:59", "A").place(0.0),
        seen("2026-10-08 11:01", "B").delay(1).place(0.2),
        seen("2026-10-08 11:11", "C").delay(1).place(1.2),
        seen("2026-10-08 11:40", "C").delay(1).place(1.9),
        seen("2026-10-08 11:45", "D").delay(3).place(2.2),
    ]);
    assert_eq!(results, vec![bound(3); 5]);
    assert_eq!(driver.decisions.len(), 1);
}

#[test]
fn a_real_arriva_express_trip_binds_from_its_first_report() {
    // 6SA3453 to Praha on 2026-10-06 (corpus 2026-10-05/06): seven trips of the line run within
    // an hour; the 11:00 one (580916:9) is the one it drives.
    let text =
        std::fs::read_to_string(common::repo().join("tests/realtime/traces/arriva-580916-9.json"))
            .unwrap();
    let (index, observations) = common::trace::from_json(&serde_json::from_str(&text).unwrap());
    let mut driver = Driver::new(index);
    let mut results: Vec<Result<JourneyKey, Reason>> =
        observations.iter().filter_map(|o| driver.step(o)).collect();
    results.dedup();
    assert_eq!(results, [Ok(express_journey(9, Date::ymd(2026, 10, 6)))]);
    let state = &driver.vehicles[&VehicleId {
        source: "arriva-express".into(),
        source_vehicle_id: "6SA3453".into(),
    }];
    assert!(state.binding.as_ref().unwrap().confirmed);
}
