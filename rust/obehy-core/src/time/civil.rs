//! Calendar dates and naive date-times, proleptic Gregorian, years 1..=9999.

use std::fmt::{self, Write as _};

use super::{MICROS_PER_DAY, TimeError, iso};

/// Days since 1970-01-01 (Howard Hinnant's `days_from_civil`).
const fn days_from_civil(year: i32, month: u32, day: u32) -> i32 {
    let y = if month <= 2 { year - 1 } else { year };
    let era = if y >= 0 { y } else { y - 399 } / 400;
    let yoe = (y - era * 400) as u32;
    let mp = (month + 9) % 12;
    let doy = (153 * mp + 2) / 5 + day - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + doe as i32 - 719_468
}

/// (year, month, day) of a day count since 1970-01-01.
const fn civil_from_days(days: i32) -> (i32, u32, u32) {
    let z = days + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = (z - era * 146_097) as u32;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = yoe as i32 + era * 400;
    (if month <= 2 { year + 1 } else { year }, month, day)
}

pub(crate) const fn days_in_month(year: i32, month: u32) -> u32 {
    match month {
        2 if year % 4 == 0 && (year % 100 != 0 || year % 400 == 0) => 29,
        2 => 28,
        4 | 6 | 9 | 11 => 30,
        _ => 31,
    }
}

/// A calendar date (a service date, a local date): days since 1970-01-01.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct Date(i32);

impl Date {
    /// The date, or None outside years 1..=9999 or the calendar.
    pub const fn new(year: i32, month: u32, day: u32) -> Option<Date> {
        if year < 1 || year > 9999 || month < 1 || month > 12 {
            return None;
        }
        if day < 1 || day > days_in_month(year, month) {
            return None;
        }
        Some(Date(days_from_civil(year, month, day)))
    }

    /// A date known to be valid (tests, constants); panics otherwise.
    pub const fn ymd(year: i32, month: u32, day: u32) -> Date {
        match Date::new(year, month, day) {
            Some(date) => date,
            None => panic!("not a valid date"),
        }
    }

    pub const fn from_days(days_since_epoch: i32) -> Date {
        Date(days_since_epoch)
    }

    pub const fn days_since_epoch(self) -> i32 {
        self.0
    }

    pub const fn year(self) -> i32 {
        civil_from_days(self.0).0
    }

    pub const fn month(self) -> u32 {
        civil_from_days(self.0).1
    }

    pub const fn day(self) -> u32 {
        civil_from_days(self.0).2
    }

    /// The date `days` later.
    #[must_use]
    pub const fn add_days(self, days: i32) -> Date {
        Date(self.0 + days)
    }

    /// Days from `other` to this date.
    pub const fn days_since(self, other: Date) -> i32 {
        self.0 - other.0
    }

    /// Midnight of this date, naive.
    pub const fn midnight(self) -> NaiveDateTime {
        NaiveDateTime(self.0 as i64 * MICROS_PER_DAY)
    }

    /// `2026-10-08`.
    pub fn isoformat(self) -> String {
        let mut text = String::with_capacity(10);
        self.write_iso(&mut text);
        text
    }

    /// [`Date::isoformat`] onto `out`.
    pub fn write_iso(self, out: &mut String) {
        let (year, month, day) = civil_from_days(self.0);
        write!(out, "{year:04}-{month:02}-{day:02}").expect("writing to a string");
    }

    /// An ISO date (`2026-10-08`).
    pub fn from_isoformat(text: &str) -> Result<Date, TimeError> {
        iso::parse_date(text)
    }
}

impl fmt::Display for Date {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.isoformat())
    }
}

impl serde::Serialize for Date {
    fn serialize<S: serde::Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.collect_str(self)
    }
}

impl<'de> serde::Deserialize<'de> for Date {
    fn deserialize<D: serde::Deserializer<'de>>(deserializer: D) -> Result<Date, D::Error> {
        let text = <std::borrow::Cow<'de, str>>::deserialize(deserializer)?;
        Date::from_isoformat(&text).map_err(serde::de::Error::custom)
    }
}

