//! Unit scenarios of `timeline/fusion.rs`, `timeline/estimate.rs`, `timeline::usable_delay` and
//! `platforms.rs`, ported from `test_rail_fusion.py`, `test_timeline.py` and `test_boards.py`
//! where they exercise these functions directly (the scenarios that run the whole core are
//! replayed bit for bit by `timeline_estimate.rs`).

mod common;

use std::sync::Arc;

use common::builder::{at, at2, local, timetable};
use obehy_core::config::Policy;
use obehy_core::config::policy::{POLICY_FILE, Predictor};
use obehy_core::index::{Index, Trip};
use obehy_core::infer::keyed::Match;
use obehy_core::model::{
    CallState, CallStatus, Delay, DelayReference, Effect, EventKind, Fact, Feed, FeedState,
    Freshness, Instance, Interval, JourneyKey, Lifecycle, NextStopPrediction, Observation,
    PlatformAssignment, PlatformLabel, PointEvent, Progress, RawRef, SourceClass, SourceSemantics,
    SourceState, SourceTrack, StopKind, UnresolvedKind,
};
use obehy_core::platforms::apply_platform;
use obehy_core::time::{Date, Instant, TimeDelta};
use obehy_core::timeline::estimate::{Inputs, estimate, monotone};
use obehy_core::timeline::fusion::{
    anchor_prediction, apply_point, current_lateness, merge, position_leads, source_track,
    tracks_with,
};
use obehy_core::timeline::plan::{Plan, PlanCache};
use obehy_core::timeline::usable_delay;

const D8: Date = Date::ymd(2026, 10, 8);

fn policy() -> Policy {
    Policy::load(
        &common::repo()
            .join("src/obehy/data/realtime")
            .join(POLICY_FILE),
    )
    .unwrap()
}

fn l(clock: &str) -> Instant {
    local(&format!("2026-10-08 {clock}"))
}

fn secs(s: i64) -> TimeDelta {
    TimeDelta::from_seconds(s)
}

/// The instance `core._open` makes of `trip`: every call scheduled, nothing heard.
fn instance(trip: &Trip, journey: JourneyKey, lifecycle: Lifecycle) -> Instance {
    let at = l("07:00");
    Instance {
        journey,
        release_id: "test-release".into(),
        trip_id: trip.trip_id.to_string(),
        mode: trip.mode.to_string(),
        lifecycle,
        calls: trip
            .calls
            .iter()
            .map(|c| {
                let mut call = CallState::new(i64::from(c.sequence), &*c.location_id, c.visit_n);
                call.passenger = c.passenger_service;
                call
            })
            .collect(),
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
        parts: Vec::new(),
        sources: Vec::new(),
        track_source: None,
        shown_source: None,
        published: None,
        held: Vec::new(),
    }
}

fn observation(source: &str, at: Instant, facts: Vec<Fact>) -> Arc<Observation> {
    Arc::new(Observation {
        source: source.into(),
        channel: "trains".into(),
        feed: Feed::Czptt,
        received_at: at,
        observed_at: Some(at),
        raw: RawRef {
            sha256: "1".repeat(64),
            item: 0,
        },
        decoder_version: 1,
        facts,
    })
}

/// test_rail_fusion's run: A ~X B C, train 100, SR70 codes 10001, 10009, 10002, 10003.
fn rail() -> (Index, Arc<Trip>, JourneyKey) {
    let mut tt = timetable(Feed::Czptt, "czptt:train_number");
    tt.trip(
        "100",
        &[D8],
        vec![
            at("A", "08:00"),
            at("~X", "08:05"),
            at2("B", "08:10", "08:12"),
            at("C", "08:20"),
        ],
        |t| t.trip_id("czptt:trip:PA1:1").mode("rail").no_shape(),
    );
    let mut index = tt.index();
    for (name, code) in [
        ("A", "10001"),
        ("X", "10009"),
        ("B", "10002"),
        ("C", "10003"),
    ] {
        index.add_location_key(&format!("czptt:{name}"), "sr70", code);
    }
    let trip = index.trip("czptt:trip:PA1:1").unwrap().clone();
    let journey = JourneyKey {
        feed: Feed::Czptt,
        namespace: "czptt:pa".into(),
        key: "PA1".into(),
        service_date: D8,
    };
    (index, trip, journey)
}

