//! Alerts to PostgreSQL (BASE_PLAN.md section 25): the writing half of `emit/alerts.py`, and the
//! read of `sql/alert_feed.sql` the GTFS-RT alert feed is made from.
//!
//! `write_alerts` keeps `rt.alert` for one channel's poll, which states its whole current set:
//! each alert is upserted (`changed_at` moves only when its content changes, or it returns), and
//! the channel's alerts missing from the poll are withdrawn.

use obehy_core::alerts::{Alert, Texts};
use obehy_core::py::json::{DumpOptions, PyJson, dumps};
use obehy_core::time::Instant;
use serde_json::Value;
use tokio_postgres::Client;
use tokio_postgres::types::ToSql;

use super::cells::{JsonText, Jsonb, PgTime};
use super::{StoreError, StoreResult};

/// What a source says about an alert: a change of any of these moves `changed_at`.
pub const CONTENT: [&str; 11] = [
    "cause",
    "effect",
    "severity",
    "periods",
    "header",
    "description",
    "url",
    "cause_detail",
    "effect_detail",
    "entities",
    "details",
];

/// `sql/alert_feed.sql`, shared with Python.
pub const ALERT_FEED_SQL: &str = include_str!("../../../../src/obehy/realtime/sql/alert_feed.sql");

fn columns(prefix: &str) -> String {
    CONTENT
        .iter()
        .map(|c| format!("{prefix}{c}"))
        .collect::<Vec<_>>()
        .join(", ")
}

/// `_UPSERT`, with numbered placeholders.
pub fn upsert_sql() -> String {
    let marks: Vec<String> = (1..=CONTENT.len() + 6).map(|i| format!("${i}")).collect();
    format!(
        "INSERT INTO rt.alert (source, alert_id, channel, {}, first_seen, last_seen, changed_at) \
         VALUES ({}) ON CONFLICT (source, alert_id) DO UPDATE SET \
         changed_at = CASE WHEN rt.alert.withdrawn_at IS NOT NULL \
         OR ({}) IS DISTINCT FROM ({}) \
         THEN excluded.changed_at ELSE rt.alert.changed_at END, \
         ({}, channel, last_seen, withdrawn_at) = ({}, excluded.channel, excluded.last_seen, NULL)",
        columns(""),
        marks.join(", "),
        columns("rt.alert."),
        columns("excluded."),
        columns(""),
        columns("excluded."),
    )
}

fn pairs(value: &Texts) -> PyJson {
    let mut out: Vec<(String, PyJson)> = Vec::new();
    for (k, v) in value {
        match out.iter_mut().find(|(key, _)| key == k) {
            Some(slot) => slot.1 = PyJson::str(v),
            None => out.push((k.clone(), PyJson::str(v))),
        }
    }
    PyJson::from_pairs(out)
}

/// `json.dumps(dict(value), ensure_ascii=False, sort_keys=True)`.
fn texts(value: &Texts) -> Jsonb {
    Jsonb(dumps(&pairs(value), DumpOptions::DEFAULT.utf8().sorted()))
}

fn iso(value: Option<Instant>) -> PyJson {
    value.map_or(PyJson::Null, |at| PyJson::str(at.isoformat()))
}

/// The content columns of an alert after source, alert ID and channel (`_row` without the
/// three times).
fn content(alert: &Alert) -> Vec<Jsonb> {
    let periods = PyJson::List(
        alert
            .periods
            .iter()
            .map(|p| PyJson::object([("start", iso(p.start)), ("end", iso(p.end))]))
            .collect(),
    );
    let entities = PyJson::List(
        alert
            .entities
            .iter()
            .map(|e| {
                PyJson::object([
                    ("kind", PyJson::str(&e.kind)),
                    ("namespace", PyJson::str(&e.namespace)),
                    ("identifier", PyJson::str(&e.identifier)),
                    (
                        "service_date",
                        PyJson::opt(e.service_date, |d| PyJson::str(d.isoformat())),
                    ),
                ])
            })
            .collect(),
    );
    vec![
        Jsonb(dumps(&periods, DumpOptions::DEFAULT)),
        texts(&alert.header),
        texts(&alert.description),
        texts(&alert.url),
        texts(&alert.cause_detail),
        texts(&alert.effect_detail),
        Jsonb(dumps(&entities, DumpOptions::DEFAULT.utf8())),
        Jsonb(dumps(
            &pairs(&alert.details),
            DumpOptions::DEFAULT.utf8().sorted(),
        )),
    ]
}

