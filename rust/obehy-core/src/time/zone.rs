//! Europe/Prague, read from the TZif file Python's `tzdata` package ships, so both sides read
//! one tzdata release byte for byte (docs/RUST_PORT.md, "Time zones").
//!
//! `tzdata/Europe-Prague.tzif` is a copy of `tzdata/zoneinfo/Europe/Prague` from the `tzdata`
//! wheel `uv.lock` pins (2026.5 = IANA 2026e); `tzdata/VERSION` names its release. The time
//! tests compare both with the Python side (`tools/gen_fixtures.py`). chrono-tz is not used:
//! its latest release (0.10.4) embeds IANA 2025b and cannot read another release. jiff only
//! parses the TZif bytes and evaluates its POSIX footer rule (no bundled or system database).
//!
//! To move to a new tzdata release: bump `tzdata` in `uv.lock`, copy the file and `VERSION`,
//! regenerate the fixtures.

use std::sync::LazyLock;

use jiff::Timestamp;
use jiff::civil;
use jiff::tz::{AmbiguousOffset, TimeZone};

use super::{Instant, NaiveDateTime};

/// The IANA release of the embedded zone.
pub const TZDATA_VERSION: &str = include_str!("../../tzdata/VERSION").trim_ascii();

/// The embedded TZif bytes (for the parity test).
pub const PRAGUE_TZIF: &[u8] = include_bytes!("../../tzdata/Europe-Prague.tzif");

static PRAGUE: LazyLock<TimeZone> = LazyLock::new(|| {
    TimeZone::tzif("Europe/Prague", PRAGUE_TZIF).expect("the embedded Europe/Prague TZif parses")
});

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
    .expect("a wall time within Python's range")
}

/// Prague's UTC offset at an instant, in seconds.
pub fn utc_offset_s(at: Instant) -> i32 {
    PRAGUE.to_offset(timestamp(at)).seconds()
}

/// The Prague wall clock of an instant (`astimezone(PRAGUE)`, naive).
pub fn local_wall(at: Instant) -> NaiveDateTime {
    at.utc()
        .plus_micros(i64::from(utc_offset_s(at)) * 1_000_000)
}

fn minus_offset(wall: NaiveDateTime, offset_s: i32) -> Instant {
    Instant::from_micros(wall.micros() - i64::from(offset_s) * 1_000_000)
}

/// `wall.replace(tzinfo=PRAGUE, fold=0)`: the offset in force before a transition, both in the
/// repeated hour (its first occurrence) and in the skipped one (read with the pre-change
/// offset).
pub fn fold0(wall: NaiveDateTime) -> Instant {
    match PRAGUE.to_ambiguous_timestamp(civil_of(wall)).offset() {
        AmbiguousOffset::Unambiguous { offset } => minus_offset(wall, offset.seconds()),
        AmbiguousOffset::Gap { before, .. } | AmbiguousOffset::Fold { before, .. } => {
            minus_offset(wall, before.seconds())
        }
    }
}

/// Every instant that reads as `wall` (`times._readings`): none in the skipped hour, two in
/// the repeated one (fold 0, the earlier, first).
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
