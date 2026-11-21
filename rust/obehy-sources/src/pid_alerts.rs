//! PID alerts from Golemio (docs/sources/pid.md, Alerts): port of
//! `realtime/sources/pid_alerts.py`, the code decoders `pid-alerts` (the VYMI events as a
//! GTFS-RT alerts dataset) and `pid-infotexts` (the infotexts departure boards show, with a
//! daily window expanded into periods, PID-Q10).

use obehy_core::alerts::{Alert, AlertEntity, Period, Texts, texts};
use obehy_core::time::{Date, Instant, ServiceTime, iso, local_date};

use crate::error::PayloadError;
use crate::reader::gtfs_rt::{self, cause_name, effect_name, severity_name};
use crate::reader::json::{self, Json};

pub const SOURCE: &str = "pid";
pub const ALERTS: &str = "alerts";
pub const INFOTEXTS: &str = "infotexts";
const ROUTES: &str = "pid:gtfs_route_id";
const STOPS: &str = "pid:gtfs_stop_id";
const TRIPS: &str = "pid:gtfs_trip_id";
pub const REPEAT_DAYS: i32 = 7;

fn translated(value: Option<&gtfs_rt::TranslatedString>) -> Texts {
    let mut found: Texts = value
        .map(|v| v.translation.as_slice())
        .unwrap_or_default()
        .iter()
        .filter_map(|t| {
            let text = t.text.clone().unwrap_or_default();
            if text.is_empty() {
                return None;
            }
            let lang = t
                .language
                .clone()
                .filter(|l| !l.is_empty())
                .unwrap_or_else(|| "cs".into());
            Some((lang, text))
        })
        .collect();
    found.sort();
    found
}

/// `datetime.fromtimestamp(epoch, UTC)`.
fn at(epoch: u64) -> Result<Instant, PayloadError> {
    let seconds = i64::try_from(epoch)
        .ok()
        .filter(|s| *s < 253_402_300_800)
        .ok_or_else(|| PayloadError::value(format!("year is out of range ({epoch})")))?;
    Ok(Instant::from_unix_seconds(seconds))
}

fn day(start: &str) -> Result<Date, PayloadError> {
    let c: Vec<char> = start.chars().collect();
    let part = |a: usize, b: usize| -> Result<i32, PayloadError> {
        let s: String = c
            .get(a..b.min(c.len()))
            .unwrap_or_default()
            .iter()
            .collect();
        obehy_core::py::text::int_from_str(&s)
            .and_then(|n| n.small())
            .and_then(|n| i32::try_from(n).ok())
            .ok_or_else(|| PayloadError::int_literal(&s))
    };
    let (y, m, d) = (part(0, 4)?, part(4, 6)?, part(6, c.len())?);
    let ok = |n: i32| u32::try_from(n).ok();
    ok(m)
        .zip(ok(d))
        .and_then(|(m, d)| Date::new(y, m, d))
        .ok_or_else(|| PayloadError::value("day is out of range for month"))
}

/// Entities deduplicated and sorted by `(kind, identifier, service_date or "")`; a trip named
/// both with and without a date cannot be compared in Python (TypeError).
fn sorted_entities(found: Vec<AlertEntity>) -> Result<Vec<AlertEntity>, PayloadError> {
    let mut unique: Vec<AlertEntity> = Vec::new();
    for e in found {
        if !unique.contains(&e) {
            unique.push(e);
        }
    }
    for a in &unique {
        for b in &unique {
            if a.kind == b.kind
                && a.identifier == b.identifier
                && a.service_date.is_some() != b.service_date.is_some()
            {
                return Err(PayloadError {
                    class: "TypeError",
                    message: "'<' not supported between instances of 'datetime.date' and 'str'"
                        .into(),
                });
            }
        }
    }
    unique.sort_by(|a, b| {
        (a.kind.as_str(), a.identifier.as_str(), a.service_date).cmp(&(
            b.kind.as_str(),
            b.identifier.as_str(),
            b.service_date,
        ))
    });
    Ok(unique)
}

