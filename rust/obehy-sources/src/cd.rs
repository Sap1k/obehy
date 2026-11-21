//! ČD train compositions (docs/sources/cd.md): port of `realtime/sources/cd.py`, the code
//! decoder `cd-composition` of the `cd/composition` lookup.
//!
//! - [`plan`]: a ČD train by its number, service date and the station to answer from, the
//!   station written as ČD writes it (CD-Q1: `54` and the five-digit SR70).
//! - [`decode`]: JSON wrapped in parentheses (CD-Q2); each coach a unit in train order, the
//!   locomotive a placeholder (CD-Q3), CD-Q4, CD-Q5 and CD-Q7 read from the coach.

use std::sync::OnceLock;

use obehy_core::compositions::{
    NotAvailable, Subject, TripView, Unit, UpstreamRequest, start_call,
};
use obehy_core::py::text;
use regex::Regex;

use crate::error::PayloadError;
use crate::reader::json::{self, Json, JsonError, write_string};

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
    // json.dumps([train], separators=(",", ":")), ensure_ascii on.
    let mut params = String::from("[{");
    let fields = [
        ("TrainNum", number.clone()),
        ("SR70", station.clone()),
        (
            "Date",
            format!("{:02}.{:02}.{:04}", date.day(), date.month(), date.year()),
        ),
        ("GetPlan", "false".to_owned()),
    ];
    for (i, (k, v)) in fields.iter().enumerate() {
        if i > 0 {
            params.push(',');
        }
        write_string(&mut params, k, true);
        params.push(':');
        write_string(&mut params, v, true);
    }
    params.push_str("}]");
    Ok(UpstreamRequest {
        cache_key: format!("{number}/{}/{station}", date.isoformat()),
        params: vec![("params".into(), params)],
        start,
    })
}

fn document(body: &[u8]) -> Result<Json, PayloadError> {
    let body = body.strip_prefix(b"\xef\xbb\xbf").unwrap_or(body);
    let decoded = std::str::from_utf8(body).map_err(|e| PayloadError {
        class: "UnicodeDecodeError",
        message: json::utf8_error_text(body, &e, "utf-8"),
    })?;
    let mut t = text::strip(decoded);
    if let Some(rest) = t.strip_suffix(';') {
        t = rest;
    }
    if t.starts_with('(') && t.ends_with(')') && !t.is_empty() {
        t = &t[1..t.len() - 1]; // CD-Q2
    }
    let document = json::loads_str(t).map_err(|e| match e {
        JsonError::Value(m) => PayloadError::value(m),
        other => PayloadError::payload(format!("not JSON: {}", other.message())),
    })?;
    match document.get("d") {
        Some(inner @ Json::Object(_)) => Ok(inner.clone()),
        _ => Err(PayloadError::payload("no 'd' object")),
    }
}

fn series() -> &'static Regex {
    static SERIES: OnceLock<Regex> = OnceLock::new();
    SERIES.get_or_init(|| crate::mapping::py_regex(r"^\d{3}(\.\d+)?$").expect("a valid pattern"))
}

/// CD-Q7: a locomotive's pictogram is `hv_…`; a multiple unit's car has a series as its type.
fn kind(image: &str, vehicle_type: Option<&Json>) -> &'static str {
    if image.to_lowercase().starts_with("hv") {
        return "locomotive";
    }
    if let Some(Json::Str(t)) = vehicle_type
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
            && text::isdigit(&chars[..chars.len() - 1].iter().collect::<String>())
            && matches!(chars[chars.len() - 1], 'l' | 'L')
    });
    marked.then_some("true")
}

/// `str(x or "")` of a JSON value.
fn str_or_empty(value: Option<&Json>) -> String {
    value
        .filter(|v| v.truthy())
        .map(Json::py_str)
        .unwrap_or_default()
}

fn unit(sequence: i64, coach: &Json, texts: &[(String, String)]) -> Unit {
    let img = str_or_empty(coach.get("Img"));
    let image = img
        .rsplit_once('/')
        .map_or(img.as_str(), |(_, i)| i)
        .to_owned();
    let mut raw_type = coach.get("Type").cloned();
    let mut details: Vec<(String, String)> = Vec::new();
    if let Some(Json::Str(t)) = &raw_type
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
    let services: &[Json] = match coach.get("Services") {
        Some(Json::Array(s)) => s,
        _ => &[],
    };
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
        if start.is_some_and(Json::truthy) || until.is_some_and(Json::truthy) {
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
    if coach.get("NumStar").is_some_and(Json::truthy) {
        details.push(("number_star".into(), "true".into())); // CD-Q4
    }
    let notes: Vec<String> = match coach.get("Notes") {
        Some(Json::Array(n)) => n.iter().filter(|n| n.truthy()).map(Json::py_str).collect(),
        _ => Vec::new(),
    };
    if !notes.is_empty() {
        details.push(("notes".into(), notes.join(" | ")));
    }
    if let Some(alt) = coach.get("ImgAlt").filter(|v| v.truthy()) {
        details.push(("label".into(), alt.py_str()));
    }
    classes.sort_unstable();
    classes.dedup();
    Unit {
        sequence,
        kind: kind.into(),
        role: "train".into(),
        vehicle_type: raw_type.filter(Json::truthy).map(|t| t.py_str()),
        coach_number: coach.get("Num").filter(|v| v.truthy()).map(Json::py_str),
        classes,
        features,
        texts: codes,
        destination: coach.get("DestSt").filter(|v| v.truthy()).map(Json::py_str),
        details,
    }
}

/// A through coach (CD-Q5): where it leaves this train and the train it goes on in.
fn direct_coach(raw: Option<&Json>) -> Vec<(String, String)> {
    let Some(raw) = raw else {
        return Vec::new();
    };
    if !matches!(raw, Json::Object(_)) {
        return if raw.truthy() {
            vec![("direct_coach".into(), raw.py_str())]
        } else {
            Vec::new()
        };
    }
    let mut found = Vec::new();
    if let Some(at) = raw.get("OdpSt").filter(|v| v.truthy()) {
        found.push(("direct_coach_leaves_at".into(), at.py_str()));
    }
    if let Some(train @ Json::Object(_)) = raw.get("NaslVl") {
        let parts: Vec<String> = ["TrType", "TrNum", "TrName"]
            .iter()
            .filter_map(|k| train.get(k).filter(|v| v.truthy()).map(Json::py_str))
            .collect();
        found.push(("direct_coach_train".into(), parts.join(" ")));
    }
    found
}

/// The units in train order; none when ČD does not know the train then.
pub fn decode(body: &[u8], _request: &UpstreamRequest) -> Result<Vec<Unit>, PayloadError> {
    let document = document(body)?;
    let texts: Vec<(String, String)> = match document.get("SvcDescEnum") {
        Some(Json::Array(items)) => items
            .iter()
            .map(|item| {
                (
                    item.get("Img").map_or_else(|| "None".into(), Json::py_str),
                    str_or_empty(item.get("Text")),
                )
            })
            .collect(),
        _ => Vec::new(),
    };
    let trains: &[Json] = match document.get("Trains") {
        Some(Json::Array(t)) => t,
        _ => &[],
    };
    let Some(first) = trains.first() else {
        return Ok(Vec::new());
    };
    let coaches: &[Json] = match first.get("Coachs") {
        Some(Json::Array(c)) => c,
        _ => &[],
    };
    Ok(coaches
        .iter()
        .enumerate()
        .map(|(i, coach)| unit(i as i64 + 1, coach, &texts))
        .collect())
}
