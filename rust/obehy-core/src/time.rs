//! The only module that converts realtime times (BASE_PLAN.md section 19.3).
//!
//! [`Instant`] is a UTC instant in whole microseconds. A
//! [`ServiceTime`] is a schedule time: wall-clock seconds from local midnight of the service
//! date, exactly as JrUtil writes it (values past 86400 continue into the next day). Reading a
//! wall-clock time on a DST night is approximate by design: the repeated autumn hour takes its
//! first occurrence and the skipped spring hour is read with the pre-change offset. Source local
//! times are stricter: a time that does not exist is rejected.
//!
//! Arithmetic happens on UTC instants only, in whole microseconds ([`TimeDelta`]).

mod civil;
mod delta;
pub mod iso;
pub mod strptime;
pub mod zone;

use std::fmt;

pub use civil::{Date, NaiveDateTime};
pub use delta::TimeDelta;
pub use strptime::Layout;

const MICROS_PER_SECOND: i64 = 1_000_000;
const MICROS_PER_DAY: i64 = 86_400 * MICROS_PER_SECOND;

/// A value that cannot be a valid time in its declared meaning.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{0}")]
pub struct TimeError(pub String);

impl TimeError {
    pub(crate) fn new(message: impl Into<String>) -> Self {
        TimeError(message.into())
    }
}

/// A UTC instant: microseconds since 1970-01-01T00:00Z.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct Instant(i64);

impl Instant {
    pub const fn from_micros(micros: i64) -> Instant {
        Instant(micros)
    }

    /// Microseconds since the epoch.
    pub const fn micros(self) -> i64 {
        self.0
    }

    pub const fn from_unix_seconds(seconds: i64) -> Instant {
        Instant(seconds * MICROS_PER_SECOND)
    }

    /// The instant whose UTC wall clock reads `utc`.
    pub const fn from_utc(utc: NaiveDateTime) -> Instant {
        Instant(utc.micros())
    }

    /// The UTC wall clock of this instant.
    pub const fn utc(self) -> NaiveDateTime {
        NaiveDateTime::from_micros(self.0)
    }

    /// Whole seconds since the epoch (rounded down).
    pub const fn unix_seconds(self) -> i64 {
        self.0.div_euclid(MICROS_PER_SECOND)
    }

    /// `2026-10-08T22:30:00+00:00`, with `.ffffff` only when the microseconds are not zero.
    pub fn isoformat(self) -> String {
        let mut text = String::with_capacity(32);
        self.utc().write_iso(&mut text);
        text.push_str("+00:00");
        text
    }

    /// An ISO time with an offset; a naive time is an error.
    pub fn from_isoformat(text: &str) -> Result<Instant, TimeError> {
        iso::parse_datetime(text)?.instant()
    }
}

impl fmt::Display for Instant {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.isoformat())
    }
}

impl serde::Serialize for Instant {
    fn serialize<S: serde::Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.collect_str(self)
    }
}

impl<'de> serde::Deserialize<'de> for Instant {
    fn deserialize<D: serde::Deserializer<'de>>(deserializer: D) -> Result<Instant, D::Error> {
        let text = <std::borrow::Cow<'de, str>>::deserialize(deserializer)?;
        Instant::from_isoformat(&text).map_err(serde::de::Error::custom)
    }
}

impl std::ops::Add<TimeDelta> for Instant {
    type Output = Instant;
    fn add(self, rhs: TimeDelta) -> Instant {
        Instant(self.0 + rhs.micros())
    }
}

impl std::ops::Sub<TimeDelta> for Instant {
    type Output = Instant;
    fn sub(self, rhs: TimeDelta) -> Instant {
        Instant(self.0 - rhs.micros())
    }
}

impl std::ops::Sub for Instant {
    type Output = TimeDelta;
    fn sub(self, rhs: Instant) -> TimeDelta {
        TimeDelta::from_micros(self.0 - rhs.0)
    }
}

/// A schedule time: wall-clock seconds from local midnight of the service date (may exceed
/// 24 h, or be negative). Ordered by date, then seconds, as the Python dataclass.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct ServiceTime {
    pub service_date: Date,
    pub seconds: i32,
}

impl ServiceTime {
    pub const fn new(service_date: Date, seconds: i32) -> ServiceTime {
        ServiceTime {
            service_date,
            seconds,
        }
    }

    /// The instant of this wall-clock schedule time (first occurrence when repeated).
    pub fn instant(self) -> Instant {
        let wall = self
            .service_date
            .midnight()
            .plus_micros(i64::from(self.seconds) * 1_000_000);
        zone::earlier(wall)
    }
}

/// The wall clock: the one place the realtime code reads it (live only; replay and warm start
/// step a simulated clock).
pub fn now() -> Instant {
    let since = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .expect("the system clock is after 1970");
    Instant(i64::try_from(since.as_micros()).expect("the system clock fits i64 microseconds"))
}

