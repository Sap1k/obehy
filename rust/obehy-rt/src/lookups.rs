//! The lookup broker (BASE_PLAN.md section 18.2): port of `runtime/lookups.py`. It answers what
//! the API was asked (`rt.lookup_request`) through the lookup connectors.
//!
//! The API records a request and NOTIFYs `obehy_lookup`; the broker wakes on it, or every
//! [`SWEEP_S`], and for each pending request and each lookup of its kind covering the trip's
//! feed:
//!
//! 1. reads what the worker knows of the trip (`sql/lookup_view.sql`) and asks the connector's
//!    `plan`: not available, or an upstream request with a cache key;
//! 2. reuses the answer of that key while it holds (`rt.detail_cache`: `ttl_s`,
//!    `not_found_ttl_s`, `retry_s` after a failure); otherwise fetches it, within the lookup's
//!    token bucket (a request without a token waits for a later pass) and daily budget (spent:
//!    failed);
//! 3. archives the answer like a poll (`<source>/<lookup>` in the raw archive), logs it
//!    (`rt.lookup_fetch`), decodes it and writes the composition, merged with the journey's
//!    earlier answers (section 28).
//!
//! A request is answered once every lookup has its outcome. The broker is a task of its own with
//! its own connection; a slow upstream answer delays only other lookups.

use std::collections::HashMap;

use futures_util::StreamExt;
use indexmap::IndexMap;
use obehy_core::compositions::{NotAvailable, Subject, TripView, Unit, UpstreamRequest, ViewCall};
use obehy_core::config::Channel;
use obehy_core::config::manifest::{Lookup, Rate};
use obehy_core::config::policy::JourneyPolicy;
use obehy_core::py::json::PyJson;
use obehy_core::time::{Date, Instant, ServiceTime, TimeDelta, local_date};
use obehy_sources::cd;
use obehy_sources::error::PayloadError;
use serde_json::{Map, Value};
use tokio_postgres::{AsyncMessage, Client, Config, NoTls};

use crate::archive::ArchiveWriter;
use crate::fetch::{Clock, quote};
use crate::index::sql::PgDate;
use crate::logs::Log;
use crate::scheduler::{Fetcher, Stop};
use crate::store::cells::{JsonText, Jsonb, PgTime};
use crate::store::compositions::write_composition;
use crate::store::{StoreError, StoreResult};

pub const LOOKUP_CHANNEL: &str = "obehy_lookup";
pub const SWEEP_S: f64 = 5.0;
pub const BATCH: i64 = 50;
/// Location keys lookups plan with.
pub const LOCATION_NAMESPACES: [&str; 1] = ["sr70"];

/// `sql/lookup_view.sql`, shared with Python (named placeholders numbered at use).
pub const LOOKUP_VIEW_SQL: &str = include_str!("../../../src/obehy/realtime/sql/lookup_view.sql");

/// A lookup connector: `plan` and `decode` (the Python `CONNECTORS` table).
pub type Plan = fn(&Subject, &TripView) -> Result<UpstreamRequest, NotAvailable>;
pub type Decode = fn(&[u8], &UpstreamRequest) -> Result<Vec<Unit>, PayloadError>;

/// The connector of a lookup, if any.
pub fn connector(source: &str, name: &str) -> Option<(Plan, Decode)> {
    match (source, name) {
        (cd::SOURCE, cd::COMPOSITION) => Some((cd::plan, cd::decode)),
        _ => None,
    }
}

/// The keys lookups plan with: the journey keys, and the CZPTT train ID (TR).
pub fn trip_namespaces(journey: &JourneyPolicy) -> Vec<String> {
    let mut found = journey.namespaces.clone();
    found.push("czptt:tr".to_owned());
    found
}

/// A token bucket: `rate.per_minute` on average, at most `rate.burst` at once.
#[derive(Debug, Clone)]
pub struct Bucket {
    pub rate: Rate,
    pub tokens: f64,
    pub at: f64,
}

impl Bucket {
    pub fn new(rate: Rate) -> Bucket {
        Bucket {
            rate,
            tokens: -1.0,
            at: 0.0,
        }
    }

    pub fn take(&mut self, now: f64) -> bool {
        let burst = self.rate.burst as f64;
        if self.tokens < 0.0 {
            self.tokens = burst;
            self.at = now;
        }
        let refilled = self.tokens + (now - self.at) * self.rate.per_minute / 60.0;
        self.tokens = if refilled < burst { refilled } else { burst };
        self.at = now;
        if self.tokens < 1.0 {
            return false;
        }
        self.tokens -= 1.0;
        true
    }
}

