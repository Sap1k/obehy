//! `[channel.decode]`: how a channel's payload becomes facts (docs/RUST_PORT.md,
//! "Config-driven sources"; the design is rust/docs/DECODE_SPEC.md, settled here on the eight
//! sources). Only the worker reads it.
//!
//! Every table is read strictly (unknown keys are errors). The shape:
//!
//! - `version` (the decoder version observations carry), `reader` (`json`, `html`,
//!   `gtfs-rt`), and either `decoder` (a code decoder: `sz-tabule`, `pid-alerts`,
//!   `pid-infotexts`, `cd-composition`) or the declarative mapping below.
//! - `entities`: the dotted path of the entity list (a numeric segment indexes a list);
//!   `root` (`object`, `array`) and `non_empty` check the document first; `errors` holds the
//!   payload error texts.
//! - `[document]` values, evaluated once per payload, and `guard`s that may drop a payload
//!   whole (DPMD-Q9).
//! - `[values]`, evaluated per entity in order: a `path` in the entity, a value `from` another
//!   (`label.Linka`: a field of a map or record value), a `template`, the `first_of` templates,
//!   a named normalizer (`fn`, with its quirk ID), or a choice by another value (`by` and
//!   `cases`). Modifiers: `trim`, `as` (a coercion), `none`, `none_before`,
//!   `invalid`, `regex` and `captures`, `near`, `fresh`, `when`/`unless`, `required`.
//! - `skip`/`keep`, `feed`, `observed_at`, then `fact` (or `[profile.*]`, each with its own
//!   facts and an optional `select`or across entities) with reusable `[groups]`.
//!
//! A choice anywhere is `{ by = "<value>", cases = [{ match = <scalar or list>, ... }, ...] }`;
//! a case without `match` is the default. Numbers match by value (`1` matches `1.0`), texts by
//! text, booleans by truth.

use indexmap::IndexMap;
use serde::de::{DeserializeOwned, Error as _};
use serde::{Deserialize, Deserializer};
use toml::{Table, Value};

