//! Scenarios of `tests/unit/realtime/test_progress.py`, `test_stops.py` and `test_timeline.py`
//! at the level of the four modules: a journey's plan (`PlanCache` over an index) and its fixes
//! stepped straight through `progress::update`, as `timeline.advance` steps them for a precise
//! source. The whole-core versions (lifecycle, estimates, history effects) come with the core
//! (R6); every call these scenarios make into the modules is also replayed against Python bit for
//! bit in `timeline_progress.rs`.

mod common;

use std::sync::Arc;

use obehy_core::config::Policy;
use obehy_core::config::policy::POLICY_FILE;
use obehy_core::index::{Call, Index, Location, Shape, Trip};
use obehy_core::model::{EventKind, EventMethod, Feed, Position, Track};
use obehy_core::time::{Date, Instant, ServiceTime};
use obehy_core::timeline::commit::{Crossing, terminus};
use obehy_core::timeline::plan::{MAX_PLANS, Plan, PlanCache, stop_zones};
use obehy_core::timeline::progress::{at_origin_or_moving, update};

const DAY: Date = Date::ymd(2026, 10, 8);
const ORIGIN: (f64, f64) = (14.0, 50.0);
const M_LON: f64 = 1.0 / 71_474.0;
const M_LAT: f64 = 1.0 / 111_195.0;

fn policy() -> Policy {
    Policy::load(
        &common::repo()
            .join("src/obehy/data/realtime")
            .join(POLICY_FILE),
    )
    .unwrap()
}

fn point(east: f64, north: f64) -> (f64, f64) {
    (ORIGIN.0 + east * M_LON, ORIGIN.1 + north * M_LAT)
}

/// "08:02" or "08:02:30" on the scenarios' service date, local time.
fn at(clock: &str) -> Instant {
    ServiceTime::new(DAY, seconds(clock)).instant()
}

fn seconds(clock: &str) -> i32 {
    let parts: Vec<i32> = clock.split(':').map(|p| p.parse().unwrap()).collect();
    parts[0] * 3600 + parts[1] * 60 + parts.get(2).copied().unwrap_or(0)
}

/// A trip over named stops (east, north metres from the origin), without a shape: the
/// stop-to-stop chord. Calls: (stop, arrival[, departure]).
fn route(stops: &[(&str, f64, f64)], calls: &[(&str, &str, Option<&str>)]) -> (Index, Arc<Trip>) {
    route_with(stops, calls, false)
}

/// `route`, optionally with a straight shape through the calls' points (the builder's default
/// shape).
fn route_with(
    stops: &[(&str, f64, f64)],
    calls: &[(&str, &str, Option<&str>)],
    shaped: bool,
) -> (Index, Arc<Trip>) {
    let mut index = Index::new("release", Feed::Jdf);
    for &(name, east, north) in stops {
        let (lon, lat) = point(east, north);
        let id: Arc<str> = Arc::from(format!("jdf:{name}").as_str());
        index.locations.insert(
            id.clone(),
            Location {
                location_id: id,
                name: Arc::from(name),
                lon: Some(lon),
                lat: Some(lat),
            },
        );
    }
    let mut visits = std::collections::HashMap::new();
    let trip = Arc::new(Trip {
        trip_id: Arc::from("trip"),
        route_id: Arc::from("route"),
        route_name: Arc::from("143"),
        mode: Arc::from("bus"),
        service_id: Arc::from("service"),
        headsign: None,
        shape_id: shaped.then(|| Arc::from("trip:shape")),
        calls: calls
            .iter()
            .enumerate()
            .map(|(i, &(stop, arrival, departure))| {
                let visit = visits.entry(stop).or_insert(0);
                *visit += 1;
                Call {
                    sequence: i as i32 + 1,
                    location_id: Arc::from(format!("jdf:{stop}").as_str()),
                    point_id: None,
                    arrival: Some(seconds(arrival)),
                    departure: Some(seconds(departure.unwrap_or(arrival))),
                    visit_n: *visit,
                    passenger_service: true,
                    on_request: false,
                }
            })
            .collect(),
        run_key: None,
        run_part: None,
        train_number: None,
        parts: Box::new([]),
        agency_id: None,
    });
    if shaped {
        let points: Vec<(f64, f64)> = calls
            .iter()
            .map(|(stop, _, _)| {
                let &(_, east, north) = stops.iter().find(|s| s.0 == *stop).unwrap();
                point(east, north)
            })
            .collect();
        let distances = obehy_core::geo::cumulative_m(&points);
        index.shapes.insert(
            Arc::from("trip:shape"),
            Arc::new(Shape {
                shape_id: Arc::from("trip:shape"),
                points: points.into(),
                distances_m: distances.into(),
            }),
        );
    }
    index.trips.insert(trip.trip_id.clone(), trip.clone());
    (index, trip)
}