/// The URL with `{name}` placeholders filled, each value quoted with nothing safe.
pub fn fill(url: &str, params: &[(String, String)]) -> String {
    let mut url = url.to_owned();
    for (name, value) in params {
        url = url.replace(&format!("{{{name}}}"), &quote(value, false));
    }
    url
}

/// `%(name)s` placeholders as `$n`, in the order of `names`.
pub fn numbered(sql: &str, names: &[&str]) -> String {
    let mut out = sql.to_owned();
    for (i, name) in names.iter().enumerate() {
        out = out.replace(&format!("%({name})s"), &format!("${}", i + 1));
    }
    out
}

fn string_map(value: &Value) -> IndexMap<String, String> {
    value
        .as_object()
        .map(|o| {
            o.iter()
                .map(|(k, v)| (k.clone(), v.as_str().unwrap_or_default().to_owned()))
                .collect()
        })
        .unwrap_or_default()
}

/// What the worker knows of a trip on a date (`read_view`); None: not in the active release.
pub async fn read_view(
    client: &Client,
    trip_id: &str,
    service_date: Date,
    now: Instant,
    trip_namespaces: &[String],
) -> StoreResult<Option<TripView>> {
    let sql = numbered(
        LOOKUP_VIEW_SQL,
        &[
            "trip_id",
            "service_date",
            "trip_namespaces",
            "location_namespaces",
        ],
    );
    let locations: Vec<&str> = LOCATION_NAMESPACES.to_vec();
    let Some(row) = client
        .query_opt(
            &sql,
            &[
                &trip_id,
                &PgDate(service_date),
                &trip_namespaces,
                &locations,
            ],
        )
        .await?
    else {
        return Ok(None);
    };
    let parse = |text: JsonText| -> StoreResult<Value> {
        serde_json::from_str(&text.0).map_err(|e| StoreError::other(e.to_string()))
    };
    let keys = parse(row.try_get(1)?)?;
    let calls = parse(row.try_get(2)?)?;
    let calls = calls
        .as_array()
        .map(|items| {
            items
                .iter()
                .map(|c| ViewCall {
                    ordinal: c["ordinal"].as_i64().unwrap_or_default(),
                    location_id: c["location_id"].as_str().unwrap_or_default().to_owned(),
                    visit_n: c["visit_n"].as_i64().unwrap_or_default(),
                    passenger: c["passenger"].as_bool().unwrap_or_default(),
                    left: c["left"].as_bool().unwrap_or_default(),
                    keys: string_map(&c["keys"]),
                })
                .collect()
        })
        .unwrap_or_default();
    Ok(Some(TripView {
        feed: row.try_get(0)?,
        trip_id: trip_id.to_owned(),
        service_date,
        now,
        keys: string_map(&keys),
        calls,
    }))
}

/// One lookup's outcome for a request (the object stored under its name in `outcomes`).
fn outcome(fields: &[(&str, &str)]) -> Value {
    let mut out = Map::new();
    for (k, v) in fields {
        out.insert((*k).to_owned(), Value::String((*v).to_owned()));
    }
    Value::Object(out)
}

/// One pass at a time over the pending requests (`serve`); its own connection.
pub struct LookupBroker<F: Fetcher> {
    pub client: Client,
    pub lookups: Vec<Lookup>,
    pub fetcher: F,
    pub archive: ArchiveWriter,
    pub clock: Clock,
    pub monotonic: Box<dyn Fn() -> f64 + Send + Sync>,
    pub log: Log,
    pub journey: JourneyPolicy,
    pub buckets: HashMap<String, Bucket>,
    pub spent: HashMap<String, (Date, i64)>,
}

impl<F: Fetcher> std::fmt::Debug for LookupBroker<F> {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("LookupBroker")
            .field("lookups", &self.lookups.len())
            .finish_non_exhaustive()
    }
}

fn qualified(lookup: &Lookup) -> String {
    format!("{}/{}", lookup.source, lookup.name)
}

