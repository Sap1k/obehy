//! The declarative mapping of `[channel.decode]` (obehy_core::config::decode): a JSON
//! payload's entities to observations (decode spec §4).
//!
//! A [`Mapping`] is compiled once from the manifest: value names become slots, templates and
//! patterns are parsed, normalizers bound. Per payload: the document is checked and its values
//! read, guards may drop it whole, then each entity's values are evaluated in order (a
//! required value missing drops the entity, `skip`/`keep` are checked as soon as their values
//! are known), its profile chosen, and its facts made. Profiles with a selector collect their
//! entities and choose one per group after the last entity. Observations come out in entity
//! order.

use std::borrow::Cow;
use std::collections::HashMap;
use std::sync::Arc;

use obehy_core::config::decode::{
    ByValue, Case, Decode, FactBody, FactGroups, FactSpec, FeedName, Gate, Guard, Invalid, Match,
    OrChoice, Required, Root, Rule, Scalar, Selector, ValueRule, ValueSpec,
};
use obehy_core::model::{
    Delay, DelayReference, Destination, Fact, Feed, Interval, LineRef, NextPoint, NextStopName,
    NextStopPrediction, Observation, PointEvent, Position, RawRef, ServiceDay, SourceDeparture,
    SourceState, TripCancelled, TripKey, TripStatus, VehicleAttribute, VehicleKey,
};
use obehy_core::time::{
    Date, Instant, Layout, TimeDelta, iso, read_digits_as_local, read_local, resolve_clock,
    within_skew,
};
use regex::Regex;

use crate::error::PayloadError;
use crate::krovak::Transformer;
use crate::normalize::{self, Norm};
use crate::reader::json::{self, Value};
use crate::value::{Val, float_int, matches};

/// What a decoder needs besides the body: reception, the policy's clock skew.
#[derive(Debug, Clone, Copy)]
pub struct Context {
    pub received_at: Instant,
    pub max_clock_skew: TimeDelta,
}

/// A manifest that does not compile.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CompileError(pub String);

impl std::fmt::Display for CompileError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for CompileError {}

fn bad<T>(message: impl Into<String>) -> Result<T, CompileError> {
    Err(CompileError(message.into()))
}

/// A reference to a value, or to a field of one (`label.Linka`, `rec.11`).
#[derive(Debug, Clone)]
pub(crate) struct Ref {
    pub slot: usize,
    pub field: Option<String>,
}

pub(crate) type Slots = [Option<Val>];

/// The value a reference names.
pub(crate) fn get<'s>(slots: &'s Slots, r: &Ref) -> Option<Cow<'s, Val>> {
    let value = slots[r.slot].as_ref()?;
    match &r.field {
        None => Some(Cow::Borrowed(value)),
        Some(field) => value.field(field).map(Cow::Owned),
    }
}

#[derive(Debug, Clone)]
enum Seg {
    Key(String),
    /// A number: an index into a list (and nothing on an object).
    Index(usize),
}

/// A dotted path; the empty path is the document itself (IDSOK-Q1: a bare array).
fn parse_path(path: &str) -> Vec<Seg> {
    if path.is_empty() {
        return Vec::new();
    }
    path.split('.')
        .map(|p| match p.parse::<usize>() {
            Ok(i) if !p.is_empty() && p.bytes().all(|b| b.is_ascii_digit()) => Seg::Index(i),
            _ => Seg::Key(p.to_owned()),
        })
        .collect()
}

fn lookup<'j>(root: &'j Value, path: &[Seg]) -> Option<&'j Value> {
    let mut at = root;
    for seg in path {
        at = match (seg, at) {
            (Seg::Key(k), Value::Object(o)) => o.get(k)?,
            (Seg::Index(i), Value::Array(a)) => a.get(*i)?,
            _ => return None,
        };
    }
    Some(at)
}

/// A template part.
#[derive(Debug, Clone)]
enum Part {
    Lit(String),
    /// `{name}`.
    Text(Ref),
    /// `{name:d}`: an integer.
    Int(Ref),
    /// `{name:0Nd}`.
    Padded(Ref, usize),
}

#[derive(Debug, Clone)]
pub(crate) struct Template(Vec<Part>);

impl Template {
    /// The text, None when a value is absent; an error when an integer part is none.
    fn eval(&self, slots: &Slots) -> Result<Option<String>, PayloadError> {
        let mut out = String::new();
        for part in &self.0 {
            match part {
                Part::Lit(t) => out.push_str(t),
                Part::Text(r) => match get(slots, r) {
                    Some(v) => out.push_str(&v.text()),
                    None => return Ok(None),
                },
                Part::Int(r) => match get(slots, r) {
                    Some(v) => out.push_str(&v.to_int()?.to_string()),
                    None => return Ok(None),
                },
                Part::Padded(r, width) => match get(slots, r) {
                    Some(v) => out.push_str(&format!("{:0width$}", v.to_int()?)),
                    None => return Ok(None),
                },
            }
        }
        Ok(Some(out))
    }
}

/// A coercion (`as`): how a JSON value is read.
#[derive(Debug, Clone)]
enum Coerce {
    Raw,
    /// A JSON integer (never a bool).
    Int,
    /// A JSON integer or an integer string (`decoding.integer`).
    Integer,
    /// An integer, a float (truncated), a bool or an integer string (DÚK).
    PyInt,
    /// A JSON number or a numeric string, never a bool (`decoding.number`).
    Number,
    /// A JSON number, never a bool.
    Real,
    Bool,
    Text,
    /// A string, stripped, not empty.
    Trimmed,
    /// A string of ASCII digits.
    Digits,
    /// A 5- or 6-digit SR70 code as its 5 digits.
    Sr70,
    /// `YYYYMMDD` as a date.
    Yyyymmdd,
    /// Two numbers, neither 0.
    Pair,
    /// An ISO time with an offset.
    Iso,
    /// ISO digits read as local time whatever the offset (ARRIVA-Q1).
    IsoLocalDigits,
    /// A local time in a `strptime` layout.
    Local(Layout),
    /// A bare `HH:MM` near a reference.
    Hhmm,
    /// A bare `HH:MM:SS` near a reference (IREDO-Q4, IDSOK-Q4).
    Hhmmss,
    /// Minutes after midnight (digits or an integer) near a reference (MPVNET-Q3).
    MinuteOfDay,
    /// A number in a string, an error if it is none.
    Float,
}

fn coerce_named(name: &str) -> Result<Coerce, CompileError> {
    if let Some(layout) = name.strip_prefix("local:") {
        return Ok(Coerce::Local(Layout::new(layout)));
    }
    Ok(match name {
        "raw" => Coerce::Raw,
        "int" => Coerce::Int,
        "integer" => Coerce::Integer,
        "py_int" => Coerce::PyInt,
        "number" => Coerce::Number,
        "real" => Coerce::Real,
        "bool" => Coerce::Bool,
        "text" => Coerce::Text,
        "trimmed" => Coerce::Trimmed,
        "digits" => Coerce::Digits,
        "sr70_5" => Coerce::Sr70,
        "yyyymmdd" => Coerce::Yyyymmdd,
        "pair" => Coerce::Pair,
        "iso" => Coerce::Iso,
        "iso_local_digits" => Coerce::IsoLocalDigits,
        "hhmm" => Coerce::Hhmm,
        "hhmmss" => Coerce::Hhmmss,
        "minute_of_day" => Coerce::MinuteOfDay,
        "float" => Coerce::Float,
        other => return bad(format!("unknown coercion `{other}`")),
    })
}