/// A GTFS-RT alerts dataset; entities without an alert, and deleted ones, are skipped.
pub fn decode_alerts(body: &[u8], _received_at: Instant) -> Result<Vec<Alert>, PayloadError> {
    let message = gtfs_rt::parse(body)
        .map_err(|_| {
            // upb's text for any malformed message.
            PayloadError::payload(
                "not a GTFS-RT message: Error parsing message with type 'transit_realtime.FeedMessage': Wire format was corrupt",
            )
        })?;
    let mut alerts = Vec::new();
    for entity in &message.entity {
        let Some(a) = &entity.alert else {
            continue;
        };
        if entity.is_deleted.unwrap_or(false) {
            continue;
        }
        let mut entities = Vec::new();
        for informed in &a.informed_entity {
            let trip_id = informed
                .trip
                .as_ref()
                .and_then(|t| t.trip_id.clone())
                .unwrap_or_default();
            if informed.trip.is_some() && !trip_id.is_empty() {
                let start = informed
                    .trip
                    .as_ref()
                    .and_then(|t| t.start_date.clone())
                    .unwrap_or_default();
                let service_date = if start.is_empty() {
                    None
                } else {
                    Some(day(&start)?)
                };
                entities.push(AlertEntity {
                    kind: "trip".into(),
                    namespace: TRIPS.into(),
                    identifier: trip_id,
                    service_date,
                });
            } else if let Some(stop) = informed.stop_id.clone().filter(|s| !s.is_empty()) {
                entities.push(AlertEntity {
                    kind: "stop".into(),
                    namespace: STOPS.into(),
                    identifier: stop,
                    service_date: None,
                });
            } else if let Some(route) = informed.route_id.clone().filter(|s| !s.is_empty()) {
                entities.push(AlertEntity {
                    kind: "route".into(),
                    namespace: ROUTES.into(),
                    identifier: route,
                    service_date: None,
                });
            }
        }
        let periods = a
            .active_period
            .iter()
            .map(|p| {
                Ok(Period {
                    start: p.start.map(at).transpose()?,
                    end: p.end.map(at).transpose()?,
                })
            })
            .collect::<Result<_, PayloadError>>()?;
        alerts.push(Alert {
            source: SOURCE.into(),
            channel: ALERTS.into(),
            alert_id: entity.id.clone().unwrap_or_default(),
            cause: cause_name(a.cause).into(),
            effect: effect_name(a.effect).into(),
            severity: severity_name(a.severity_level).into(),
            periods,
            header: translated(a.header_text.as_ref()),
            description: translated(a.description_text.as_ref()),
            url: translated(a.url.as_ref()),
            cause_detail: translated(a.cause_detail.as_ref()),
            effect_detail: translated(a.effect_detail.as_ref()),
            entities: sorted_entities(entities)?,
            details: Vec::new(),
        });
    }
    alerts.sort_by(|a, b| a.alert_id.cmp(&b.alert_id));
    Ok(alerts)
}

/// An infotext's time: aware ISO or none; anything else that is no ISO time is an error.
fn time(value: Option<&Json>) -> Result<Option<Instant>, PayloadError> {
    let Some(text) = value.and_then(Json::as_str).filter(|t| !t.is_empty()) else {
        return Ok(None);
    };
    let parsed = iso::parse_datetime(text).map_err(|e| PayloadError::value(e.0))?;
    if parsed.offset_us.is_none() {
        return Ok(None);
    }
    parsed
        .instant()
        .map(Some)
        .map_err(|e| PayloadError::value(e.0))
}

/// `HH:MM:SS` as seconds; anything else is Python's ValueError.
fn seconds(clock: &str) -> Result<i32, PayloadError> {
    let parts: Vec<&str> = clock.split(':').collect();
    if parts.len() != 3 {
        let message = if parts.len() < 3 {
            format!(
                "not enough values to unpack (expected 3, got {})",
                parts.len()
            )
        } else {
            "too many values to unpack (expected 3)".to_owned()
        };
        return Err(PayloadError::value(message));
    }
    let mut total: i64 = 0;
    for (part, scale) in parts.iter().zip([3600, 60, 1]) {
        let n = obehy_core::py::text::int_from_str(part)
            .ok_or_else(|| PayloadError::int_literal(part))?
            .small()
            .and_then(|n| i64::try_from(n).ok())
            .ok_or_else(|| PayloadError::value("seconds out of range"))?;
        total += n * scale;
    }
    i32::try_from(total).map_err(|_| PayloadError::value("seconds out of range"))
}