impl<F: Fetcher> LookupBroker<F> {
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        client: Client,
        lookups: &[Lookup],
        fetcher: F,
        archive: ArchiveWriter,
        clock: Clock,
        monotonic: Box<dyn Fn() -> f64 + Send + Sync>,
        log: Log,
        journey: JourneyPolicy,
    ) -> LookupBroker<F> {
        let mut known = Vec::new();
        let mut buckets = HashMap::new();
        for lookup in lookups {
            if connector(&lookup.source, &lookup.name).is_none() {
                log(&format!("{}: no connector; not asked", qualified(lookup)));
                continue;
            }
            buckets.insert(qualified(lookup), Bucket::new(lookup.rate.clone()));
            known.push(lookup.clone());
        }
        LookupBroker {
            client,
            lookups: known,
            fetcher,
            archive,
            clock,
            monotonic,
            log,
            journey,
            buckets,
            spent: HashMap::new(),
        }
    }

    /// Answer what can be answered now; returns the requests answered.
    pub async fn serve(&mut self) -> StoreResult<usize> {
        let pending = self
            .client
            .query(
                "SELECT kind, feed, trip_id, service_date, from_location, outcomes \
                 FROM rt.lookup_request WHERE pending ORDER BY requested_at LIMIT $1",
                &[&BATCH],
            )
            .await?;
        let namespaces = trip_namespaces(&self.journey);
        let mut answered = 0;
        let mut views: HashMap<(String, Date), Option<TripView>> = HashMap::new();
        for row in pending {
            let now = (self.clock)();
            let kind: String = row.try_get(0)?;
            let feed: String = row.try_get(1)?;
            let trip_id: String = row.try_get(2)?;
            let service_date = row.try_get::<_, PgDate>(3)?.0;
            let from_location: String = row.try_get(4)?;
            let outcomes: JsonText = row.try_get(5)?;
            let mut outcomes: Map<String, Value> =
                serde_json::from_str(&outcomes.0).map_err(|e| StoreError::other(e.to_string()))?;
            let covering: Vec<Lookup> = self
                .lookups
                .iter()
                .filter(|l| l.kind == kind && l.feeds.contains(&feed))
                .cloned()
                .collect();
            let subject = Subject {
                feed: feed.clone(),
                trip_id: trip_id.clone(),
                service_date,
                from_location: (!from_location.is_empty()).then(|| from_location.clone()),
            };
            let place = (trip_id.clone(), service_date);
            if !views.contains_key(&place) {
                let view =
                    read_view(&self.client, &trip_id, service_date, now, &namespaces).await?;
                views.insert(place.clone(), view);
            }
            let view = views[&place].clone();
            for lookup in &covering {
                let name = qualified(lookup);
                if outcomes.contains_key(&name) {
                    continue;
                }
                let Some(view) = &view else {
                    outcomes.insert(
                        name,
                        outcome(&[
                            ("outcome", "not_available"),
                            ("reason", "the trip is not in the active release"),
                        ]),
                    );
                    continue;
                };
                if let Some(found) = self.answer(lookup, &subject, view, now).await? {
                    outcomes.insert(name, found);
                }
            }
            let done = covering
                .iter()
                .all(|l| outcomes.contains_key(&qualified(l)));
            let text = Jsonb(Value::Object(outcomes).to_string());
            self.client
                .execute(
                    "UPDATE rt.lookup_request SET outcomes = $1, pending = $2, \
                     answered_at = CASE WHEN $3 THEN statement_timestamp() ELSE answered_at END \
                     WHERE (kind, feed, trip_id, service_date, from_location) \
                     = ($4, $5, $6, $7, $8)",
                    &[
                        &text,
                        &!done,
                        &done,
                        &kind,
                        &feed,
                        &trip_id,
                        &PgDate(service_date),
                        &from_location,
                    ],
                )
                .await?;
            answered += usize::from(done);
        }
        Ok(answered)
    }

    /// The lookup's outcome for the subject; None to wait for a token.
    async fn answer(
        &mut self,
        lookup: &Lookup,
        subject: &Subject,
        view: &TripView,
        now: Instant,
    ) -> StoreResult<Option<Value>> {
        let (plan, decode) = connector(&lookup.source, &lookup.name).expect("known lookups only");
        let keys = |namespace: &str| view.keys.get(namespace).cloned();
        if let Some(refusal) = lookup
            .applies
            .refusal(&keys, view.service_date, local_date(now))
        {
            return Ok(Some(outcome(&[
                ("outcome", "not_available"),
                ("reason", &refusal),
            ])));
        }
        let planned = match plan(subject, view) {
            Ok(planned) => planned,
            Err(refused) => {
                return Ok(Some(outcome(&[
                    ("outcome", "not_available"),
                    ("reason", &refused.reason),
                ])));
            }
        };
        let cached = self
            .client
            .query_opt(
                "SELECT outcome, fetch_id FROM rt.detail_cache \
                 WHERE source = $1 AND lookup = $2 AND cache_key = $3 AND expires_at > $4",
                &[
                    &lookup.source,
                    &lookup.name,
                    &planned.cache_key,
                    &PgTime(now),
                ],
            )
            .await?;
        if let Some(cached) = cached {
            let found: String = cached.try_get(0)?;
            return Ok(Some(outcome(&[
                ("outcome", &found),
                ("cache_key", &planned.cache_key),
            ])));
        }
        let name = qualified(lookup);
        let monotonic = (self.monotonic)();
        if !self
            .buckets
            .get_mut(&name)
            .expect("a bucket per known lookup")
            .take(monotonic)
        {
            return Ok(None);
        }
        if !self.spend(lookup, now).await? {
            return Ok(Some(outcome(&[
                ("outcome", "failed"),
                ("reason", "the daily budget is spent"),
                ("cache_key", &planned.cache_key),
            ])));
        }
        self.fetch(lookup, view, &planned, decode).await.map(Some)
    }

    async fn spend(&mut self, lookup: &Lookup, now: Instant) -> StoreResult<bool> {
        let today = local_date(now);
        let name = qualified(lookup);
        let mut spent = match self.spent.get(&name) {
            Some((day, spent)) if *day == today => *spent,
            _ => {
                let midnight = ServiceTime::new(today, 0).instant(); // local midnight
                let row = self
                    .client
                    .query_one(
                        "SELECT count(*) AS n FROM rt.lookup_fetch \
                         WHERE source = $1 AND lookup = $2 AND requested_at >= $3",
                        &[&lookup.source, &lookup.name, &PgTime(midnight)],
                    )
                    .await?;
                row.try_get::<_, i64>(0)?
            }
        };
        if spent >= lookup.daily_budget {
            self.spent.insert(name, (today, spent));
            return Ok(false);
        }
        spent += 1;
        self.spent.insert(name, (today, spent));
        Ok(true)
    }

    async fn fetch(
        &mut self,
        lookup: &Lookup,
        view: &TripView,
        planned: &UpstreamRequest,
        decode: Decode,
    ) -> StoreResult<Value> {
        let request = Channel {
            url: fill(&lookup.request.url, &planned.params),
            ..lookup.request.clone()
        };
        let poll = self.fetcher.fetch(&request, None).await;
        let mut request_json = vec![("cache_key".to_owned(), PyJson::str(&planned.cache_key))];
        for (k, v) in &planned.params {
            match request_json.iter_mut().find(|(key, _)| key == k) {
                Some(slot) => slot.1 = PyJson::str(v),
                None => request_json.push((k.clone(), PyJson::str(v))),
            }
        }
        let extra = [("request".to_owned(), PyJson::Object(request_json))];
        let stored = self
            .archive
            .append(
                &lookup.source,
                &lookup.name,
                &poll,
                poll.body.as_deref(),
                &extra,
            )
            .map_err(|e| StoreError::other(format!("archive: {e}")))?;
        let mut units: Vec<Unit> = Vec::new();
        let mut error = if poll.ok() { None } else { poll.error.clone() };
        if error.is_none()
            && let Some(body) = &poll.body
        {
            match decode(body, planned) {
                Ok(found) => units = found,
                Err(e) => error = Some(format!("undecodable answer: {}", e.message)),
            }
        }
        let result = if error.is_some() {
            "failed"
        } else if units.is_empty() {
            "not_found"
        } else {
            "answered"
        };
        if let Some(error) = &error {
            (self.log)(&format!(
                "{} {}: {error}",
                qualified(lookup),
                planned.cache_key
            ));
        }
        let received = poll.received_at;
        let status = poll.status.map(i32::from);
        let logged = self
            .client
            .query_one(
                "INSERT INTO rt.lookup_fetch (source, lookup, cache_key, requested_at, \
                 received_at, status, error, raw_sha256, outcome) \
                 VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) RETURNING fetch_id",
                &[
                    &lookup.source,
                    &lookup.name,
                    &planned.cache_key,
                    &PgTime(poll.requested_at),
                    &PgTime(poll.received_at),
                    &status,
                    &error,
                    &stored.sha256,
                    &result,
                ],
            )
            .await?;
        let fetch_id: i64 = logged.try_get(0)?;
        let mut composition_id: Option<i64> = None;
        if result == "answered" {
            composition_id = Some(
                write_composition(
                    &self.client,
                    &self.journey,
                    view,
                    &lookup.source,
                    &planned.start,
                    &units,
                    received,
                    fetch_id,
                )
                .await?,
            );
        }
        let hold = match result {
            "answered" => lookup.ttl_s,
            "not_found" => lookup.not_found_ttl_s,
            _ => lookup.retry_s,
        };
        self.client
            .execute(
                "INSERT INTO rt.detail_cache (source, lookup, cache_key, fetch_id, outcome, \
                 fetched_at, expires_at, composition_id) \
                 VALUES ($1, $2, $3, $4, $5, $6, $7, $8) \
                 ON CONFLICT (source, lookup, cache_key) DO UPDATE SET \
                 fetch_id = excluded.fetch_id, outcome = excluded.outcome, \
                 fetched_at = excluded.fetched_at, expires_at = excluded.expires_at, \
                 composition_id = excluded.composition_id",
                &[
                    &lookup.source,
                    &lookup.name,
                    &planned.cache_key,
                    &fetch_id,
                    &result,
                    &PgTime(received),
                    &PgTime(received + TimeDelta::from_secs_f64(hold)),
                    &composition_id,
                ],
            )
            .await?;
        let mut answer = outcome(&[("outcome", result), ("cache_key", &planned.cache_key)]);
        if let (Some(error), Value::Object(map)) = (error, &mut answer) {
            map.insert("reason".to_owned(), Value::String(error));
        }
        Ok(answer)
    }
}