fn json_int(j: &Value) -> Option<i64> {
    j.as_i64()
}

fn int_text(t: &str) -> Option<i64> {
    t.trim().parse().ok()
}

fn float_text(t: &str) -> Option<f64> {
    t.trim().parse().ok()
}

fn digits(t: &str) -> bool {
    !t.is_empty() && t.bytes().all(|b| b.is_ascii_digit())
}

/// `HH:MM` (or `H:MM`) as microseconds after midnight.
pub(crate) fn hhmm(text: &str) -> Option<i64> {
    let (h, m) = text.trim().split_once(':')?;
    if !(1..=2).contains(&h.len()) || m.len() != 2 || !digits(h) || !digits(m) {
        return None;
    }
    let (hours, minutes): (i64, i64) = (h.parse().ok()?, m.parse().ok()?);
    if hours > 23 || minutes > 59 {
        return None;
    }
    Some((hours * 60 + minutes) * 60_000_000)
}

/// `HH:MM:SS` (or `H:MM:SS`) as microseconds after midnight.
pub(crate) fn hhmmss(text: &str) -> Option<i64> {
    let (hm, sec) = text.trim().rsplit_once(':')?;
    if sec.len() != 2 || !digits(sec) {
        return None;
    }
    let seconds: i64 = sec.parse().ok()?;
    (seconds <= 59).then_some(())?;
    Some(hhmm(hm)? + seconds * 1_000_000)
}

impl Coerce {
    fn apply(
        &self,
        j: &Value,
        invalid: Invalid,
        near: Option<Instant>,
        ctx: &Context,
    ) -> Result<Option<Val>, PayloadError> {
        let s = j.as_str();
        Ok(match self {
            Coerce::Raw => Some(Val::Json(j.clone())),
            Coerce::Int => json_int(j).map(Val::Int),
            Coerce::Integer => s.map_or_else(|| json_int(j), int_text).map(Val::Int),
            Coerce::PyInt => match j {
                Value::Bool(b) => Some(Val::Int(i64::from(*b))),
                Value::String(t) => int_text(t).map(Val::Int),
                Value::Number(n) => n
                    .as_i64()
                    .or_else(|| n.as_f64().and_then(float_int))
                    .map(Val::Int),
                _ => None,
            },
            Coerce::Number => s.map_or_else(|| j.as_f64(), float_text).map(Val::Float),
            Coerce::Real => j.as_f64().map(Val::Float),
            Coerce::Bool => j.as_bool().map(Val::Bool),
            Coerce::Text => s.map(|t| Val::Text(t.to_owned())),
            Coerce::Trimmed => s
                .map(str::trim)
                .filter(|t| !t.is_empty())
                .map(|t| Val::Text(t.to_owned())),
            Coerce::Digits => s.filter(|t| digits(t)).map(|t| Val::Text(t.to_owned())),
            Coerce::Sr70 => s
                .filter(|t| digits(t) && matches!(t.len(), 5 | 6))
                .map(|t| Val::Text(t[..5].to_owned())),
            Coerce::Yyyymmdd => s
                .filter(|t| t.len() == 8 && digits(t))
                .and_then(|t| {
                    Date::new(
                        t[..4].parse().ok()?,
                        t[4..6].parse().ok()?,
                        t[6..].parse().ok()?,
                    )
                })
                .map(Val::Date),
            Coerce::Pair => match j.as_array().map(Vec::as_slice) {
                Some([x, y]) => match (x.as_f64(), y.as_f64()) {
                    (Some(x), Some(y)) if x != 0.0 && y != 0.0 => Some(Val::Pair(x, y)),
                    _ => None,
                },
                _ => None,
            },
            Coerce::Iso => match s.filter(|t| !t.is_empty()) {
                None => None,
                Some(t) => match iso::parse_datetime(t)
                    .and_then(|parsed| parsed.offset_us.map(|_| parsed.instant()).transpose())
                {
                    Ok(at) => at.map(Val::Instant),
                    Err(e) => match invalid {
                        Invalid::Fail => return Err(PayloadError::new(e.0)),
                        Invalid::None => None,
                    },
                },
            },
            Coerce::IsoLocalDigits => s
                .and_then(|t| read_digits_as_local(t, ctx.received_at).ok().flatten())
                .map(Val::Instant),
            Coerce::Local(layout) => s
                .and_then(|t| read_local(t, layout, ctx.received_at))
                .map(Val::Instant),
            Coerce::Hhmm => s
                .and_then(hhmm)
                .and_then(|clock| resolve_clock(clock, near.unwrap_or(ctx.received_at)))
                .map(Val::Instant),
            Coerce::Hhmmss => s
                .and_then(hhmmss)
                .and_then(|clock| resolve_clock(clock, near.unwrap_or(ctx.received_at)))
                .map(Val::Instant),
            Coerce::MinuteOfDay => s
                .map_or_else(|| json_int(j), |t| digits(t).then(|| int_text(t)).flatten())
                .filter(|minute| (0..24 * 60).contains(minute))
                .and_then(|minute| {
                    resolve_clock(minute * 60_000_000, near.unwrap_or(ctx.received_at))
                })
                .map(Val::Instant),
            Coerce::Float => match s {
                Some(t) => Some(Val::Float(float_text(t).ok_or_else(|| not_a_number(t))?)),
                None => None,
            },
        })
    }

    /// A regex capture read as this coercion: text, a number or an integer (an error if it is
    /// none).
    fn capture(&self, t: &str) -> Result<Option<Val>, PayloadError> {
        Ok(Some(match self {
            Coerce::Float => Val::Float(float_text(t).ok_or_else(|| not_a_number(t))?),
            Coerce::PyInt | Coerce::Int | Coerce::Integer => {
                Val::Int(int_text(t).ok_or_else(|| not_a_number(t))?)
            }
            _ => Val::Text(t.to_owned()),
        }))
    }
}

fn not_a_number(t: &str) -> PayloadError {
    PayloadError::new(format!("not a number: {t:?}"))
}

/// A manifest pattern, matched at the start of the text.
pub(crate) fn pattern(pattern: &str) -> Result<Regex, CompileError> {
    let body = pattern.strip_prefix('^').unwrap_or(pattern);
    Regex::new(&format!("^(?:{body})"))
        .map_err(|e| CompileError(format!("pattern {pattern:?}: {e}")))
}

/// `5m`, `59s`, `2h`.
pub(crate) fn duration(text: &str) -> Result<TimeDelta, CompileError> {
    let (number, unit) = text.split_at(text.len().saturating_sub(1));
    let n: i64 = number
        .parse()
        .map_err(|_| CompileError(format!("bad duration {text:?}")))?;
    match unit {
        "s" => Ok(TimeDelta::from_seconds(n)),
        "m" => Ok(TimeDelta::from_minutes(n)),
        "h" => Ok(TimeDelta::from_hours(n)),
        _ => bad(format!("bad duration {text:?}")),
    }
}

#[derive(Debug, Clone)]
enum Source {
    Path(Vec<Seg>),
    From(Ref),
    Template(Template),
    FirstOf(Vec<Template>),
}

