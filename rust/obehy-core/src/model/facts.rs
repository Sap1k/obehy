//! Observations and the facts sources state (docs/R1_SLICE.md and R2_SLICE.md section 3). A
//! fact's JSON form for `rt.observation` is its serde form (tagged by `t`), versioned by
//! [`FACT_SCHEMA_VERSION`] ([`super::json`]).

use std::fmt;
use std::str::FromStr;

use serde::{Deserialize, Serialize};

use crate::time::{Date, Instant, TimeDelta};

/// A static feed: core state is partitioned by it.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum Feed {
    Jdf,
    Czptt,
}

/// Every feed, in order.
pub const FEEDS: [Feed; 2] = [Feed::Jdf, Feed::Czptt];

impl Feed {
    pub const fn as_str(self) -> &'static str {
        match self {
            Feed::Jdf => "jdf",
            Feed::Czptt => "czptt",
        }
    }
}

impl fmt::Display for Feed {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

impl FromStr for Feed {
    type Err = String;
    fn from_str(text: &str) -> Result<Feed, String> {
        match text {
            "jdf" => Ok(Feed::Jdf),
            "czptt" => Ok(Feed::Czptt),
            other => Err(format!("unknown feed {other:?}")),
        }
    }
}

pub const FACT_SCHEMA_VERSION: i64 = 5;
/// Stored facts of these versions still read. v5 dropped facts no connector produced
/// (stop_event, next_stop).
pub const READABLE_FACT_SCHEMAS: [i64; 2] = [4, 5];

/// A closed interval of instants, ordered by (lo, hi).
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
pub struct Interval {
    pub lo: Instant,
    pub hi: Instant,
}

impl Interval {
    pub const fn new(lo: Instant, hi: Instant) -> Interval {
        Interval { lo, hi }
    }

    /// The middle of the interval.
    pub fn mid(self) -> Instant {
        self.lo + (self.hi - self.lo).div_int(2)
    }
}

/// The archived payload an observation was decoded from, and its position in it.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct RawRef {
    pub sha256: String,
    pub item: i64,
}

#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct VehicleKey {
    #[serde(rename = "id")]
    pub source_vehicle_id: String,
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
pub struct TripKey {
    #[serde(rename = "ns")]
    pub namespace: String,
    pub key: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct Position {
    pub lat: f64,
    pub lon: f64,
    pub bearing: Option<f64>,
}

macro_rules! literal {
    ($(#[$meta:meta])* $name:ident { $($variant:ident = $text:literal),+ $(,)? }) => {
        $(#[$meta])*
        #[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
        pub enum $name {
            $($variant),+
        }

        impl $name {
            pub const ALL: &'static [$name] = &[$($name::$variant),+];

            pub const fn as_str(self) -> &'static str {
                match self {
                    $($name::$variant => $text),+
                }
            }

            pub fn parse(text: &str) -> Option<$name> {
                match text {
                    $($text => Some($name::$variant),)+
                    _ => None,
                }
            }
        }

        impl ::serde::Serialize for $name {
            fn serialize<S: ::serde::Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
                serializer.serialize_str(self.as_str())
            }
        }

        impl<'de> ::serde::Deserialize<'de> for $name {
            fn deserialize<D: ::serde::Deserializer<'de>>(deserializer: D) -> Result<$name, D::Error> {
                let text = <::std::borrow::Cow<'de, str>>::deserialize(deserializer)?;
                $name::parse(&text).ok_or_else(|| {
                    ::serde::de::Error::custom(format!("unknown {} {text:?}", stringify!($name)))
                })
            }
        }

        impl ::std::fmt::Display for $name {
            fn fmt(&self, f: &mut ::std::fmt::Formatter<'_>) -> ::std::fmt::Result {
                f.write_str(self.as_str())
            }
        }
    };
}
pub(crate) use literal;

