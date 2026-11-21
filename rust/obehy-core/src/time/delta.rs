//! Durations in exact microseconds, rounded as Python's `timedelta` rounds.
//!
//! Python's `timedelta` arithmetic is exact: `td * float` and `td / float` go through
//! `float.as_integer_ratio()` and round half to even, `td / int` rounds half to even (not
//! toward zero), `td // int` floors, and `timedelta(seconds=float)` has its own rounding
//! (`from_secs_f64`). Each is mirrored here, so durations never differ by a microsecond.

use std::ops::{Add, Neg, Sub};

use crate::py::num::py_floor_div;

/// A signed duration in microseconds (Python's `timedelta`).
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

    /// `timedelta(seconds=value)` for a float, rounded exactly as CPython rounds it: the whole
    /// seconds exactly, the fraction times 10^6 in floating point, its whole part exactly, and
    /// what remains rounded half to even against the total. Panics on NaN or infinity (Python
    /// raises).
    pub fn from_secs_f64(value: f64) -> TimeDelta {
        assert!(
            value.is_finite(),
            "timedelta(seconds={value}) is not a duration"
        );
        let whole_s = value.trunc();
        let fraction = value - whole_s; // modf: exact
        let mut micros = (whole_s as i128) * 1_000_000;
        if fraction != 0.0 {
            let scaled = 1e6 * fraction;
            let whole_us = scaled.trunc();
            micros += whole_us as i128;
            let leftover = scaled - whole_us;
            if leftover != 0.0 {
                let mut rounded = leftover.round(); // C round(): half away from zero
                if (rounded - leftover).abs() == 0.5 {
                    let odd = f64::from(u8::from(micros.rem_euclid(2) == 1));
                    rounded = 2.0 * ((leftover + odd) * 0.5).round() - odd;
                }
                micros += rounded as i128;
            }
        }
        TimeDelta(i64::try_from(micros).expect("timedelta out of range"))
    }

    pub const fn micros(self) -> i64 {
        self.0
    }

    /// `timedelta.total_seconds()`: a correctly rounded division, exact for |µs| < 2^53.
    pub fn total_seconds(self) -> f64 {
        self.0 as f64 / 1e6
    }

    #[must_use]
    pub const fn abs(self) -> TimeDelta {
        TimeDelta(self.0.abs())
    }

    /// `td * factor` (exact, half to even; `times.scaled_micros`).
    #[must_use]
    pub fn mul_f64(self, factor: f64) -> TimeDelta {
        let (numerator, shift) = ratio(factor);
        // factor = numerator * 2^shift exactly.
        let product = i128::from(self.0) * i128::from(numerator);
        let micros = if shift >= 0 {
            product
                .checked_mul(1_i128 << shift)
                .expect("timedelta out of range")
        } else {
            shift_round_half_even(product, (-shift) as u32)
        };
        TimeDelta(i64::try_from(micros).expect("timedelta out of range"))
    }

    /// `td / divisor` for a float (exact, half to even). Panics on zero (Python raises).
    #[must_use]
    pub fn div_f64(self, divisor: f64) -> TimeDelta {
        let (numerator, shift) = ratio(divisor);
        assert!(numerator != 0, "timedelta division by zero");
        // td / (n * 2^s) = td * 2^-s / n
        let micros = if shift <= 0 {
            let scaled = i128::from(self.0)
                .checked_mul(
                    1_i128
                        .checked_shl((-shift) as u32)
                        .expect("timedelta out of range"),
                )
                .expect("timedelta out of range");
            divide_and_round(scaled, i128::from(numerator))
        } else {
            let denominator = i128::from(numerator)
                .checked_mul(
                    1_i128
                        .checked_shl(shift as u32)
                        .expect("timedelta out of range"),
                )
                .expect("timedelta out of range");
            divide_and_round(i128::from(self.0), denominator)
        };
        TimeDelta(i64::try_from(micros).expect("timedelta out of range"))
    }

    /// `td / n` for an integer: half to even, not toward zero. Panics on zero.
    #[must_use]
    pub fn div_int(self, divisor: i64) -> TimeDelta {
        assert!(divisor != 0, "timedelta division by zero");
        let micros = divide_and_round(i128::from(self.0), i128::from(divisor));
        TimeDelta(i64::try_from(micros).expect("timedelta out of range"))
    }

    /// `td // n`: floor.
    #[must_use]
    pub const fn floor_div_int(self, divisor: i64) -> TimeDelta {
        TimeDelta(py_floor_div(self.0, divisor))
    }

    /// `td / td`: a float (correctly rounded int/int division, exact for |µs| < 2^53).
    pub fn div_delta(self, other: TimeDelta) -> f64 {
        assert!(other.0 != 0, "timedelta division by zero");
        self.0 as f64 / other.0 as f64
    }

    /// `td * n` for an integer.
    #[must_use]
    pub const fn mul_int(self, factor: i64) -> TimeDelta {
        TimeDelta(self.0 * factor)
    }
}