#[derive(Debug, Clone)]
struct CRule {
    source: Source,
    trim: bool,
    coerce: Option<Coerce>,
    none: Vec<Scalar>,
    none_before: Option<Instant>,
    invalid: Invalid,
    regex: Option<(Regex, Vec<usize>, Option<Coerce>)>,
    near: Option<Ref>,
    fresh: bool,
    when: Vec<(Ref, Match)>,
    unless: Vec<(Ref, Match)>,
}

#[derive(Debug, Clone)]
enum One {
    Rule(Box<CRule>),
    Fn(Box<Norm>),
}

#[derive(Debug, Clone)]
enum CValue {
    One(One),
    Choice {
        by: Ref,
        cases: Vec<(Option<Match>, One)>,
    },
}

#[derive(Debug, Clone)]
struct CSlot {
    slot: usize,
    value: CValue,
    required: Required,
}

/// A choice by a value, or a plain value.
#[derive(Debug, Clone)]
pub(crate) enum Pick<T> {
    Plain(T),
    Choice {
        by: Ref,
        cases: Vec<(Option<Match>, T)>,
    },
}

impl<T> Pick<T> {
    fn pick(&self, slots: &Slots) -> Option<&T> {
        match self {
            Pick::Plain(t) => Some(t),
            Pick::Choice { by, cases } => {
                let value = get(slots, by);
                cases
                    .iter()
                    .find(|(m, _)| m.as_ref().is_none_or(|m| matches(value.as_deref(), m)))
                    .map(|(_, t)| t)
            }
        }
    }
}

/// A fact input: a value by name (optional with `?`) or a template.
#[derive(Debug, Clone)]
struct In {
    source: InSource,
    optional: bool,
}

#[derive(Debug, Clone)]
enum InSource {
    Ref(Ref),
    Template(Template),
}

#[derive(Debug, Clone)]
enum CBody {
    VehicleKey(Template),
    TripKey(String, Template),
    LineRef(String, Template),
    Destination(In, bool),
    NextStopName {
        name: In,
        scheduled: Option<In>,
        townless: bool,
    },
    Position {
        lat: Option<In>,
        lon: Option<In>,
        xy: Option<(In, usize)>,
        bearing: Option<In>,
        zero_is_none: bool,
        needs_time: bool,
    },
    Delay {
        s: In,
        scale: i64,
        default: Option<i64>,
        reference: DelayReference,
    },
    SourceState(Pick<Template>),
    SourceDeparture(In),
    ServiceDay(In),
    PointEvent {
        name: In,
        codes: In,
        scheduled: In,
        actual: In,
        width: TimeDelta,
        standing: Pick<bool>,
    },
    NextPoint {
        name: In,
        sr70: In,
    },
    NextStopPrediction {
        sr70: In,
        scheduled: In,
        predicted: In,
    },
    TripStatus {
        replacement_bus: Pick<bool>,
        diverted: Pick<bool>,
    },
    TripCancelled,
    VehicleAttribute(String, In),
}

#[derive(Debug, Clone, Default)]
struct CGate {
    when: Vec<(Ref, Match)>,
    unless: Vec<(Ref, Match)>,
    if_fact: Option<String>,
}

#[derive(Debug, Clone)]
enum CFact {
    One(CGate, Pick<CBody>),
    Group(CGate, Vec<CFact>),
}

#[derive(Debug, Clone)]
struct BlockCurrent {
    group_by: Ref,
    state: Ref,
    start: Ref,
    running: Vec<String>,
    off_route: String,
    pre_trip: Vec<String>,
    out: usize,
}

#[derive(Debug, Clone)]
struct CProfile {
    when: Vec<(Ref, Match)>,
    require: Vec<Ref>,
    require_fact: Option<String>,
    select: Option<BlockCurrent>,
    facts: Vec<CFact>,
}

#[derive(Debug, Clone)]
enum CGuard {
    StaleBody {
        value: Ref,
        cut_at: String,
        layout: Layout,
    },
}

/// Files a manifest names (the SR70 catalogue), read by the caller: no I/O here.
pub type Files<'a> = &'a dyn Fn(&str) -> Result<String, String>;

/// A compiled `[channel.decode]`.
#[derive(Debug, Clone)]
pub struct Mapping {
    pub version: i64,
    root: Option<Root>,
    non_empty: bool,
    json_error: String,
    root_error: String,
    entities_error: String,
    entities: Vec<Seg>,
    document: Vec<CSlot>,
    guards: Vec<CGuard>,
    values: Vec<CSlot>,
    slots: usize,
    skip: Vec<(Ref, Match)>,
    keep: Vec<(Ref, Match)>,
    /// The value after which `skip`/`keep` are checked.
    filter_after: Option<usize>,
    feed: Pick<Feed>,
    observed_at: Option<(Ref, bool)>,
    profiles: Vec<CProfile>,
    krovak: Arc<Transformer>,
}

pub(crate) struct Compiler<'a> {
    names: HashMap<String, usize>,
    slots: usize,
    groups: &'a FactGroups,
    files: Files<'a>,
    catalogues: HashMap<String, Arc<HashMap<String, Vec<String>>>>,
}