fn straight() -> (Index, Arc<Trip>) {
    route(
        &[("A", 0.0, 0.0), ("B", 1000.0, 0.0), ("C", 2000.0, 0.0)],
        &[
            ("A", "08:00", None),
            ("B", "08:02", None),
            ("C", "08:04", None),
        ],
    )
}

const ZAVLEK_STOPS: &[(&str, f64, f64)] = &[
    ("A", 0.0, 0.0),
    ("B", 1000.0, 0.0),
    ("C", 1000.0, 800.0),
    ("D", 2000.0, 0.0),
];
const ZAVLEK_CALLS: &[(&str, &str, Option<&str>)] = &[
    ("A", "08:00", None),
    ("B", "08:02", None),
    ("C", "08:04", None),
    ("B", "08:06", None),
    ("D", "08:08", None),
];
const ZAVLEK_DRIVE: &[(&str, f64, f64)] = &[
    ("08:00", 0.0, 0.0),
    ("08:02", 1000.0, 0.0),
    ("08:04", 1000.0, 800.0),
    ("08:06", 1000.0, 0.0),
    ("08:08", 2000.0, 0.0),
];

type FixSpec = (Instant, f64, f64, Option<f64>);

/// Fixes every `every_s` seconds along straight lines between timed waypoints (the Python
/// tests' `drive`).
fn drive(waypoints: &[(&str, f64, f64)], every_s: i64, bearing: bool) -> Vec<FixSpec> {
    let mut fixes = Vec::new();
    for pair in waypoints.windows(2) {
        let ((c0, e0, n0), (c1, e1, n1)) = (pair[0], pair[1]);
        let (t0, t1) = (at(c0), at(c1));
        let span = (t1 - t0).micros() / 1_000_000;
        let steps = (span / every_s).max(1);
        let heading = ((e1, n1) != (e0, n0)).then(|| {
            obehy_core::py::num::py_fmod(obehy_core::geo::degrees((e1 - e0).atan2(n1 - n0)), 360.0)
        });
        for k in 0..steps {
            let share = k as f64 / steps as f64;
            let moment = t0 + (t1 - t0).mul_f64(share);
            fixes.push((
                Instant::from_micros(moment.micros() / 1_000_000 * 1_000_000),
                e0 + (e1 - e0) * share,
                n0 + (n1 - n0) * share,
                if bearing { heading } else { None },
            ));
        }
    }
    let (c, e, n) = waypoints[waypoints.len() - 1];
    fixes.push((at(c), e, n, None));
    fixes
}

/// One vehicle's fixes stepped through the tracker as `timeline.advance` steps a precise
/// source's: every crossing committed, in order.
struct Tracker<'a> {
    plan: Arc<Plan>,
    policy: &'a Policy,
    track: Option<Track>,
    before: Option<Position>,
    crossed: Vec<Crossing>,
}

