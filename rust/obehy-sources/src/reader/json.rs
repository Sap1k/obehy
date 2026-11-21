//! JSON as Python's `json.loads` reads it and `json.dumps` writes it (decode spec §4.1).
//!
//! Reading: `json.loads(bytes)` detects the encoding (a UTF-8 BOM, UTF-16 or UTF-32 by BOM or
//! by its zero bytes), accepts `NaN`, `Infinity` and `-Infinity`, keeps integers of any size,
//! reads floats correctly rounded (`1e400` is infinity), and keeps the last of duplicate keys at
//! the first one's place. Errors carry Python's texts (`Expecting value: line 1 column 1
//! (char 0)`), so a payload error reads the same from both.
//!
//! One accepted divergence: Python decodes a lone UTF-16 surrogate (`"\ud800"`, or one encoded
//! in the bytes with `surrogatepass`) into its string; Rust strings cannot hold one, so such a
//! payload is a [`JsonError::LoneSurrogate`] here. The facts harness shows none in the archive.

use std::fmt::Write as _;
use std::hash::{BuildHasherDefault, Hasher};

use indexmap::IndexMap;
use obehy_core::py::json::float_text;
use obehy_core::py::text::repr_str;

/// A JSON object: keys in first-seen order, the last duplicate's value.
pub type Object = IndexMap<String, Json, BuildHasherDefault<Fnv>>;

/// FNV-1a: object keys are short and a payload holds millions of them; order never depends
/// on it (an `IndexMap` keeps insertion order).
#[derive(Debug, Clone, Copy)]
pub struct Fnv(u64);

impl Default for Fnv {
    fn default() -> Fnv {
        Fnv(0xcbf2_9ce4_8422_2325)
    }
}

impl Hasher for Fnv {
    fn finish(&self) -> u64 {
        self.0
    }

    fn write(&mut self, bytes: &[u8]) {
        for &b in bytes {
            self.0 = (self.0 ^ u64::from(b)).wrapping_mul(0x0100_0000_01b3);
        }
    }
}

/// A JSON value with Python's distinctions: ints (of any size) and floats apart.
#[derive(Debug, Clone, PartialEq)]
pub enum Json {
    Null,
    Bool(bool),
    Int(i64),
    /// An integer beyond i64, as normalised decimal text.
    BigInt(Box<str>),
    Float(f64),
    Str(String),
    Array(Vec<Json>),
    Object(Object),
}

impl Json {
    pub fn as_object(&self) -> Option<&Object> {
        match self {
            Json::Object(o) => Some(o),
            _ => None,
        }
    }

    pub fn as_array(&self) -> Option<&[Json]> {
        match self {
            Json::Array(a) => Some(a),
            _ => None,
        }
    }

    pub fn as_str(&self) -> Option<&str> {
        match self {
            Json::Str(s) => Some(s),
            _ => None,
        }
    }

    /// `dict.get(key)` on an object; None for anything else.
    pub fn get(&self, key: &str) -> Option<&Json> {
        self.as_object().and_then(|o| o.get(key))
    }

    /// Python truthiness.
    pub fn truthy(&self) -> bool {
        match self {
            Json::Null => false,
            Json::Bool(b) => *b,
            Json::Int(n) => *n != 0,
            Json::BigInt(_) => true,
            Json::Float(x) => *x != 0.0,
            Json::Str(s) => !s.is_empty(),
            Json::Array(a) => !a.is_empty(),
            Json::Object(o) => !o.is_empty(),
        }
    }

    /// Python's `str(value)`: a string as itself, containers as their `repr`.
    pub fn py_str(&self) -> String {
        match self {
            Json::Str(s) => s.clone(),
            other => other.py_repr(),
        }
    }

    /// Python's `repr(value)` of the decoded value.
    pub fn py_repr(&self) -> String {
        match self {
            Json::Null => "None".into(),
            Json::Bool(true) => "True".into(),
            Json::Bool(false) => "False".into(),
            Json::Int(n) => n.to_string(),
            Json::BigInt(t) => t.to_string(),
            Json::Float(x) => obehy_core::py::num::py_float_repr(*x)
                .replace("NaN", "nan")
                .replace("Infinity", "inf"),
            Json::Str(s) => repr_str(s),
            Json::Array(items) => {
                let parts: Vec<String> = items.iter().map(Json::py_repr).collect();
                format!("[{}]", parts.join(", "))
            }
            Json::Object(o) => {
                let parts: Vec<String> = o
                    .iter()
                    .map(|(k, v)| format!("{}: {}", repr_str(k), v.py_repr()))
                    .collect();
                format!("{{{}}}", parts.join(", "))
            }
        }
    }
}

