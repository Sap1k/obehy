//! ČD train compositions (docs/sources/cd.md): the code decoder `cd-composition` of the
//! `cd/composition` lookup.
//!
//! - [`plan`]: a ČD train by its number, service date and the station to answer from, the
//!   station written as ČD writes it (CD-Q1: `54` and the five-digit SR70).
//! - [`decode`]: JSON wrapped in parentheses (CD-Q2); each coach a unit in train order, the
//!   locomotive a placeholder (CD-Q3), CD-Q4, CD-Q5 and CD-Q7 read from the coach.

use std::sync::OnceLock;

use obehy_core::compositions::{
    NotAvailable, Subject, TripView, Unit, UpstreamRequest, start_call,
};
use regex::Regex;

use crate::error::PayloadError;
use crate::reader::json::{self, Value, truthy};

pub const SOURCE: &str = "cd";
pub const COMPOSITION: &str = "composition";
const COUNTRY: &str = "54";

/// ČD's service icons onto the vehicle attribute vocabulary; any other is `raw:cd:<code>`.
const SERVICES: [(&str, &str); 18] = [
    ("wifi", "wifi"),
    ("wifi2", "wifi"),
    ("zas", "power_sockets"),
    ("neginv", "wheelchair_lift"),
    ("povkolo", "bicycle_reservation_required"),
    ("vp", "open_saloon"),
    ("D", "family_compartment"),
    ("kino", "children_cinema"),
    ("inv", "wheelchair_accessible"),
    ("mozkolo", "bicycle_reservation_possible"),
    ("mozzav", "luggage_storage"),
    ("leh", "couchette"),
    ("luz", "sleeper"),
    ("rest", "restaurant"),
    ("kal", "refreshments"),
    ("tichy", "quiet_zone"),
    ("business", "business_compartment"),
    ("damy", "ladies_compartment"),
];

/// The upstream request for a subject, or why there is none.
pub fn plan(subject: &Subject, view: &TripView) -> Result<UpstreamRequest, NotAvailable> {
    let Some(number) = view.keys.get("czptt:train_number") else {
        return Err(NotAvailable::new("the train has no train number"));
    };
    let start = start_call(subject, view)?;
    let Some(sr70) = start.keys.get("sr70") else {
        return Err(NotAvailable::new(format!(
            "{} has no SR70 code",
            start.location_id
        )));
    };
    let station = format!("{COUNTRY}{sr70}");
    let date = view.service_date;
    let params = serde_json::json!([{
        "TrainNum": number,
        "SR70": station,
        "Date": format!("{:02}.{:02}.{:04}", date.day(), date.month(), date.year()),
        "GetPlan": "false",
    }])
    .to_string();
    Ok(UpstreamRequest {
        cache_key: format!("{number}/{}/{station}", date.isoformat()),
        params: vec![("params".into(), params)],
        start,
    })
}

fn document(body: &[u8]) -> Result<Value, PayloadError> {
    let body = body.strip_prefix(b"\xef\xbb\xbf").unwrap_or(body);
    let decoded =
        std::str::from_utf8(body).map_err(|e| PayloadError::new(format!("not UTF-8: {e}")))?;
    let mut t = decoded.trim();
    if let Some(rest) = t.strip_suffix(';') {
        t = rest;
    }
    if t.starts_with('(') && t.ends_with(')') && !t.is_empty() {
        t = &t[1..t.len() - 1]; // CD-Q2
    }
    let document = json::parse(t.as_bytes(), "not JSON")?;
    match document.get("d") {
        Some(inner @ Value::Object(_)) => Ok(inner.clone()),
        _ => Err(PayloadError::new("no 'd' object")),
    }
}

fn series() -> &'static Regex {
    static SERIES: OnceLock<Regex> = OnceLock::new();
    SERIES.get_or_init(|| Regex::new(r"^\d{3}(\.\d+)?$").expect("a valid pattern"))
}

/// CD-Q7: a locomotive's pictogram is `hv_…`; a multiple unit's car has a series as its type.
fn kind(image: &str, vehicle_type: Option<&Value>) -> &'static str {
    if image.to_lowercase().starts_with("hv") {
        return "locomotive";
    }
    if let Some(Value::String(t)) = vehicle_type
        && series().is_match(t)
    {
        return "unit";
    }
    "coach"
}

/// CD-Q7: the driving cab from the pictogram's name.
fn cab(image: &str) -> Option<&'static str> {
    let stem = image.rsplit_once('.').map_or(image, |(s, _)| s);
    let tokens: Vec<&str> = stem.split('_').collect();
    for token in &tokens[1..] {
        match token.to_lowercase().as_str() {
            "l" => return Some("front"),
            "r" => return Some("rear"),
            "lr" => return Some("both"),
            "m" => return None,
            _ => {}
        }
    }
    if tokens[0] == "RV" {
        return Some("true");
    }
    let marked = tokens.iter().any(|t| {
        let chars: Vec<char> = t.chars().collect();
        chars.len() > 1
            && chars[..chars.len() - 1].iter().all(char::is_ascii_digit)
            && matches!(chars[chars.len() - 1], 'l' | 'L')
    });
    marked.then_some("true")
}

