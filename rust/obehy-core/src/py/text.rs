//! Python's `str` methods and the `int()`/`float()` readings of text, with Python's Unicode
//! tables (`unicode.rs`, generated from this Python's `unicodedata`; rust/tools/gen_tables.py).
//!
//! - `isdecimal` (Unicode Nd) is what `int()` and `float()` read as digits; `isdigit` is wider
//!   (Numeric_Type Digit: `²` passes `isdigit` and fails `int()`).
//! - `isspace` is what `strip()`, `split()` and `int()` skip; it also counts U+001C..U+001F.

use super::unicode::{CASEFOLD, DECIMAL, DIGIT, SPACE};

fn in_ranges(c: char, table: &[(u32, u32)]) -> bool {
    let cp = c as u32;
    table
        .binary_search_by(|&(lo, hi)| {
            if hi < cp {
                std::cmp::Ordering::Less
            } else if lo > cp {
                std::cmp::Ordering::Greater
            } else {
                std::cmp::Ordering::Equal
            }
        })
        .is_ok()
}

/// The digit value of a character `str.isdecimal` accepts.
pub fn decimal_value(c: char) -> Option<u32> {
    if c.is_ascii_digit() {
        return Some(c as u32 - '0' as u32);
    }
    let cp = c as u32;
    let i = DECIMAL.partition_point(|&(_, hi, _)| hi < cp);
    DECIMAL
        .get(i)
        .filter(|&&(lo, _, _)| lo <= cp)
        .map(|&(lo, _, value)| value + cp - lo)
}

pub fn is_decimal_char(c: char) -> bool {
    decimal_value(c).is_some()
}

pub fn is_digit_char(c: char) -> bool {
    c.is_ascii_digit() || in_ranges(c, DIGIT)
}

pub fn is_space_char(c: char) -> bool {
    matches!(c, ' ' | '\t'..='\r') || (c as u32 > 0x7f || c < ' ') && in_ranges(c, SPACE)
}

/// `str.isdigit()`: non-empty and every character a digit.
pub fn isdigit(text: &str) -> bool {
    !text.is_empty() && text.chars().all(is_digit_char)
}

/// `str.isdecimal()`.
pub fn isdecimal(text: &str) -> bool {
    !text.is_empty() && text.chars().all(is_decimal_char)
}

/// `str.strip()`.
pub fn strip(text: &str) -> &str {
    text.trim_matches(is_space_char)
}

/// `str.split()` without arguments.
pub fn split(text: &str) -> Vec<&str> {
    text.split(is_space_char)
        .filter(|p| !p.is_empty())
        .collect()
}

/// `str.casefold()`: Python's full Unicode case folding, character by character (`ß` folds to
/// `ss`, `İ` to `i̇`); not `to_lowercase`.
pub fn casefold(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for c in text.chars() {
        if c.is_ascii() {
            out.push(c.to_ascii_lowercase());
            continue;
        }
        let cp = c as u32;
        match CASEFOLD.binary_search_by_key(&cp, |&(from, _)| from) {
            Ok(i) => out.push_str(CASEFOLD[i].1),
            Err(_) => out.push(c),
        }
    }
    out
}

/// `" ".join(text.split())`.
pub fn normalize_space(text: &str) -> String {
    split(text).join(" ")
}

/// The text `int()` and `float()` read: stripped, decimals as ASCII digits (CPython's
/// `_PyUnicode_TransformDecimalAndSpaceToASCII`); None if a non-ASCII character is left.
fn ascii_number(text: &str) -> Option<String> {
    let mut out = String::with_capacity(text.len());
    for c in strip(text).chars() {
        if c.is_ascii() {
            out.push(c);
        } else if let Some(d) = decimal_value(c) {
            out.push(char::from_digit(d, 10).expect("a decimal digit"));
        } else if is_space_char(c) {
            out.push(' ');
        } else {
            return None;
        }
    }
    Some(out)
}

/// Removes PEP 515 underscores: each must sit between two digits. None if one does not.
fn without_underscores(text: &str) -> Option<String> {
    let bytes = text.as_bytes();
    let mut out = String::with_capacity(text.len());
    for (i, &b) in bytes.iter().enumerate() {
        if b == b'_' {
            let before = i > 0 && bytes[i - 1].is_ascii_digit();
            let after = bytes.get(i + 1).is_some_and(u8::is_ascii_digit);
            if !(before && after) {
                return None;
            }
        } else {
            out.push(b as char);
        }
    }
    Some(out)
}

/// An integer as Python holds it: small ones exactly, larger ones by their decimal text.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub enum PyInt {
    Small(i128),
    /// Normalised: optional `-`, no leading zeros.
    Big(String),
}

impl PyInt {
    pub fn small(&self) -> Option<i128> {
        match self {
            PyInt::Small(n) => Some(*n),
            PyInt::Big(_) => None,
        }
    }

    /// `str(n)`.
    pub fn text(&self) -> String {
        match self {
            PyInt::Small(n) => n.to_string(),
            PyInt::Big(t) => t.clone(),
        }
    }

    /// `format(n, f"0{width}d")`: the sign, then zeros up to `width` characters in all.
    pub fn zero_padded(&self, width: usize) -> String {
        let text = self.text();
        let (sign, digits) = match text.strip_prefix('-') {
            Some(d) => ("-", d),
            None => ("", text.as_str()),
        };
        let pad = width.saturating_sub(sign.len() + digits.len());
        format!("{sign}{}{digits}", "0".repeat(pad))
    }