/// A naive date-time (a local wall clock): microseconds since 1970-01-01T00:00 on its own
/// clock.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct NaiveDateTime(i64);

impl NaiveDateTime {
    /// The date-time, or None if a field is out of range.
    pub fn new(date: Date, hour: u32, minute: u32, second: u32, micro: u32) -> Option<Self> {
        if hour > 23 || minute > 59 || second > 59 || micro > 999_999 {
            return None;
        }
        let of_day = ((i64::from(hour) * 60 + i64::from(minute)) * 60 + i64::from(second))
            * 1_000_000
            + i64::from(micro);
        Some(date.midnight().plus_micros(of_day))
    }

    pub const fn from_micros(micros: i64) -> NaiveDateTime {
        NaiveDateTime(micros)
    }

    pub const fn micros(self) -> i64 {
        self.0
    }

    #[must_use]
    pub const fn plus_micros(self, micros: i64) -> NaiveDateTime {
        NaiveDateTime(self.0 + micros)
    }

    pub const fn date(self) -> Date {
        Date(self.0.div_euclid(MICROS_PER_DAY) as i32)
    }

    /// Microseconds since midnight.
    pub const fn micros_of_day(self) -> i64 {
        self.0.rem_euclid(MICROS_PER_DAY)
    }

    /// (hour, minute, second, microsecond).
    pub const fn hms_micro(self) -> (u32, u32, u32, u32) {
        let of_day = self.micros_of_day();
        let seconds = of_day / 1_000_000;
        (
            (seconds / 3600) as u32,
            (seconds / 60 % 60) as u32,
            (seconds % 60) as u32,
            (of_day % 1_000_000) as u32,
        )
    }

    /// `2026-10-08T22:30:00[.ffffff]`.
    pub fn isoformat(self) -> String {
        let mut text = String::with_capacity(26);
        self.write_iso(&mut text);
        text
    }

    /// [`NaiveDateTime::isoformat`] onto `out`.
    pub fn write_iso(self, out: &mut String) {
        let (hour, minute, second, micro) = self.hms_micro();
        self.date().write_iso(out);
        write!(out, "T{hour:02}:{minute:02}:{second:02}").expect("writing to a string");
        if micro != 0 {
            write!(out, ".{micro:06}").expect("writing to a string");
        }
    }
}

impl fmt::Display for NaiveDateTime {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.isoformat())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn civil_days_round_trip_over_the_whole_range() {
        let first = days_from_civil(1, 1, 1);
        let last = days_from_civil(9999, 12, 31);
        let mut expected = (1, 1, 1);
        for days in first..=last {
            assert_eq!(civil_from_days(days), expected);
            let (y, m, d) = expected;
            expected = if d < days_in_month(y, m) {
                (y, m, d + 1)
            } else if m < 12 {
                (y, m + 1, 1)
            } else {
                (y + 1, 1, 1)
            };
        }
        assert_eq!(days_from_civil(1970, 1, 1), 0);
        assert_eq!(Date::ymd(2026, 10, 8).days_since_epoch(), 20_734);
    }

    #[test]
    fn dates_are_validated_and_formatted() {
        assert!(Date::new(2026, 2, 29).is_none());
        assert!(Date::new(2028, 2, 29).is_some());
        assert!(Date::new(0, 1, 1).is_none() && Date::new(10_000, 1, 1).is_none());
        assert_eq!(Date::ymd(33, 1, 5).isoformat(), "0033-01-05");
        let at = NaiveDateTime::new(Date::ymd(2026, 10, 8), 22, 30, 0, 0).unwrap();
        assert_eq!(at.isoformat(), "2026-10-08T22:30:00");
        assert_eq!(at.plus_micros(7).isoformat(), "2026-10-08T22:30:00.000007");
        let before_epoch = NaiveDateTime::new(Date::ymd(1969, 12, 31), 23, 59, 59, 1).unwrap();
        assert_eq!(before_epoch.isoformat(), "1969-12-31T23:59:59.000001");
    }
}
