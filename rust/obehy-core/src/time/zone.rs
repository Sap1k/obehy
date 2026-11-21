//! Europe/Prague, from the IANA database jiff bundles (`jiff-tzdb`): the same zone on every
//! platform, whatever the host has installed.

use std::sync::LazyLock;

use jiff::Timestamp;
use jiff::civil;
use jiff::tz::{AmbiguousOffset, TimeZone};

use super::{Instant, NaiveDateTime};

static PRAGUE: LazyLock<TimeZone> =
    LazyLock::new(|| TimeZone::get("Europe/Prague").expect("the bundled database has Prague"));

fn timestamp(at: Instant) -> Timestamp {
    Timestamp::from_microsecond(at.micros()).expect("an instant within jiff's range")
}

fn civil_of(wall: NaiveDateTime) -> civil::DateTime {
    let date = wall.date();
    let (hour, minute, second, micro) = wall.hms_micro();
    civil::DateTime::new(
        date.year() as i16,
        date.month() as i8,
        date.day() as i8,
        hour as i8,
        minute as i8,
        second as i8,
        micro as i32 * 1000,
    )
    .expect("a wall time within jiff's range")
}

/// Prague's UTC offset at an instant, in seconds.
pub fn utc_offset_s(at: Instant) -> i32 {
    PRAGUE.to_offset(timestamp(at)).seconds()
}

/// The Prague wall clock of an instant.
pub fn local_wall(at: Instant) -> NaiveDateTime {
    at.utc()
        .plus_micros(i64::from(utc_offset_s(at)) * 1_000_000)
}

fn minus_offset(wall: NaiveDateTime, offset_s: i32) -> Instant {
    Instant::from_micros(wall.micros() - i64::from(offset_s) * 1_000_000)
}

/// The instant of a wall time, with the offset in force before a transition: the first
/// occurrence of a repeated hour, and a skipped one read with the pre-change offset.
pub fn earlier(wall: NaiveDateTime) -> Instant {
    match PRAGUE.to_ambiguous_timestamp(civil_of(wall)).offset() {
        AmbiguousOffset::Unambiguous { offset } => minus_offset(wall, offset.seconds()),
        AmbiguousOffset::Gap { before, .. } | AmbiguousOffset::Fold { before, .. } => {
            minus_offset(wall, before.seconds())
        }
    }
}

/// Every instant that reads as `wall`: none in the skipped hour, two in the repeated one
/// (earlier first).
pub fn readings(wall: NaiveDateTime) -> Vec<Instant> {
    match PRAGUE.to_ambiguous_timestamp(civil_of(wall)).offset() {
        AmbiguousOffset::Unambiguous { offset } => vec![minus_offset(wall, offset.seconds())],
        AmbiguousOffset::Gap { .. } => Vec::new(),
        AmbiguousOffset::Fold { before, after } => {
            vec![
                minus_offset(wall, before.seconds()),
                minus_offset(wall, after.seconds()),
            ]
        }
    }
}
