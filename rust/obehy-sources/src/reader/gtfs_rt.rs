//! GTFS-RT service alerts as protobuf (`gtfs-realtime.proto`, proto2): only the messages and
//! fields the alert decoder reads. Fields keep proto2 presence (`Option`), enums stay `i32`
//! (an unknown value reads as the field's default, as Python's protobuf does for a closed
//! enum), required fields are not enforced (Python's `ParseFromString` does not either).

use prost::Message;

#[derive(Clone, PartialEq, Message)]
pub struct FeedMessage {
    #[prost(message, repeated, tag = "2")]
    pub entity: Vec<FeedEntity>,
}

#[derive(Clone, PartialEq, Message)]
pub struct FeedEntity {
    #[prost(string, optional, tag = "1")]
    pub id: Option<String>,
    #[prost(bool, optional, tag = "2")]
    pub is_deleted: Option<bool>,
    #[prost(message, optional, tag = "5")]
    pub alert: Option<Alert>,
}

#[derive(Clone, PartialEq, Message)]
pub struct Alert {
    #[prost(message, repeated, tag = "1")]
    pub active_period: Vec<TimeRange>,
    #[prost(message, repeated, tag = "5")]
    pub informed_entity: Vec<EntitySelector>,
    #[prost(int32, optional, tag = "6")]
    pub cause: Option<i32>,
    #[prost(int32, optional, tag = "7")]
    pub effect: Option<i32>,
    #[prost(message, optional, tag = "8")]
    pub url: Option<TranslatedString>,
    #[prost(message, optional, tag = "10")]
    pub header_text: Option<TranslatedString>,
    #[prost(message, optional, tag = "11")]
    pub description_text: Option<TranslatedString>,
    #[prost(int32, optional, tag = "14")]
    pub severity_level: Option<i32>,
    #[prost(message, optional, tag = "17")]
    pub cause_detail: Option<TranslatedString>,
    #[prost(message, optional, tag = "18")]
    pub effect_detail: Option<TranslatedString>,
}

#[derive(Clone, PartialEq, Message)]
pub struct TimeRange {
    #[prost(uint64, optional, tag = "1")]
    pub start: Option<u64>,
    #[prost(uint64, optional, tag = "2")]
    pub end: Option<u64>,
}

#[derive(Clone, PartialEq, Message)]
pub struct EntitySelector {
    #[prost(string, optional, tag = "2")]
    pub route_id: Option<String>,
    #[prost(message, optional, tag = "4")]
    pub trip: Option<TripDescriptor>,
    #[prost(string, optional, tag = "5")]
    pub stop_id: Option<String>,
}

#[derive(Clone, PartialEq, Message)]
pub struct TripDescriptor {
    #[prost(string, optional, tag = "1")]
    pub trip_id: Option<String>,
    #[prost(string, optional, tag = "3")]
    pub start_date: Option<String>,
}

#[derive(Clone, PartialEq, Message)]
pub struct TranslatedString {
    #[prost(message, repeated, tag = "1")]
    pub translation: Vec<Translation>,
}

#[derive(Clone, PartialEq, Message)]
pub struct Translation {
    #[prost(string, optional, tag = "1")]
    pub text: Option<String>,
    #[prost(string, optional, tag = "2")]
    pub language: Option<String>,
}

/// `Alert.Cause` names by number; anything else reads as the default, `UNKNOWN_CAUSE`.
pub fn cause_name(value: Option<i32>) -> &'static str {
    const NAMES: [&str; 13] = [
        "UNKNOWN_CAUSE",
        "OTHER_CAUSE",
        "TECHNICAL_PROBLEM",
        "STRIKE",
        "DEMONSTRATION",
        "ACCIDENT",
        "HOLIDAY",
        "WEATHER",
        "MAINTENANCE",
        "CONSTRUCTION",
        "POLICE_ACTIVITY",
        "MEDICAL_EMERGENCY",
        "SPECIAL_EVENT",
    ];
    name(&NAMES, value, 1)
}

/// `Alert.Effect` names; default `UNKNOWN_EFFECT` (8).
pub fn effect_name(value: Option<i32>) -> &'static str {
    const NAMES: [&str; 11] = [
        "NO_SERVICE",
        "REDUCED_SERVICE",
        "SIGNIFICANT_DELAYS",
        "DETOUR",
        "ADDITIONAL_SERVICE",
        "MODIFIED_SERVICE",
        "OTHER_EFFECT",
        "UNKNOWN_EFFECT",
        "STOP_MOVED",
        "NO_EFFECT",
        "ACCESSIBILITY_ISSUE",
    ];
    name(&NAMES, value, 8)
}

/// `Alert.SeverityLevel` names; default `UNKNOWN_SEVERITY` (1).
pub fn severity_name(value: Option<i32>) -> &'static str {
    const NAMES: [&str; 4] = ["UNKNOWN_SEVERITY", "INFO", "WARNING", "SEVERE"];
    name(&NAMES, value, 1)
}

/// Names numbered from 1.
fn name(names: &[&'static str], value: Option<i32>, default: i32) -> &'static str {
    let known = |v: i32| {
        usize::try_from(v - 1)
            .ok()
            .and_then(|i| names.get(i).copied())
    };
    value
        .and_then(known)
        .unwrap_or_else(|| known(default).expect("a known default"))
}

/// Parses a FeedMessage.
pub fn parse(body: &[u8]) -> Result<FeedMessage, prost::DecodeError> {
    FeedMessage::decode(body)
}