impl Compiler<'_> {
    fn define(&mut self, name: &str) -> usize {
        if let Some(&slot) = self.names.get(name) {
            return slot;
        }
        let slot = self.slots;
        self.slots += 1;
        self.names.insert(name.to_owned(), slot);
        slot
    }

    pub(crate) fn reference(&self, name: &str) -> Result<Ref, CompileError> {
        if let Some(&slot) = self.names.get(name) {
            return Ok(Ref { slot, field: None });
        }
        if let Some((base, field)) = name.split_once('.')
            && let Some(&slot) = self.names.get(base)
        {
            return Ok(Ref {
                slot,
                field: Some(field.to_owned()),
            });
        }
        bad(format!("`{name}` names no value defined before it"))
    }

    fn by_value(&self, by: Option<&ByValue>) -> Result<Vec<(Ref, Match)>, CompileError> {
        by.into_iter()
            .flatten()
            .map(|(name, m)| Ok((self.reference(name)?, m.clone())))
            .collect()
    }

    fn template(&self, text: &str) -> Result<Template, CompileError> {
        let mut parts = Vec::new();
        let mut rest = text;
        while let Some(open) = rest.find('{') {
            if open > 0 {
                parts.push(Part::Lit(rest[..open].to_owned()));
            }
            let close = rest[open..]
                .find('}')
                .ok_or_else(|| CompileError(format!("unclosed `{{` in {text:?}")))?
                + open;
            let inner = &rest[open + 1..close];
            let part = match inner.split_once(':') {
                None => Part::Text(self.reference(inner)?),
                Some((name, "d")) => Part::Int(self.reference(name)?),
                Some((name, spec)) => {
                    let width = spec
                        .strip_prefix('0')
                        .and_then(|w| w.strip_suffix('d'))
                        .and_then(|w| w.parse().ok())
                        .ok_or_else(|| CompileError(format!("bad format {spec:?} in {text:?}")))?;
                    Part::Padded(self.reference(name)?, width)
                }
            };
            parts.push(part);
            rest = &rest[close + 1..];
        }
        if !rest.is_empty() {
            parts.push(Part::Lit(rest.to_owned()));
        }
        Ok(Template(parts))
    }

    fn input(&self, text: &str) -> Result<In, CompileError> {
        if text.contains('{') {
            return Ok(In {
                source: InSource::Template(self.template(text)?),
                optional: false,
            });
        }
        let (name, optional) = match text.strip_suffix('?') {
            Some(n) => (n, true),
            None => (text, false),
        };
        Ok(In {
            source: InSource::Ref(self.reference(name)?),
            optional,
        })
    }

    fn rule(&mut self, r: &Rule) -> Result<CRule, CompileError> {
        let sources = [
            r.path.is_some(),
            r.from.is_some(),
            r.template.is_some(),
            r.first_of.is_some(),
        ];
        if sources.iter().filter(|&&s| s).count() != 1 {
            return bad("a value needs exactly one of path, from, template, first_of");
        }
        let source = if let Some(p) = &r.path {
            Source::Path(parse_path(p))
        } else if let Some(f) = &r.from {
            Source::From(self.reference(f)?)
        } else if let Some(t) = &r.template {
            Source::Template(self.template(t)?)
        } else {
            let list = r.first_of.as_deref().unwrap_or_default();
            Source::FirstOf(
                list.iter()
                    .map(|t| self.template(t))
                    .collect::<Result<_, _>>()?,
            )
        };
        let coerce = r.coerce.as_deref().map(coerce_named).transpose()?;
        let none_before = r
            .none_before
            .as_deref()
            .map(|t| Instant::from_isoformat(t).map_err(|e| CompileError(e.0)))
            .transpose()?;
        let near = r.near.as_deref().map(|n| self.reference(n)).transpose()?;
        let when = self.by_value(r.when.as_ref())?;
        let unless = self.by_value(r.unless.as_ref())?;
        let regex = match &r.regex {
            None => None,
            Some(p) => {
                let compiled = pattern(p)?;
                let slots = r.captures.iter().map(|c| self.define(c)).collect();
                let captures_as = r.captures_as.as_deref().map(coerce_named).transpose()?;
                Some((compiled, slots, captures_as))
            }
        };
        Ok(CRule {
            source,
            trim: r.trim,
            coerce,
            none: r.none.clone(),
            none_before,
            invalid: r.invalid,
            regex,
            near,
            fresh: r.fresh,
            when,
            unless,
        })
    }

    fn one(&mut self, v: &ValueRule, name: &str) -> Result<One, CompileError> {
        Ok(match v {
            ValueRule::Rule(r) => One::Rule(Box::new(self.rule(r)?)),
            ValueRule::Fn(n) => One::Fn(Box::new(normalize::compile(self, n, name)?)),
        })
    }

    fn value(&mut self, name: &str, spec: &ValueSpec) -> Result<CSlot, CompileError> {
        let value = match &spec.source {
            OrChoice::Plain(v) => CValue::One(self.one(v, name)?),
            OrChoice::Choice { by, cases } => {
                let by = self.reference(by)?;
                let mut compiled = Vec::new();
                for Case { matches, then } in cases {
                    compiled.push((matches.clone(), self.one(then, name)?));
                }
                CValue::Choice {
                    by,
                    cases: compiled,
                }
            }
        };
        let slot = self.define(name);
        Ok(CSlot {
            slot,
            value,
            required: spec.required,
        })
    }

    fn bool_pick(&self, c: &OrChoice<bool>) -> Result<Pick<bool>, CompileError> {
        Ok(match c {
            OrChoice::Plain(b) => Pick::Plain(*b),
            OrChoice::Choice { by, cases } => Pick::Choice {
                by: self.reference(by)?,
                cases: cases.iter().map(|c| (c.matches.clone(), c.then)).collect(),
            },
        })
    }

    fn body(&self, b: &FactBody) -> Result<CBody, CompileError> {
        let opt = |name: &Option<String>| name.as_deref().map(|n| self.input(n)).transpose();
        Ok(match b {
            FactBody::VehicleKey { id } => CBody::VehicleKey(self.template(id)?),
            FactBody::TripKey { ns, key } => CBody::TripKey(ns.clone(), self.template(key)?),
            FactBody::LineRef { ns, value } => CBody::LineRef(ns.clone(), self.template(value)?),
            FactBody::Destination { name, townless } => {
                CBody::Destination(self.input(name)?, *townless)
            }
            FactBody::NextStopName {
                name,
                scheduled,
                townless,
            } => CBody::NextStopName {
                name: self.input(name)?,
                scheduled: scheduled.as_deref().map(|s| self.input(s)).transpose()?,
                townless: *townless,
            },
            FactBody::Position {
                lat,
                lon,
                xy,
                crs,
                round,
                bearing,
                zero_is_none,
                needs_time,
            } => {
                let xy = match xy {
                    None => None,
                    Some(name) => {
                        if crs.as_deref() != Some("EPSG:5514") {
                            return bad("a position's xy needs crs = \"EPSG:5514\"");
                        }
                        Some((self.input(name)?, round.unwrap_or(6)))
                    }
                };
                CBody::Position {
                    lat: opt(lat)?,
                    lon: opt(lon)?,
                    xy,
                    bearing: opt(bearing)?,
                    zero_is_none: *zero_is_none,
                    needs_time: *needs_time,
                }
            }
            FactBody::Delay {
                s,
                scale,
                default,
                reference,
            } => CBody::Delay {
                s: self.input(s)?,
                scale: scale.unwrap_or(1),
                default: *default,
                reference: DelayReference::parse(reference)
                    .ok_or_else(|| CompileError(format!("unknown delay ref {reference:?}")))?,
            },
            FactBody::SourceState { code } => CBody::SourceState(match code {
                OrChoice::Plain(c) => Pick::Plain(self.template(&c.0)?),
                OrChoice::Choice { by, cases } => Pick::Choice {
                    by: self.reference(by)?,
                    cases: cases
                        .iter()
                        .map(|c| Ok((c.matches.clone(), self.template(&c.then.0)?)))
                        .collect::<Result<_, CompileError>>()?,
                },
            }),
            FactBody::SourceDeparture { at } => CBody::SourceDeparture(self.input(at)?),
            FactBody::ServiceDay { day } => CBody::ServiceDay(self.input(day)?),
            FactBody::PointEvent {
                name,
                codes,
                scheduled,
                actual,
                width,
                standing,
            } => CBody::PointEvent {
                name: self.input(name)?,
                codes: self.input(codes)?,
                scheduled: self.input(scheduled)?,
                actual: self.input(actual)?,
                width: duration(width)?,
                standing: self.bool_pick(standing)?,
            },
            FactBody::NextPoint { name, sr70 } => CBody::NextPoint {
                name: self.input(name)?,
                sr70: self.input(sr70)?,
            },
            FactBody::NextStopPrediction {
                sr70,
                scheduled,
                predicted,
            } => CBody::NextStopPrediction {
                sr70: self.input(sr70)?,
                scheduled: self.input(scheduled)?,
                predicted: self.input(predicted)?,
            },
            FactBody::TripStatus {
                replacement_bus,
                diverted,
            } => CBody::TripStatus {
                replacement_bus: self.bool_pick(replacement_bus)?,
                diverted: self.bool_pick(diverted)?,
            },
            FactBody::TripCancelled {} => CBody::TripCancelled,
            FactBody::VehicleAttribute { a, value } => {
                CBody::VehicleAttribute(a.clone(), self.input(value)?)
            }
        })
    }

    fn gate(&self, g: &Gate) -> Result<CGate, CompileError> {
        if let Some(t) = &g.if_fact
            && !FACT_TYPES.contains(&t.as_str())
        {
            return bad(format!("unknown fact type {t:?}"));
        }
        Ok(CGate {
            when: self.by_value(g.when.as_ref())?,
            unless: self.by_value(g.unless.as_ref())?,
            if_fact: g.if_fact.clone(),
        })
    }

    fn facts(&self, specs: &[FactSpec], depth: usize) -> Result<Vec<CFact>, CompileError> {
        specs
            .iter()
            .map(|spec| match spec {
                FactSpec::Fact { gate, body } => {
                    let body = match body {
                        OrChoice::Plain(b) => Pick::Plain(self.body(b)?),
                        OrChoice::Choice { by, cases } => Pick::Choice {
                            by: self.reference(by)?,
                            cases: cases
                                .iter()
                                .map(|c| Ok((c.matches.clone(), self.body(&c.then)?)))
                                .collect::<Result<_, CompileError>>()?,
                        },
                    };
                    Ok(CFact::One(self.gate(gate)?, body))
                }
                FactSpec::Use { gate, group } => {
                    if depth > 4 {
                        return bad("fact groups nest too deep");
                    }
                    let inner = self
                        .groups
                        .get(group)
                        .ok_or_else(|| CompileError(format!("no fact group {group:?}")))?;
                    Ok(CFact::Group(
                        self.gate(gate)?,
                        self.facts(inner, depth + 1)?,
                    ))
                }
            })
            .collect()
    }

    pub(crate) fn catalogue(
        &mut self,
        path: &str,
    ) -> Result<Arc<HashMap<String, Vec<String>>>, CompileError> {
        if let Some(found) = self.catalogues.get(path) {
            return Ok(found.clone());
        }
        let text = (self.files)(path).map_err(|e| CompileError(format!("{path}: {e}")))?;
        let table = Arc::new(normalize::name_codes(&text));
        self.catalogues.insert(path.to_owned(), table.clone());
        Ok(table)
    }
}

