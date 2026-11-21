//! `datetime.strptime` for the numeric layouts sources use (`%d %m %Y %H %M %S %f %%`), with
//! Python's own regular expressions (`_strptime.TimeRE`): `%d` takes one or two digits (or a
//! space and one), whitespace in the layout matches any run of whitespace, matching ignores
//! case, and the match must consume the whole text. `times.read_local`'s fast path accepts a
//! subset of what `strptime` accepts and reads it the same, so only `strptime` is mirrored.
//!
//! Digits are ASCII only: Python's `\d` also takes other Unicode digits (none seen in a
//! source).

use regex::Regex;

use super::{Date, NaiveDateTime};

/// A compiled `strptime` layout.
#[derive(Debug, Clone)]
pub struct Layout {
    text: String,
    regex: Regex,
}

fn directive(c: char) -> &'static str {
    match c {
        'd' => r"(?P<d>3[01]|[12][0-9]|0[1-9]|[1-9]| [1-9])",
        'f' => r"(?P<f>[0-9]{1,6})",
        'H' => r"(?P<H>2[0-3]|[0-1][0-9]|[0-9])",
        'm' => r"(?P<m>1[0-2]|0[1-9]|[1-9])",
        'M' => r"(?P<M>[0-5][0-9]|[0-9])",
        'S' => r"(?P<S>6[0-1]|[0-5][0-9]|[0-9])",
        'Y' => r"(?P<Y>[0-9][0-9][0-9][0-9])",
        '%' => "%",
        other => panic!("strptime directive %{other} is not supported"),
    }
}

impl Layout {
    /// Compiles a layout; panics on a directive the sources do not use (a programming error).
    pub fn new(layout: &str) -> Layout {
        let mut pattern = String::from(r"(?i)^(?:");
        let mut chars = layout.chars().peekable();
        while let Some(c) = chars.next() {
            if c == '%' {
                let next = chars.next().expect("a directive after %");
                pattern.push_str(directive(next));
            } else if c.is_whitespace() {
                while chars.peek().is_some_and(|c| c.is_whitespace()) {
                    chars.next();
                }
                pattern.push_str(r"\s+");
            } else {
                pattern.push_str(&regex::escape(&c.to_string()));
            }
        }
        pattern.push(')');
        Layout {
            text: layout.to_owned(),
            regex: Regex::new(&pattern).expect("a valid strptime pattern"),
        }
    }

    pub fn as_str(&self) -> &str {
        &self.text
    }

    /// The naive date-time `text` reads as, or None (no match, text left over, or a field out
    /// of range: Python's `ValueError`). Missing fields default as Python's do (1900-01-01).
    pub fn parse(&self, text: &str) -> Option<NaiveDateTime> {
        let found = self.regex.captures(text)?;
        if found.get(0)?.end() != text.len() {
            return None;
        }
        let field = |name: &str, default: u32| -> u32 {
            found
                .name(name)
                .map_or(default, |m| m.as_str().trim().parse().expect("digits"))
        };
        let micro = found.name("f").map_or(0, |m| {
            let digits = m.as_str();
            digits.parse::<u32>().expect("digits") * 10_u32.pow(6 - digits.len() as u32)
        });
        let date = Date::new(field("Y", 1900) as i32, field("m", 1), field("d", 1))?;
        NaiveDateTime::new(date, field("H", 0), field("M", 0), field("S", 0), micro)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn python_strptime_readings() {
        let layout = Layout::new("%d.%m.%Y %H:%M:%S");
        let at = |text: &str| layout.parse(text).map(|t| t.isoformat());
        assert_eq!(
            at("8.10.2026 9:5:7").as_deref(),
            Some("2026-10-08T09:05:07")
        );
        assert_eq!(
            at(" 8.10.2026  22:30:00").as_deref(),
            Some("2026-10-08T22:30:00")
        );
        assert_eq!(at("31.02.2026 10:00:00"), None);
        assert_eq!(at("08.10.2026 24:00:00"), None);
        assert_eq!(at("08.10.2026 10:00:60"), None);
        assert_eq!(at("08.10.2026 10:00:00x"), None);
        let iso = Layout::new("%Y-%m-%dT%H:%M:%S");
        assert_eq!(
            iso.parse("2026-10-08t22:30:00")
                .map(|t| t.isoformat())
                .as_deref(),
            Some("2026-10-08T22:30:00")
        );
    }
}
