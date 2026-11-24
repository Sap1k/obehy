//! PID infotexts from Golemio (docs/sources/pid.md, Alerts): the code decoder `pid-infotexts`,
//! the VYMI texts departure boards show, with a daily window expanded into periods (PID-Q10).
//! PID's GTFS-RT alerts are the generic `gtfs-rt-alerts`.

use obehy_core::alerts::{Alert, AlertEntity, Period, texts};
use obehy_core::time::{Instant, ServiceTime, iso, local_date};

use crate::error::PayloadError;
use crate::reader::json::{self, Value, truthy};

pub const SOURCE: &str = "pid";
pub const INFOTEXTS: &str = "infotexts";
const STOPS: &str = "pid:gtfs_stop_id";
pub const REPEAT_DAYS: i32 = 7;

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