literal! {
    /// What a delay is measured at; `point`: the last railway point the source reports (SŽ
    /// `de`).
    DelayReference { Arrival = "arrival", Departure = "departure", Unknown = "unknown", Point = "point" }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct Delay {
    #[serde(rename = "s")]
    pub seconds: i64,
    #[serde(rename = "ref")]
    pub reference: DelayReference,
}

/// A source's own trip state code, interpreted by policy per source.
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct SourceState {
    pub code: String,
}

/// How the core reads one channel's facts where sources differ, from its manifest's
/// `[channel.semantics]`. The defaults take every fact at face value.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Default)]
pub struct SourceSemantics {
    /// In a pre-trip state the delay is the time since the scheduled departure (DUK-Q14).
    pub pre_trip_delay_is_elapsed: bool,
    /// A pre-trip key after the trip's scheduled end is left over from an earlier trip
    /// (DUK-Q15).
    pub pre_trip_after_end_is_stale: bool,
    /// A vehicle's entry repeated unchanged is no news: no fix, no event, no freshness (SZ-Q6).
    pub unchanged_entry_is_no_news: bool,
    /// The delay is whole minutes rounded down: `d` means `[d, d + 59 s]` (ARRIVA-Q3, SZ-Q12).
    pub delay_floor_minute: bool,
    /// The channel's `trip_key_feed`: an observation whose trip key this feed's static data
    /// holds goes to this feed instead of the decoder's (IDSJMK-Q1; `Runner::route`).
    pub trip_key_feed: Option<Feed>,
}

impl SourceSemantics {
    /// The manifest keys, in declaration order.
    pub const FIELDS: [&'static str; 4] = [
        "pre_trip_delay_is_elapsed",
        "pre_trip_after_end_is_stale",
        "unchanged_entry_is_no_news",
        "delay_floor_minute",
    ];
}

/// A time or delay a source gives in whole minutes rounded down spans this beyond its value.
pub const FLOORED_MINUTE: TimeDelta = TimeDelta::from_seconds(59);
pub const FLOORED_MINUTE_MIDDLE_S: i64 = 30;

/// The departure the source plans for the run of the trip it reports (DÚK `TODepartureDT`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct SourceDeparture {
    pub at: Instant,
}

literal! {
    EventKind { Arrival = "arrival", Departure = "departure", Passage = "passage" }
}

/// The line a keyless source names (`cis:line`): generates the candidate trips.
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct LineRef {
    #[serde(rename = "ns")]
    pub namespace: String,
    pub value: String,
}

/// The destination stop's name, matched only against a candidate's own last stop. A
/// `townless` name may leave out the stop name's leading parts (MPVNET-Q10).
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct Destination {
    pub name: String,
    #[serde(default, skip_serializing_if = "is_false")]
    pub townless: bool,
}

/// The next stop by name; a change means the vehicle has just left the call before it. With
/// `scheduled`, the timetable's departure there as the source knows it (MPVNET-Q3).
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct NextStopName {
    pub name: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub scheduled: Option<Instant>,
    #[serde(default, skip_serializing_if = "is_false")]
    pub townless: bool,
}

fn is_false(value: &bool) -> bool {
    !*value
}

/// The operating date the source names for the run it reports (SŽ `id`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct ServiceDay {
    pub day: Date,
}

/// The last railway point the source reports the train reached or passed (SŽ `cna`).
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct PointEvent {
    pub name: String,
    pub codes: Vec<String>,
    pub scheduled: Option<Instant>,
    #[serde(flatten)]
    pub actual: Interval,
    pub standing: bool,
}

/// The next railway point (SŽ `nna`, `zst_sr70` without its check digit).
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct NextPoint {
    pub name: String,
    pub sr70: Option<String>,
}

/// The source's own prediction for the next passenger stop (SŽ `nsn70`, `nst`, `nsp`).
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct NextStopPrediction {
    pub sr70: String,
    pub scheduled: Option<Instant>,
    pub predicted: Option<Instant>,
}

/// What a source says about the vehicle itself (BASE_PLAN.md section 27).
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct VehicleAttribute {
    #[serde(rename = "a")]
    pub attribute: String,
    pub value: String,
}

/// SŽ's replacement-bus and diversion flags, part of the entry SZ-Q6 compares.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct TripStatus {
    pub replacement_bus: bool,
    pub diverted: bool,
}

/// The source says the trip its keys name does not run (stated without a vehicle).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct TripCancelled;

literal! {
    PlatformLabel { Platform = "platform", Track = "track" }
}