/// test_timeline's bus with a real dwell at B: A 08:00, B 08:07/08:10, C 08:15.
fn bus(calls: Vec<(String, Option<i32>, Option<i32>)>) -> (Index, Arc<Trip>, JourneyKey) {
    let mut tt = timetable(Feed::Jdf, "cis:line_trip");
    tt.trip("582492:143", &[D8], calls, |t| t);
    let index = tt.index();
    let trip = index.trips.values().next().unwrap().clone();
    let journey = JourneyKey {
        feed: Feed::Jdf,
        namespace: "cis:line_trip".into(),
        key: "582492:143".into(),
        service_date: D8,
    };
    (index, trip, journey)
}

fn plan_of(index: &Index, trip: &Trip, policy: &Policy) -> Arc<Plan> {
    PlanCache::new().plan(trip, D8, index, policy).unwrap()
}

#[test]
fn r15_a_crossing_inside_the_point_minute_narrows_it_one_outside_loses() {
    let policy = policy();
    let point = Interval::new(l("08:11"), l("08:11:59"));
    let inside = Interval::new(l("08:11:20"), l("08:12:30"));
    let outside = Interval::new(l("08:15"), l("08:15:30"));
    let narrowed = Interval::new(l("08:11:20"), l("08:12:30"));
    assert_eq!(merge(Some(point), inside, false, &policy), narrowed);
    assert_eq!(merge(Some(point), outside, false, &policy), point);
    assert_eq!(merge(Some(inside), point, true, &policy), narrowed);
    assert_eq!(merge(None, point, true, &policy), point);
}

#[test]
fn source_tracks_stay_sorted_by_source() {
    let at = l("08:00");
    let mut sources = Vec::new();
    for name in ["sz-mapa", "duk", "pid", "duk"] {
        let mut track = source_track(name, at);
        track.delay_s = Some(sources.len() as i64);
        tracks_with(&mut sources, track);
    }
    let names: Vec<&str> = sources.iter().map(|t| t.source.as_str()).collect();
    assert_eq!(names, ["duk", "pid", "sz-mapa"]);
    assert_eq!(sources[0].delay_s, Some(3)); // replaced in place
}

#[test]
fn r14_a_fresh_better_ranked_fix_keeps_a_lower_source_from_taking_the_tracker_back() {
    let policy = policy();
    let (_, trip, journey) = rail();
    let mut run = instance(&trip, journey, Lifecycle::Running);
    let best = policy.rail.position_preference[0].clone();
    let mut gps = source_track(&best, l("08:00"));
    gps.position_at = Some(l("08:00"));
    run.sources = vec![gps];
    let fresh = TimeDelta::from_seconds(policy.rail.position_fresh_s);
    assert!(!position_leads(
        &run,
        "sz-mapa",
        l("08:00") + fresh,
        &policy
    ));
    let later = l("08:00") + fresh + TimeDelta::from_micros(1);
    assert!(position_leads(&run, "sz-mapa", later, &policy));
    assert!(position_leads(&run, &best, l("08:00"), &policy));
}

#[test]
fn usable_delay_discards_garbage_and_pre_trip_elapsed_and_floors_minutes() {
    let policy = policy();
    let (_, trip, journey) = bus(vec![at("A", "08:00"), at("B", "08:05")]);
    let running = instance(&trip, journey.clone(), Lifecycle::Running);
    let pre_trip = instance(&trip, journey, Lifecycle::PreTrip);
    let delay = |seconds: i64| -> Fact {
        Delay {
            seconds,
            reference: DelayReference::Unknown,
        }
        .into()
    };
    let plain = SourceSemantics::default();
    let duk = SourceSemantics {
        pre_trip_delay_is_elapsed: true,
        ..SourceSemantics::default()
    };
    let floored = SourceSemantics {
        delay_floor_minute: true,
        ..SourceSemantics::default()
    };
    let o = |facts: Vec<Fact>| observation("duk", l("08:01"), facts);
    let discard = policy.delay.discard_below_s;
    // DUK-Q6: a garbage negative delay is discarded.
    assert_eq!(
        usable_delay(&running, &o(vec![delay(discard)]), &policy, plain),
        None
    );
    assert_eq!(
        usable_delay(&running, &o(vec![delay(discard + 1)]), &policy, plain),
        Some(discard + 1)
    );
    // DUK-Q14/Q18: elapsed time before departure, or while the source still says pre-trip.
    assert_eq!(
        usable_delay(&pre_trip, &o(vec![delay(120)]), &policy, duk),
        None
    );
    let says_pre_trip: Fact = SourceState {
        code: "pre_trip".into(),
    }
    .into();
    assert_eq!(
        usable_delay(&running, &o(vec![delay(120), says_pre_trip]), &policy, duk),
        None
    );
    assert_eq!(
        usable_delay(&running, &o(vec![delay(120)]), &policy, duk),
        Some(120)
    );
    assert_eq!(
        usable_delay(&pre_trip, &o(vec![delay(120)]), &policy, plain),
        Some(120)
    );
    // SZ-Q12: whole minutes rounded down stand for the minute's middle.
    assert_eq!(
        usable_delay(&running, &o(vec![delay(60)]), &policy, floored),
        Some(90)
    );
    assert_eq!(usable_delay(&running, &o(vec![]), &policy, plain), None);
}

