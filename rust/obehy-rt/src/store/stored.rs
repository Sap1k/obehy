//! Reading stored observations back for warm replay (docs/R1_SLICE.md section 5), with binary COPY
//! out instead of a server-side cursor.
//!
//! The window is read in `received_at` order, which the `observation_time` index streams without
//! a sort; a chunk never splits observations received at the same instant, so sorting each chunk
//! by the full processing key (the runner's) gives the same order as one sort over the window.
//! [`spawn_reader`] reads and decodes on a thread with its own connection, so waiting on the
//! database and decoding the facts overlap with the core.
//!
//! With `alive` (policy `warm_replay.mode = "vehicles"`) only what the state of the vehicles
//! still live depends on is read (the query is Python's, text for text: read its module
//! docstring).

use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{Receiver, SyncSender, TrySendError, sync_channel};
use std::time::Duration;

use futures_util::StreamExt;
use obehy_core::model::json::facts_from_json_text;
use obehy_core::model::{Feed, Observation, RawRef};
use obehy_core::time::Instant;
use tokio_postgres::binary_copy::BinaryCopyOutStream;
use tokio_postgres::types::Type;
use tokio_postgres::{Client, NoTls};

use super::cells::{JsonText, PgTime};
use super::{StoreError, StoreResult};

pub const CHUNK: usize = 20_000;
/// Decoded chunks the reader may run ahead of the core.
pub const DEPTH: usize = 4;

const COLUMNS: &str = "o.source, o.channel, o.feed, o.received_at, o.observed_at, o.raw_sha256, \
 o.raw_item, o.decoder_version, o.facts";

const TYPES: [Type; 9] = [
    Type::TEXT,
    Type::TEXT,
    Type::TEXT,
    Type::TIMESTAMPTZ,
    Type::TIMESTAMPTZ,
    Type::TEXT,
    Type::INT4,
    Type::INT4,
    Type::JSONB,
];

/// What to read: received in `[since, until)`, and with `alive` only what live vehicles need.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Window {
    pub since: Instant,
    pub until: Instant,
    pub alive: Option<Instant>,
}

fn literal(at: Instant) -> String {
    format!("'{}'::timestamptz", at.isoformat())
}

/// The query of a window (`_WINDOW` or `_BY_VEHICLE`), its instants written in (COPY takes no
/// parameters).
pub fn query(window: &Window) -> String {
    let since = literal(window.since);
    let until = literal(window.until);
    match window.alive {
        None => format!(
            "SELECT {COLUMNS} FROM rt.observation o \
             WHERE o.received_at >= {since} AND o.received_at < {until} \
             ORDER BY o.received_at"
        ),
        Some(alive) => {
            let alive = literal(alive);
            format!(
                "WITH latest AS ( \
                 SELECT DISTINCT ON (source, vehicle_id) source, vehicle_id, received_at, \
                 state_since, feed, journey_namespace, journey_key, service_date \
                 FROM rt.observation \
                 WHERE received_at >= {since} AND received_at < {until} \
                 AND vehicle_id IS NOT NULL \
                 ORDER BY source, vehicle_id, received_at DESC \
                 ), live AS ( \
                 SELECT * FROM latest WHERE received_at >= {alive} \
                 ), journeys AS ( \
                 SELECT feed, journey_namespace, journey_key, service_date \
                 FROM live WHERE journey_key IS NOT NULL \
                 UNION \
                 SELECT feed, journey_namespace, journey_key, service_date \
                 FROM rt.observation \
                 WHERE received_at >= {alive} AND received_at < {until} \
                 AND vehicle_id IS NULL \
                 AND state_since IS NOT NULL AND journey_key IS NOT NULL \
                 ) \
                 SELECT {COLUMNS} FROM rt.observation o \
                 LEFT JOIN live l ON l.source = o.source AND l.vehicle_id = o.vehicle_id \
                 LEFT JOIN journeys j ON j.feed = o.feed \
                 AND j.journey_namespace = o.journey_namespace \
                 AND j.journey_key = o.journey_key AND j.service_date = o.service_date \
                 WHERE o.received_at >= {since} AND o.received_at < {until} AND ( \
                 o.state_since IS NULL OR o.received_at >= l.state_since \
                 OR j.journey_key IS NOT NULL \
                 ) \
                 ORDER BY o.received_at"
            )
        }
    }
}