/// One channel's (or a shared block's) decoding.
#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Decode {
    pub version: Option<i64>,
    pub reader: Reader,
    pub decoder: Option<CodeDecoder>,
    pub entities: Option<String>,
    pub root: Option<Root>,
    #[serde(default)]
    pub non_empty: bool,
    #[serde(default)]
    pub errors: Errors,
    #[serde(default)]
    pub document: IndexMap<String, ValueSpec>,
    #[serde(default)]
    pub guard: Vec<Guard>,
    #[serde(default)]
    pub values: IndexMap<String, ValueSpec>,
    pub skip: Option<ByValue>,
    pub keep: Option<ByValue>,
    pub feed: Option<OrChoice<FeedName>>,
    pub observed_at: Option<ObservedAt>,
    #[serde(default)]
    pub groups: FactGroups,
    #[serde(default)]
    pub profile: IndexMap<String, Profile>,
    #[serde(default)]
    pub fact: Vec<FactSpec>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum Reader {
    Json,
    Html,
    GtfsRt,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum CodeDecoder {
    SzTabule,
    PidAlerts,
    PidInfotexts,
    CdComposition,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Root {
    Object,
    Array,
}

/// Payload error texts.
#[derive(Debug, Clone, Default, PartialEq, Eq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Errors {
    /// Prefix of an unreadable body (default `not JSON`).
    pub json: Option<String>,
    /// The root has the wrong type.
    pub root: Option<String>,
    /// The entity list (or a `required = "payload"` document value) is missing.
    pub entities: Option<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum FeedName {
    Jdf,
    Czptt,
}

/// A source time: the value, and whether one beyond the policy's clock skew is dropped (T15).
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ObservedAt {
    pub value: String,
    #[serde(default)]
    pub fresh: bool,
}

/// `name = scalar or [scalars]`, every entry matching.
pub type ByValue = IndexMap<String, Match>;

/// Named fact groups (`[channel.decode.groups]`).
pub type FactGroups = IndexMap<String, Vec<FactSpec>>;

/// A scalar or a list of scalars (any of them).
#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(untagged)]
pub enum Match {
    One(Scalar),
    Any(Vec<Scalar>),
}

impl Match {
    pub fn scalars(&self) -> &[Scalar] {
        match self {
            Match::One(s) => std::slice::from_ref(s),
            Match::Any(list) => list,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(untagged)]
pub enum Scalar {
    Bool(bool),
    Int(i64),
    Float(f64),
    Text(String),
}

/// A value or a choice of values by another value.
#[derive(Debug, Clone, PartialEq)]
pub enum OrChoice<T> {
    Plain(T),
    Choice { by: String, cases: Vec<Case<T>> },
}

#[derive(Debug, Clone, PartialEq)]
pub struct Case<T> {
    /// None: the default.
    pub matches: Option<Match>,
    pub then: T,
}

/// How a choice case holds its body: scalars under `value`, tables as the case's own keys.
pub trait CaseBody: DeserializeOwned {
    const SCALAR: bool = false;
}

impl CaseBody for FeedName {
    const SCALAR: bool = true;
}
impl CaseBody for bool {
    const SCALAR: bool = true;
}

fn from_value<T: DeserializeOwned, E: serde::de::Error>(value: Value) -> Result<T, E> {
    T::deserialize(value).map_err(E::custom)
}

impl<'de, T: CaseBody> Deserialize<'de> for OrChoice<T> {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        let value = Value::deserialize(d)?;
        let Value::Table(mut table) = value else {
            return from_value(value).map(OrChoice::Plain);
        };
        let Some(by) = table.remove("by") else {
            return from_value(Value::Table(table)).map(OrChoice::Plain);
        };
        let by = by
            .as_str()
            .ok_or_else(|| D::Error::custom("`by` names a value"))?
            .to_owned();
        let cases = match table.remove("cases") {
            Some(Value::Array(cases)) => cases,
            _ => return Err(D::Error::custom("a choice needs `cases`")),
        };
        if let Some(key) = table.keys().next() {
            return Err(D::Error::custom(format!("unknown key `{key}` in a choice")));
        }
        let cases = cases
            .into_iter()
            .map(|case| case_of::<T, D::Error>(case, &Table::new()))
            .collect::<Result<_, _>>()?;
        Ok(OrChoice::Choice { by, cases })
    }
}

/// One case: `match` taken off, the rest (plus `inject`ed keys) read as the body.
fn case_of<T: CaseBody, E: serde::de::Error>(case: Value, inject: &Table) -> Result<Case<T>, E> {
    let Value::Table(mut table) = case else {
        return Err(E::custom("a case is a table"));
    };
    let matches = table.remove("match").map(from_value).transpose()?;
    for (k, v) in inject {
        table.insert(k.clone(), v.clone());
    }
    let then = if T::SCALAR {
        let value = table
            .remove("value")
            .ok_or_else(|| E::custom("a case needs `value`"))?;
        if let Some(key) = table.keys().next() {
            return Err(E::custom(format!("unknown key `{key}` in a case")));
        }
        from_value(value)?
    } else {
        from_value(Value::Table(table))?
    };
    Ok(Case { matches, then })
}

/// `required = true` drops the entity when the value is absent; `"payload"` makes the payload
/// an error (a document value).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum Required {
    #[default]
    No,
    Entity,
    Payload,
}

impl<'de> Deserialize<'de> for Required {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        match Value::deserialize(d)? {
            Value::Boolean(true) => Ok(Required::Entity),
            Value::Boolean(false) => Ok(Required::No),
            Value::String(s) if s == "payload" => Ok(Required::Payload),
            other => Err(D::Error::custom(format!(
                "required is true, false or \"payload\", not {other}"
            ))),
        }
    }
}

/// How an `iso` text that is no ISO time counts.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Invalid {
    /// Absent.
    #[default]
    None,
    /// The whole payload is undecodable.
    Fail,
}