#[test]
fn the_point_delay_leads_while_newer_than_the_last_gps_fix() {
    let (_, trip, journey) = rail();
    let mut run = instance(&trip, journey, Lifecycle::Running);
    assert_eq!(current_lateness(&run), (None, None));
    run.reported_delay_s = Some(30);
    assert_eq!(
        current_lateness(&run),
        (Some(secs(30)), Some(SourceClass::Source))
    );
    let mut sz = source_track("sz-mapa", l("08:10"));
    sz.delay_s = Some(90);
    sz.delay_at = Some(l("08:10"));
    sz.delay_reference = Some(DelayReference::Point);
    let mut gps = source_track("duk", l("08:09"));
    gps.position_at = Some(l("08:09"));
    run.sources = vec![gps, sz];
    assert_eq!(
        current_lateness(&run),
        (Some(secs(90)), Some(SourceClass::Source))
    );
    // A newer GPS fix: the measured lateness, rounded to the nearest second.
    run.sources[0].position_at = Some(l("08:11"));
    let mut track = obehy_core::model::Track {
        hypotheses: Vec::new(),
        committed_m: 0.0,
        unmatched_since: None,
        seen_at: None,
        crossed_at: None,
        off_path: Some(obehy_core::model::OffPath {
            along_m: 0.0,
            at: l("08:11"),
            lateness_s: 42.4,
        }),
        fix: None,
        at_stop: None,
    };
    run.track = Some(track.clone());
    assert_eq!(
        current_lateness(&run),
        (Some(secs(42)), Some(SourceClass::Gps))
    );
    track.off_path.as_mut().unwrap().lateness_s = 43.5;
    run.track = Some(track);
    assert_eq!(
        current_lateness(&run),
        (Some(secs(44)), Some(SourceClass::Gps))
    );
}

#[test]
fn predictors_anchor_change_sz_and_propagate() {
    let (index, trip, journey) = rail();
    let mut run = instance(&trip, journey, Lifecycle::Running);
    run.progress = Some(Progress {
        distance_m: 2000.0,
        call_index: 2,
        at: l("08:13"),
    });
    let mut sz = source_track("sz-mapa", l("08:13:10"));
    sz.delay_s = Some(90); // a floored minute (SZ-Q12)
    sz.delay_at = Some(l("08:13:10"));
    sz.delay_reference = Some(DelayReference::Point);
    sz.prediction = Some(NextStopPrediction {
        sr70: "10003".into(),
        scheduled: Some(l("08:20")),
        predicted: Some(l("08:24")),
    });
    run.sources = vec![sz];
    // The own anchor is SŽ's point delay too (no GPS): the anchor's change is SŽ's prediction.
    let four = Some((3, secs(240)));
    assert_eq!(
        anchor_prediction(&run, &trip, &index, Predictor::AnchorChange),
        four
    );
    assert_eq!(anchor_prediction(&run, &trip, &index, Predictor::Sz), four);
    assert_eq!(
        anchor_prediction(&run, &trip, &index, Predictor::Propagate),
        None
    );
    // A predicted stop behind progress is no anchor.
    run.progress.as_mut().unwrap().call_index = 3;
    assert_eq!(anchor_prediction(&run, &trip, &index, Predictor::Sz), None);
}

fn point(name: &str, codes: &[&str], at: &str, standing: bool) -> Fact {
    let lo = l(at);
    PointEvent {
        name: name.into(),
        codes: codes.iter().map(|c| (*c).to_owned()).collect(),
        scheduled: None,
        actual: Interval::new(lo, lo + secs(59)),
        standing,
    }
    .into()
}

fn sz_track(run: &Instance) -> Instance {
    obehy_core::timeline::fusion::with_track(run.clone(), source_track("sz-mapa", l("08:00")))
}