/// Connect to `url`; the connection runs on its own task. Notifications (after `LISTEN`) come
/// on the returned receiver.
pub async fn listen(
    config: &Config,
    channel: &str,
) -> Result<(Client, tokio::sync::mpsc::UnboundedReceiver<String>), tokio_postgres::Error> {
    let (client, mut connection) = config.connect(NoTls).await?;
    let (sender, receiver) = tokio::sync::mpsc::unbounded_channel();
    tokio::spawn(async move {
        let mut messages = futures_util::stream::poll_fn(move |cx| connection.poll_message(cx));
        while let Some(message) = messages.next().await {
            match message {
                Ok(AsyncMessage::Notification(note)) => {
                    let _ = sender.send(note.channel().to_owned());
                }
                Ok(_) => {}
                Err(error) => {
                    crate::logs::log(&format!("listener connection: {error}"));
                    break;
                }
            }
        }
    });
    client.batch_execute(&format!("LISTEN {channel}")).await?;
    Ok((client, receiver))
}

/// Serve on every `NOTIFY obehy_lookup`, and every `sweep_s` (tokens refill, requests waiting
/// for one), until `stop`. A database error ends it, as it ends Python's broker task.
pub async fn run_broker<F: Fetcher>(
    broker: &mut LookupBroker<F>,
    database: &Config,
    stop: &Stop,
    sweep_s: f64,
) -> StoreResult<()> {
    let (_listener, mut notes) = listen(database, LOOKUP_CHANNEL).await?;
    while !stop.is_set() {
        broker.serve().await?;
        tokio::select! {
            _ = notes.recv() => {}
            () = stop.sleep(sweep_s) => {}
        }
    }
    Ok(())
}