/// One value: where it comes from and how it is read.
#[derive(Debug, Clone, PartialEq)]
pub struct ValueSpec {
    pub source: OrChoice<ValueRule>,
    /// Applies to the whole value, also a choice.
    pub required: Required,
}

impl<'de> Deserialize<'de> for ValueSpec {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        let mut table = Table::deserialize(d)?;
        let required = table
            .remove("required")
            .map(from_value)
            .transpose()?
            .unwrap_or_default();
        let source = from_value(Value::Table(table))?;
        Ok(ValueSpec { source, required })
    }
}

impl CaseBody for ValueRule {}

/// A value read by rule, or made by a named normalizer (a table with `fn`).
#[derive(Debug, Clone, PartialEq)]
pub enum ValueRule {
    Rule(Box<Rule>),
    Fn(Box<Normalizer>),
}

impl<'de> Deserialize<'de> for ValueRule {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        let table = Table::deserialize(d)?;
        if table.contains_key("fn") {
            from_value(Value::Table(table)).map(|n| ValueRule::Fn(Box::new(n)))
        } else {
            from_value(Value::Table(table)).map(|r| ValueRule::Rule(Box::new(r)))
        }
    }
}

/// Where a value comes from (exactly one source) and how it is read.
#[derive(Debug, Clone, PartialEq, Default, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Rule {
    pub path: Option<String>,
    pub from: Option<String>,
    pub template: Option<String>,
    pub first_of: Option<Vec<String>>,
    #[serde(default)]
    pub trim: bool,
    #[serde(rename = "as")]
    pub coerce: Option<String>,
    #[serde(default)]
    pub none: Vec<Scalar>,
    pub none_before: Option<String>,
    #[serde(default)]
    pub invalid: Invalid,
    pub regex: Option<String>,
    #[serde(default)]
    pub captures: Vec<String>,
    pub captures_as: Option<String>,
    pub near: Option<String>,
    #[serde(default)]
    pub fresh: bool,
    pub when: Option<ByValue>,
    pub unless: Option<ByValue>,
}

/// One quirk ID or several.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
#[serde(untagged)]
pub enum Quirks {
    One(String),
    Many(Vec<String>),
}

impl Quirks {
    pub fn ids(&self) -> Vec<&str> {
        match self {
            Quirks::One(q) => vec![q.as_str()],
            Quirks::Many(list) => list.iter().map(String::as_str).collect(),
        }
    }
}

/// The named normalizers that make a value: the quirks that need logic, each with its ID
/// (docs/sources/<source>.md).
#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(tag = "fn", rename_all = "snake_case", deny_unknown_fields)]
pub enum Normalizer {
    /// DUK-Q1: `value` (the time read from `text`) less its offset once more when it is later
    /// than `against` by that offset, within `tolerance`.
    OffsetAppliedTwice {
        quirk: Quirks,
        value: String,
        text: String,
        against: String,
        tolerance: String,
    },
    /// PID-Q4: the state is `canceled`, or the flag is JSON `true` (not `1`).
    PidCancelled {
        quirk: Quirks,
        state: String,
        flag: String,
        canceled: String,
    },
    /// SZ-Q4: every 5-digit SR70 code the catalogue gives the name.
    CatalogueCodes {
        quirk: Quirks,
        name: String,
        catalogue: String,
    },
    /// SZ-Q12: a truncated minute's middle.
    MinuteMiddle { quirk: Quirks, value: String },
    /// DPMD-Q2: the popup's `<br>label: text` pairs, a map.
    PopupLabels { quirk: Quirks, value: String },
    /// DPMD-Q10: the arrow's heading, only while moving.
    MovingBearing {
        quirk: Quirks,
        speed: String,
        arrow: String,
        /// Only computed when these values are present (Python reads the arrow only for a
        /// position it keeps).
        #[serde(default)]
        needs: Vec<String>,
    },
    /// MPVNET-Q2, Q3: the pipe-separated record by vehicle kind; `<name>.kind` is `bus` or
    /// `train`, `<name>.<n>` its fields.
    MpvnetRecord {
        quirk: Quirks,
        record: String,
        kind: String,
        category: String,
        bus: RecordLayout,
        train: RecordLayout,
    },
    /// MPVNET-Q6: `min * 60 + s`, negative with the minus group.
    SignedMinSec {
        quirk: Quirks,
        value: String,
        regex: String,
    },
}