/// The fact type names (`if_fact`, `require_fact`), as `model_json` tags them.
pub const FACT_TYPES: [&str; 17] = [
    "vehicle_key",
    "trip_key",
    "line_ref",
    "destination",
    "next_stop_name",
    "position",
    "delay",
    "source_state",
    "source_departure",
    "service_day",
    "point_event",
    "next_point",
    "next_stop_prediction",
    "trip_status",
    "trip_cancelled",
    "platform",
    "vehicle_attribute",
];

fn fact_type(f: &Fact) -> &'static str {
    match f {
        Fact::VehicleKey(_) => "vehicle_key",
        Fact::TripKey(_) => "trip_key",
        Fact::LineRef(_) => "line_ref",
        Fact::Destination(_) => "destination",
        Fact::NextStopName(_) => "next_stop_name",
        Fact::Position(_) => "position",
        Fact::Delay(_) => "delay",
        Fact::SourceState(_) => "source_state",
        Fact::SourceDeparture(_) => "source_departure",
        Fact::ServiceDay(_) => "service_day",
        Fact::PointEvent(_) => "point_event",
        Fact::NextPoint(_) => "next_point",
        Fact::NextStopPrediction(_) => "next_stop_prediction",
        Fact::TripStatus(_) => "trip_status",
        Fact::TripCancelled(_) => "trip_cancelled",
        Fact::PlatformAssignment(_) => "platform",
        Fact::VehicleAttribute(_) => "vehicle_attribute",
    }
}

fn all_match(slots: &Slots, conditions: &[(Ref, Match)]) -> bool {
    conditions
        .iter()
        .all(|(r, m)| matches(get(slots, r).as_deref(), m))
}

/// `when` all match, and `unless` not all (an empty `unless` never stops).
fn gates(slots: &Slots, when: &[(Ref, Match)], unless: &[(Ref, Match)]) -> bool {
    all_match(slots, when) && (unless.is_empty() || !all_match(slots, unless))
}

impl Mapping {
    /// Compiles a declarative decode section.
    pub fn compile(decode: &Decode, files: Files<'_>) -> Result<Mapping, CompileError> {
        let Some(version) = decode.version else {
            return bad("a declarative decode needs `version`");
        };
        let mut c = Compiler {
            names: HashMap::new(),
            slots: 0,
            groups: &decode.groups,
            files,
            catalogues: HashMap::new(),
        };
        let mut document = Vec::new();
        for (name, spec) in &decode.document {
            document.push(c.value(name, spec)?);
        }
        let guards = decode
            .guard
            .iter()
            .map(|g| match g {
                Guard::StaleBody {
                    value,
                    cut_at,
                    layout,
                    ..
                } => Ok(CGuard::StaleBody {
                    value: c.reference(value)?,
                    cut_at: cut_at.clone(),
                    layout: Layout::new(layout),
                }),
            })
            .collect::<Result<_, CompileError>>()?;
        let mut values = Vec::new();
        for (name, spec) in &decode.values {
            values.push(c.value(name, spec)?);
        }
        // Profiles' selector outputs are values too.
        for profile in decode.profile.values() {
            if let Some(Selector::BlockCurrent { out, .. }) = &profile.select {
                c.define(out);
            }
        }
        let skip = c.by_value(decode.skip.as_ref())?;
        let keep = c.by_value(decode.keep.as_ref())?;
        let filter_after = if skip.is_empty() && keep.is_empty() {
            None
        } else {
            let referenced = skip
                .iter()
                .chain(&keep)
                .filter_map(|(r, _)| values.iter().position(|v| v.slot == r.slot))
                .max()
                .unwrap_or(0);
            let leading_required = values
                .iter()
                .take_while(|v| v.required == Required::Entity)
                .count();
            Some(referenced.max(leading_required.saturating_sub(1)))
        };
        let feed = match &decode.feed {
            None => return bad("a declarative decode needs `feed`"),
            Some(OrChoice::Plain(f)) => Pick::Plain(feed_of(*f)),
            Some(OrChoice::Choice { by, cases }) => Pick::Choice {
                by: c.reference(by)?,
                cases: cases
                    .iter()
                    .map(|k| (k.matches.clone(), feed_of(k.then)))
                    .collect(),
            },
        };
        let observed_at = decode
            .observed_at
            .as_ref()
            .map(|o| Ok::<_, CompileError>((c.reference(&o.value)?, o.fresh)))
            .transpose()?;
        let mut profiles = Vec::new();
        if decode.profile.is_empty() {
            profiles.push(CProfile {
                when: Vec::new(),
                require: Vec::new(),
                require_fact: None,
                select: None,
                facts: c.facts(&decode.fact, 0)?,
            });
        } else {
            if !decode.fact.is_empty() {
                return bad("facts go in the profiles when there are profiles");
            }
            for profile in decode.profile.values() {
                let select = match &profile.select {
                    None => None,
                    Some(Selector::BlockCurrent {
                        group_by,
                        state,
                        start,
                        running,
                        off_route,
                        pre_trip,
                        out,
                        ..
                    }) => Some(BlockCurrent {
                        group_by: c.reference(group_by)?,
                        state: c.reference(state)?,
                        start: c.reference(start)?,
                        running: running.clone(),
                        off_route: off_route.clone(),
                        pre_trip: pre_trip.clone(),
                        out: c.reference(out)?.slot,
                    }),
                };
                profiles.push(CProfile {
                    when: c.by_value(profile.when.as_ref())?,
                    require: profile
                        .require
                        .iter()
                        .map(|n| c.reference(n))
                        .collect::<Result<_, _>>()?,
                    require_fact: profile.require_fact.clone(),
                    select,
                    facts: c.facts(&profile.fact, 0)?,
                });
            }
        }
        Ok(Mapping {
            version,
            root: decode.root,
            non_empty: decode.non_empty,
            json_error: decode
                .errors
                .json
                .clone()
                .unwrap_or_else(|| "not JSON".into()),
            root_error: decode
                .errors
                .root
                .clone()
                .unwrap_or_else(|| "payload has the wrong shape".into()),
            entities_error: decode.errors.entities.clone().unwrap_or_else(|| {
                format!(
                    "payload lacks {}",
                    decode.entities.as_deref().unwrap_or("entities")
                )
            }),
            entities: parse_path(
                decode
                    .entities
                    .as_deref()
                    .ok_or_else(|| CompileError("a declarative decode needs `entities`".into()))?,
            ),
            document,
            guards,
            values,
            slots: c.slots,
            skip,
            keep,
            filter_after,
            feed,
            observed_at,
            profiles,
            krovak: Arc::new(Transformer::new()),
        })
    }

