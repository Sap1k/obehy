//! Python's numeric semantics where they differ from Rust's (rust/docs/PORTING_HAZARDS.md):
//! `round` (half to even), `min`/`max` (the first of equal values wins, never `f64::min`),
//! floored `//` and `%`, CPython's own `math.hypot`, and `float.__repr__`.

/// `round(x)` for a float: half to even (an int in Python; a whole float here).
pub fn py_round(x: f64) -> f64 {
    x.round_ties_even()
}

/// `round(x)` as Python's int; panics on NaN or infinity (Python raises).
pub fn py_round_int(x: f64) -> i64 {
    assert!(x.is_finite(), "cannot round {x} to an integer");
    x.round_ties_even() as i64
}

/// `round(x, ndigits)`: the exact binary value correctly rounded to `ndigits` decimals, half
/// to even, read back as a float (CPython goes through `_Py_dg_dtoa` mode 3 and `strtod`;
/// Rust's fixed-precision formatting rounds the exact value the same way).
pub fn py_round_digits(x: f64, ndigits: usize) -> f64 {
    if !x.is_finite() {
        return x;
    }
    let rounded: f64 = format!("{x:.ndigits$}")
        .parse()
        .expect("a formatted float parses");
    // Python keeps the sign of a value rounded to zero.
    if rounded == 0.0 {
        0.0_f64.copysign(x)
    } else {
        rounded
    }
}

/// `max(a, b)`: `b` only if it is greater, so the first wins ties (-0.0 vs 0.0) and NaN
/// comparisons keep `a`.
pub fn py_max(a: f64, b: f64) -> f64 {
    if b > a { b } else { a }
}

/// `min(a, b)`: `b` only if it is less.
pub fn py_min(a: f64, b: f64) -> f64 {
    if b < a { b } else { a }
}

/// Python's `a // b` on integers: floor, whatever the signs. Panics on zero.
pub const fn py_floor_div(a: i64, b: i64) -> i64 {
    let q = a / b;
    if a % b != 0 && ((a < 0) != (b < 0)) {
        q - 1
    } else {
        q
    }
}

/// Python's `a % b` on integers: the remainder takes the divisor's sign.
pub const fn py_mod(a: i64, b: i64) -> i64 {
    a - py_floor_div(a, b) * b
}

/// Python's `a % b` on floats (`float_rem`): `fmod`, moved to the divisor's sign; a zero
/// result takes the divisor's sign (`-0.0 % 360.0` is `0.0`). Panics on a zero divisor.
pub fn py_fmod(a: f64, b: f64) -> f64 {
    assert!(b != 0.0, "float modulo by zero");
    let mut m = a % b; // C fmod
    if m != 0.0 {
        if (b < 0.0) != (m < 0.0) {
            m += b;
        }
    } else {
        m = 0.0_f64.copysign(b);
    }
    m
}

/// Python's `a // b` on floats (`float_floor_div`, from `float_divmod`). Panics on zero.
pub fn py_ffloordiv(a: f64, b: f64) -> f64 {
    assert!(b != 0.0, "float floor division by zero");
    let m = a % b;
    let mut div = (a - m) / b;
    if m != 0.0 && ((b < 0.0) != (m < 0.0)) {
        div -= 1.0;
    }
    if div != 0.0 {
        let floor = div.floor();
        if div - floor > 0.5 {
            floor + 1.0
        } else {
            floor
        }
    } else {
        0.0_f64.copysign(a / b)
    }
}

/// `frexp`: (m, e) with x = m * 2^e and 0.5 <= |m| < 1, for finite nonzero x.
fn frexp(x: f64) -> (f64, i32) {
    let bits = x.to_bits();
    let exponent = ((bits >> 52) & 0x7ff) as i32;
    if exponent == 0 {
        let (m, e) = frexp(x * f64::from_bits(0x4350_0000_0000_0000)); // 2^54
        return (m, e - 54);
    }
    let m = f64::from_bits((bits & !(0x7ff << 52)) | (1022 << 52));
    (m, exponent - 1022)
}

fn ldexp(x: f64, e: i32) -> f64 {
    x * 2_f64.powi(e)
}

/// Dekker product: (hi, lo) with hi + lo = x * y exactly.
fn dl_mul(x: f64, y: f64) -> (f64, f64) {
    let z = x * y;
    (z, x.mul_add(y, -z))
}

/// Fast two-sum for |a| >= |b|.
fn dl_fast_sum(a: f64, b: f64) -> (f64, f64) {
    let x = a + b;
    let z = x - a;
    (x, b - z)
}

/// CPython's `vector_norm` (Modules/mathmodule.c, 3.12+), op for op.
fn vector_norm(vec: &mut [f64], max: f64, found_nan: bool) -> f64 {
    if max.is_infinite() {
        return max;
    }
    if found_nan {
        return f64::NAN;
    }
    if max == 0.0 || vec.len() <= 1 {
        return max;
    }
    let (_, max_e) = frexp(max);
    if max_e < -1023 {
        for x in vec.iter_mut() {
            *x /= f64::MIN_POSITIVE;
        }
        return f64::MIN_POSITIVE * vector_norm(vec, max / f64::MIN_POSITIVE, found_nan);
    }
    let scale = ldexp(1.0, -max_e);
    let (mut csum, mut frac1, mut frac2) = (1.0, 0.0, 0.0);
    for &x in vec.iter() {
        let x = x * scale;
        let (hi, lo) = dl_mul(x, x);
        let (sum, rest) = dl_fast_sum(csum, hi);
        csum = sum;
        frac1 += lo;
        frac2 += rest;
    }
    let mut h = (csum - 1.0 + (frac1 + frac2)).sqrt();
    let (hi, lo) = dl_mul(-h, h);
    let (sum, rest) = dl_fast_sum(csum, hi);
    csum = sum;
    frac1 += lo;
    frac2 += rest;
    let x = csum - 1.0 + (frac1 + frac2);
    h += x / (2.0 * h);
    h / scale
}