/// A finite float as `numerator * 2^shift` exactly (`float.as_integer_ratio()` up to a power
/// of two common to both terms, which changes no rounding). Panics on NaN or infinity.
fn ratio(value: f64) -> (i64, i32) {
    assert!(value.is_finite(), "{value} has no integer ratio");
    let bits = value.to_bits();
    let negative = bits >> 63 == 1;
    let exponent = ((bits >> 52) & 0x7ff) as i32;
    let fraction = (bits & ((1 << 52) - 1)) as i64;
    let (mantissa, shift) = if exponent == 0 {
        (fraction, -1074)
    } else {
        (fraction | (1 << 52), exponent - 1075)
    };
    (if negative { -mantissa } else { mantissa }, shift)
}

/// `value / 2^shift`, rounded half to even (floor division and remainder, as Python's
/// `divmod`).
fn shift_round_half_even(value: i128, shift: u32) -> i128 {
    if shift >= 127 {
        // |value| < 2^117 (an i64 times a 53-bit mantissa): the quotient is below one half.
        return 0;
    }
    divide_and_round(value, 1_i128 << shift)
}

/// Python's `datetime._divide_and_round`: `a / b` rounded half to even, with floor `divmod`.
fn divide_and_round(a: i128, b: i128) -> i128 {
    let mut q = a.div_euclid(b);
    let mut r = a.rem_euclid(b);
    // Python's divmod takes the remainder with the divisor's sign.
    if b < 0 && r != 0 {
        q -= 1;
        r += b;
    }
    let r2 = r * 2;
    let greater_than_half = if b > 0 { r2 > b } else { r2 < b };
    if greater_than_half || (r2 == b && q.rem_euclid(2) == 1) {
        q += 1;
    }
    q
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn integer_division_rounds_half_to_even() {
        let td = TimeDelta::from_micros;
        assert_eq!(td(5).div_int(2), td(2));
        assert_eq!(td(7).div_int(2), td(4));
        assert_eq!(td(-5).div_int(2), td(-2));
        assert_eq!(td(-7).div_int(2), td(-4));
        assert_eq!(td(5).div_int(-2), td(-2));
        assert_eq!(td(-7).floor_div_int(2), td(-4));
        assert_eq!(td(7).floor_div_int(-2), td(-4));
    }

    #[test]
    fn float_seconds_round_like_cpython() {
        let s = TimeDelta::from_secs_f64;
        assert_eq!(s(1.5).micros(), 1_500_000);
        assert_eq!(s(-0.0000005).micros(), 0);
        assert_eq!(s(0.0000005).micros(), 0);
        assert_eq!(s(0.0000015).micros(), 2);
        assert_eq!(s(1e-7).micros(), 0);
        assert_eq!(s(-1.0000025).micros(), -1_000_002);
    }

    #[test]
    fn tiny_and_large_factors() {
        let td = TimeDelta::from_micros(123_456_789);
        assert_eq!(td.mul_f64(1e-300), TimeDelta::ZERO);
        assert_eq!(td.mul_f64(f64::from_bits(1)), TimeDelta::ZERO);
        assert_eq!(td.mul_f64(1024.0).micros(), 123_456_789 * 1024);
        assert_eq!(td.mul_f64(0.5).micros(), 61_728_394); // 61_728_394.5 -> even
        assert_eq!(td.div_f64(2.0).micros(), 61_728_394);
        assert_eq!(TimeDelta::from_micros(3).mul_f64(-0.5).micros(), -2); // -1.5 -> -2
    }
}
