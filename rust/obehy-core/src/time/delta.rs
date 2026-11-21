//! Durations in whole microseconds.

use std::ops::{Add, Neg, Sub};

/// A signed duration in microseconds.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Default)]
pub struct TimeDelta(i64);

impl TimeDelta {
    pub const ZERO: TimeDelta = TimeDelta(0);

    pub const fn from_micros(micros: i64) -> TimeDelta {
        TimeDelta(micros)
    }

    pub const fn from_seconds(seconds: i64) -> TimeDelta {
        TimeDelta(seconds * 1_000_000)
    }

    pub const fn from_minutes(minutes: i64) -> TimeDelta {
        TimeDelta::from_seconds(minutes * 60)
    }

    pub const fn from_hours(hours: i64) -> TimeDelta {
        TimeDelta::from_seconds(hours * 3600)
    }

    pub const fn from_days(days: i64) -> TimeDelta {
        TimeDelta::from_seconds(days * 86_400)
    }

    /// Float seconds, rounded to the nearest microsecond.
    pub fn from_secs_f64(seconds: f64) -> TimeDelta {
        debug_assert!(seconds.is_finite(), "{seconds} s is not a duration");
        TimeDelta((seconds * 1e6).round() as i64)
    }

    pub const fn micros(self) -> i64 {
        self.0
    }

    pub fn total_seconds(self) -> f64 {
        self.0 as f64 / 1e6
    }

    #[must_use]
    pub const fn abs(self) -> TimeDelta {
        TimeDelta(self.0.abs())
    }

    /// Scaled by `factor`, rounded to the nearest microsecond.
    #[must_use]
    pub fn mul_f64(self, factor: f64) -> TimeDelta {
        TimeDelta((self.0 as f64 * factor).round() as i64)
    }

    /// Divided by `divisor`, rounded to the nearest microsecond.
    #[must_use]
    pub fn div_f64(self, divisor: f64) -> TimeDelta {
        TimeDelta((self.0 as f64 / divisor).round() as i64)
    }

    /// Divided by an integer, toward zero.
    #[must_use]
    pub const fn div_int(self, divisor: i64) -> TimeDelta {
        TimeDelta(self.0 / divisor)
    }

    /// Divided by an integer, toward negative infinity.
    #[must_use]
    pub const fn floor_div_int(self, divisor: i64) -> TimeDelta {
        TimeDelta(self.0.div_euclid(divisor))
    }

    /// The ratio of two durations.
    pub fn div_delta(self, other: TimeDelta) -> f64 {
        self.0 as f64 / other.0 as f64
    }

    #[must_use]
    pub const fn mul_int(self, factor: i64) -> TimeDelta {
        TimeDelta(self.0 * factor)
    }
}

impl Add for TimeDelta {
    type Output = TimeDelta;
    fn add(self, rhs: TimeDelta) -> TimeDelta {
        TimeDelta(self.0 + rhs.0)
    }
}

impl Sub for TimeDelta {
    type Output = TimeDelta;
    fn sub(self, rhs: TimeDelta) -> TimeDelta {
        TimeDelta(self.0 - rhs.0)
    }
}

impl Neg for TimeDelta {
    type Output = TimeDelta;
    fn neg(self) -> TimeDelta {
        TimeDelta(-self.0)
    }
}