/// `math.hypot(*coordinates)`: CPython's own correctly rounded norm, not libm's `hypot`.
pub fn py_hypot(coordinates: &[f64]) -> f64 {
    let mut vec: Vec<f64> = coordinates.iter().map(|x| x.abs()).collect();
    let mut max = 0.0_f64;
    let mut found_nan = false;
    for &x in &vec {
        found_nan |= x.is_nan();
        if x > max {
            max = x;
        }
    }
    vector_norm(&mut vec, max, found_nan)
}

/// `float.__repr__` (and `str`): the shortest digits that read back to `x`, in fixed notation
/// for decimal exponents -4 <= e < 16 (with `.0` when integral), else `1e-05` / `1.5e+16`.
pub fn py_float_repr(x: f64) -> String {
    if x.is_nan() {
        return "nan".to_owned();
    }
    if x.is_infinite() {
        return if x > 0.0 { "inf" } else { "-inf" }.to_owned();
    }
    if x == 0.0 {
        return if x.is_sign_negative() { "-0.0" } else { "0.0" }.to_owned();
    }
    // Rust's `{:e}` gives the shortest round-trip length, but where two digit strings of that
    // length both read back it may not pick the nearer one, as dtoa's mode 0 does: format the
    // exact value correctly rounded to that many digits instead (still reads back to `x`).
    let shortest = format!("{:e}", x.abs());
    let length = shortest
        .split_once('e')
        .expect("an exponent")
        .0
        .replace('.', "")
        .len();
    let scientific = format!("{:.*e}", length - 1, x.abs());
    debug_assert_eq!(scientific.parse::<f64>().ok(), Some(x.abs()));
    let (mantissa, exponent) = scientific.split_once('e').expect("an exponent");
    let digits: String = mantissa.chars().filter(|c| *c != '.').collect();
    let exponent: i32 = exponent.parse().expect("an integer exponent");
    let decpt = exponent + 1; // digits are 0.d1d2... * 10^decpt
    let mut out = String::with_capacity(digits.len() + 8);
    if x < 0.0 {
        out.push('-');
    }
    if decpt <= -4 || decpt > 16 {
        out.push_str(&digits[..1]);
        if digits.len() > 1 {
            out.push('.');
            out.push_str(&digits[1..]);
        }
        let e = decpt - 1;
        out.push('e');
        out.push(if e < 0 { '-' } else { '+' });
        out.push_str(&format!("{:02}", e.abs()));
    } else if decpt <= 0 {
        out.push_str("0.");
        out.extend(std::iter::repeat_n('0', (-decpt) as usize));
        out.push_str(&digits);
    } else {
        let point = decpt as usize;
        if point >= digits.len() {
            out.push_str(&digits);
            out.extend(std::iter::repeat_n('0', point - digits.len()));
            out.push_str(".0");
        } else {
            out.push_str(&digits[..point]);
            out.push('.');
            out.push_str(&digits[point..]);
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn repr_edges() {
        for (x, text) in [
            (1.0, "1.0"),
            (0.1, "0.1"),
            (1e16, "1e+16"),
            (1e15, "1000000000000000.0"),
            (123456789012345680.0, "1.2345678901234568e+17"),
            (0.0001, "0.0001"),
            (0.00001, "1e-05"),
            (1.5e-7, "1.5e-07"),
            (-0.0, "-0.0"),
            (5e-324, "5e-324"),
            (1.7976931348623157e308, "1.7976931348623157e+308"),
            (50.123456, "50.123456"),
            (-14.5, "-14.5"),
            (9007199254740993.0, "9007199254740992.0"),
        ] {
            assert_eq!(py_float_repr(x), text);
        }
    }

    #[test]
    fn rounding_and_order() {
        assert_eq!(py_round(2.5), 2.0);
        assert_eq!(py_round(-0.5), -0.0);
        assert_eq!(py_round_digits(2.675, 2), 2.67); // the binary value is below 2.675
        assert_eq!(py_round_digits(50.1234565, 6), 50.123457);
        assert!(py_round_digits(-0.0000001, 3).is_sign_negative());
        assert!(py_max(0.0, -0.0).is_sign_positive() && py_max(-0.0, 0.0).is_sign_negative());
        assert_eq!(py_floor_div(-7, 2), -4);
        assert_eq!(py_mod(-7, 2), 1);
        assert_eq!(py_mod(7, -2), -1);
        assert!(py_fmod(-0.0, 360.0).is_sign_positive());
        assert_eq!(py_fmod(-90.0, 360.0), 270.0);
        assert_eq!(py_ffloordiv(-7.0, 2.0), -4.0);
    }

    #[test]
    fn hypot_is_cpythons() {
        assert_eq!(py_hypot(&[3.0, 4.0]), 5.0);
        assert_eq!(py_hypot(&[0.0, 0.0]), 0.0);
        assert!(py_hypot(&[f64::INFINITY, f64::NAN]).is_infinite());
    }
}