/// `received_at`-ordered batches regrouped so no instant spans two of them: the observations
/// of a batch's last instant wait for the next batch, where more may follow.
#[derive(Debug, Default)]
pub struct WholeInstants {
    pending: Vec<Observation>,
}

impl WholeInstants {
    /// Add a batch; the whole instants now complete.
    pub fn push(&mut self, batch: Vec<Observation>) -> Option<Vec<Observation>> {
        self.pending.extend(batch);
        let last = self.pending.last()?.received_at;
        let mut cut = self.pending.len();
        while cut > 0 && self.pending[cut - 1].received_at == last {
            cut -= 1;
        }
        if cut == 0 {
            return None;
        }
        let rest = self.pending.split_off(cut);
        Some(std::mem::replace(&mut self.pending, rest))
    }

    /// What is left at the end.
    pub fn finish(self) -> Option<Vec<Observation>> {
        (!self.pending.is_empty()).then_some(self.pending)
    }
}

fn decode_row(row: &tokio_postgres::binary_copy::BinaryCopyOutRow) -> StoreResult<Observation> {
    let err = |e: tokio_postgres::Error| StoreError::Database(e);
    let feed: &str = row.try_get(2).map_err(err)?;
    let facts: JsonText = row.try_get(8).map_err(err)?;
    Ok(Observation {
        source: row.try_get::<&str>(0).map_err(err)?.to_owned(),
        channel: row.try_get::<&str>(1).map_err(err)?.to_owned(),
        feed: feed.parse::<Feed>().map_err(StoreError::other)?,
        received_at: row.try_get::<PgTime>(3).map_err(err)?.0,
        observed_at: row.try_get::<Option<PgTime>>(4).map_err(err)?.map(|t| t.0),
        raw: RawRef {
            sha256: row.try_get::<&str>(5).map_err(err)?.to_owned(),
            item: i64::from(row.try_get::<i32>(6).map_err(err)?),
        },
        decoder_version: i64::from(row.try_get::<i32>(7).map_err(err)?),
        facts: facts_from_json_text(&facts.0).map_err(|e| StoreError::other(e.0))?,
    })
}

/// Stored observations of `window`, in `received_at` order, about `chunk` at a time and never
/// splitting an instant, handed to `each` (which returns false to stop); how many were read.
pub async fn stored_observations(
    client: &Client,
    window: &Window,
    chunk: usize,
    mut each: impl FnMut(Vec<Observation>) -> bool,
) -> StoreResult<usize> {
    let statement = format!("COPY ({}) TO STDOUT (FORMAT binary)", query(window));
    let stream = client.copy_out(statement.as_str()).await?;
    let mut rows = std::pin::pin!(BinaryCopyOutStream::new(stream, &TYPES));
    let mut whole = WholeInstants::default();
    let mut batch = Vec::with_capacity(chunk);
    let mut count = 0;
    while let Some(row) = rows.next().await {
        batch.push(decode_row(&row?)?);
        if batch.len() >= chunk {
            count += batch.len();
            let full = std::mem::replace(&mut batch, Vec::with_capacity(chunk));
            if let Some(ready) = whole.push(full)
                && !each(ready)
            {
                return Ok(count);
            }
        }
    }
    count += batch.len();
    if let Some(ready) = whole.push(batch)
        && !each(ready)
    {
        return Ok(count);
    }
    if let Some(rest) = whole.finish() {
        each(rest);
    }
    Ok(count)
}

/// Chunks read ahead on a thread with its own connection and runtime (`stream_observations`).
/// Dropping the receiver stops the reader; a reader error arrives as the last item.
#[derive(Debug)]
pub struct Reader {
    pub chunks: Receiver<StoreResult<Vec<Observation>>>,
    cancelled: Arc<AtomicBool>,
    thread: Option<std::thread::JoinHandle<()>>,
}