fn periods(
    start: Option<Instant>,
    end: Option<Instant>,
    repeat: Option<&Json>,
    received_at: Instant,
) -> Result<Vec<Period>, PayloadError> {
    let begin = repeat
        .and_then(|r| r.get("time_start"))
        .and_then(Json::as_str);
    let finish = repeat
        .and_then(|r| r.get("time_end"))
        .and_then(Json::as_str);
    let (Some(begin), Some(finish)) = (begin, finish) else {
        return Ok(vec![Period { start, end }]);
    };
    let lo = seconds(begin)?;
    let mut hi = seconds(finish)?;
    if hi <= lo {
        hi += 24 * 3600;
    }
    let first = local_date(received_at).add_days(-1);
    let mut found = Vec::new();
    for n in 0..=REPEAT_DAYS {
        let day = first.add_days(n);
        let (ws, we) = (
            ServiceTime::new(day, lo).instant(),
            ServiceTime::new(day, hi).instant(),
        );
        if end.is_some_and(|e| ws >= e) || start.is_some_and(|s| we <= s) {
            continue;
        }
        found.push(Period {
            start: Some(start.map_or(ws, |s| if s > ws { s } else { ws })),
            end: Some(end.map_or(we, |e| if e < we { e } else { we })),
        });
    }
    Ok(found)
}

/// The infotexts list; entries without an id or a text are skipped.
pub fn decode_infotexts(body: &[u8], received_at: Instant) -> Result<Vec<Alert>, PayloadError> {
    let document = json::loads(body).map_err(|e| match e {
        json::JsonError::Value(m) => PayloadError::value(m),
        other => PayloadError::payload(format!("not JSON: {}", other.message())),
    })?;
    let Json::Array(items) = document else {
        return Err(PayloadError::payload("infotexts are not a list"));
    };
    let mut alerts = Vec::new();
    for item in &items {
        if !matches!(item, Json::Object(_)) {
            continue;
        }
        let (Some(Json::Str(identifier)), Some(Json::Str(text))) =
            (item.get("id"), item.get("text"))
        else {
            continue;
        };
        if text.is_empty() {
            continue;
        }
        let stops: &[Json] = match item.get("related_stops") {
            Some(Json::Array(s)) => s,
            _ => &[],
        };
        let repeat = item.get("repeat").filter(|r| r.truthy());
        let mut entities: Vec<AlertEntity> = Vec::new();
        for stop in stops {
            if let Some(Json::Str(id)) = stop.get("id") {
                let entity = AlertEntity {
                    kind: "stop".into(),
                    namespace: STOPS.into(),
                    identifier: id.clone(),
                    service_date: None,
                };
                if !entities.contains(&entity) {
                    entities.push(entity);
                }
            }
        }
        entities.sort_by(|a, b| a.identifier.cmp(&b.identifier));
        let severity = match item.get("priority").map(Json::py_str).as_deref() {
            Some("low") => "INFO",
            Some("normal") => "WARNING",
            Some("high") => "SEVERE",
            _ => "UNKNOWN_SEVERITY",
        };
        let text_en = item.get("text_en").and_then(Json::as_str);
        alerts.push(Alert {
            source: SOURCE.into(),
            channel: INFOTEXTS.into(),
            alert_id: format!("infotext-{identifier}"),
            cause: "UNKNOWN_CAUSE".into(),
            effect: "UNKNOWN_EFFECT".into(),
            severity: severity.into(),
            periods: periods(
                time(item.get("valid_from"))?,
                time(item.get("valid_to"))?,
                repeat,
                received_at,
            )?,
            header: texts(&[("cs", Some(text.as_str())), ("en", text_en)]),
            description: Vec::new(),
            url: Vec::new(),
            cause_detail: Vec::new(),
            effect_detail: Vec::new(),
            entities,
            details: match item.get("display_type") {
                Some(Json::Str(d)) => vec![("display_type".into(), d.clone())],
                _ => Vec::new(),
            },
        });
    }
    alerts.sort_by(|a, b| a.alert_id.cmp(&b.alert_id));
    Ok(alerts)
}
