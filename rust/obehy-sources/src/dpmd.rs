//! DPMD: the recorder filter `dpmd`, port of `dpmd.read_traffic_state` and `dpmd.recorded`.
//!
//! `TrafficState.js` is JavaScript, not JSON (DPMD-Q2): `name=<literal>;` statements after a
//! leading `/* {"TimeStamp": …} */` comment, unquoted keys, backtick strings, strings
//! concatenated with page variables (kept as `${name}`). It is read by a narrow literal
//! reader, never evaluated; anything outside that grammar is a payload error. The archive
//! keeps the timestamp, the data hash and the vehicles in service without their drivers
//! (DPMD-Q5, DPMD-Q8), as JSON byte for byte Python's (its sha256 names it). Decoding that JSON
//! is declarative (the manifest).

use std::sync::OnceLock;

use indexmap::IndexMap;
use obehy_core::py::text::{self, repr_str};
use regex::Regex;

use crate::arriva_express::Filtered;
use crate::error::PayloadError;
use crate::reader::json::{self, Json, Object};

const KEPT: [&str; 12] = [
    "id", "line", "vehicle", "course", "body", "fuel", "color", "hue", "arrow", "coords", "final1",
    "final2",
];

struct Reader {
    text: Vec<char>,
    at: usize,
}

fn number_regex() -> &'static Regex {
    static NUMBER: OnceLock<Regex> = OnceLock::new();
    NUMBER.get_or_init(|| Regex::new(r"^-?\d+(?:\.\d+)?").expect("a valid pattern"))
}

fn driver_regex() -> &'static Regex {
    static DRIVER: OnceLock<Regex> = OnceLock::new();
    DRIVER.get_or_init(|| Regex::new(r"<br>Řidič:[^<]*").expect("a valid pattern"))
}

fn name_start(c: char) -> bool {
    c.is_ascii_alphabetic() || c == '_' || c == '$'
}

fn name_char(c: char) -> bool {
    c.is_ascii_alphanumeric() || c == '_' || c == '$'
}

impl Reader {
    fn fail(&self, what: &str) -> PayloadError {
        let end = (self.at + 40).min(self.text.len());
        let shown: String = self.text[self.at.min(end)..end].iter().collect();
        PayloadError::payload(format!(
            "{what} at offset {}: {}",
            self.at,
            repr_str(&shown)
        ))
    }

    fn space(&mut self) {
        while self.at < self.text.len() && text::is_space_char(self.text[self.at]) {
            self.at += 1;
        }
    }

    fn peek(&mut self) -> Option<char> {
        self.space();
        self.text.get(self.at).copied()
    }

    fn expect(&mut self, c: char) -> Result<(), PayloadError> {
        if self.peek() != Some(c) {
            return Err(self.fail(&format!("expected {}", repr_str(&c.to_string()))));
        }
        self.at += 1;
        Ok(())
    }

    fn name(&mut self) -> Result<String, PayloadError> {
        self.space();
        if !self.text.get(self.at).copied().is_some_and(name_start) {
            return Err(self.fail("expected a name"));
        }
        let start = self.at;
        self.at += 1;
        while self.text.get(self.at).copied().is_some_and(name_char) {
            self.at += 1;
        }
        Ok(self.text[start..self.at].iter().collect())
    }

    fn value(&mut self) -> Result<Json, PayloadError> {
        let c = self.peek();
        match c {
            Some('[') => return self.array(),
            Some('{') => return self.object(),
            // Python's `char in "`'"` is also true for "" (the end): `string()` then fails.
            Some('`' | '\'') | None => return self.concatenation().map(Json::Str),
            _ => {}
        }
        let rest: String = self.text[self.at..].iter().take(4096).collect();
        if let Some(found) = number_regex().find(&rest) {
            let number = found.as_str().to_owned();
            self.at += number.chars().count();
            if number.contains('.') {
                let x = text::float_from_str(&number).expect("a matched number");
                return Ok(Json::Float(x));
            }
            let n = text::int_from_str(&number).expect("a matched number");
            return Ok(match n.small().and_then(|v| i64::try_from(v).ok()) {
                Some(v) => Json::Int(v),
                None => Json::BigInt(n.text().into()),
            });
        }
        let word = self.name()?;
        match word.as_str() {
            "null" => Ok(Json::Null),
            "true" => Ok(Json::Bool(true)),
            "false" => Ok(Json::Bool(false)),
            _ => Err(self.fail(&format!("unexpected {}", repr_str(&word)))),
        }
    }

    fn array(&mut self) -> Result<Json, PayloadError> {
        self.expect('[')?;
        let mut items = Vec::new();
        while self.peek() != Some(']') {
            items.push(self.value()?);
            if self.peek() == Some(',') {
                self.at += 1;
            }
        }
        self.at += 1;
        Ok(Json::Array(items))
    }

    fn object(&mut self) -> Result<Json, PayloadError> {
        self.expect('{')?;
        let mut fields = Object::default();
        while self.peek() != Some('}') {
            let key = self.name()?;
            self.expect(':')?;
            let value = self.value()?;
            fields.insert(key, value);
            if self.peek() == Some(',') {
                self.at += 1;
            }
        }
        self.at += 1;
        Ok(Json::Object(fields))
    }

    fn concatenation(&mut self) -> Result<String, PayloadError> {
        let mut out = self.string()?;
        while self.peek() == Some('+') {
            self.at += 1;
            if matches!(self.peek(), Some('`' | '\'')) {
                out.push_str(&self.string()?);
            } else {
                let name = self.name()?;
                out.push_str("${");
                out.push_str(&name);
                out.push('}');
            }
        }
        Ok(out)
    }

