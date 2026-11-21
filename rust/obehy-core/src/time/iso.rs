//! `datetime.fromisoformat` and `date.fromisoformat` as CPython 3.13 implements them
//! (`_datetimemodule.c`), quirks included: any single character separates date and time, a
//! fraction of any length is cut to microseconds, one stray character may stand between the
//! time and its offset, and an offset may carry seconds and microseconds.

use super::civil::days_in_month;
use super::{Date, Instant, NaiveDateTime, TimeError};

/// A parsed ISO date-time: its digits and its offset (None: naive).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ParsedDateTime {
    pub wall: NaiveDateTime,
    pub offset_us: Option<i64>,
}

impl ParsedDateTime {
    /// `instant(value)`: the UTC instant of an aware time; a naive one is an error.
    pub fn instant(self) -> Result<Instant, TimeError> {
        let Some(offset_us) = self.offset_us else {
            return Err(TimeError::new(format!(
                "naive datetime {} cannot be an Instant",
                self.wall.isoformat()
            )));
        };
        let utc = self.wall.micros() - offset_us;
        let year = NaiveDateTime::from_micros(utc).date().year();
        if !(1..=9999).contains(&year) {
            return Err(TimeError::new("date value out of range"));
        }
        Ok(Instant::from_micros(utc))
    }
}

/// C-string view: bytes past the end read as NUL, as CPython's parser reads them.
struct Text<'a>(&'a [u8]);

impl Text<'_> {
    fn at(&self, index: usize) -> u8 {
        self.0.get(index).copied().unwrap_or(0)
    }
}

fn invalid(text: &str) -> TimeError {
    TimeError::new(format!("Invalid isoformat string: {text:?}"))
}

/// `parse_digits`: exactly `count` ASCII digits from `at`; the position after them.
fn digits(text: &Text<'_>, at: usize, count: usize, value: &mut i64) -> Option<usize> {
    for i in 0..count {
        let digit = text.at(at + i).wrapping_sub(b'0');
        if digit > 9 {
            return None;
        }
        *value = *value * 10 + i64::from(digit);
    }
    Some(at + count)
}

fn iso_to_ymd(year: i64, week: i64, day: i64) -> Result<(i64, i64, i64), i32> {
    if !(1..=9999).contains(&year) {
        return Err(-2);
    }
    if week <= 0 || week >= 53 {
        let mut out_of_range = true;
        if week == 53 {
            // Week 53 exists if the year starts on a Thursday, or a Wednesday in a leap year.
            let first_weekday = Date::ymd(year as i32, 1, 1)
                .days_since_epoch()
                .rem_euclid(7);
            // 1970-01-01 was a Thursday: 0 = Thursday.
            let leap = days_in_month(year as i32, 2) == 29;
            if first_weekday == 0 || (first_weekday == 6 && leap) {
                out_of_range = false;
            }
        }
        if out_of_range {
            return Err(-2);
        }
    }
    if day <= 0 || day >= 8 {
        return Err(-3);
    }
    // Monday of week 1: the week with the year's first Thursday.
    let jan1 = i64::from(Date::ymd(year as i32, 1, 1).days_since_epoch());
    let jan1_weekday = (jan1 + 3).rem_euclid(7); // 0 = Monday
    let mut week1_monday = jan1 - jan1_weekday;
    if jan1_weekday > 3 {
        week1_monday += 7;
    }
    let ordinal = week1_monday + (week - 1) * 7 + (day - 1);
    let date = Date::from_days(ordinal as i32);
    Ok((
        i64::from(date.year()),
        i64::from(date.month()),
        i64::from(date.day()),
    ))
}

/// `parse_isoformat_date`: (year, month, day) unchecked, or a negative code.
fn parse_date_part(text: &Text<'_>, len: usize) -> Result<(i64, i64, i64), i32> {
    let mut year = 0;
    let mut p = digits(text, 0, 4, &mut year).ok_or(-1)?;
    let uses_separator = text.at(p) == b'-';
    if uses_separator {
        p += 1;
    }
    if text.at(p) == b'W' {
        p += 1;
        let mut week = 0;
        p = digits(text, p, 2, &mut week).ok_or(-3)?;
        let mut day = 0;
        if p < len {
            if uses_separator {
                if text.at(p) != b'-' {
                    return Err(-2);
                }
                p += 1;
            }
            digits(text, p, 1, &mut day).ok_or(-4)?;
        } else {
            day = 1;
        }
        return iso_to_ymd(year, week, day).map_err(|code| -3 + code);
    }
    let mut month = 0;
    p = digits(text, p, 2, &mut month).ok_or(-1)?;
    if uses_separator {
        if text.at(p) != b'-' {
            return Err(-2);
        }
        p += 1;
    }
    let mut day = 0;
    digits(text, p, 2, &mut day).ok_or(-1)?;
    Ok((year, month, day))
}