impl<'a> Tracker<'a> {
    fn new(route: &(Index, Arc<Trip>), policy: &'a Policy) -> Tracker<'a> {
        let mut plans = PlanCache::new();
        let plan = plans.plan(&route.1, DAY, &route.0, policy).unwrap();
        Tracker {
            plan,
            policy,
            track: None,
            before: None,
            crossed: Vec::new(),
        }
    }

    fn run(&mut self, fixes: &[FixSpec]) -> &mut Self {
        for &(when, east, north, bearing) in fixes {
            let (lon, lat) = point(east, north);
            let position = Position { lat, lon, bearing };
            if self
                .track
                .as_ref()
                .and_then(|t| t.seen_at)
                .is_some_and(|s| when <= s)
            {
                continue; // repeated or out of order: no news
            }
            let step = update(
                &self.plan,
                self.track.as_ref(),
                &position,
                when,
                "bus",
                "duk",
                &self.policy.progress,
                self.policy.prediction.dwell_credit_s.get("bus"),
                self.policy.lifecycle.departure_margin_m,
                self.policy.lifecycle.terminal_radius_m,
                self.before.as_ref(),
            );
            self.crossed.extend(step.crossed);
            self.track = Some(step.track);
            self.before = Some(position);
        }
        self
    }

    fn events(&self) -> Vec<(usize, EventKind)> {
        self.crossed.iter().map(|c| (c.call, c.kind)).collect()
    }

    fn arrivals(&self) -> Vec<usize> {
        self.crossed
            .iter()
            .filter(|c| c.kind == EventKind::Arrival)
            .map(|c| c.call)
            .collect()
    }

    fn times_sorted(&self) -> bool {
        self.crossed.windows(2).all(|w| w[0].at <= w[1].at)
    }
}

#[test]
fn straight_run() {
    let policy = policy();
    let route = straight();
    let mut d = Tracker::new(&route, &policy);
    d.run(&drive(
        &[
            ("08:00", 0.0, 0.0),
            ("08:02", 1000.0, 0.0),
            ("08:04", 2000.0, 0.0),
        ],
        15,
        false,
    ));
    assert_eq!(
        d.events(),
        vec![
            (0, EventKind::Departure),
            (1, EventKind::Arrival),
            (1, EventKind::Departure),
            (2, EventKind::Arrival),
        ]
    );
    assert!(d.times_sorted());
    assert!(
        d.crossed
            .iter()
            .all(|c| c.method == Some(EventMethod::Progress))
    );
}

#[test]
fn zavlek_out_and_back_with_and_without_bearing() {
    let policy = policy();
    for bearing in [false, true] {
        let route = route(ZAVLEK_STOPS, ZAVLEK_CALLS);
        let mut d = Tracker::new(&route, &policy);
        d.run(&drive(ZAVLEK_DRIVE, 15, bearing));
        assert_eq!(d.arrivals(), vec![1, 2, 3, 4], "bearing {bearing}");
        assert!(d.times_sorted());
    }
}

#[test]
fn first_fix_inside_a_zavlek_is_resolved_by_the_next_fixes() {
    let policy = policy();
    let route = route(ZAVLEK_STOPS, ZAVLEK_CALLS);
    let fixes: Vec<FixSpec> = drive(ZAVLEK_DRIVE, 15, false)
        .into_iter()
        .filter(|f| f.0 >= at("08:05:00"))
        .collect();
    let mut d = Tracker::new(&route, &policy);
    d.run(&fixes);
    assert_eq!(d.arrivals(), vec![3, 4]);
}

#[test]
fn early_and_late_running_are_followed_not_rejected() {
    let policy = policy();
    let route = straight();
    let mut early = Tracker::new(&route, &policy);
    early.run(&drive(
        &[
            ("07:50", 0.0, 0.0),
            ("07:52", 1000.0, 0.0),
            ("07:54", 2000.0, 0.0),
        ],
        15,
        false,
    ));
    assert_eq!(early.arrivals(), vec![1, 2]);
    let mut late = Tracker::new(&route, &policy);
    late.run(&drive(
        &[
            ("08:00", 0.0, 0.0),
            ("08:02", 1000.0, 0.0),
            ("08:17", 1000.0, 0.0),
            ("08:19", 2000.0, 0.0),
        ],
        15,
        false,
    ));
    assert_eq!(late.arrivals(), vec![1, 2]);
}

/// test_timeline's geometry: stops `STEP_LON` (0.014 degrees) apart, and `east(m)` a point
/// `m` metres east of A on its scale (`m * STEP_LON / 1003.5` degrees), in this file's metres.
const STEP_M: f64 = 0.014 * 71_474.0;

fn east(metres: f64) -> f64 {
    metres * 0.014 / 1003.5 * 71_474.0
}

const JITTER: [f64; 19] = [
    -15.855051165015386,
    -31.423574346794826,
    13.58410257358684,
    -38.48073419992115,
    3.2293803876020277,
    -12.087997477867304,
    -39.78009677027639,
    0.6692159870478207,
    -41.62539074022136,
    -5.971888470385274,
    -38.7130118782843,
    -36.835828799052145,
    -6.793272977173743,
    29.41669122048343,
    -33.85782349653189,
    -24.90849318536869,
    11.468990016503035,
    40.29380482113051,
    6.939265375574877,
];

#[test]
fn a_bus_standing_in_gps_jitter_has_not_left() {
    // test_stops: three minutes at B with fixes scattered up to 45 m along the road; forward
    // jitter must not carry the reading over the departure trigger. The scatter is Python's
    // `random.Random(7).uniform(-45, 45)`, as the Python test draws it.
    let policy = policy();
    let route = route_with(
        &[
            ("A", 0.0, 0.0),
            ("B", STEP_M, 0.0),
            ("C", 2.0 * STEP_M, 0.0),
        ],
        &[
            ("A", "08:00", None),
            ("B", "08:05", None),
            ("C", "08:10", None),
        ],
        true,
    );
    let mut d = Tracker::new(&route, &policy);
    d.run(&[
        (at("08:00:00"), 0.0, 0.0, None),
        (at("08:02:30"), east(500.0), 0.0, None),
        (at("08:04:40"), east(900.0), 0.0, None),
    ]);
    let mut moment = seconds("08:05:00");
    for noise in JITTER {
        assert!(moment <= seconds("08:08:00"));
        d.run(&[(
            ServiceTime::new(DAY, moment).instant(),
            east(1000.0 + noise),
            0.0,
            None,
        )]);
        assert!(
            !d.events().contains(&(1, EventKind::Departure)),
            "left B at {moment}"
        );
        moment += 10;
    }
    d.run(&[
        (at("08:08:10"), east(1150.0), 0.0, None),
        (at("08:08:20"), east(1300.0), 0.0, None),
    ]);
    let left = d
        .crossed
        .iter()
        .find(|c| c.call == 1 && c.kind == EventKind::Departure)
        .expect("B's departure");
    assert!(at("08:08:00") <= left.at && left.at <= at("08:08:10"));
    assert!(left.fixes.lo >= at("08:08:00") && left.fixes.hi <= at("08:08:10"));
    assert!(d.times_sorted());
}

#[test]
fn committed_progress_never_goes_back_and_jitter_makes_no_duplicate_event() {
    let policy = policy();
    let route = straight();
    let mut d = Tracker::new(&route, &policy);
    let mut fixes = drive(&[("08:00", 0.0, 0.0), ("08:02", 1000.0, 0.0)], 15, false);
    // Jitter back and forth around B, then on.
    for (k, east) in [990.0, 1010.0, 985.0, 1015.0, 995.0]
        .into_iter()
        .enumerate()
    {
        fixes.push((
            at("08:02") + obehy_core::time::TimeDelta::from_seconds(10 * (k as i64 + 1)),
            east,
            0.0,
            None,
        ));
    }
    fixes.extend(drive(
        &[("08:03", 1000.0, 0.0), ("08:05", 2000.0, 0.0)],
        15,
        false,
    ));
    let mut committed = f64::NEG_INFINITY;
    for fix in &fixes {
        d.run(std::slice::from_ref(fix));
        let now = d.track.as_ref().unwrap().committed_m;
        assert!(now >= committed);
        committed = now;
    }
    let mut events = d.events();
    let n = events.len();
    events.dedup();
    assert_eq!(events.len(), n, "no duplicate event");
    assert!(d.times_sorted());
}

#[test]
fn lost_near_the_terminus_its_arrival_is_inferred_only_when_close() {
    let policy = policy();
    let line = straight();
    let mut near = Tracker::new(&line, &policy);
    near.run(&drive(
        &[
            ("08:00", 0.0, 0.0),
            ("08:02", 1000.0, 0.0),
            ("08:03:30", 1750.0, 0.0),
        ],
        15,
        false,
    ));
    let history = &near.track.as_ref().unwrap().hypotheses[0].history;
    let arrived = terminus(&near.plan, history, &policy.progress).expect("an inferred arrival");
    assert!(arrived.lo >= history[history.len() - 1].at && arrived.lo <= arrived.hi);

    let far = route(
        &[("A", 0.0, 0.0), ("B", 1000.0, 0.0), ("C", 20_000.0, 0.0)],
        &[
            ("A", "08:00", None),
            ("B", "08:02", None),
            ("C", "08:40", None),
        ],
    );
    let mut d = Tracker::new(&far, &policy);
    d.run(&drive(
        &[
            ("08:00", 0.0, 0.0),
            ("08:02", 1000.0, 0.0),
            ("08:04", 2000.0, 0.0),
        ],
        15,
        false,
    ));
    let history = &d.track.as_ref().unwrap().hypotheses[0].history;
    assert_eq!(terminus(&d.plan, history, &policy.progress), None);
}

#[test]
fn first_seen_just_past_the_origin_its_departure_is_inferred() {
    let policy = policy();
    let route = straight();
    let mut d = Tracker::new(&route, &policy);
    d.run(&drive(
        &[("08:00:30", 250.0, 0.0), ("08:02", 1000.0, 0.0)],
        15,
        false,
    ));
    let origin = d.crossed.first().expect("the origin's departure");
    assert_eq!((origin.call, origin.kind), (0, EventKind::Departure));
    assert_eq!(origin.method, Some(EventMethod::Inferred));
    assert!(origin.at <= at("08:00:30"));
}

#[test]
fn a_vehicle_on_a_stand_past_the_first_stop_has_not_started() {
    // DUK-Q19: standing 300 m past the first stop, or driving to it over the trip's later
    // roads half an hour early, is not running.
    let policy = policy();
    let route = straight();
    let mut plans = PlanCache::new();
    let plan = plans.plan(&route.1, DAY, &route.0, &policy).unwrap();
    let fix = |east: f64| {
        let (lon, lat) = point(east, 0.0);
        Position {
            lat,
            lon,
            bearing: None,
        }
    };
    let progress = &policy.progress;
    let terminal = policy.lifecycle.terminal_radius_m;
    assert!(at_origin_or_moving(
        &plan,
        None,
        &fix(0.0),
        at("07:59"),
        progress,
        terminal
    ));
    assert!(!at_origin_or_moving(
        &plan,
        Some(&fix(300.0)),
        &fix(300.0),
        at("07:30"),
        progress,
        terminal
    ));
    assert!(!at_origin_or_moving(
        &plan,
        Some(&fix(1500.0)),
        &fix(1900.0),
        at("07:31"),
        progress,
        terminal
    ));
    assert!(at_origin_or_moving(
        &plan,
        Some(&fix(300.0)),
        &fix(1000.0),
        at("08:01"),
        progress,
        terminal
    ));
}

#[test]
fn close_stops_trigger_at_the_midpoint_and_events_follow_call_order() {
    let policy = policy();
    let route = route(
        &[
            ("A", 0.0, 0.0),
            ("B", 1000.0, 0.0),
            ("B2", 1030.0, 0.0),
            ("C", 2000.0, 0.0),
        ],
        &[
            ("A", "08:00", None),
            ("B", "08:02", None),
            ("B2", "08:02", None),
            ("C", "08:04", None),
        ],
    );
    let mut plans = PlanCache::new();
    let plan = plans.plan(&route.1, DAY, &route.0, &policy).unwrap();
    let zones = stop_zones(&plan.path, &policy);
    let (b, b2) = (plan.path.call_distances_m[1], plan.path.call_distances_m[2]);
    assert_eq!(zones[1].departure_m, Some((b + b2) / 2.0));
    assert_eq!(zones[2].arrival_m, Some((b + b2) / 2.0));
    // The departure of B sorts before the arrival at B2 at the same point.
    let at_mid: Vec<_> = plan
        .triggers
        .iter()
        .filter(|t| t.along_m == (b + b2) / 2.0)
        .map(|t| (t.call, t.kind))
        .collect();
    assert_eq!(
        at_mid,
        vec![(1, EventKind::Departure), (2, EventKind::Arrival)]
    );
    let mut d = Tracker::new(&route, &policy);
    d.run(&drive(
        &[
            ("08:00", 0.0, 0.0),
            ("08:02", 1015.0, 0.0),
            ("08:04", 2000.0, 0.0),
        ],
        15,
        false,
    ));
    assert!(d.times_sorted());
    let calls: Vec<usize> = d.crossed.iter().map(|c| c.call).collect();
    assert!(calls.windows(2).all(|w| w[0] <= w[1]), "{calls:?}");
}

#[test]
fn the_timetable_is_the_dwell_at_a_call_and_interpolated_between() {
    let policy = policy();
    let route = route(
        &[("A", 0.0, 0.0), ("B", 1000.0, 0.0), ("C", 2000.0, 0.0)],
        &[
            ("A", "08:00", None),
            ("B", "08:10", Some("08:15")),
            ("C", "08:25", None),
        ],
    );
    let mut plans = PlanCache::new();
    let plan = plans.plan(&route.1, DAY, &route.0, &policy).unwrap();
    let tt = &plan.timetable;
    let b = plan.path.call_distances_m[1];
    // Waiting at the origin is never early; standing at B inside its dwell is on time.
    assert_eq!(tt.lateness_s(at("07:50"), 0.0), 0.0);
    assert_eq!(tt.lateness_s(at("08:12"), b), 0.0);
    assert_eq!(tt.lateness_s(at("08:16"), b), 60.0);
    assert_eq!(tt.lateness_s(at("08:06"), b), -240.0);
    let window = tt.window(b).unwrap();
    assert_eq!(
        (window.earliest, window.latest),
        (Some(at("08:10")), Some(at("08:15")))
    );
    // Halfway between B's departure trigger and C's arrival trigger: halfway in time too.
    let (leave, reach) = (
        plan.zones[1].departure_m.unwrap(),
        plan.zones[2].arrival_m.unwrap(),
    );
    let mid = tt.planned(at("08:00"), (leave + reach) / 2.0).unwrap();
    assert_eq!(mid, at("08:20"));
    assert_eq!(plan.call_index_at(b), -1 + 1);
    assert!(plan.at_stop(b) && !plan.at_stop((leave + reach) / 2.0));
}

#[test]
fn the_plan_cache_keeps_plans_per_release_and_sweeps() {
    let policy = policy();
    let (mut index, trip) = straight();
    let mut plans = PlanCache::new();
    let first = plans.plan(&trip, DAY, &index, &policy).unwrap();
    assert!(Arc::ptr_eq(
        &first,
        &plans.plan(&trip, DAY, &index, &policy).unwrap()
    ));
    let next_day = plans.plan(&trip, DAY.add_days(1), &index, &policy).unwrap();
    assert!(
        Arc::ptr_eq(&first.path, &next_day.path),
        "one path per trip"
    );
    assert_eq!(plans.sizes(), (1, 2));
    plans.sweep(|_| true, DAY.add_days(1));
    assert_eq!(plans.sizes(), (1, 1));
    plans.sweep(|trip_id| trip_id != "trip", DAY);
    assert_eq!(plans.sizes(), (0, 0));
    // A new release drops everything.
    plans.plan(&trip, DAY, &index, &policy).unwrap();
    index.release_id = "next".to_owned();
    let other = plans.plan(&trip, DAY, &index, &policy).unwrap();
    assert!(!Arc::ptr_eq(&first, &other));
    assert_eq!(plans.release_id(), "next");
    assert_eq!(plans.sizes(), (1, 1));
    // A missing location is the index's miss, as Python's IndexMiss.
    index.locations.clear();
    index.release_id = "third".to_owned();
    assert!(plans.plan(&trip, DAY, &index, &policy).is_err());
    assert_eq!(MAX_PLANS, 50_000);
}