/// One poll of an alert channel: its whole current set at `at`, in one transaction.
pub async fn write_alerts(
    client: &Client,
    source: &str,
    channel: &str,
    alerts: &[Alert],
    at: Instant,
) -> StoreResult<()> {
    client.batch_execute("BEGIN").await?;
    let outcome = async {
        if !alerts.is_empty() {
            let upsert = client.prepare(&upsert_sql()).await?;
            let at = PgTime(at);
            let rows: Vec<Vec<Jsonb>> = alerts.iter().map(content).collect();
            // Pipelined, executed in order: Python's `executemany`.
            let pending = alerts.iter().zip(&rows).map(|(alert, json)| {
                let mut values: Vec<&(dyn ToSql + Sync)> = vec![
                    &alert.source,
                    &alert.alert_id,
                    &alert.channel,
                    &alert.cause,
                    &alert.effect,
                    &alert.severity,
                ];
                values.extend(json.iter().map(|j| j as &(dyn ToSql + Sync)));
                values.extend([&at as &(dyn ToSql + Sync), &at, &at]);
                let upsert = &upsert;
                async move { client.execute(upsert, &values).await }
            });
            futures_util::future::try_join_all(pending).await?;
        }
        let ids: Vec<&str> = alerts.iter().map(|a| a.alert_id.as_str()).collect();
        client
            .execute(
                "UPDATE rt.alert SET withdrawn_at = $1 WHERE source = $2 AND channel = $3 \
                 AND withdrawn_at IS NULL AND NOT alert_id = ANY($4)",
                &[&PgTime(at), &source, &channel, &ids],
            )
            .await?;
        Ok::<_, tokio_postgres::Error>(())
    }
    .await;
    match outcome {
        Ok(()) => Ok(client.batch_execute("COMMIT").await?),
        Err(error) => {
            let _ = client.batch_execute("ROLLBACK").await;
            Err(error.into())
        }
    }
}

/// One alert of one feed, as `alert_feed.sql` reads it (JSON columns parsed).
#[derive(Debug, Clone, PartialEq)]
pub struct AlertFeedRow {
    pub feed: String,
    pub source: String,
    pub alert_id: String,
    pub cause: String,
    pub effect: String,
    pub severity: String,
    pub periods: Value,
    pub header: Value,
    pub description: Value,
    pub url: Value,
    pub cause_detail: Value,
    pub effect_detail: Value,
    pub entities: Value,
    pub details: Value,
    pub targets: Value,
}

/// The current alerts of every feed with their resolved entities (`AlertFeed._load`), by feed,
/// source and ID.
pub async fn alert_feed_rows(client: &Client) -> StoreResult<Vec<AlertFeedRow>> {
    let json = |row: &tokio_postgres::Row, i: usize| -> StoreResult<Value> {
        let text: JsonText = row.try_get(i)?;
        serde_json::from_str(&text.0).map_err(|e| StoreError::other(e.to_string()))
    };
    let mut out = Vec::new();
    for row in client.query(ALERT_FEED_SQL, &[]).await? {
        out.push(AlertFeedRow {
            feed: row.try_get(0)?,
            source: row.try_get(1)?,
            alert_id: row.try_get(2)?,
            cause: row.try_get(3)?,
            effect: row.try_get(4)?,
            severity: row.try_get(5)?,
            periods: json(&row, 6)?,
            header: json(&row, 7)?,
            description: json(&row, 8)?,
            url: json(&row, 9)?,
            cause_detail: json(&row, 10)?,
            effect_detail: json(&row, 11)?,
            entities: json(&row, 12)?,
            details: json(&row, 13)?,
            targets: json(&row, 14)?,
        });
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_upsert_numbers_seventeen_values() {
        let text = upsert_sql();
        assert!(text.contains("$17)"));
        assert!(!text.contains("$18"));
        assert!(text.contains("(cause, effect, severity, periods, header, description, url"));
    }
}
