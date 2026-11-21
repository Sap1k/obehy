//! ISO 8601 / RFC 3339 dates and date-times, parsed with jiff: `2026-10-05T21:57:48+02:00`,
//! `2026-10-05 21:57:48.25Z`, a bare date, or a naive date-time.

use jiff::fmt::temporal::{Pieces, PiecesOffset};

use super::{Date, Instant, NaiveDateTime, TimeError};

/// A parsed ISO date-time: its digits and its offset (None: naive).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ParsedDateTime {
    pub wall: NaiveDateTime,
    pub offset_us: Option<i64>,
}

impl ParsedDateTime {
    /// The UTC instant of an aware time; a naive one is an error.
    pub fn instant(self) -> Result<Instant, TimeError> {
        let offset_us = self.offset_us.ok_or_else(|| {
            TimeError::new(format!("naive datetime {} cannot be an Instant", self.wall))
        })?;
        Ok(Instant::from_micros(self.wall.micros() - offset_us))
    }
}

fn invalid(text: &str, error: impl std::fmt::Display) -> TimeError {
    TimeError::new(format!("invalid ISO time {text:?}: {error}"))
}

fn date_of(date: jiff::civil::Date) -> Option<Date> {
    Date::new(
        i32::from(date.year()),
        date.month() as u32,
        date.day() as u32,
    )
}

/// An ISO date-time, with or without an offset (`Z` is UTC).
pub fn parse_datetime(text: &str) -> Result<ParsedDateTime, TimeError> {
    let pieces = Pieces::parse(text).map_err(|e| invalid(text, e))?;
    let time = pieces.time().unwrap_or(jiff::civil::Time::midnight());
    let date = date_of(pieces.date()).ok_or_else(|| invalid(text, "out of range"))?;
    let wall = NaiveDateTime::new(
        date,
        time.hour() as u32,
        time.minute() as u32,
        time.second() as u32,
        (time.subsec_nanosecond() / 1000) as u32,
    )
    .ok_or_else(|| invalid(text, "out of range"))?;
    let offset_us = pieces.offset().map(|offset| match offset {
        PiecesOffset::Numeric(numeric) => i64::from(numeric.offset().seconds()) * 1_000_000,
        _ => 0,
    });
    Ok(ParsedDateTime { wall, offset_us })
}

/// An ISO calendar date (`2026-10-05`).
pub fn parse_date(text: &str) -> Result<Date, TimeError> {
    let date = text
        .parse::<jiff::civil::Date>()
        .map_err(|e| invalid(text, e))?;
    date_of(date).ok_or_else(|| invalid(text, "out of range"))
}
