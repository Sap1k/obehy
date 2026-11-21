//! Where realtime rows are stored. `rt.observation` is partitioned per UTC hour
//! (`observation_YYYYMMDDHH`) and every history table per service date (`<table>_YYYYMMDD`), so
//! retention drops whole partitions (`obehy jobs`). Older servers kept daily
//! observation partitions (`observation_YYYYMMDD`); they are recognized until dropped.
//! `history.term` keeps the strings history refers to by number.

use std::collections::HashMap;

use obehy_core::time::{Date, Instant};
use tokio_postgres::Client;
use tokio_postgres::error::SqlState;

use super::StoreResult;

pub const HISTORY_TABLES: [&str; 8] = [
    "journey",
    "journey_schedule",
    "journey_call",
    "actual_stop_event",
    "vehicle_assignment",
    "journey_link",
    "platform_evidence",
    "journey_cancellation",
];

const HOUR_US: i64 = 3_600_000_000;

/// The UTC hour an instant falls in, which names its observation partition.
pub fn hour_of(at: Instant) -> Instant {
    Instant::from_micros(at.micros().div_euclid(HOUR_US) * HOUR_US)
}

fn compact_date(day: Date) -> String {
    day.isoformat().replace('-', "")
}

pub fn observation_name(hour: Instant) -> String {
    let (h, _, _, _) = hour.utc().hms_micro();
    format!("observation_{}{h:02}", compact_date(hour.utc().date()))
}

fn parse_day(digits: &str) -> Option<Date> {
    if digits.len() != 8 || !digits.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    let text = format!("{}-{}-{}", &digits[..4], &digits[4..6], &digits[6..]);
    Date::from_isoformat(&text).ok()
}

/// When an observation partition's range ends (hourly or the older daily), if it is one.
pub fn observation_end(name: &str) -> Option<Instant> {
    let rest = name.strip_prefix("observation_")?;
    let day = parse_day(rest.get(..8)?)?;
    let start = Instant::from_micros(i64::from(day.days_since_epoch()) * 24 * HOUR_US);
    match rest.get(8..)? {
        "" => Some(Instant::from_micros(start.micros() + 24 * HOUR_US)),
        hour if hour.len() == 2 && hour.bytes().all(|b| b.is_ascii_digit()) => {
            let h: i64 = hour.parse().ok()?;
            Some(Instant::from_micros(start.micros() + (h + 1) * HOUR_US))
        }
        _ => None,
    }
}

/// The service date of a daily history partition, if `name` is one.
pub fn history_day(name: &str) -> Option<Date> {
    let (table, day) = name.rsplit_once('_')?;
    if !HISTORY_TABLES.contains(&table) {
        return None;
    }
    parse_day(day)
}

fn timestamp_literal(at: Instant) -> String {
    format!("'{}'::timestamptz", at.isoformat())
}

/// The partition of one UTC hour. A daily partition of an older server already covers the
/// hours of its day; the overlap is the sign.
pub async fn create_observation_partition(client: &Client, hour: Instant) -> StoreResult<()> {
    let end = Instant::from_micros(hour.micros() + HOUR_US);
    let statement = format!(
        "CREATE TABLE IF NOT EXISTS rt.\"{}\" PARTITION OF rt.observation \
         FOR VALUES FROM ({}) TO ({})",
        observation_name(hour),
        timestamp_literal(hour),
        timestamp_literal(end),
    );
    match client.batch_execute(&statement).await {
        Err(error) if error.code() == Some(&SqlState::INVALID_OBJECT_DEFINITION) => Ok(()),
        other => Ok(other?),
    }
}

/// Every history table's partition of one service date.
pub async fn create_history_partitions(client: &Client, day: Date) -> StoreResult<()> {
    let next = Date::from_days(day.days_since_epoch() + 1);
    let mut statements = String::new();
    for table in HISTORY_TABLES {
        statements.push_str(&format!(
            "CREATE TABLE IF NOT EXISTS history.\"{table}_{}\" PARTITION OF history.\"{table}\" \
             FOR VALUES FROM ('{}'::date) TO ('{}'::date);",
            compact_date(day),
            day.isoformat(),
            next.isoformat(),
        ));
    }
    Ok(client.batch_execute(&statements).await?)
}

/// `history.term`: each string stored once and referred to by number. Missing ones are added
/// outside the writer's transaction, so a rolled-back write never leaves a number known here
/// but not stored.
#[derive(Debug, Clone, Default)]
pub struct Terms {
    pub ids: HashMap<String, i32>,
}

impl Terms {
    pub async fn load(client: &Client) -> StoreResult<Terms> {
        let rows = client
            .query("SELECT value, term_id FROM history.term", &[])
            .await?;
        let mut ids = HashMap::with_capacity(rows.len());
        for row in rows {
            ids.insert(row.try_get(0)?, row.try_get(1)?);
        }
        Ok(Terms { ids })
    }

    pub async fn add(&mut self, client: &Client, values: &[String]) -> StoreResult<()> {
        let mut missing: Vec<&str> = values
            .iter()
            .map(String::as_str)
            .filter(|v| !self.ids.contains_key(*v))
            .collect();
        missing.sort_unstable();
        missing.dedup();
        if missing.is_empty() {
            return Ok(());
        }
        // One statement: the missing values inserted in sorted order (their numbers in that
        // order statements in a transaction give them), the others read.
        let rows = client
            .query(
                "WITH added AS (INSERT INTO history.term (value) SELECT unnest($1::text[])                  ON CONFLICT (value) DO NOTHING RETURNING value, term_id)                  SELECT value, term_id FROM added UNION ALL                  SELECT value, term_id FROM history.term WHERE value = ANY($1)",
                &[&missing],
            )
            .await?;
        for row in rows {
            self.ids.insert(row.try_get(0)?, row.try_get(1)?);
        }
        Ok(())
    }

    pub fn get(&self, value: &str) -> StoreResult<i32> {
        self.ids
            .get(value)
            .copied()
            .ok_or_else(|| super::StoreError::other(format!("term {value:?} was not prepared")))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn partitions_are_named_and_recognized() {
        let at = Instant::from_isoformat("2026-10-12T05:59:59.999999+00:00").unwrap();
        let hour = hour_of(at);
        assert_eq!(hour.isoformat(), "2026-10-12T05:00:00+00:00");
        assert_eq!(observation_name(hour), "observation_2026101205");
        assert_eq!(
            observation_end("observation_2026101205").map(Instant::isoformat),
            Some("2026-10-12T06:00:00+00:00".to_owned())
        );
        assert_eq!(
            observation_end("observation_20261012").map(Instant::isoformat),
            Some("2026-10-13T00:00:00+00:00".to_owned())
        );
        assert_eq!(observation_end("observation_2026101"), None);
        assert_eq!(observation_end("observation_default"), None);
        assert_eq!(
            history_day("actual_stop_event_20261012"),
            Some(Date::ymd(2026, 10, 12))
        );
        assert_eq!(
            history_day("journey_20261012"),
            Some(Date::ymd(2026, 10, 12))
        );
        assert_eq!(history_day("unknown_20261012"), None);
        assert_eq!(history_day("journey_2026101"), None);
        let before = Instant::from_isoformat("1969-12-31T23:30:00+00:00").unwrap();
        assert_eq!(hour_of(before).isoformat(), "1969-12-31T23:00:00+00:00");
    }
}