#[test]
fn sz_q3_standing_is_the_arrival_leaving_the_departure_and_a_railway_point_a_passage() {
    let policy = policy();
    let (index, trip, journey) = rail();
    let plan = plan_of(&index, &trip, &policy);
    let run = sz_track(&instance(&trip, journey, Lifecycle::PreTrip));
    let o = observation(
        "sz-mapa",
        l("08:11:10"),
        vec![point("B", &["10002"], "08:11", true)],
    );
    let (placed, effects) = apply_point(run.clone(), &trip, &plan, &o, None, &index, &policy);
    let Effect::WriteEvent(event) = &effects[0] else {
        panic!("an event: {effects:?}");
    };
    assert_eq!(
        (event.kind, event.location_id.as_str()),
        (EventKind::Arrival, "czptt:B")
    );
    assert_eq!(placed.lifecycle, Lifecycle::Running);
    assert_eq!(
        placed.calls[2].passed_arrival,
        Some(l("08:11") + TimeDelta::from_micros(29_500_000))
    );
    assert_eq!(placed.source_track("sz-mapa").unwrap().point, Some(2));
    assert_eq!(
        placed.progress.unwrap().call_index,
        plan.call_index_at(placed.progress.unwrap().distance_m)
    );
    // The same point again: nothing new to record.
    let previous = placed.source_track("sz-mapa").cloned();
    let (_, again) = apply_point(placed, &trip, &plan, &o, previous.as_ref(), &index, &policy);
    assert!(again.is_empty());
    // Through X, a railway point: a passage, which also passes its arrival.
    let o = observation(
        "sz-mapa",
        l("08:05:10"),
        vec![point("X", &["10009"], "08:05", false)],
    );
    let (passed, effects) = apply_point(run.clone(), &trip, &plan, &o, None, &index, &policy);
    let Effect::WriteEvent(event) = &effects[0] else {
        panic!("an event: {effects:?}");
    };
    assert_eq!(event.kind, EventKind::Passage);
    assert_eq!(
        passed.calls[1].passed_arrival,
        passed.calls[1].passed_departure
    );
    // Waiting at the origin: nothing has happened yet.
    let o = observation(
        "sz-mapa",
        l("07:58"),
        vec![point("A", &["10001"], "07:57", true)],
    );
    let (same, effects) = apply_point(run.clone(), &trip, &plan, &o, None, &index, &policy);
    assert!(effects.is_empty());
    assert_eq!(same, run);
}

#[test]
fn a_point_that_names_no_call_or_contradicts_the_run_moves_nothing() {
    let policy = policy();
    let (index, trip, journey) = rail();
    let plan = plan_of(&index, &trip, &policy);
    let mut run = sz_track(&instance(&trip, journey, Lifecycle::Running));
    let o = observation(
        "sz-mapa",
        l("08:11"),
        vec![point("Q", &["99999"], "08:11", true)],
    );
    let (same, effects) = apply_point(run.clone(), &trip, &plan, &o, None, &index, &policy);
    assert_eq!(same, run);
    let Effect::Unresolved(u) = &effects[0] else {
        panic!("unresolved: {effects:?}");
    };
    assert_eq!((u.kind, u.name.as_str()), (UnresolvedKind::Point, "Q"));
    // C passed at 08:21: a report of B at 08:30 cannot be (SZ-Q10, stale report).
    let mut calls = run.calls.to_vec();
    calls[3].passed_arrival = Some(l("08:21"));
    run.calls = calls.into();
    let o = observation(
        "sz-mapa",
        l("08:30"),
        vec![point("B", &["10002"], "08:30", false)],
    );
    let (_, effects) = apply_point(run.clone(), &trip, &plan, &o, None, &index, &policy);
    let Effect::Unresolved(u) = &effects[0] else {
        panic!("unresolved: {effects:?}");
    };
    assert_eq!(u.kind, UnresolvedKind::Inconsistent);
}

fn predicted(run: &Instance) -> Vec<(Option<Instant>, Option<Instant>, CallStatus)> {
    run.calls
        .iter()
        .map(|c| (c.estimated_arrival, c.estimated_departure, c.status))
        .collect()
}

