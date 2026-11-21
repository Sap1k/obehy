//! The realtime processes' log line (port of `realtime/logs.py`): a UTC timestamp to the second
//! and the message, on stdout, flushed. The worker, the recorder and egress all log alike.

use std::io::Write as _;
use std::sync::Arc;

use obehy_core::time::{Instant, now};

/// Where a component logs; tests pass a collector.
pub type Log = Arc<dyn Fn(&str) + Send + Sync>;

/// `now().isoformat(timespec="seconds")`: `2026-11-21T10:00:00+00:00`.
pub fn stamp(at: Instant) -> String {
    let utc = at.utc();
    let (hour, minute, second, _) = utc.hms_micro();
    format!(
        "{}T{hour:02}:{minute:02}:{second:02}+00:00",
        utc.date().isoformat()
    )
}

pub fn log(message: &str) {
    let mut out = std::io::stdout().lock();
    let _ = writeln!(out, "{} {message}", stamp(now()));
    let _ = out.flush();
}

/// [`log`] as a [`Log`].
pub fn stdout() -> Log {
    Arc::new(log)
}

#[cfg(test)]
mod tests {
    use super::*;
    use obehy_core::time::Date;

    #[test]
    fn a_log_line_is_stamped_to_the_second_in_utc() {
        let at = Instant::from_utc(Date::ymd(2026, 10, 5).midnight())
            + obehy_core::time::TimeDelta::from_micros(3_723_999_999);
        assert_eq!(stamp(at), "2026-10-05T01:02:03+00:00");
    }
}