    /// `float(n)`: correctly rounded; None where Python raises OverflowError.
    pub fn to_f64(&self) -> Option<f64> {
        let x: f64 = self.text().parse().ok()?;
        x.is_finite().then_some(x)
    }

    /// From decimal ASCII digits with an optional sign.
    pub fn from_digits(negative: bool, digits: &str) -> PyInt {
        let trimmed = digits.trim_start_matches('0');
        if trimmed.is_empty() {
            return PyInt::Small(0);
        }
        if trimmed.len() <= 38
            && let Ok(n) = trimmed.parse::<i128>()
        {
            return PyInt::Small(if negative { -n } else { n });
        }
        PyInt::Big(format!("{}{trimmed}", if negative { "-" } else { "" }))
    }
}

/// `int(text)` (base 10): surrounding whitespace, a sign, Unicode decimal digits and PEP 515
/// underscores; None where Python raises ValueError.
pub fn int_from_str(text: &str) -> Option<PyInt> {
    let ascii = without_underscores(&ascii_number(text)?)?;
    let (negative, digits) = match ascii.as_bytes().first() {
        Some(b'-') => (true, &ascii[1..]),
        Some(b'+') => (false, &ascii[1..]),
        _ => (false, ascii.as_str()),
    };
    if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    Some(PyInt::from_digits(negative, digits))
}

/// `float(text)`: as [`int_from_str`] plus a fraction, an exponent, `inf`, `infinity` and
/// `nan` in any case; None where Python raises ValueError.
pub fn float_from_str(text: &str) -> Option<f64> {
    let ascii = ascii_number(text)?;
    let body = ascii.trim_start_matches(['+', '-']);
    if ascii.len() - body.len() > 1 {
        return None;
    }
    let lower = body.to_ascii_lowercase();
    if matches!(lower.as_str(), "inf" | "infinity" | "nan") {
        let negative = ascii.starts_with('-');
        let x = if lower == "nan" {
            f64::NAN
        } else {
            f64::INFINITY
        };
        return Some(if negative { -x } else { x });
    }
    let plain = without_underscores(&ascii)?;
    let rest = plain.trim_start_matches(['+', '-']);
    // Python's grammar: digits [. digits] | . digits, then [e [sign] digits].
    let (mantissa, exponent) = match rest.find(['e', 'E']) {
        Some(i) => (&rest[..i], Some(&rest[i + 1..])),
        None => (rest, None),
    };
    let (whole, fraction) = match mantissa.split_once('.') {
        Some((w, f)) => (w, Some(f)),
        None => (mantissa, None),
    };
    let digits = |s: &str| s.bytes().all(|b| b.is_ascii_digit());
    let mantissa_ok = digits(whole)
        && fraction.is_none_or(digits)
        && !(whole.is_empty() && fraction.is_none_or(str::is_empty));
    let exponent_ok = exponent.is_none_or(|e| {
        let e = e.strip_prefix(['+', '-']).unwrap_or(e);
        !e.is_empty() && digits(e)
    });
    if !mantissa_ok || !exponent_ok {
        return None;
    }
    plain.parse().ok()
}

/// `repr(text)` of a `str`, as error messages show it.
pub fn repr_str(text: &str) -> String {
    let quote = if text.contains('\'') && !text.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::with_capacity(text.len() + 2);
    out.push(quote);
    for c in text.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c if (c as u32) < 0x20 || c as u32 == 0x7f => {
                out.push_str(&format!("\\x{:02x}", c as u32));
            }
            c if (0x80..0xa0).contains(&(c as u32)) => {
                out.push_str(&format!("\\x{:02x}", c as u32));
            }
            c => out.push(c),
        }
    }
    out.push(quote);
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn digits_and_decimals_follow_python() {
        assert!(isdigit("0123") && isdigit("²") && isdigit("٣"));
        assert!(!isdecimal("²") && isdecimal("٣") && !isdigit("") && !isdigit("1a"));
        assert_eq!(int_from_str(" ٣4_5\n"), Some(PyInt::Small(345)));
        assert_eq!(int_from_str("²"), None);
        assert_eq!(int_from_str("1__0"), None);
        assert_eq!(int_from_str("-0007"), Some(PyInt::Small(-7)));
        assert_eq!(int_from_str("+"), None);
        assert_eq!(PyInt::Small(-5).zero_padded(6), "-00005");
        assert_eq!(PyInt::Small(626).zero_padded(6), "000626");
        assert_eq!(float_from_str(" 1_0.5 "), Some(10.5));
        assert_eq!(float_from_str("-Infinity"), Some(f64::NEG_INFINITY));
        assert_eq!(float_from_str("٣.5"), Some(3.5));
        assert_eq!(float_from_str("."), None);
        assert_eq!(float_from_str("1e"), None);
        assert_eq!(float_from_str("1."), Some(1.0));
        assert_eq!(float_from_str("²"), None);
        assert_eq!(strip("\u{1c} a \u{a0}"), "a");
        assert_eq!(split(" a\u{2003}b  "), vec!["a", "b"]);
        assert_eq!(repr_str("a'b"), "\"a'b\"");
        assert_eq!(repr_str("x\n"), "'x\\n'");
    }
}