    /// A payload's observations (`decode(body, sha256, received_at, skew)` of a connector).
    pub fn decode(
        &self,
        body: &[u8],
        sha256: &str,
        source: &str,
        channel: &str,
        ctx: &Context,
    ) -> Result<Vec<Observation>, PayloadError> {
        let document = json::parse(body, &self.json_error)?;
        match self.root {
            Some(Root::Object) if !document.is_object() => {
                return Err(PayloadError::new(&self.root_error));
            }
            Some(Root::Array) => match &document {
                Value::Array(a) if !(self.non_empty && a.is_empty()) => {}
                _ => return Err(PayloadError::new(&self.root_error)),
            },
            _ => {}
        }
        let Some(Value::Array(entities)) = lookup(&document, &self.entities) else {
            return Err(PayloadError::new(&self.entities_error));
        };
        let mut base: Vec<Option<Val>> = vec![None; self.slots];
        for cs in &self.document {
            let found = self.evaluate(&cs.value, &document, &base, ctx)?;
            if found.missing(cs.required) {
                return Err(PayloadError::new(&self.entities_error));
            }
            found.store(&mut base, cs.slot);
        }
        for guard in &self.guards {
            if self.guarded(guard, &base, ctx) {
                return Ok(Vec::new());
            }
        }
        let mut out: Vec<(usize, Observation)> = Vec::new();
        let mut selected: Vec<Vec<(usize, Vec<Option<Val>>)>> =
            vec![Vec::new(); self.profiles.len()];
        'entities: for (item, entity) in entities.iter().enumerate() {
            if !entity.is_object() {
                continue;
            }
            let mut slots = base.clone();
            for (index, cs) in self.values.iter().enumerate() {
                let found = self.evaluate(&cs.value, entity, &slots, ctx)?;
                if found.missing(cs.required) {
                    continue 'entities;
                }
                found.store(&mut slots, cs.slot);
                if self.filter_after == Some(index) && !self.passes_filter(&slots) {
                    continue 'entities;
                }
            }
            let Some(p) = self.profiles.iter().position(|p| {
                all_match(&slots, &p.when) && p.require.iter().all(|r| get(&slots, r).is_some())
            }) else {
                continue;
            };
            if self.profiles[p].select.is_some() {
                selected[p].push((item, slots));
                continue;
            }
            if let Some(o) = self.observation(
                &self.profiles[p],
                &slots,
                item,
                sha256,
                source,
                channel,
                ctx,
            )? {
                out.push((item, o));
            }
        }
        for (p, entries) in selected.into_iter().enumerate() {
            let profile = &self.profiles[p];
            let Some(select) = &profile.select else {
                continue;
            };
            for (item, slots) in normalize::block_current(select_view(select), entries) {
                if let Some(o) =
                    self.observation(profile, &slots, item, sha256, source, channel, ctx)?
                {
                    out.push((item, o));
                }
            }
        }
        out.sort_by_key(|(item, _)| *item);
        Ok(out.into_iter().map(|(_, o)| o).collect())
    }

    fn passes_filter(&self, slots: &Slots) -> bool {
        let skipped = !self.skip.is_empty() && all_match(slots, &self.skip);
        let kept = self.keep.is_empty() || all_match(slots, &self.keep);
        !skipped && kept
    }

    fn guarded(&self, guard: &CGuard, slots: &Slots, ctx: &Context) -> bool {
        match guard {
            CGuard::StaleBody {
                value,
                cut_at,
                layout,
            } => {
                let Some(stamp) = get(slots, value) else {
                    return true;
                };
                let stamp = stamp.text();
                let head = stamp.split(cut_at.as_str()).next().unwrap_or_default();
                match read_local(head, layout, ctx.received_at) {
                    Some(at) => !within_skew(at, ctx.received_at, ctx.max_clock_skew),
                    None => true,
                }
            }
        }
    }

    fn evaluate(
        &self,
        value: &CValue,
        entity: &Value,
        slots: &Slots,
        ctx: &Context,
    ) -> Result<Evaluated, PayloadError> {
        match value {
            CValue::One(one) => self.one(one, entity, slots, ctx),
            CValue::Choice { by, cases } => {
                let chosen = get(slots, by);
                let case = cases
                    .iter()
                    .find(|(m, _)| m.as_ref().is_none_or(|m| matches(chosen.as_deref(), m)));
                match case {
                    Some((_, one)) => self.one(one, entity, slots, ctx),
                    None => Ok(Evaluated::default()),
                }
            }
        }
    }

    fn one(
        &self,
        one: &One,
        entity: &Value,
        slots: &Slots,
        ctx: &Context,
    ) -> Result<Evaluated, PayloadError> {
        match one {
            One::Fn(norm) => Ok(Evaluated {
                value: normalize::evaluate(norm, slots, ctx)?,
                ..Evaluated::default()
            }),
            One::Rule(rule) => self.rule(rule, entity, slots, ctx),
        }
    }

    fn rule(
        &self,
        r: &CRule,
        entity: &Value,
        slots: &Slots,
        ctx: &Context,
    ) -> Result<Evaluated, PayloadError> {
        if !gates(slots, &r.when, &r.unless) {
            return Ok(Evaluated {
                gated_off: true,
                ..Evaluated::default()
            });
        }
        let input: Option<Cow<'_, Val>> = match &r.source {
            Source::Path(path) => match lookup(entity, path) {
                None => None,
                Some(j) => return self.finish(r, Cow::Borrowed(j), slots, ctx),
            },
            Source::From(reference) => get(slots, reference),
            Source::Template(t) => t.eval(slots)?.map(|s| Cow::Owned(Val::Text(s))),
            Source::FirstOf(list) => {
                let mut found = None;
                for t in list {
                    if let Some(s) = t.eval(slots)? {
                        found = Some(Cow::Owned(Val::Text(s)));
                        break;
                    }
                }
                found
            }
        };
        let Some(input) = input else {
            return Ok(Evaluated::default());
        };
        match input.as_ref() {
            // Strings and JSON go through trim and the coercion.
            Val::Json(j) => self.finish(r, Cow::Owned(j.clone()), slots, ctx),
            Val::Text(s) => self.finish(r, Cow::Owned(Value::String(s.clone())), slots, ctx),
            // Other values (times, numbers made by a normalizer) pass as they are.
            other if r.coerce.is_none() => self.post(r, Some(other.clone()), ctx),
            other => self.finish(r, Cow::Owned(other.to_json()), slots, ctx),
        }
    }

    /// Trim, coerce, then the modifiers.
    fn finish(
        &self,
        r: &CRule,
        j: Cow<'_, Value>,
        slots: &Slots,
        ctx: &Context,
    ) -> Result<Evaluated, PayloadError> {
        let j = match (r.trim, j.as_ref()) {
            (true, Value::String(s)) => Cow::Owned(Value::String(s.trim().to_owned())),
            _ => j,
        };
        let near = r
            .near
            .as_ref()
            .and_then(|n| get(slots, n))
            .and_then(|v| v.as_instant());
        let value = match &r.coerce {
            Some(c) => c.apply(&j, r.invalid, near, ctx)?,
            None => Some(match j.into_owned() {
                Value::String(s) => Val::Text(s),
                other => Val::Json(other),
            }),
        };
        self.post(r, value, ctx)
    }

    /// `none`, `none_before`, `regex` (and its captures), `fresh`.
    fn post(
        &self,
        r: &CRule,
        value: Option<Val>,
        ctx: &Context,
    ) -> Result<Evaluated, PayloadError> {
        let mut out = Evaluated::default();
        let Some(value) = value else {
            return Ok(out);
        };
        if r.none.iter().any(|s| value.equals(s)) {
            return Ok(out);
        }
        if let (Some(bound), Some(at)) = (r.none_before, value.as_instant())
            && at < bound
        {
            return Ok(out);
        }
        if let Some((regex, slots, captures_as)) = &r.regex {
            let Some(found) = value.as_text().and_then(|t| regex.captures(t)) else {
                return Ok(out);
            };
            for (i, &slot) in slots.iter().enumerate() {
                let captured = match found.get(i + 1) {
                    None => None,
                    Some(m) => match captures_as {
                        Some(c) => c.capture(m.as_str())?,
                        None => Some(Val::Text(m.as_str().to_owned())),
                    },
                };
                out.captures.push((slot, captured));
            }
        }
        if r.fresh
            && let Some(at) = value.as_instant()
            && !within_skew(at, ctx.received_at, ctx.max_clock_skew)
        {
            return Ok(out);
        }
        out.value = Some(value);
        Ok(out)
    }

    #[allow(clippy::too_many_arguments)]
    fn observation(
        &self,
        profile: &CProfile,
        slots: &Slots,
        item: usize,
        sha256: &str,
        source: &str,
        channel: &str,
        ctx: &Context,
    ) -> Result<Option<Observation>, PayloadError> {
        let Some(&feed) = self.feed.pick(slots) else {
            return Ok(None);
        };
        let observed_at = self.observed_at.as_ref().and_then(|(r, fresh)| {
            let at = get(slots, r)?.as_instant()?;
            (!fresh || within_skew(at, ctx.received_at, ctx.max_clock_skew)).then_some(at)
        });
        let mut facts = Vec::new();
        self.make(&profile.facts, slots, observed_at, &mut facts)?;
        if let Some(t) = &profile.require_fact
            && !facts.iter().any(|f| fact_type(f) == t)
        {
            return Ok(None);
        }
        Ok(Some(Observation {
            source: source.to_owned(),
            channel: channel.to_owned(),
            feed,
            received_at: ctx.received_at,
            observed_at,
            raw: RawRef {
                sha256: sha256.to_owned(),
                item: item as i64,
            },
            decoder_version: self.version,
            facts,
        }))
    }

    fn make(
        &self,
        specs: &[CFact],
        slots: &Slots,
        observed_at: Option<Instant>,
        facts: &mut Vec<Fact>,
    ) -> Result<(), PayloadError> {
        for spec in specs {
            let (gate, open) = match spec {
                CFact::One(gate, _) | CFact::Group(gate, _) => (gate, true),
            };
            if !open || !gates(slots, &gate.when, &gate.unless) {
                continue;
            }
            if let Some(t) = &gate.if_fact
                && !facts.iter().any(|f| fact_type(f) == t)
            {
                continue;
            }
            match spec {
                CFact::Group(_, inner) => self.make(inner, slots, observed_at, facts)?,
                CFact::One(_, body) => {
                    if let Some(body) = body.pick(slots)
                        && let Some(fact) = self.fact(body, slots, observed_at)?
                    {
                        facts.push(fact);
                    }
                }
            }
        }
        Ok(())
    }

    fn fact(
        &self,
        body: &CBody,
        slots: &Slots,
        observed_at: Option<Instant>,
    ) -> Result<Option<Fact>, PayloadError> {
        /// An input's value; `Err(())`-like None when a required input is absent.
        enum Got<'s> {
            Value(Cow<'s, Val>),
            Null,
            Missing,
        }
        let got = |i: &In| -> Result<Got<'_>, PayloadError> {
            let v = match &i.source {
                InSource::Ref(r) => get(slots, r),
                InSource::Template(t) => t.eval(slots)?.map(|s| Cow::Owned(Val::Text(s))),
            };
            Ok(match v {
                Some(v) => Got::Value(v),
                None if i.optional => Got::Null,
                None => Got::Missing,
            })
        };
        macro_rules! need {
            ($input:expr) => {
                match got($input)? {
                    Got::Value(v) => Some(v),
                    Got::Null => None,
                    Got::Missing => return Ok(None),
                }
            };
        }
        let text_of = |v: &Val| v.text();
        let instant = |v: Option<Cow<'_, Val>>| v.and_then(|v| v.as_instant());
        let float = |v: Option<Cow<'_, Val>>| -> Option<f64> {
            v.and_then(|v| match v.as_ref() {
                Val::Float(x) => Some(*x),
                Val::Int(n) => Some(*n as f64),
                _ => None,
            })
        };
        Ok(Some(match body {
            CBody::VehicleKey(t) => match t.eval(slots)? {
                Some(id) => Fact::VehicleKey(VehicleKey {
                    source_vehicle_id: id,
                }),
                None => return Ok(None),
            },
            CBody::TripKey(ns, t) => match t.eval(slots)? {
                Some(key) => Fact::TripKey(TripKey {
                    namespace: ns.clone(),
                    key,
                }),
                None => return Ok(None),
            },
            CBody::LineRef(ns, t) => match t.eval(slots)? {
                Some(value) => Fact::LineRef(LineRef {
                    namespace: ns.clone(),
                    value,
                }),
                None => return Ok(None),
            },
            CBody::Destination(i, townless) => match need!(i) {
                Some(v) => Fact::Destination(Destination {
                    name: text_of(&v),
                    townless: *townless,
                }),
                None => return Ok(None),
            },
            CBody::NextStopName {
                name,
                scheduled,
                townless,
            } => {
                let Some(name) = need!(name).map(|v| text_of(&v)) else {
                    return Ok(None);
                };
                let scheduled = match scheduled {
                    Some(i) => instant(need!(i)),
                    None => None,
                };
                Fact::NextStopName(NextStopName {
                    name,
                    scheduled,
                    townless: *townless,
                })
            }
            CBody::Position {
                lat,
                lon,
                xy,
                bearing,
                zero_is_none,
                needs_time,
            } => {
                if *needs_time && observed_at.is_none() {
                    return Ok(None);
                }
                let bearing = match bearing {
                    Some(b) => float(need!(b)),
                    None => None,
                };
                let (lat, lon) = if let Some((input, places)) = xy {
                    let Some(v) = need!(input) else {
                        return Ok(None);
                    };
                    let Val::Pair(x, y) = v.as_ref() else {
                        return Ok(None);
                    };
                    let (lon, lat) = self.krovak.transform(*x, *y);
                    (round_to(lat, *places), round_to(lon, *places))
                } else {
                    let (Some(lat_in), Some(lon_in)) = (lat, lon) else {
                        return Ok(None);
                    };
                    match (float(need!(lat_in)), float(need!(lon_in))) {
                        (Some(a), Some(o)) => (a, o),
                        _ => return Ok(None),
                    }
                };
                if *zero_is_none && (lat == 0.0 || lon == 0.0) {
                    return Ok(None);
                }
                Fact::Position(Position { lat, lon, bearing })
            }
            CBody::Delay {
                s,
                scale,
                default,
                reference,
            } => {
                let value = match got(s)? {
                    Got::Value(v) => Some(v),
                    Got::Null | Got::Missing => None,
                };
                let seconds = match value {
                    Some(v) => v.to_int()? * scale,
                    None => match default {
                        Some(d) => d * scale,
                        None => return Ok(None),
                    },
                };
                Fact::Delay(Delay {
                    seconds,
                    reference: *reference,
                })
            }
            CBody::SourceState(pick) => match pick.pick(slots) {
                Some(t) => match t.eval(slots)? {
                    Some(code) => Fact::SourceState(SourceState { code }),
                    None => return Ok(None),
                },
                None => return Ok(None),
            },
            CBody::SourceDeparture(i) => match instant(need!(i)) {
                Some(at) => Fact::SourceDeparture(SourceDeparture { at }),
                None => return Ok(None),
            },
            CBody::ServiceDay(i) => match need!(i).as_deref() {
                Some(Val::Date(day)) => Fact::ServiceDay(ServiceDay { day: *day }),
                _ => return Ok(None),
            },
            CBody::PointEvent {
                name,
                codes,
                scheduled,
                actual,
                width,
                standing,
            } => {
                let Some(name) = need!(name).map(|v| text_of(&v)) else {
                    return Ok(None);
                };
                let codes = match need!(codes).as_deref() {
                    Some(Val::Codes(c)) => c.clone(),
                    _ => Vec::new(),
                };
                let scheduled = instant(need!(scheduled));
                let Some(actual) = instant(need!(actual)) else {
                    return Ok(None);
                };
                Fact::PointEvent(PointEvent {
                    name,
                    codes,
                    scheduled,
                    actual: Interval::new(actual, actual + *width),
                    standing: standing.pick(slots).copied().unwrap_or(false),
                })
            }
            CBody::NextPoint { name, sr70 } => {
                let Some(name) = need!(name).map(|v| text_of(&v)) else {
                    return Ok(None);
                };
                Fact::NextPoint(NextPoint {
                    name,
                    sr70: need!(sr70).map(|v| text_of(&v)),
                })
            }
            CBody::NextStopPrediction {
                sr70,
                scheduled,
                predicted,
            } => {
                let Some(sr70) = need!(sr70).map(|v| text_of(&v)) else {
                    return Ok(None);
                };
                Fact::NextStopPrediction(NextStopPrediction {
                    sr70,
                    scheduled: instant(need!(scheduled)),
                    predicted: instant(need!(predicted)),
                })
            }
            CBody::TripStatus {
                replacement_bus,
                diverted,
            } => Fact::TripStatus(TripStatus {
                replacement_bus: replacement_bus.pick(slots).copied().unwrap_or(false),
                diverted: diverted.pick(slots).copied().unwrap_or(false),
            }),
            CBody::TripCancelled => Fact::TripCancelled(TripCancelled),
            CBody::VehicleAttribute(a, i) => match need!(i) {
                Some(v) => Fact::VehicleAttribute(VehicleAttribute {
                    attribute: a.clone(),
                    value: text_of(&v),
                }),
                None => return Ok(None),
            },
        }))
    }
}

