//! PID alerts from Golemio (docs/sources/pid.md, Alerts): the code decoders `pid-alerts` (the
//! VYMI events as a
//! GTFS-RT alerts dataset) and `pid-infotexts` (the infotexts departure boards show, with a
//! daily window expanded into periods, PID-Q10).

use obehy_core::alerts::{Alert, AlertEntity, Period, Texts, texts};
use obehy_core::time::{Date, Instant, ServiceTime, iso, local_date};

use crate::error::PayloadError;
use crate::reader::gtfs_rt::{self, cause_name, effect_name, severity_name};
use crate::reader::json::{self, Value, truthy};

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

/// Seconds since the epoch.
fn at(epoch: u64) -> Result<Instant, PayloadError> {
    i64::try_from(epoch)
        .ok()
        .filter(|s| *s < 253_402_300_800)
        .map(Instant::from_unix_seconds)
        .ok_or_else(|| PayloadError::new(format!("time out of range ({epoch})")))
}

/// `YYYYMMDD`.
fn day(start: &str) -> Result<Date, PayloadError> {
    let bad = || PayloadError::new(format!("bad start date {start:?}"));
    if start.len() != 8 || !start.bytes().all(|b| b.is_ascii_digit()) {
        return Err(bad());
    }
    let part = |range: std::ops::Range<usize>| start[range].parse().map_err(|_| bad());
    Date::new(part(0..4)? as i32, part(4..6)?, part(6..8)?).ok_or_else(bad)
}

/// Entities deduplicated and sorted by `(kind, identifier, service_date)`.
fn sorted_entities(mut found: Vec<AlertEntity>) -> Result<Vec<AlertEntity>, PayloadError> {
    found.sort_by(|a, b| {
        (&a.kind, &a.identifier, a.service_date).cmp(&(&b.kind, &b.identifier, b.service_date))
    });
    found.dedup();
    Ok(found)
}

/// A GTFS-RT alerts dataset; entities without an alert, and deleted ones, are skipped.
pub fn decode_alerts(body: &[u8], _received_at: Instant) -> Result<Vec<Alert>, PayloadError> {
    let message = gtfs_rt::parse(body).map_err(|_| PayloadError::new("not a GTFS-RT message"))?;
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
fn time(value: Option<&Value>) -> Result<Option<Instant>, PayloadError> {
    let Some(text) = value.and_then(Value::as_str).filter(|t| !t.is_empty()) else {
        return Ok(None);
    };
    let parsed = iso::parse_datetime(text).map_err(|e| PayloadError::new(e.0))?;
    if parsed.offset_us.is_none() {
        return Ok(None);
    }
    parsed
        .instant()
        .map(Some)
        .map_err(|e| PayloadError::new(e.0))
}

/// `HH:MM:SS` as seconds.
fn seconds(clock: &str) -> Result<i32, PayloadError> {
    let bad = || PayloadError::new(format!("bad clock time {clock:?}"));
    let parts: Vec<i32> = clock
        .split(':')
        .map(|p| p.trim().parse().map_err(|_| bad()))
        .collect::<Result<_, _>>()?;
    match parts[..] {
        [h, m, s] => Ok(h * 3600 + m * 60 + s),
        _ => Err(bad()),
    }
}

fn periods(
    start: Option<Instant>,
    end: Option<Instant>,
    repeat: Option<&Value>,
    received_at: Instant,
) -> Result<Vec<Period>, PayloadError> {
    let begin = repeat
        .and_then(|r| r.get("time_start"))
        .and_then(Value::as_str);
    let finish = repeat
        .and_then(|r| r.get("time_end"))
        .and_then(Value::as_str);
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
            start: Some(start.map_or(ws, |s| s.max(ws))),
            end: Some(end.map_or(we, |e| e.min(we))),
        });
    }
    Ok(found)
}

/// The infotexts list; entries without an id or a text are skipped.
pub fn decode_infotexts(body: &[u8], received_at: Instant) -> Result<Vec<Alert>, PayloadError> {
    let Value::Array(items) = json::parse(body, "not JSON")? else {
        return Err(PayloadError::new("infotexts are not a list"));
    };
    let mut alerts = Vec::new();
    for item in &items {
        let (Some(Value::String(identifier)), Some(Value::String(text))) =
            (item.get("id"), item.get("text"))
        else {
            continue;
        };
        if text.is_empty() {
            continue;
        }
        let stops = item
            .get("related_stops")
            .and_then(Value::as_array)
            .map_or(&[][..], Vec::as_slice);
        let repeat = item.get("repeat").filter(|r| truthy(r));
        let mut entities: Vec<AlertEntity> = Vec::new();
        for stop in stops {
            if let Some(Value::String(id)) = stop.get("id") {
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
        let severity = match item.get("priority").map(json::text).as_deref() {
            Some("low") => "INFO",
            Some("normal") => "WARNING",
            Some("high") => "SEVERE",
            _ => "UNKNOWN_SEVERITY",
        };
        let text_en = item.get("text_en").and_then(Value::as_str);
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
                Some(Value::String(d)) => vec![("display_type".into(), d.clone())],
                _ => Vec::new(),
            },
        });
    }
    alerts.sort_by(|a, b| a.alert_id.cmp(&b.alert_id));
    Ok(alerts)
}