    fn string(&mut self) -> Result<String, PayloadError> {
        let Some(&quote) = self.text.get(self.at) else {
            return Err(PayloadError {
                class: "IndexError",
                message: "string index out of range".into(),
            });
        };
        self.at += 1;
        let mut out = String::new();
        loop {
            let stop = (self.at..self.text.len()).find(|&i| {
                let c = self.text[i];
                c == quote
                    || c == '\\'
                    || (quote == '`' && c == '$' && self.text.get(i + 1) == Some(&'{'))
            });
            let Some(stop) = stop else {
                self.at = self.text.len();
                return Err(self.fail("unterminated string"));
            };
            out.extend(&self.text[self.at..stop]);
            self.at = stop;
            let c = self.text[stop];
            if c == quote {
                self.at += 1;
                return Ok(out);
            }
            if c == '\\' {
                let escaped = match self.text.get(self.at + 1) {
                    Some('n') => '\n',
                    Some('t') => '\t',
                    Some('r') => '\r',
                    Some('\\') => '\\',
                    Some('`') => '`',
                    Some('\'') => '\'',
                    Some('"') => '"',
                    Some('/') => '/',
                    _ => return Err(self.fail("unknown escape")),
                };
                out.push(escaped);
                self.at += 2;
                continue;
            }
            return Err(self.fail("template substitution"));
        }
    }
}

/// `/* {...} */` at the start: the brace text and where the comment ends.
fn timestamp_comment(text: &[char]) -> Option<(String, usize)> {
    let mut i = 0;
    while i < text.len() && text::is_space_char(text[i]) {
        i += 1;
    }
    if text.get(i) != Some(&'/') || text.get(i + 1) != Some(&'*') {
        return None;
    }
    i += 2;
    while i < text.len() && text::is_space_char(text[i]) {
        i += 1;
    }
    if text.get(i) != Some(&'{') {
        return None;
    }
    let open = i;
    for j in open + 1..text.len() {
        if text[j] != '}' {
            continue;
        }
        let mut k = j + 1;
        while k < text.len() && text::is_space_char(text[k]) {
            k += 1;
        }
        if text.get(k) == Some(&'*') && text.get(k + 1) == Some(&'/') {
            return Some((text[open..=j].iter().collect(), k + 2));
        }
    }
    None
}

/// The statements of `TrafficState.js` by name, plus `TimeStamp` from its leading comment.
pub fn read_traffic_state(body: &[u8]) -> Result<IndexMap<String, Json>, PayloadError> {
    let decoded = std::str::from_utf8(body).map_err(|e| {
        PayloadError::payload(format!(
            "not UTF-8: {}",
            json::utf8_error_text(body, &e, "utf-8")
        ))
    })?;
    let chars: Vec<char> = decoded.chars().collect();
    let (header, end) = timestamp_comment(&chars)
        .ok_or_else(|| PayloadError::payload("no leading TimeStamp comment"))?;
    let stamp = json::loads_str(&header).map_err(|e| match e {
        json::JsonError::Value(m) => PayloadError::value(m),
        other => PayloadError::payload(format!(
            "TimeStamp comment is not JSON: {}",
            other.message()
        )),
    })?;
    let Some(Json::Str(timestamp)) = stamp.get("TimeStamp") else {
        return Err(PayloadError::payload(
            "TimeStamp comment without a TimeStamp",
        ));
    };
    let mut statements = IndexMap::new();
    statements.insert("TimeStamp".to_owned(), Json::Str(timestamp.clone()));
    let mut reader = Reader {
        text: chars,
        at: end,
    };
    while reader.peek().is_some() {
        let name = reader.name()?;
        reader.expect('=')?;
        let value = reader.value()?;
        statements.insert(name, value);
        reader.expect(';')?;
    }
    Ok(statements)
}

/// The archived JSON, vehicles kept and dropped.
pub fn recorded(body: &[u8]) -> Result<Filtered, PayloadError> {
    let statements = read_traffic_state(body)?;
    let Some(Json::Array(entries)) = statements.get("buspositions") else {
        return Err(PayloadError::payload("buspositions is not a list"));
    };
    let mut kept = Vec::new();
    for entry in entries {
        let Json::Object(bus) = entry else {
            return Err(PayloadError::payload("a bus position is not an object"));
        };
        let off = bus.get("off");
        let parked = matches!(off, Some(Json::Int(1) | Json::Bool(true)))
            || matches!(off, Some(Json::Float(x)) if *x == 1.0);
        if parked {
            continue;
        }
        let mut vehicle = Object::default();
        for key in KEPT {
            if let Some(v) = bus.get(key) {
                vehicle.insert(key.to_owned(), v.clone());
            }
        }
        if let Some(Json::Str(text)) = vehicle.get_mut("body") {
            *text = driver_regex().replace_all(text, "").into_owned();
        }
        kept.push(Json::Object(vehicle));
    }
    let mut document = Object::default();
    document.insert("TimeStamp".into(), statements["TimeStamp"].clone());
    document.insert(
        "datahash".into(),
        statements.get("datahash").cloned().unwrap_or(Json::Null),
    );
    let dropped = entries.len() - kept.len();
    let count = kept.len();
    document.insert("buspositions".into(), Json::Array(kept));
    Ok(Filtered {
        body: json::dumps_compact(&Json::Object(document), false).into_bytes(),
        kept: count,
        dropped,
    })
}