/// Why a body is not JSON, as Python says it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum JsonError {
    /// `UnicodeDecodeError` (its text).
    Unicode(String),
    /// `JSONDecodeError` (its text, with line, column and char).
    Decode(String),
    /// A `ValueError` that is not a decode error (an integer of more than 4300 digits).
    Value(String),
    /// A lone surrogate: Python would read it; here it is refused (module docs).
    LoneSurrogate,
}

impl JsonError {
    /// The exception's text.
    pub fn message(&self) -> String {
        match self {
            JsonError::Unicode(m) | JsonError::Decode(m) | JsonError::Value(m) => m.clone(),
            JsonError::LoneSurrogate => "lone surrogate (not representable in Rust)".into(),
        }
    }
}

/// `json.loads(body)` of bytes.
pub fn loads(body: &[u8]) -> Result<Json, JsonError> {
    loads_str(&decode_text(body)?)
}

/// The text `json.loads` decodes bytes to (`json.detect_encoding`, `surrogatepass`).
pub fn decode_text(b: &[u8]) -> Result<String, JsonError> {
    let encoding = detect_encoding(b);
    match encoding {
        "utf-8" => utf8(b, "utf-8"),
        "utf-8-sig" => utf8(&b[3..], "utf-8"),
        "utf-16" => utf16(&b[2..], b.starts_with(&[0xFF, 0xFE]), "utf-16"),
        "utf-16-le" => utf16(b, true, "utf-16-le"),
        "utf-16-be" => utf16(b, false, "utf-16-be"),
        "utf-32" => utf32(&b[4..], b.starts_with(&[0xFF, 0xFE, 0, 0]), "utf-32"),
        "utf-32-le" => utf32(b, true, "utf-32-le"),
        _ => utf32(b, false, "utf-32-be"),
    }
}

fn detect_encoding(b: &[u8]) -> &'static str {
    if b.starts_with(&[0, 0, 0xFE, 0xFF]) || b.starts_with(&[0xFF, 0xFE, 0, 0]) {
        return "utf-32";
    }
    if b.starts_with(&[0xFE, 0xFF]) || b.starts_with(&[0xFF, 0xFE]) {
        return "utf-16";
    }
    if b.starts_with(&[0xEF, 0xBB, 0xBF]) {
        return "utf-8-sig";
    }
    if b.len() >= 4 {
        if b[0] == 0 {
            return if b[1] != 0 { "utf-16-be" } else { "utf-32-be" };
        }
        if b[1] == 0 {
            return if b[2] != 0 || b[3] != 0 {
                "utf-16-le"
            } else {
                "utf-32-le"
            };
        }
    } else if b.len() == 2 {
        if b[0] == 0 {
            return "utf-16-be";
        }
        if b[1] == 0 {
            return "utf-16-le";
        }
    }
    "utf-8"
}

/// Strict UTF-8 with CPython's error text; an encoded surrogate (`ED A0..BF`), which
/// `surrogatepass` would accept, is [`JsonError::LoneSurrogate`].
pub fn utf8(b: &[u8], codec: &str) -> Result<String, JsonError> {
    match std::str::from_utf8(b) {
        Ok(s) => Ok(s.to_owned()),
        Err(e) => {
            let start = e.valid_up_to();
            if b[start] == 0xED && b.get(start + 1).is_some_and(|c| (0xA0..=0xBF).contains(c)) {
                return Err(JsonError::LoneSurrogate);
            }
            Err(JsonError::Unicode(utf8_error_text(b, &e, codec)))
        }
    }
}

/// CPython's `UnicodeDecodeError` text for a strict UTF-8 decode.
pub fn utf8_error_text(b: &[u8], e: &std::str::Utf8Error, codec: &str) -> String {
    let start = e.valid_up_to();
    let (end, reason) = match e.error_len() {
        None => (b.len(), "unexpected end of data"),
        Some(n) => {
            let first = b[start];
            let reason = if (0x80..=0xC1).contains(&first) || first >= 0xF5 {
                "invalid start byte"
            } else {
                "invalid continuation byte"
            };
            (start + n, reason)
        }
    };
    if end - start == 1 {
        format!(
            "'{codec}' codec can't decode byte 0x{:02x} in position {start}: {reason}",
            b[start]
        )
    } else {
        format!(
            "'{codec}' codec can't decode bytes in position {start}-{}: {reason}",
            end - 1
        )
    }
}