#[test]
fn a_source_delay_predicts_the_calls_ahead_and_without_one_they_stay_scheduled() {
    let policy = policy();
    let (index, trip, journey) = bus(vec![at("A", "08:00"), at("B", "08:05"), at("C", "08:10")]);
    let plan = plan_of(&index, &trip, &policy);
    let run = instance(&trip, journey, Lifecycle::Running);
    let inputs = Inputs {
        current: (Some(secs(120)), Some(SourceClass::Source)),
        ..Inputs::default()
    };
    let out = estimate(run.clone(), &plan.scheduled, &policy.prediction, &inputs);
    let p = CallStatus::Predicted;
    assert_eq!(
        predicted(&out),
        [
            (Some(l("08:02")), Some(l("08:02")), p),
            (Some(l("08:07")), Some(l("08:07")), p),
            (Some(l("08:12")), Some(l("08:12")), p),
        ]
    );
    assert!(
        out.calls
            .iter()
            .all(|c| c.source_class == Some(SourceClass::Source))
    );
    let out = estimate(run, &plan.scheduled, &policy.prediction, &Inputs::default());
    assert!(out.calls.iter().all(|c| c.status == CallStatus::Scheduled));
}

#[test]
fn a_late_bus_recovers_in_a_real_dwell_and_early_running_fades() {
    let policy = policy();
    let prediction = &policy.prediction;
    let (index, trip, journey) = bus(vec![
        at("A", "08:00"),
        at2("B", "08:07", "08:10"),
        at("C", "08:15"),
    ]);
    let plan = plan_of(&index, &trip, &policy);
    let mut run = instance(&trip, journey, Lifecycle::Running);
    let mut calls = run.calls.to_vec();
    calls[0].passed_departure = Some(l("08:00"));
    run.calls = calls.into();
    run.progress = Some(Progress {
        distance_m: 250.0,
        call_index: 0,
        at: l("08:04"),
    });
    let late = |seconds: i64| Inputs {
        current: (Some(secs(seconds)), Some(SourceClass::Gps)),
        ..Inputs::default()
    };
    let out = estimate(run.clone(), &plan.scheduled, prediction, &late(45));
    assert_eq!(out.calls[1].estimated_arrival, Some(l("08:07:45")));
    assert_eq!(out.calls[1].estimated_departure, Some(l("08:10"))); // the dwell absorbs it
    assert_eq!(out.calls[2].estimated_arrival, Some(l("08:15")));
    // Six minutes late: the dwell absorbs lateness only down to the minimum dwell.
    let min_dwell = prediction.min_dwell_s.delta("bus");
    let out = estimate(run.clone(), &plan.scheduled, prediction, &late(360));
    assert_eq!(out.calls[1].estimated_arrival, Some(l("08:13")));
    assert_eq!(
        out.calls[1].estimated_departure,
        Some(l("08:13") + min_dwell)
    );
    let carried = l("08:13") + min_dwell - l("08:10");
    assert_eq!(out.calls[2].estimated_arrival, Some(l("08:15") + carried));
    // About 105 s early: holds to the next call, then the mode's early_keep of the lead.
    let early = Inputs {
        current: (Some(secs(-105)), Some(SourceClass::Gps)),
        ..Inputs::default()
    };
    let out = estimate(run, &plan.scheduled, prediction, &early);
    let max_early = prediction.max_early_departure_s.delta("bus");
    let at_b = secs(-105).max(-max_early);
    assert_eq!(out.calls[1].estimated_arrival, Some(l("08:07") - secs(105)));
    assert_eq!(out.calls[1].estimated_departure, Some(l("08:10") + at_b));
    let faded = at_b.mul_f64(prediction.early_keep.get("bus"));
    let expected = (l("08:15") + faded).max(l("08:10") + at_b);
    assert_eq!(out.calls[2].estimated_arrival, Some(expected));
}

#[test]
fn a_published_prediction_holds_within_the_deadband() {
    let policy = policy();
    let prediction = &policy.prediction;
    let hold = prediction.publish_deadband_s.delta("bus");
    assert!(hold > TimeDelta::ZERO, "the bus deadband");
    let (index, trip, journey) = bus(vec![at("A", "08:00"), at("B", "08:05")]);
    let plan = plan_of(&index, &trip, &policy);
    let run = instance(&trip, journey, Lifecycle::Running);
    let with = |delay: TimeDelta| Inputs {
        current: (Some(delay), Some(SourceClass::Source)),
        ..Inputs::default()
    };
    let first = estimate(run, &plan.scheduled, prediction, &with(secs(60)));
    let near = with(secs(60) + hold - TimeDelta::from_micros(1));
    let kept = estimate(first.clone(), &plan.scheduled, prediction, &near);
    assert_eq!(kept.calls[1].estimated_arrival, Some(l("08:06")));
    let far = with(secs(60) + hold);
    let moved = estimate(first, &plan.scheduled, prediction, &far);
    assert_eq!(moved.calls[1].estimated_arrival, Some(l("08:06") + hold));
}

