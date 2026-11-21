//! Source date-time layouts (`%d.%m.%Y %H:%M:%S`), parsed with jiff's `strptime`.

use super::{Date, NaiveDateTime};

/// A `strptime` layout.
#[derive(Debug, Clone)]
pub struct Layout(String);

impl Layout {
    pub fn new(layout: &str) -> Layout {
        Layout(layout.to_owned())
    }

    pub fn as_str(&self) -> &str {
        &self.0
    }

    /// The naive date-time `text` reads as, or None.
    pub fn parse(&self, text: &str) -> Option<NaiveDateTime> {
        let at = jiff::civil::DateTime::strptime(&self.0, text.trim()).ok()?;
        let date = Date::new(i32::from(at.year()), at.month() as u32, at.day() as u32)?;
        NaiveDateTime::new(
            date,
            at.hour() as u32,
            at.minute() as u32,
            at.second() as u32,
            (at.subsec_nanosecond() / 1000) as u32,
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn source_layouts() {
        let layout = Layout::new("%d.%m.%Y %H:%M:%S");
        let at = |text: &str| layout.parse(text).map(|t| t.isoformat());
        assert_eq!(
            at("08.10.2026 22:30:00").as_deref(),
            Some("2026-10-08T22:30:00")
        );
        assert_eq!(
            at(" 8.10.2026 09:05:07 ").as_deref(),
            Some("2026-10-08T09:05:07")
        );
        assert_eq!(at("31.02.2026 10:00:00"), None);
        assert_eq!(at("08.10.2026 24:00:00"), None);
        assert_eq!(at("08.10.2026 10:00:00x"), None);
    }
}