impl Normalizer {
    pub fn quirk(&self) -> &Quirks {
        match self {
            Normalizer::OffsetAppliedTwice { quirk, .. }
            | Normalizer::PidCancelled { quirk, .. }
            | Normalizer::CatalogueCodes { quirk, .. }
            | Normalizer::MinuteMiddle { quirk, .. }
            | Normalizer::PopupLabels { quirk, .. }
            | Normalizer::MovingBearing { quirk, .. }
            | Normalizer::MpvnetRecord { quirk, .. }
            | Normalizer::SignedMinSec { quirk, .. } => quirk,
        }
    }
}

/// Which records are a kind's: the `kind` field's value and the field count; a bus also its
/// category and the pattern of its line field.
#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RecordLayout {
    pub t: String,
    pub fields: usize,
    pub category: Option<Scalar>,
    pub line_field: Option<usize>,
    pub line_regex: Option<String>,
}

/// A payload-level normalizer: when it fires, the payload gives no observations.
#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(tag = "fn", rename_all = "snake_case", deny_unknown_fields)]
pub enum Guard {
    /// DPMD-Q9: a body whose time (`value` cut at `cut_at`, in `layout`) is beyond the skew.
    StaleBody {
        quirk: Quirks,
        value: String,
        cut_at: String,
        layout: String,
    },
}

/// A normalizer choosing one entity per group across the payload.
#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(tag = "fn", rename_all = "snake_case", deny_unknown_fields)]
pub enum Selector {
    /// PID-Q6, Q4: of a vehicle's block, the entry it is running (on route first), else the
    /// next to start; `out` its trip state (`pre_trip`, `running`) when its trip may bind.
    BlockCurrent {
        quirk: Quirks,
        group_by: String,
        state: String,
        start: String,
        running: Vec<String>,
        off_route: String,
        pre_trip: Vec<String>,
        out: String,
    },
}

/// A set of facts for the entities it applies to.
#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Profile {
    pub when: Option<ByValue>,
    #[serde(default)]
    pub require: Vec<String>,
    /// The observation is dropped unless a fact of this type was made.
    pub require_fact: Option<String>,
    /// A normalizer choosing one entity per group across the payload.
    pub select: Option<Selector>,
    pub fact: Vec<FactSpec>,
}

/// When a fact is made.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct Gate {
    pub when: Option<ByValue>,
    pub unless: Option<ByValue>,
    /// Only if the observation already has a fact of this type.
    pub if_fact: Option<String>,
}

/// One fact (or a group of them) of an observation.
#[derive(Debug, Clone, PartialEq)]
pub enum FactSpec {
    Fact {
        gate: Gate,
        body: OrChoice<FactBody>,
    },
    Use {
        gate: Gate,
        group: String,
    },
}

impl<'de> Deserialize<'de> for FactSpec {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        let mut table = Table::deserialize(d)?;
        let gate = Gate {
            when: table.remove("when").map(from_value).transpose()?,
            unless: table.remove("unless").map(from_value).transpose()?,
            if_fact: table.remove("if_fact").map(from_value).transpose()?,
        };
        if let Some(group) = table.remove("use") {
            if let Some(key) = table.keys().next() {
                return Err(D::Error::custom(format!(
                    "unknown key `{key}` beside `use`"
                )));
            }
            return Ok(FactSpec::Use {
                gate,
                group: from_value(group)?,
            });
        }
        let t = table
            .remove("t")
            .ok_or_else(|| D::Error::custom("a fact needs `t`"))?;
        let Some(by) = table.remove("by") else {
            table.insert("t".into(), t);
            return Ok(FactSpec::Fact {
                gate,
                body: OrChoice::Plain(from_value(Value::Table(table))?),
            });
        };
        let by: String = from_value(by)?;
        let Some(Value::Array(cases)) = table.remove("cases") else {
            return Err(D::Error::custom("a choice needs `cases`"));
        };
        if let Some(key) = table.keys().next() {
            return Err(D::Error::custom(format!("unknown key `{key}` in a choice")));
        }
        let inject = Table::from_iter([("t".to_owned(), t)]);
        let cases = cases
            .into_iter()
            .map(|c| case_of::<FactBody, D::Error>(c, &inject))
            .collect::<Result<_, _>>()?;
        Ok(FactSpec::Fact {
            gate,
            body: OrChoice::Choice { by, cases },
        })
    }
}

