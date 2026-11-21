//! The time scenario table (docs/R1_SLICE.md section 2), rewritten from
//! `tests/unit/realtime/test_times.py`. T2-T5 are binding scenarios and T13 is
//! `history.vehicle_chains`: only their time parts are here (the core's and the database's
//! tests carry the rest).

use super::*;

fn utc(text: &str) -> Instant {
    let parsed = iso::parse_datetime(text).expect("an ISO time");
    assert!(parsed.offset_us.is_none());
    Instant::from_utc(parsed.wall)
}

fn wall(text: &str) -> NaiveDateTime {
    iso::parse_datetime(text).expect("an ISO time").wall
}

fn st(day: &str, clock: &str) -> ServiceTime {
    let (sign, clock) = clock
        .strip_prefix('-')
        .map_or((1, clock), |rest| (-1, rest));
    let (hours, minutes) = clock.split_once(':').unwrap();
    let seconds = hours.parse::<i32>().unwrap() * 3600 + minutes.parse::<i32>().unwrap() * 60;
    ServiceTime::new(Date::from_isoformat(day).unwrap(), sign * seconds)
}

#[test]
fn t1_times_past_24_continue_into_the_next_day() {
    assert_eq!(st("2026-10-08", "24:30").instant(), utc("2026-10-08T22:30"));
}

#[test]
fn t2_a_night_trip_belongs_to_yesterday_time_part() {
    // 00:20 local on 10-09: the 23:50 trip of 10-08 is among the candidate dates, 30 min ago.
    let observed = utc("2026-10-08T22:20");
    assert!(candidate_service_dates(observed).contains(&Date::ymd(2026, 10, 8)));
    assert_eq!(
        observed - st("2026-10-08", "23:50").instant(),
        TimeDelta::from_minutes(30)
    );
}

#[test]
fn t3_waiting_for_tomorrow_time_part() {
    // 23:55 local on 10-08: the 00:05 trip of 10-09 is a candidate, 10 min ahead.
    let observed = utc("2026-10-08T21:55");
    assert!(candidate_service_dates(observed).contains(&Date::ymd(2026, 10, 9)));
    assert_eq!(
        st("2026-10-09", "00:05").instant() - observed,
        TimeDelta::from_minutes(10)
    );
}

#[test]
fn t6_spring_forward_schedule_times() {
    for (clock, expected) in [
        ("01:30", "2026-03-29T00:30"),
        ("02:30", "2026-03-29T01:30"), // skipped hour: read with the pre-change offset
        ("05:00", "2026-03-29T03:00"),
    ] {
        assert_eq!(st("2026-03-29", clock).instant(), utc(expected), "{clock}");
    }
}

#[test]
fn t7_fall_back_schedule_times() {
    for (clock, expected) in [
        ("02:30", "2026-10-25T00:30"), // repeated hour: first occurrence
        ("05:00", "2026-10-25T04:00"),
    ] {
        assert_eq!(st("2026-10-25", clock).instant(), utc(expected), "{clock}");
    }
}

#[test]
fn t8_repeated_hour_takes_the_reading_closest_to_reception() {
    let local = wall("2026-10-25T02:30");
    for (received, expected) in [
        ("2026-10-25T00:31", "2026-10-25T00:30"),
        ("2026-10-25T01:31", "2026-10-25T01:30"),
    ] {
        assert_eq!(resolve_local(local, utc(received)), Some(utc(expected)));
    }
}

#[test]
fn t9_a_local_time_in_the_skipped_hour_is_rejected() {
    assert_eq!(
        resolve_local(wall("2026-03-29T02:30"), utc("2026-03-29T00:31")),
        None
    );
}

#[test]
fn t10_duk_q1_an_offset_applied_twice_is_taken_off_once_more() {
    let five = TimeDelta::from_minutes(5);
    let activity = utc("2026-07-01T08:00:10");
    let read = |text: &str| read_unless_offset_twice(text, activity, five).unwrap();
    // Summer and winter: the label's own offset ahead of the right clock.
    assert_eq!(
        read("2026-07-01T12:00:00+02:00"),
        Some(utc("2026-07-01T08:00"))
    );
    assert_eq!(
        read("2026-07-01T10:00:00+01:00"),
        Some(utc("2026-07-01T08:00"))
    );
    // A right time, or one behind its reference (a frozen fix), is read as labelled.
    assert_eq!(
        read("2026-07-01T10:00:00+02:00"),
        Some(utc("2026-07-01T08:00"))
    );
    assert_eq!(
        read("2026-07-01T08:00:00+02:00"),
        Some(utc("2026-07-01T06:00"))
    );
    // UTC-labelled times are never shifted; naive ones are None.
    assert_eq!(
        read("2026-07-01T08:00:00+00:00"),
        Some(utc("2026-07-01T08:00"))
    );
    assert_eq!(read("2026-07-01T08:00:00"), None);
    assert!(read_unless_offset_twice("08:00", activity, five).is_err());
}