/// The Europe/Prague wall-clock time of an instant, naive: for comparing a time of day.
pub fn local_wall(value: Instant) -> NaiveDateTime {
    zone::local_wall(value)
}

/// The instant of a Europe/Prague wall-clock time given without an offset (a command line's);
/// the first of a repeated hour. An aware time is just [`Instant`].
pub fn wall_instant(wall: NaiveDateTime) -> Instant {
    zone::earlier(wall)
}

/// The Europe/Prague calendar date of an instant.
pub fn local_date(value: Instant) -> Date {
    zone::local_wall(value).date()
}

/// Service dates an observation can belong to: local yesterday, today and tomorrow.
pub fn candidate_service_dates(value: Instant) -> [Date; 3] {
    let today = local_date(value);
    [today.add_days(-1), today, today.add_days(1)]
}

/// Microseconds since the epoch.
pub const fn micros(value: Instant) -> i64 {
    value.micros()
}

/// `delta_us` microseconds times `factor`, rounded to the nearest microsecond.
pub fn scaled_micros(delta_us: i64, factor: f64) -> i64 {
    TimeDelta::from_micros(delta_us).mul_f64(factor).micros()
}

/// The first instant after `at` on a round multiple of `every_s` seconds (emit ticks).
pub fn next_multiple(at: Instant, every_s: i64) -> Instant {
    assert!(every_s > 0, "next_multiple needs a positive period");
    Instant::from_unix_seconds((at.unix_seconds().div_euclid(every_s) + 1) * every_s)
}

/// Every instant that reads as the naive local time `wall`: none (the skipped hour), one, or
/// two (the repeated hour, earlier first).
pub fn readings(wall: NaiveDateTime) -> Vec<Instant> {
    zone::readings(wall)
}

fn closest(readings: &[Instant], received_at: Instant) -> Option<Instant> {
    readings
        .iter()
        .copied()
        .min_by_key(|reading| ((*reading - received_at).abs(), *reading))
}

/// A source's naive local date-time: the reading closest to `received_at`, or None if the time
/// does not exist (the skipped spring hour).
pub fn resolve_local(wall: NaiveDateTime, received_at: Instant) -> Option<Instant> {
    closest(&zone::readings(wall), received_at)
}

/// A source's local date-time text in `strptime` layout (SZ-Q2: `md`), resolved like
/// [`resolve_local`]; None if it does not parse or does not exist.
pub fn read_local(text: &str, layout: &Layout, received_at: Instant) -> Option<Instant> {
    let wall = layout.parse(text)?;
    resolve_local(wall, received_at)
}

/// A bare local time of day (`HH:mm`, as microseconds after midnight): the reading within
/// 12 h of `received_at` closest to it, or None if no such reading exists.
pub fn resolve_clock(clock_us: i64, received_at: Instant) -> Option<Instant> {
    assert!((0..MICROS_PER_DAY).contains(&clock_us), "a time of day");
    let limit = TimeDelta::from_hours(12);
    let near: Vec<Instant> = candidate_service_dates(received_at)
        .into_iter()
        .flat_map(|day| zone::readings(day.midnight().plus_micros(clock_us)))
        .filter(|reading| (*reading - received_at).abs() <= limit)
        .collect();
    closest(&near, received_at)
}

/// An ISO time with its offset; when it is later than `reference` (a clock of the same device
/// known to be right) by its own positive offset, within `tolerance`, the offset was applied
/// twice (DUK-Q1) and is taken off once more. Only ever subtracted: a time behind its
/// reference is just older. None without an offset; an error if the text is no ISO time.
pub fn read_unless_offset_twice(
    text: &str,
    reference: Instant,
    tolerance: TimeDelta,
) -> Result<Option<Instant>, TimeError> {
    let parsed = iso::parse_datetime(text)?;
    let Some(offset_us) = parsed.offset_us else {
        return Ok(None);
    };
    let labelled = parsed.instant()?;
    let offset = TimeDelta::from_micros(offset_us);
    if offset > TimeDelta::ZERO && (labelled - reference - offset).abs() <= tolerance {
        return Ok(Some(labelled - offset));
    }
    Ok(Some(labelled))
}

/// An ISO time whose digits are local time whatever offset it is labelled with (ARRIVA-Q1);
/// an error if the text is no ISO time.
pub fn read_digits_as_local(
    text: &str,
    received_at: Instant,
) -> Result<Option<Instant>, TimeError> {
    Ok(resolve_local(iso::parse_datetime(text)?.wall, received_at))
}

/// Whether a source time is plausible: no further from reception than the policy allows.
pub fn within_skew(value: Instant, received_at: Instant, max_skew: TimeDelta) -> bool {
    (value - received_at).abs() <= max_skew
}

#[cfg(test)]
mod tests;