/// `parse_hh_mm_ss_ff` over `[start, end)`: Ok(true) when something is left over.
fn parse_time_fields(
    text: &Text<'_>,
    start: usize,
    end: usize,
    out: &mut [i64; 4],
) -> Result<bool, i32> {
    *out = [0; 4];
    let mut p = start;
    let mut has_separator = true;
    for (i, slot) in out.iter_mut().take(3).enumerate() {
        p = digits(text, p, 2, slot).ok_or(-3)?;
        let c = text.at(p);
        p += 1;
        if i == 0 {
            has_separator = c == b':';
        }
        if p >= end {
            return Ok(c != 0);
        } else if has_separator && c == b':' {
            continue;
        } else if c == b'.' || c == b',' {
            break;
        } else if !has_separator {
            p -= 1;
        } else {
            return Err(-4);
        }
    }
    let remains = end.saturating_sub(p);
    let to_parse = remains.min(6);
    p = digits(text, p, to_parse, &mut out[3]).ok_or(-3)?;
    if to_parse < 6 {
        // CPython multiplies by correction[to_parse - 1]; with nothing to parse it reads
        // before the table, which cannot happen: the loop returned when `p >= end`.
        out[3] *= 10_i64.pow((6 - to_parse) as u32);
    }
    while text.at(p).is_ascii_digit() {
        p += 1;
    }
    Ok(text.at(p) != 0)
}

/// `parse_isoformat_time`: (h, m, s, µs, offset in µs if any).
fn parse_time_part(
    text: &Text<'_>,
    start: usize,
    end: usize,
) -> Result<([i64; 4], Option<i64>), i32> {
    let mut tz = start;
    loop {
        let c = text.at(tz);
        if c == b'Z' || c == b'+' || c == b'-' {
            break;
        }
        tz += 1;
        if tz >= end {
            break;
        }
    }
    let mut fields = [0; 4];
    let rest = parse_time_fields(text, start, tz, &mut fields)?;
    if tz == end {
        return if rest { Err(-5) } else { Ok((fields, None)) };
    }
    if text.at(tz) == b'Z' {
        return if text.at(tz + 1) != 0 {
            Err(-5)
        } else {
            Ok((fields, Some(0)))
        };
    }
    let sign = if text.at(tz) == b'-' { -1 } else { 1 };
    let mut zone = [0; 4];
    let rest = parse_time_fields(text, tz + 1, end, &mut zone)?;
    if rest {
        return Err(-5);
    }
    let seconds = zone[0] * 3600 + zone[1] * 60 + zone[2];
    Ok((fields, Some(sign * (seconds * 1_000_000 + zone[3]))))
}

/// `_find_isoformat_datetime_separator`.
fn separator(text: &Text<'_>, len: usize) -> Option<usize> {
    if len == 7 {
        return Some(7);
    }
    if text.at(4) == b'-' {
        if text.at(5) == b'W' {
            if len < 8 {
                return None;
            }
            if len > 8 && text.at(8) == b'-' {
                if len == 9 {
                    return None;
                }
                if len > 10 && text.at(10).is_ascii_digit() {
                    return Some(8);
                }
                return Some(10);
            }
            return Some(8);
        }
        return Some(10);
    }
    if text.at(4) == b'W' {
        let mut idx = 7;
        while idx < len && text.at(idx).is_ascii_digit() {
            idx += 1;
        }
        if idx < 9 {
            return Some(idx);
        }
        return Some(if idx % 2 == 0 { 7 } else { 8 });
    }
    Some(8)
}

fn checked_date(year: i64, month: i64, day: i64) -> Result<Date, TimeError> {
    if !(1..=9999).contains(&year) {
        return Err(TimeError::new(format!("year {year} is out of range")));
    }
    if !(1..=12).contains(&month) {
        return Err(TimeError::new("month must be in 1..12"));
    }
    Date::new(year as i32, month as u32, day as u32)
        .ok_or_else(|| TimeError::new("day is out of range for month"))
}

/// `datetime.fromisoformat(text)`.
pub fn parse_datetime(text: &str) -> Result<ParsedDateTime, TimeError> {
    let bytes = text.as_bytes();
    let view = Text(bytes);
    let len = bytes.len();
    let at = separator(&view, len).ok_or_else(|| invalid(text))?;
    let (year, month, day) = parse_date_part(&view, at).map_err(|_| invalid(text))?;
    let (fields, offset_us) = if len > at {
        // The separator is any one character, ASCII or not.
        let width = text[at..].chars().next().map_or(1, char::len_utf8);
        parse_time_part(&view, at + width, len).map_err(|_| invalid(text))?
    } else {
        ([0; 4], None)
    };
    let date = checked_date(year, month, day)?;
    let [hour, minute, second, micro] = fields;
    if hour > 23 {
        return Err(TimeError::new("hour must be in 0..23"));
    }
    if minute > 59 {
        return Err(TimeError::new("minute must be in 0..59"));
    }
    if second > 59 {
        return Err(TimeError::new("second must be in 0..59"));
    }
    if let Some(offset) = offset_us
        && offset.abs() >= 86_400 * 1_000_000
    {
        return Err(TimeError::new(
            "offset must be strictly between -24 h and 24 h",
        ));
    }
    let wall = NaiveDateTime::new(
        date,
        hour as u32,
        minute as u32,
        second as u32,
        micro as u32,
    )
    .expect("fields checked");
    Ok(ParsedDateTime { wall, offset_us })
}

/// `date.fromisoformat(text)`.
pub fn parse_date(text: &str) -> Result<Date, TimeError> {
    let len = text.len();
    if !matches!(len, 7 | 8 | 10) {
        return Err(invalid(text));
    }
    let (year, month, day) =
        parse_date_part(&Text(text.as_bytes()), len).map_err(|_| invalid(text))?;
    checked_date(year, month, day)
}