impl Iterator for Reader {
    type Item = StoreResult<Vec<Observation>>;

    fn next(&mut self) -> Option<Self::Item> {
        self.chunks.recv().ok()
    }
}

impl Drop for Reader {
    fn drop(&mut self) {
        self.cancelled.store(true, Ordering::SeqCst);
        // Unblock a reader waiting on a full queue.
        while self.chunks.try_recv().is_ok() {}
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
    }
}

fn put(
    sender: &SyncSender<StoreResult<Vec<Observation>>>,
    cancelled: &AtomicBool,
    mut item: StoreResult<Vec<Observation>>,
) -> bool {
    loop {
        if cancelled.load(Ordering::SeqCst) {
            return false;
        }
        match sender.try_send(item) {
            Ok(()) => return true,
            Err(TrySendError::Full(back)) => {
                item = back;
                std::thread::sleep(Duration::from_millis(5));
            }
            Err(TrySendError::Disconnected(_)) => return false,
        }
    }
}

/// Start reading `window` from `url` on a thread of its own.
pub fn spawn_reader(url: &str, window: Window, chunk: usize) -> Reader {
    let (sender, chunks) = sync_channel(DEPTH);
    let cancelled = Arc::new(AtomicBool::new(false));
    let flag = Arc::clone(&cancelled);
    let url = url.to_owned();
    let thread = std::thread::Builder::new()
        .name("warm-replay-read".into())
        .spawn(move || {
            let read = || -> StoreResult<()> {
                let runtime = tokio::runtime::Builder::new_current_thread()
                    .enable_all()
                    .build()
                    .map_err(|e| StoreError::other(e.to_string()))?;
                runtime.block_on(async {
                    let (client, connection) = tokio_postgres::connect(&url, NoTls).await?;
                    let driver = tokio::spawn(connection);
                    let outcome = stored_observations(&client, &window, chunk, |part| {
                        put(&sender, &flag, Ok(part))
                    })
                    .await;
                    drop(client);
                    let _ = driver.await;
                    outcome.map(|_| ())
                })
            };
            if let Err(error) = read() {
                put(&sender, &flag, Err(error));
            }
        })
        .expect("starting the warm replay reader");
    Reader {
        chunks,
        cancelled,
        thread: Some(thread),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn obs(at: i64, item: i64) -> Observation {
        Observation {
            source: "s".into(),
            channel: "c".into(),
            feed: Feed::Jdf,
            received_at: Instant::from_micros(at),
            observed_at: None,
            raw: RawRef {
                sha256: "x".into(),
                item,
            },
            decoder_version: 1,
            facts: Vec::new(),
        }
    }

    fn items(batch: &[Observation]) -> Vec<i64> {
        batch.iter().map(|o| o.raw.item).collect()
    }

    #[test]
    fn whole_instants_never_split_an_instant() {
        let mut whole = WholeInstants::default();
        let first = whole.push(vec![obs(1, 0), obs(2, 1), obs(2, 2)]).unwrap();
        assert_eq!(items(&first), [0]);
        assert!(whole.push(vec![obs(2, 3)]).is_none());
        let second = whole.push(vec![obs(3, 4)]).unwrap();
        assert_eq!(items(&second), [1, 2, 3]);
        assert!(whole.push(Vec::new()).is_none());
        assert_eq!(items(&whole.finish().unwrap()), [4]);
    }

    #[test]
    fn the_window_query_writes_its_instants_in() {
        let window = Window {
            since: Instant::from_micros(0),
            until: Instant::from_micros(3_600_000_000),
            alive: None,
        };
        let text = query(&window);
        assert!(text.contains("o.received_at >= '1970-01-01T00:00:00+00:00'::timestamptz"));
        assert!(text.ends_with("ORDER BY o.received_at"));
        let alive = query(&Window {
            alive: Some(Instant::from_micros(1_800_000_000)),
            ..window
        });
        assert!(alive.contains("received_at >= '1970-01-01T00:30:00+00:00'::timestamptz"));
    }
}