impl CaseBody for FactBody {}

/// The facts of `facts.py`, read from values. A field naming a value may end in `?`: the
/// fact is made without it (null); otherwise an absent value means no fact. Templates are
/// `{name}`, `{name:d}` (an integer) and `{name:06d}`.
#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(tag = "t", rename_all = "snake_case", deny_unknown_fields)]
pub enum FactBody {
    VehicleKey {
        id: String,
    },
    TripKey {
        ns: String,
        key: String,
    },
    LineRef {
        ns: String,
        value: String,
    },
    Destination {
        name: String,
    },
    NextStopName {
        name: String,
    },
    Position {
        lat: Option<String>,
        lon: Option<String>,
        /// A coordinate pair in `crs`, transformed and rounded to `round` decimals.
        xy: Option<String>,
        crs: Option<String>,
        round: Option<usize>,
        bearing: Option<String>,
        /// A coordinate of 0 means none.
        zero_is_none: bool,
        /// Dropped without a source time.
        needs_time: bool,
    },
    Delay {
        s: String,
        scale: Option<i64>,
        default: Option<i64>,
        #[serde(rename = "ref")]
        reference: String,
    },
    SourceState {
        code: OrChoice<Code>,
    },
    SourceDeparture {
        at: String,
    },
    ServiceDay {
        day: String,
    },
    PointEvent {
        name: String,
        codes: String,
        scheduled: String,
        actual: String,
        width: String,
        standing: OrChoice<bool>,
    },
    NextPoint {
        name: String,
        sr70: String,
    },
    NextStopPrediction {
        sr70: String,
        scheduled: String,
        predicted: String,
    },
    TripStatus {
        replacement_bus: OrChoice<bool>,
        diverted: OrChoice<bool>,
    },
    TripCancelled {},
    VehicleAttribute {
        a: String,
        value: String,
    },
}

/// A source state code: a template.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
#[serde(transparent)]
pub struct Code(pub String);

impl CaseBody for Code {
    const SCALAR: bool = true;
}

/// The decode section of a manifest channel: a table, or the name of a shared
/// `[decode.<name>]` block.
pub fn channel_decode(
    raw: Option<&Value>,
    shared: &IndexMap<String, Decode>,
    at: &str,
) -> Result<Option<Decode>, String> {
    match raw {
        None => Ok(None),
        Some(Value::String(name)) => shared
            .get(name)
            .cloned()
            .map(Some)
            .ok_or_else(|| format!("{at}: no [decode.{name}] block")),
        Some(value) => Decode::deserialize(value.clone())
            .map(Some)
            .map_err(|e| format!("{at}: decode: {e}")),
    }
}

/// The shared `[decode.<name>]` blocks of a manifest.
pub fn shared_decodes(document: &Table, at: &str) -> Result<IndexMap<String, Decode>, String> {
    let Some(raw) = document.get("decode") else {
        return Ok(IndexMap::new());
    };
    let Value::Table(blocks) = raw else {
        return Err(format!("{at}: [decode] must hold named tables"));
    };
    blocks
        .iter()
        .map(|(name, block)| {
            Decode::deserialize(block.clone())
                .map(|d| (name.clone(), d))
                .map_err(|e| format!("{at}: decode.{name}: {e}"))
        })
        .collect()
}