#[test]
fn monotone_repair_never_lets_times_decrease() {
    let (_, trip, journey) = bus(vec![at("A", "08:00"), at("B", "08:05"), at("C", "08:10")]);
    let mut run = instance(&trip, journey, Lifecycle::Running);
    let mut calls = run.calls.to_vec();
    calls[0].estimated_departure = Some(l("08:03"));
    calls[1].estimated_arrival = Some(l("08:02"));
    calls[1].estimated_departure = Some(l("08:06"));
    calls[2].estimated_arrival = Some(l("08:05"));
    run.calls = calls.into();
    let out = monotone(run);
    let times: Vec<_> = out
        .calls
        .iter()
        .map(|c| (c.estimated_arrival, c.estimated_departure))
        .collect();
    assert_eq!(
        times,
        [
            (None, Some(l("08:03"))),
            (Some(l("08:03")), Some(l("08:06"))),
            (Some(l("08:06")), None),
        ]
    );
}

fn board(value: &str, label: PlatformLabel, when: &str, received: &str) -> Arc<Observation> {
    observation(
        "sz-tabule",
        l(received),
        vec![
            PlatformAssignment {
                station: "10002".into(),
                kind: StopKind::Departure,
                scheduled: l(when),
                value: value.into(),
                label,
            }
            .into(),
        ],
    )
}

#[test]
fn r11_r12_a_newer_assignment_replaces_the_older_and_a_row_matching_no_call_is_reported() {
    let (mut index, trip, journey) = rail();
    let mut state = FeedState::new(Feed::Czptt);
    let found = Match {
        journey: journey.clone(),
        trip: trip.clone(),
        start: l("08:00"),
        end: l("08:20"),
    };
    let opened = instance(&trip, journey.clone(), Lifecycle::Forecast);
    let mut apply = |state: &mut FeedState, o: &Arc<Observation>| {
        apply_platform(
            state,
            o,
            &mut index,
            |_, _| Ok(found.clone()),
            |state, _, m| {
                if !state.instances.contains_key(&m.journey) {
                    state.instances.insert(m.journey.clone(), opened.clone());
                }
                Vec::new()
            },
        )
    };
    let effects = apply(
        &mut state,
        &board("2", PlatformLabel::Track, "08:12", "07:40"),
    );
    assert!(
        matches!(effects[0], Effect::RecordPlatform(_)),
        "{effects:?}"
    );
    assert!(state.dirty.contains(&journey));
    let platform = |state: &FeedState| state.instances[&journey].calls[2].platform.clone().unwrap();
    assert_eq!(
        (
            platform(&state).value.as_str(),
            platform(&state).assigned_at
        ),
        ("2", l("07:40"))
    );
    // The same value again keeps the first assignment; a new value or label replaces it.
    apply(
        &mut state,
        &board("2", PlatformLabel::Track, "08:12", "07:45"),
    );
    assert_eq!(platform(&state).assigned_at, l("07:40"));
    apply(
        &mut state,
        &board("2", PlatformLabel::Platform, "08:12", "07:50"),
    );
    assert_eq!(
        (platform(&state).label, platform(&state).assigned_at),
        (PlatformLabel::Platform, l("07:50"))
    );
    assert_eq!(state.instances[&journey].freshness.heard_at, l("07:50"));
    // No departure from B at 08:03: reported, nothing assigned.
    let effects = apply(
        &mut state,
        &board("3", PlatformLabel::Track, "08:03", "07:55"),
    );
    let Effect::Unresolved(u) = &effects[0] else {
        panic!("unresolved: {effects:?}");
    };
    assert_eq!(u.kind, UnresolvedKind::Platform);
    assert_eq!(
        u.name,
        format!("10002 departure {}", l("08:03").isoformat())
    );
    // Unbound: only the result.
    let effects = apply_platform(
        &mut state,
        &board("4", PlatformLabel::Track, "08:12", "07:56"),
        &mut index,
        |_, _| Err(obehy_core::model::Reason::NoTrip),
        |_, _, _| unreachable!(),
    );
    assert_eq!(effects.len(), 1);
    let _: Option<&SourceTrack> = None;
}