/// The broker's connection (`open_broker`); it runs on its own task.
pub async fn connect(database: &Config) -> Result<Client, tokio_postgres::Error> {
    let (client, connection) = database.connect(NoTls).await?;
    tokio::spawn(async move {
        if let Err(error) = connection.await {
            crate::logs::log(&format!("lookup broker connection: {error}"));
        }
    });
    Ok(client)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_bucket_refills_at_its_rate_up_to_its_burst() {
        let mut bucket = Bucket::new(Rate {
            per_minute: 6.0,
            burst: 2,
        });
        assert!(bucket.take(0.0));
        assert!(bucket.take(0.0));
        assert!(!bucket.take(0.0));
        assert!(!bucket.take(5.0));
        assert!(bucket.take(10.0));
        assert!(!bucket.take(10.0));
        assert!(bucket.take(1000.0));
        assert!(bucket.take(1000.0));
        assert!(!bucket.take(1000.0));
    }

    #[test]
    fn placeholders_are_filled_quoted_and_numbered() {
        let url = fill(
            "https://x/{train}/{date}",
            &[
                ("train".into(), "R 6 / 2".into()),
                ("date".into(), "2026-10-08".into()),
            ],
        );
        assert_eq!(url, "https://x/R%206%20%2F%202/2026-10-08");
        assert_eq!(
            numbered("a = %(x)s AND b = %(y)s OR %(x)s", &["x", "y"]),
            "a = $1 AND b = $2 OR $1"
        );
        assert!(
            numbered(
                LOOKUP_VIEW_SQL,
                &[
                    "trip_id",
                    "service_date",
                    "trip_namespaces",
                    "location_namespaces"
                ]
            )
            .contains("$4::text[]")
        );
    }
}