fn utf16(b: &[u8], little: bool, codec: &str) -> Result<String, JsonError> {
    let units = b.chunks(2).map(|c| {
        if c.len() < 2 {
            None
        } else if little {
            Some(u16::from_le_bytes([c[0], c[1]]))
        } else {
            Some(u16::from_be_bytes([c[0], c[1]]))
        }
    });
    let mut out = String::with_capacity(b.len() / 2);
    let mut whole = Vec::with_capacity(b.len() / 2);
    for unit in units {
        match unit {
            Some(u) => whole.push(u),
            None => {
                return Err(JsonError::Unicode(format!(
                    "'{codec}' codec can't decode byte 0x{:02x} in position {}: truncated data",
                    b[b.len() - 1],
                    b.len() - 1
                )));
            }
        }
    }
    for c in char::decode_utf16(whole) {
        out.push(c.map_err(|_| JsonError::LoneSurrogate)?);
    }
    Ok(out)
}

fn utf32(b: &[u8], little: bool, codec: &str) -> Result<String, JsonError> {
    let mut out = String::with_capacity(b.len() / 4);
    for (i, c) in b.chunks(4).enumerate() {
        if c.len() < 4 {
            return Err(JsonError::Unicode(format!(
                "'{codec}' codec can't decode bytes in position {}-{}: truncated data",
                i * 4,
                b.len() - 1
            )));
        }
        let n = if little {
            u32::from_le_bytes([c[0], c[1], c[2], c[3]])
        } else {
            u32::from_be_bytes([c[0], c[1], c[2], c[3]])
        };
        match char::from_u32(n) {
            Some(ch) => out.push(ch),
            None if (0xD800..0xE000).contains(&n) => return Err(JsonError::LoneSurrogate),
            None => {
                return Err(JsonError::Unicode(format!(
                    "'{codec}' codec can't decode bytes in position {}-{}: code point not in range(0x110000)",
                    i * 4,
                    i * 4 + 3
                )));
            }
        }
    }
    Ok(out)
}

const MAX_DEPTH: usize = 900;
const MAX_INT_DIGITS: usize = 4300;

/// `json.loads(text)` of a string.
pub fn loads_str(s: &str) -> Result<Json, JsonError> {
    let parser = Parser {
        s: s.as_bytes(),
        text: s,
    };
    if s.starts_with('\u{feff}') {
        return Err(parser.error("Unexpected UTF-8 BOM (decode using utf-8-sig)", 0));
    }
    let start = parser.ws(0);
    let (value, end) = match parser.value(start, 0) {
        Ok(found) => found,
        Err(Fail::Stop(at)) => return Err(parser.error("Expecting value", at)),
        Err(Fail::Error(e)) => return Err(e),
    };
    let end = parser.ws(end);
    if end != s.len() {
        return Err(parser.error("Extra data", end));
    }
    Ok(value)
}

enum Fail {
    /// Python's StopIteration: no value starts here.
    Stop(usize),
    Error(JsonError),
}

impl From<JsonError> for Fail {
    fn from(e: JsonError) -> Fail {
        Fail::Error(e)
    }
}

struct Parser<'a> {
    s: &'a [u8],
    text: &'a str,
}