literal! {
    /// An arrival or a departure (a board row, a platform record).
    StopKind { Arrival = "arrival", Departure = "departure" }
}

/// A station board row (SZT): where the train with this key arrives or departs at the
/// station (SR70, 5 digits) at its scheduled time.
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct PlatformAssignment {
    pub station: String,
    pub kind: StopKind,
    pub scheduled: Instant,
    pub value: String,
    pub label: PlatformLabel,
}

/// The closed union of facts.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "t", rename_all = "snake_case")]
pub enum Fact {
    VehicleKey(VehicleKey),
    TripKey(TripKey),
    LineRef(LineRef),
    Destination(Destination),
    NextStopName(NextStopName),
    Position(Position),
    Delay(Delay),
    SourceState(SourceState),
    SourceDeparture(SourceDeparture),
    ServiceDay(ServiceDay),
    PointEvent(PointEvent),
    NextPoint(NextPoint),
    NextStopPrediction(NextStopPrediction),
    TripStatus(TripStatus),
    TripCancelled(TripCancelled),
    #[serde(rename = "platform")]
    PlatformAssignment(PlatformAssignment),
    VehicleAttribute(VehicleAttribute),
}

/// A fact type: what `Observation.first` and `.all` select by.
pub trait FactKind: Sized {
    fn of(fact: &Fact) -> Option<&Self>;
}

macro_rules! fact_kinds {
    ($($name:ident),+) => {
        $(
            impl FactKind for $name {
                fn of(fact: &Fact) -> Option<&$name> {
                    match fact {
                        Fact::$name(inner) => Some(inner),
                        _ => None,
                    }
                }
            }

            impl From<$name> for Fact {
                fn from(inner: $name) -> Fact {
                    Fact::$name(inner)
                }
            }
        )+
    };
}

fact_kinds!(
    VehicleKey,
    TripKey,
    LineRef,
    Destination,
    NextStopName,
    Position,
    Delay,
    SourceState,
    SourceDeparture,
    ServiceDay,
    PointEvent,
    NextPoint,
    NextStopPrediction,
    TripStatus,
    TripCancelled,
    PlatformAssignment,
    VehicleAttribute
);

/// One decoded entry of a payload.
#[derive(Debug, Clone, PartialEq)]
pub struct Observation {
    pub source: String,
    pub channel: String,
    pub feed: Feed,
    pub received_at: Instant,
    pub observed_at: Option<Instant>,
    pub raw: RawRef,
    pub decoder_version: i64,
    pub facts: Vec<Fact>,
}

impl Observation {
    /// The first fact of a type.
    pub fn first<T: FactKind>(&self) -> Option<&T> {
        self.facts.iter().find_map(T::of)
    }

    /// Every fact of a type, in order.
    pub fn all<'a, T: FactKind + 'a>(&'a self) -> impl Iterator<Item = &'a T> {
        self.facts.iter().filter_map(T::of)
    }

    /// The best time of the observation: the source time, else reception.
    pub fn at(&self) -> Instant {
        self.observed_at.unwrap_or(self.received_at)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn interval_middle() {
        let i = Interval::new(Instant::from_micros(0), Instant::from_micros(6));
        assert_eq!(i.mid(), Instant::from_micros(3));
    }

    #[test]
    fn first_and_all_select_by_type() {
        let obs = Observation {
            source: "duk".into(),
            channel: "vehicles".into(),
            feed: Feed::Jdf,
            received_at: Instant::from_micros(10),
            observed_at: None,
            raw: RawRef {
                sha256: "ab".into(),
                item: 0,
            },
            decoder_version: 1,
            facts: vec![
                TripKey {
                    namespace: "a".into(),
                    key: "1".into(),
                }
                .into(),
                VehicleKey {
                    source_vehicle_id: "v".into(),
                }
                .into(),
                TripKey {
                    namespace: "b".into(),
                    key: "2".into(),
                }
                .into(),
            ],
        };
        assert_eq!(obs.first::<TripKey>().unwrap().namespace, "a");
        assert_eq!(obs.all::<TripKey>().count(), 2);
        assert!(obs.first::<Position>().is_none());
        assert_eq!(obs.at(), Instant::from_micros(10));
    }
}