/// A present value as text, else empty.
fn str_or_empty(value: Option<&Value>) -> String {
    present(value).unwrap_or_default()
}

/// A value as text if it is present (not null, false, zero or empty).
fn present(value: Option<&Value>) -> Option<String> {
    value.filter(|v| truthy(v)).map(json::text)
}

fn unit(sequence: i64, coach: &Value, texts: &[(String, String)]) -> Unit {
    let img = str_or_empty(coach.get("Img"));
    let image = img
        .rsplit_once('/')
        .map_or(img.as_str(), |(_, i)| i)
        .to_owned();
    let mut raw_type = coach.get("Type").cloned();
    let mut details: Vec<(String, String)> = Vec::new();
    if let Some(Value::String(t)) = &raw_type
        && t.starts_with('#')
    {
        details.push(("type".into(), t.clone())); // CD-Q3: not a type
        raw_type = None;
    }
    let kind = kind(&image, raw_type.as_ref());
    if let Some(c) = cab(&image) {
        details.push(("cab".into(), c.into()));
    }
    let mut classes: Vec<i64> = Vec::new();
    let mut features: Vec<String> = Vec::new();
    let mut codes: Vec<(String, String)> = Vec::new();
    let services = coach
        .get("Services")
        .and_then(Value::as_array)
        .map_or(&[][..], Vec::as_slice);
    for service in services {
        let code = str_or_empty(service.get("Img"));
        if code.is_empty() {
            continue;
        }
        let text = texts
            .iter()
            .rev()
            .find(|(k, _)| *k == code)
            .map(|(_, t)| t.clone())
            .unwrap_or_default();
        codes.push((code.clone(), text));
        let (start, until) = (service.get("StartStation"), service.get("DestStation"));
        if start.is_some_and(truthy) || until.is_some_and(truthy) {
            details.push((
                format!("service:{code}"),
                format!("{}/{}", str_or_empty(start), str_or_empty(until)),
            ));
        }
        match code.as_str() {
            "1" => classes.push(1),
            "2" => classes.push(2),
            "12" => classes.extend([1, 2]),
            _ => {
                let feature = SERVICES
                    .iter()
                    .find(|(c, _)| *c == code)
                    .map_or_else(|| format!("raw:{SOURCE}:{code}"), |(_, f)| (*f).to_owned());
                if !features.contains(&feature) {
                    features.push(feature);
                }
            }
        }
    }
    details.extend(direct_coach(coach.get("DirectCoach")));
    if coach.get("NumStar").is_some_and(truthy) {
        details.push(("number_star".into(), "true".into())); // CD-Q4
    }
    let notes: Vec<String> = coach
        .get("Notes")
        .and_then(Value::as_array)
        .map(|n| n.iter().filter_map(|n| present(Some(n))).collect())
        .unwrap_or_default();
    if !notes.is_empty() {
        details.push(("notes".into(), notes.join(" | ")));
    }
    if let Some(alt) = present(coach.get("ImgAlt")) {
        details.push(("label".into(), alt));
    }
    classes.sort_unstable();
    classes.dedup();
    Unit {
        sequence,
        kind: kind.into(),
        role: "train".into(),
        vehicle_type: present(raw_type.as_ref()),
        coach_number: present(coach.get("Num")),
        classes,
        features,
        texts: codes,
        destination: present(coach.get("DestSt")),
        details,
    }
}

/// A through coach (CD-Q5): where it leaves this train and the train it goes on in.
fn direct_coach(raw: Option<&Value>) -> Vec<(String, String)> {
    let Some(raw) = raw else {
        return Vec::new();
    };
    if !raw.is_object() {
        return present(Some(raw))
            .map(|text| vec![("direct_coach".into(), text)])
            .unwrap_or_default();
    }
    let mut found = Vec::new();
    if let Some(at) = present(raw.get("OdpSt")) {
        found.push(("direct_coach_leaves_at".into(), at));
    }
    if let Some(train @ Value::Object(_)) = raw.get("NaslVl") {
        let parts: Vec<String> = ["TrType", "TrNum", "TrName"]
            .iter()
            .filter_map(|k| present(train.get(k)))
            .collect();
        found.push(("direct_coach_train".into(), parts.join(" ")));
    }
    found
}

/// The units in train order; none when ČD does not know the train then.
pub fn decode(body: &[u8], _request: &UpstreamRequest) -> Result<Vec<Unit>, PayloadError> {
    let document = document(body)?;
    let texts: Vec<(String, String)> = document
        .get("SvcDescEnum")
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .filter_map(|item| {
                    Some((json::text(item.get("Img")?), str_or_empty(item.get("Text"))))
                })
                .collect()
        })
        .unwrap_or_default();
    let Some(first) = document
        .get("Trains")
        .and_then(Value::as_array)
        .and_then(|t| t.first())
    else {
        return Ok(Vec::new());
    };
    let coaches = first
        .get("Coachs")
        .and_then(Value::as_array)
        .map_or(&[][..], Vec::as_slice);
    Ok(coaches
        .iter()
        .enumerate()
        .map(|(i, coach)| unit(i as i64 + 1, coach, &texts))
        .collect())
}