impl Parser<'_> {
    /// `JSONDecodeError(msg, doc, pos)`; `at` is a byte offset, Python counts characters.
    fn error(&self, message: &str, at: usize) -> JsonError {
        let before = &self.text[..at];
        let pos = before.chars().count();
        let line = before.matches('\n').count() + 1;
        let column = match before.rfind('\n') {
            Some(nl) => before[nl + 1..].chars().count() + 1,
            None => pos + 1,
        };
        JsonError::Decode(format!(
            "{message}: line {line} column {column} (char {pos})"
        ))
    }

    fn ws(&self, mut i: usize) -> usize {
        while i < self.s.len() && matches!(self.s[i], b' ' | b'\t' | b'\n' | b'\r') {
            i += 1;
        }
        i
    }

    fn starts(&self, i: usize, word: &[u8]) -> bool {
        self.s[i..].starts_with(word)
    }

    fn value(&self, i: usize, depth: usize) -> Result<(Json, usize), Fail> {
        let Some(&c) = self.s.get(i) else {
            return Err(Fail::Stop(i));
        };
        match c {
            b'"' => {
                let (text, end) = self.string(i + 1)?;
                Ok((Json::Str(text), end))
            }
            b'{' => self.guarded(depth, |d| self.object(i + 1, d)),
            b'[' => self.guarded(depth, |d| self.array(i + 1, d)),
            b'n' if self.starts(i, b"null") => Ok((Json::Null, i + 4)),
            b't' if self.starts(i, b"true") => Ok((Json::Bool(true), i + 4)),
            b'f' if self.starts(i, b"false") => Ok((Json::Bool(false), i + 5)),
            b'N' if self.starts(i, b"NaN") => Ok((Json::Float(f64::NAN), i + 3)),
            b'I' if self.starts(i, b"Infinity") => Ok((Json::Float(f64::INFINITY), i + 8)),
            b'-' if self.starts(i, b"-Infinity") => Ok((Json::Float(f64::NEG_INFINITY), i + 9)),
            _ => self.number(i),
        }
    }

    fn guarded(
        &self,
        depth: usize,
        f: impl FnOnce(usize) -> Result<(Json, usize), Fail>,
    ) -> Result<(Json, usize), Fail> {
        if depth >= MAX_DEPTH {
            return Err(Fail::Error(JsonError::Value(
                "maximum recursion depth exceeded while decoding a JSON object from a unicode string"
                    .into(),
            )));
        }
        f(depth + 1)
    }

    fn digit(&self, i: usize) -> bool {
        self.s.get(i).is_some_and(u8::is_ascii_digit)
    }

    fn number(&self, start: usize) -> Result<(Json, usize), Fail> {
        let s = self.s;
        let end_idx = s.len() as isize - 1;
        let mut i = start;
        if s[i] == b'-' {
            i += 1;
            if i as isize > end_idx {
                return Err(Fail::Stop(start));
            }
        }
        if (b'1'..=b'9').contains(&s[i]) {
            i += 1;
            while self.digit(i) {
                i += 1;
            }
        } else if s[i] == b'0' {
            i += 1;
        } else {
            return Err(Fail::Stop(start));
        }
        let mut is_float = false;
        if (i as isize) < end_idx && s[i] == b'.' && self.digit(i + 1) {
            is_float = true;
            i += 2;
            while self.digit(i) {
                i += 1;
            }
        }
        if (i as isize) < end_idx && (s[i] == b'e' || s[i] == b'E') {
            let e_start = i;
            i += 1;
            if (i as isize) < end_idx && (s[i] == b'-' || s[i] == b'+') {
                i += 1;
            }
            while self.digit(i) {
                i += 1;
            }
            if s[i - 1].is_ascii_digit() {
                is_float = true;
            } else {
                i = e_start;
            }
        }
        let text = &self.text[start..i];
        if is_float {
            let x: f64 = text.parse().expect("a JSON float parses");
            return Ok((Json::Float(x), i));
        }
        if let Ok(n) = text.parse::<i64>() {
            return Ok((Json::Int(n), i));
        }
        let digits = text.trim_start_matches('-');
        if digits.len() > MAX_INT_DIGITS {
            return Err(Fail::Error(JsonError::Value(format!(
                "Exceeds the limit ({MAX_INT_DIGITS} digits) for integer string conversion: value has {} digits; use sys.set_int_max_str_digits() to increase the limit",
                digits.len()
            ))));
        }
        Ok((Json::BigInt(text.into()), i))
    }

    /// `scanstring` (strict): `i` is just after the opening quote.
    fn string(&self, i: usize) -> Result<(String, usize), JsonError> {
        let s = self.s;
        let begin = i - 1;
        let mut out = String::new();
        let mut end = i;
        loop {
            let mut next = end;
            while next < s.len() {
                let d = s[next];
                if d == b'"' || d == b'\\' {
                    break;
                }
                if d <= 0x1f {
                    return Err(self.error("Invalid control character at", next));
                }
                next += 1;
            }
            if next >= s.len() {
                return Err(self.error("Unterminated string starting at", begin));
            }
            out.push_str(&self.text[end..next]);
            if s[next] == b'"' {
                return Ok((out, next + 1));
            }
            // A backslash.
            let next = next + 1;
            if next == s.len() {
                return Err(self.error("Unterminated string starting at", begin));
            }
            let c = s[next];
            if c != b'u' {
                end = next + 1;
                let ch = match c {
                    b'"' => '"',
                    b'\\' => '\\',
                    b'/' => '/',
                    b'b' => '\u{8}',
                    b'f' => '\u{c}',
                    b'n' => '\n',
                    b'r' => '\r',
                    b't' => '\t',
                    _ => return Err(self.error("Invalid \\escape", end - 2)),
                };
                out.push(ch);
                continue;
            }
            let mut next = next + 1;
            end = next + 4;
            if end >= s.len() {
                return Err(self.error("Invalid \\uXXXX escape", next - 1));
            }
            let c = self
                .hex4(next)
                .ok_or_else(|| self.error("Invalid \\uXXXX escape", next - 1))?;
            next = end;
            let mut code = c;
            if (0xD800..0xDC00).contains(&c)
                && end + 6 < s.len()
                && s[next] == b'\\'
                && s[next + 1] == b'u'
            {
                let c2 = self
                    .hex4(next + 2)
                    .ok_or_else(|| self.error("Invalid \\uXXXX escape", end + 1))?;
                if (0xDC00..0xE000).contains(&c2) {
                    code = 0x10000 + ((c - 0xD800) << 10) + (c2 - 0xDC00);
                    end += 6;
                }
            }
            match char::from_u32(code) {
                Some(ch) => out.push(ch),
                None => return Err(JsonError::LoneSurrogate),
            }
        }
    }

    fn hex4(&self, at: usize) -> Option<u32> {
        let digits = self.s.get(at..at + 4)?;
        let mut value = 0;
        for &d in digits {
            value = value * 16 + char::from(d).to_digit(16)?;
        }
        Some(value)
    }

    fn object(&self, i: usize, depth: usize) -> Result<(Json, usize), Fail> {
        let s = self.s;
        let mut object = Object::default();
        let mut i = self.ws(i);
        if s.get(i) == Some(&b'}') {
            return Ok((Json::Object(object), i + 1));
        }
        loop {
            if s.get(i) != Some(&b'"') {
                return Err(self
                    .error("Expecting property name enclosed in double quotes", i)
                    .into());
            }
            let (key, next) = self.string(i + 1)?;
            i = self.ws(next);
            if s.get(i) != Some(&b':') {
                return Err(self.error("Expecting ':' delimiter", i).into());
            }
            i = self.ws(i + 1);
            let (value, next) = self.value(i, depth)?;
            object.insert(key, value);
            i = self.ws(next);
            if s.get(i) == Some(&b'}') {
                return Ok((Json::Object(object), i + 1));
            }
            if s.get(i) != Some(&b',') {
                return Err(self.error("Expecting ',' delimiter", i).into());
            }
            let comma = i;
            i = self.ws(i + 1);
            if s.get(i) == Some(&b'}') {
                return Err(self
                    .error("Illegal trailing comma before end of object", comma)
                    .into());
            }
        }
    }

    fn array(&self, i: usize, depth: usize) -> Result<(Json, usize), Fail> {
        let s = self.s;
        let mut items = Vec::new();
        let mut i = self.ws(i);
        if s.get(i) == Some(&b']') {
            return Ok((Json::Array(items), i + 1));
        }
        loop {
            let (value, next) = self.value(i, depth)?;
            items.push(value);
            i = self.ws(next);
            if s.get(i) == Some(&b']') {
                return Ok((Json::Array(items), i + 1));
            }
            if s.get(i) != Some(&b',') {
                return Err(self.error("Expecting ',' delimiter", i).into());
            }
            let comma = i;
            i = self.ws(i + 1);
            if s.get(i) == Some(&b']') {
                return Err(self
                    .error("Illegal trailing comma before end of array", comma)
                    .into());
            }
        }
    }
}