#[test]
fn t11_arriva_q1_digits_are_local_time_labelled_utc() {
    let received = utc("2026-07-01T08:00:20");
    assert_eq!(
        read_digits_as_local("2026-07-01T10:00:00+00:00", received).unwrap(),
        Some(utc("2026-07-01T08:00"))
    );
}

#[test]
fn t12_sz_q2_bare_clock_time_across_midnight() {
    let received = utc("2026-10-08T22:01"); // 00:01 local on 10-09
    let clock = (23 * 3600 + 59 * 60) * 1_000_000;
    assert_eq!(
        resolve_clock(clock, received),
        Some(utc("2026-10-08T21:59"))
    );
}

#[test]
fn t13_a_vehicle_day_gap_is_measured_on_instants() {
    // Journeys 22:00-23:30 and 00:15-01:00: 45 min apart, well within a 3 h gap.
    let end = st("2026-10-08", "23:30").instant();
    let next = st("2026-10-09", "00:15").instant();
    assert_eq!(next - end, TimeDelta::from_minutes(45));
}

#[test]
fn t14_trip_across_the_fall_back_change_keeps_its_real_length() {
    let start = st("2026-10-25", "01:00").instant();
    let end = st("2026-10-25", "04:00").instant();
    assert_eq!(
        (start, end),
        (utc("2026-10-24T23:00"), utc("2026-10-25T03:00"))
    );
    assert_eq!(end - start, TimeDelta::from_hours(4));
}

#[test]
fn t15_a_clock_lagging_the_switch_is_beyond_the_skew_limit() {
    let received = utc("2026-10-25T02:00:10"); // 03:00 CET; the bus still reports CEST
    let reported = Instant::from_isoformat("2026-10-25T03:00:05+02:00").unwrap();
    let skew = TimeDelta::from_minutes(20);
    assert!(!within_skew(reported, received, skew));
    assert!(within_skew(
        received - TimeDelta::from_seconds(5),
        received,
        skew
    ));
}

#[test]
fn candidate_service_dates_follow_the_local_calendar() {
    assert_eq!(
        candidate_service_dates(utc("2026-10-08T22:20")),
        [
            Date::ymd(2026, 10, 8),
            Date::ymd(2026, 10, 9),
            Date::ymd(2026, 10, 10)
        ]
    );
}

#[test]
fn naive_times_are_not_instants() {
    assert!(Instant::from_isoformat("2026-10-08T12:00:00").is_err());
}

#[test]
fn a_command_line_wall_time_is_prague_and_the_first_of_a_repeated_hour() {
    assert_eq!(
        wall_instant(wall("2026-10-25T02:30")),
        utc("2026-10-25T00:30")
    );
}

#[test]
fn an_instants_wall_time_is_its_prague_time_of_day() {
    assert_eq!(
        local_wall(utc("2026-07-01T22:30")),
        wall("2026-07-02T00:30")
    );
}

#[test]
fn isoformat_and_ticks() {
    assert_eq!(
        utc("2026-10-08T22:30").isoformat(),
        "2026-10-08T22:30:00+00:00"
    );
    assert_eq!(
        (utc("2026-10-08T22:30") + TimeDelta::from_micros(5)).to_string(),
        "2026-10-08T22:30:00.000005+00:00"
    );
    assert_eq!(
        next_multiple(utc("2026-10-08T22:30:04.5"), 10),
        utc("2026-10-08T22:30:10")
    );
    assert_eq!(
        next_multiple(utc("2026-10-08T22:30:10"), 10),
        utc("2026-10-08T22:30:20")
    );
    assert_eq!(scaled_micros(3, 0.5), 2);
    assert_eq!(scaled_micros(-3, 0.5), -2);
}

#[test]
fn read_local_strips_and_resolves() {
    let layout = Layout::new("%d.%m.%Y %H:%M:%S");
    let received = utc("2026-10-25T01:31");
    assert_eq!(
        read_local(" 25.10.2026 02:30:00\n", &layout, received),
        Some(utc("2026-10-25T01:30"))
    );
    assert_eq!(read_local("garbage", &layout, received), None);
}
