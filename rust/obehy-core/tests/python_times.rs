//! Python parity of `times.py`, `zoneinfo` and `datetime`: the same tzdata release and TZif
//! bytes, every Prague transition 1970-2100, schedule times and local readings around them,
//! `fromisoformat`, `timedelta` rounding, and the source-time readers.

mod common;

use obehy_core::model::Interval;
use obehy_core::time::{
    self, Date, Instant, Layout, NaiveDateTime, ServiceTime, TimeDelta, iso, zone,
};
use serde_json::Value;

fn at(value: &Value) -> Instant {
    Instant::from_micros(common::int(value))
}

fn opt_at(value: &Value) -> Option<Instant> {
    value.as_i64().map(Instant::from_micros)
}

fn wall(text: &str) -> NaiveDateTime {
    iso::parse_datetime(text).unwrap().wall
}

#[test]
fn the_same_tzdata_release_and_bytes_as_python() {
    let data = common::json("tz.json");
    // Python: tzdata 2026.5 (uv.lock, Windows) = IANA 2026e; Rust embeds the same file.
    assert_eq!(data["iana_version"], zone::TZDATA_VERSION);
    let hex = data["tzif_hex"].as_str().unwrap();
    let bytes: Vec<u8> = (0..hex.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&hex[i..i + 2], 16).unwrap())
        .collect();
    assert_eq!(
        bytes,
        zone::PRAGUE_TZIF,
        "the embedded TZif is not Python's"
    );
}

#[test]
fn every_transition_from_1970_to_2100() {
    let data = common::json("tz.json");
    let transitions = data["transitions"].as_array().unwrap();
    assert!(transitions.len() > 200, "{}", transitions.len());
    let mut previous: Option<i64> = None;
    for change in transitions {
        let t = Instant::from_unix_seconds(common::int(&change[0]));
        let after = common::int(&change[1]);
        assert_eq!(i64::from(zone::utc_offset_s(t)), after, "at {t}");
        if let Some(before) = previous {
            assert_eq!(
                i64::from(zone::utc_offset_s(t - TimeDelta::from_seconds(1))),
                before,
                "before {t}"
            );
        }
        previous = Some(after);
    }
}

#[test]
fn schedule_times_readings_and_walls() {
    let data = common::json("tz.json");
    for case in data["schedule"].as_array().unwrap() {
        let date = Date::from_isoformat(case[0].as_str().unwrap()).unwrap();
        let st = ServiceTime::new(date, common::int(&case[1]) as i32);
        assert_eq!(st.instant(), at(&case[2]), "{st:?}");
    }
    for case in data["readings"].as_array().unwrap() {
        let local = wall(case[0].as_str().unwrap());
        let expected: Vec<Instant> = case[1].as_array().unwrap().iter().map(at).collect();
        assert_eq!(time::readings(local), expected, "{local}");
    }
    for case in data["walls"].as_array().unwrap() {
        let instant = at(&case[0]);
        assert_eq!(
            time::local_wall(instant).isoformat(),
            case[1].as_str().unwrap()
        );
        assert_eq!(
            time::local_date(instant).isoformat(),
            case[2].as_str().unwrap()
        );
    }
}

#[test]
fn fromisoformat_as_cpython() {
    for case in common::json("iso.json").as_array().unwrap() {
        let text = case["text"].as_str().unwrap();
        match iso::parse_datetime(text) {
            Ok(parsed) => {
                assert!(
                    case.get("error").is_none(),
                    "{text:?} parsed, Python refuses it"
                );
                assert_eq!(
                    parsed.wall.isoformat(),
                    case["wall"].as_str().unwrap(),
                    "{text:?}"
                );
                assert_eq!(parsed.offset_us, case["offset_us"].as_i64(), "{text:?}");
            }
            Err(error) => assert!(case.get("error").is_some(), "{text:?}: {error}"),
        }
        let date = Date::from_isoformat(text).ok().map(|d| d.isoformat());
        assert_eq!(date.as_deref(), case["date"].as_str(), "date {text:?}");
    }
}

#[test]
fn timedelta_rounding_as_cpython() {
    let data = common::json("times.json");
    for case in data["scaled"].as_array().unwrap() {
        let (delta, factor) = (common::int(&case[0]), common::float(&case[1]));
        assert_eq!(
            TimeDelta::from_micros(delta).mul_f64(factor).micros(),
            common::int(&case[2])
        );
        assert_eq!(time::scaled_micros(delta, factor), common::int(&case[3]));
    }
    for case in data["divided"].as_array().unwrap() {
        let td = TimeDelta::from_micros(common::int(&case[0]));
        let n = common::int(&case[1]);
        assert_eq!(
            td.div_int(n).micros(),
            common::int(&case[2]),
            "{td:?} / {n}"
        );
        let f = common::float(&case[3]);
        assert_eq!(
            td.div_f64(f).micros(),
            common::int(&case[4]),
            "{td:?} / {f}"
        );
        assert_eq!(
            td.floor_div_int(n).micros(),
            common::int(&case[5]),
            "{td:?} // {n}"
        );
    }
    for case in data["seconds"].as_array().unwrap() {
        let x = common::float(&case[0]);
        assert_eq!(
            TimeDelta::from_secs_f64(x).micros(),
            common::int(&case[1]),
            "timedelta(seconds={x})"
        );
    }
    for case in data["mids"].as_array().unwrap() {
        assert_eq!(
            Interval::new(at(&case[0]), at(&case[1])).mid(),
            at(&case[2])
        );
    }
    for case in data["ticks"].as_array().unwrap() {
        assert_eq!(
            time::next_multiple(at(&case[0]), common::int(&case[1])),
            at(&case[2])
        );
    }
}

#[test]
fn source_time_readers_as_python() {
    let data = common::json("times.json");
    for case in data["read_local"].as_array().unwrap() {
        let (text, layout) = (
            case[0].as_str().unwrap(),
            Layout::new(case[1].as_str().unwrap()),
        );
        assert_eq!(
            time::read_local(text, &layout, at(&case[2])),
            opt_at(&case[3]),
            "{text:?} {layout:?}"
        );
    }
    for case in data["resolve_clock"].as_array().unwrap() {
        assert_eq!(
            time::resolve_clock(common::int(&case[0]), at(&case[1])),
            opt_at(&case[2])
        );
    }
    for case in data["offsets"].as_array().unwrap() {
        let text = case[0].as_str().unwrap();
        let reference = at(&case[1]);
        let five = TimeDelta::from_minutes(5);
        assert_eq!(
            time::read_unless_offset_twice(text, reference, five).unwrap(),
            opt_at(&case[2]),
            "{text}"
        );
        assert_eq!(
            time::read_digits_as_local(text, reference).unwrap(),
            opt_at(&case[3]),
            "{text}"
        );
    }
}