/// `json.dumps(value, ensure_ascii=..., separators=(",", ":"))`.
pub fn dumps_compact(value: &Json, ensure_ascii: bool) -> String {
    let mut out = String::new();
    write(&mut out, value, ensure_ascii);
    out
}

fn write(out: &mut String, value: &Json, ensure_ascii: bool) {
    match value {
        Json::Null => out.push_str("null"),
        Json::Bool(true) => out.push_str("true"),
        Json::Bool(false) => out.push_str("false"),
        Json::Int(n) => write!(out, "{n}").expect("writing to a string"),
        Json::BigInt(t) => out.push_str(t),
        Json::Float(x) => out.push_str(&float_text(*x)),
        Json::Str(s) => write_string(out, s, ensure_ascii),
        Json::Array(items) => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write(out, item, ensure_ascii);
            }
            out.push(']');
        }
        Json::Object(o) => {
            out.push('{');
            for (i, (k, v)) in o.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_string(out, k, ensure_ascii);
                out.push(':');
                write(out, v, ensure_ascii);
            }
            out.push('}');
        }
    }
}

/// A string as `json.dumps` writes it.
pub fn write_string(out: &mut String, text: &str, ensure_ascii: bool) {
    out.push('"');
    for c in text.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => write!(out, "\\u{:04x}", c as u32).expect("writing"),
            c if ensure_ascii && (c as u32) > 0x7e => {
                let mut units = [0_u16; 2];
                for unit in c.encode_utf16(&mut units) {
                    write!(out, "\\u{unit:04x}").expect("writing");
                }
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

#[cfg(test)]
mod tests {
    use super::*;

    fn err(text: &str) -> String {
        loads(text.as_bytes()).unwrap_err().message()
    }

    #[test]
    fn reads_like_python() {
        let v = loads(br#"{"a": 1, "b": [NaN, -Infinity, 1e400, -0, 2.50], "a": 3}"#).unwrap();
        let o = v.as_object().unwrap();
        assert_eq!(o.keys().collect::<Vec<_>>(), ["a", "b"]);
        assert_eq!(o["a"], Json::Int(3));
        let b = o["b"].as_array().unwrap();
        assert!(matches!(b[0], Json::Float(x) if x.is_nan()));
        assert_eq!(b[2], Json::Float(f64::INFINITY));
        assert_eq!(b[3], Json::Int(0));
        assert_eq!(
            loads(b"123456789012345678901234567890").unwrap(),
            Json::BigInt("123456789012345678901234567890".into())
        );
        assert_eq!(
            loads(b"\xef\xbb\xbf\"\\ud83d\\ude8c\"").unwrap(),
            Json::Str("\u{1F68C}".into())
        );
        assert_eq!(loads(b"\"\\ud800\""), Err(JsonError::LoneSurrogate));
    }

    #[test]
    fn errors_read_like_python() {
        assert_eq!(err(""), "Expecting value: line 1 column 1 (char 0)");
        assert_eq!(err("<html>"), "Expecting value: line 1 column 1 (char 0)");
        assert_eq!(
            err("{\"a\" 1}"),
            "Expecting ':' delimiter: line 1 column 6 (char 5)"
        );
        assert_eq!(
            err("[1,]"),
            "Illegal trailing comma before end of array: line 1 column 3 (char 2)"
        );
        assert_eq!(err("[1]x"), "Extra data: line 1 column 4 (char 3)");
        assert_eq!(
            err("\n\"á"),
            "Unterminated string starting at: line 2 column 1 (char 1)"
        );
        assert_eq!(loads("\u{feff}1".as_bytes()), Ok(Json::Int(1)));
        assert_eq!(
            loads_str("\u{feff}1").unwrap_err().message(),
            "Unexpected UTF-8 BOM (decode using utf-8-sig): line 1 column 1 (char 0)"
        );
        assert_eq!(
            loads(b"\xff").unwrap_err().message(),
            "'utf-8' codec can't decode byte 0xff in position 0: invalid start byte"
        );
    }

    #[test]
    fn writes_like_python() {
        let v = loads("{\"x\":[1.0,1e-07,\"ž\\n\\u0001\"],\"y\":null}".as_bytes()).unwrap();
        assert_eq!(
            dumps_compact(&v, false),
            "{\"x\":[1.0,1e-07,\"ž\\n\\u0001\"],\"y\":null}"
        );
        assert_eq!(
            dumps_compact(&v, true),
            "{\"x\":[1.0,1e-07,\"\\u017e\\n\\u0001\"],\"y\":null}"
        );
    }
}