/// A value and the regex captures it defines.
#[derive(Debug, Default)]
struct Evaluated {
    value: Option<Val>,
    captures: Vec<(usize, Option<Val>)>,
    /// `when`/`unless` said no: absent, but not missing (`required` does not apply).
    gated_off: bool,
}

impl Evaluated {
    fn missing(&self, required: Required) -> bool {
        self.value.is_none() && !self.gated_off && required != Required::No
    }
}

impl Evaluated {
    fn store(self, slots: &mut [Option<Val>], slot: usize) {
        for (s, v) in self.captures {
            slots[s] = v;
        }
        slots[slot] = self.value;
    }
}

fn feed_of(f: FeedName) -> Feed {
    match f {
        FeedName::Jdf => Feed::Jdf,
        FeedName::Czptt => Feed::Czptt,
    }
}

/// `x` rounded to `places` decimals.
fn round_to(x: f64, places: usize) -> f64 {
    let scale = 10_f64.powi(places as i32);
    (x * scale).round() / scale
}

fn select_view(b: &BlockCurrent) -> normalize::BlockView<'_> {
    normalize::BlockView {
        group_by: &b.group_by,
        state: &b.state,
        start: &b.start,
        running: &b.running,
        off_route: &b.off_route,
        pre_trip: &b.pre_trip,
        out: b.out,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn patterns_match_at_the_start() {
        let id = pattern(r"^TR/([^/]+)/(\d{8})$").unwrap();
        assert!(id.is_match("TR/1154/20261012"));
        assert!(!id.is_match(
            "TR/1154/20261012
"
        ));
        assert!(!id.is_match("xTR/1154/20261012"));
        assert!(pattern("a").unwrap().is_match("ab"));
    }

    #[test]
    fn durations_and_paths() {
        assert_eq!(duration("5m").unwrap(), TimeDelta::from_minutes(5));
        assert_eq!(duration("59s").unwrap(), TimeDelta::from_seconds(59));
        let doc: Value = serde_json::from_str(r#"[{"a": [1, {"b": 2}]}]"#).unwrap();
        assert_eq!(lookup(&doc, &parse_path("0.a.1.b")), Some(&Value::from(2)));
        assert_eq!(lookup(&doc, &parse_path("a")), None);
        assert_eq!(lookup(&doc, &parse_path("")), Some(&doc));
        let obj: Value = serde_json::from_str(r#"{"0": 1}"#).unwrap();
        assert_eq!(lookup(&obj, &parse_path("0")), None);
    }

    #[test]
    fn clock_times() {
        assert_eq!(hhmm(" 7:05 "), Some(7 * 3_600_000_000 + 5 * 60_000_000));
        assert_eq!(hhmm("24:00"), None);
        assert_eq!(hhmm("7:5"), None);
        assert_eq!(hhmm("07:05:00"), None);
        assert_eq!(
            hhmmss("7:05:09"),
            Some((7 * 3_600 + 5 * 60 + 9) * 1_000_000)
        );
        assert_eq!(
            hhmmss("23:59:59"),
            Some((23 * 3_600 + 59 * 60 + 59) * 1_000_000)
        );
        assert_eq!(hhmmss("24:00:00"), None);
        assert_eq!(hhmmss("07:05:60"), None);
        assert_eq!(hhmmss("07:05"), None);
        assert_eq!(hhmmss("07:5:00"), None);
        assert_eq!(round_to(50.123_456_5, 6), 50.123_457);
    }
}
