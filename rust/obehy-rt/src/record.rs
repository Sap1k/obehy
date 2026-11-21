//! `obehy-rt record`: poll realtime channels and archive their payloads without processing them
//! (port of `realtime/record.py` and `commands._record`).
//!
//! The only exception is a channel with a named filter, which drops entries Oběhy never uses
//! before the payload is stored (Arriva's fleet-wide feed is reduced to Arriva Express, DPMD's
//! JavaScript to its vehicles in service). The filters are connector code (obehy-sources);
//! [`Filters::shipped`] binds them. A channel whose filter is not bound is not recorded (it is
//! never stored unfiltered by mistake).

use std::collections::BTreeMap;
use std::path::Path;
use std::sync::{Arc, Mutex};

use indexmap::IndexMap;
use obehy_core::config::Channel;
use obehy_core::config::manifest::RECORDER_FILTERS;
use obehy_core::config::policy::BoardPolicy;
use obehy_core::py::json::PyJson;
use obehy_core::py::text::{float_from_str, is_decimal_char, repr_str, strip};
use tokio::task::JoinSet;

use crate::archive::{ArchiveWriter, Poll, sha256_hex};
use crate::demand::{DueSource, run_demand};
use crate::fetch::Clock;
use crate::logs::Log;
use crate::scheduler::{Fetcher, HandlerError, OnPoll, Stop, run_channel};

mod command;

pub use command::{RecordArgs, SourceList, run_command};

pub const SUMMARY_INTERVAL_S: f64 = 600.0;

/// A filter's output: the payload to store and how many entries it kept and dropped.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Filtered {
    pub body: Vec<u8>,
    pub kept: i64,
    pub dropped: i64,
}

/// The payload does not have the shape the filter expects (the message is archived).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FilterError(pub String);

/// A recorder filter: `fn(source payload) -> stored payload`, versioned in the index
/// (`"filter": "dpmd@1"`). The functions are the connectors' (`obehy_sources::*::recorded`),
/// writing the bytes Python's `recorded` writes.
#[derive(Debug, Clone, Copy)]
pub struct PayloadFilter {
    pub version: u32,
    pub apply: fn(&[u8]) -> Result<Filtered, FilterError>,
}

impl PayloadFilter {
    pub fn label(&self, name: &str) -> String {
        format!("{name}@{}", self.version)
    }
}

/// A connector's `recorded` as a filter. Python catches only its `PayloadError` and lets any
/// other `ValueError` end the recorder; here every refusal is stored unfiltered, with its text.
fn connector(
    recorded: fn(
        &[u8],
    )
        -> Result<obehy_sources::arriva_express::Filtered, obehy_sources::PayloadError>,
    body: &[u8],
) -> Result<Filtered, FilterError> {
    recorded(body)
        .map(|f| Filtered {
            body: f.body,
            kept: f.kept as i64,
            dropped: f.dropped as i64,
        })
        .map_err(|e| FilterError(e.message))
}

fn arriva_express(body: &[u8]) -> Result<Filtered, FilterError> {
    connector(obehy_sources::arriva_express::recorded, body)
}

/// DPMD's `TrafficState.js` as JSON: vehicles in service, no stops, shapes or drivers.
fn dpmd(body: &[u8]) -> Result<Filtered, FilterError> {
    connector(obehy_sources::dpmd::recorded, body)
}

/// The recorder's filters by name (`record.FILTERS`).
#[derive(Debug, Clone, Default)]
pub struct Filters(pub BTreeMap<String, PayloadFilter>);

impl Filters {
    /// The filters of [`RECORDER_FILTERS`], at Python's versions.
    pub fn shipped() -> Filters {
        let filter = |version, apply| PayloadFilter { version, apply };
        let filters = BTreeMap::from([
            ("arriva-express".to_owned(), filter(1, arriva_express as _)),
            ("dpmd".to_owned(), filter(1, dpmd as _)),
        ]);
        debug_assert!(
            filters
                .keys()
                .all(|k| RECORDER_FILTERS.contains(&k.as_str()))
        );
        Filters(filters)
    }

    pub fn get(&self, name: &str) -> Option<&PayloadFilter> {
        self.0.get(name)
    }
}

/// The body to store and the extra index fields for a channel's filter (`archived_payload`).
pub fn archived_payload(
    channel: &Channel,
    poll: &Poll,
    filters: &Filters,
) -> (Option<Vec<u8>>, Vec<(String, PyJson)>) {
    let Some(name) = &channel.filter else {
        return (poll.body.clone(), Vec::new());
    };
    let Some(filter) = filters.get(name) else {
        // Refused at start; a worker that got here stores the payload as sent, and says so.
        return (
            poll.body.clone(),
            vec![(
                "filter_error".to_owned(),
                PyJson::str(format!("filter {name} not available")),
            )],
        );
    };
    let mut extra = vec![("filter".to_owned(), PyJson::Str(filter.label(name)))];
    let Some(body) = poll.body.as_deref().filter(|_| poll.ok()) else {
        return (poll.body.clone(), extra);
    };
    extra.push(("source_bytes".to_owned(), PyJson::Int(body.len() as i64)));
    extra.push(("source_sha256".to_owned(), PyJson::Str(sha256_hex(body))));
    match (filter.apply)(body) {
        Err(FilterError(message)) => {
            // Store the unfiltered payload so that a schema change is never lost silently.
            extra.push(("filter_error".to_owned(), PyJson::Str(message)));
            (Some(body.to_vec()), extra)
        }
        Ok(filtered) => {
            extra.push(("kept".to_owned(), PyJson::Int(filtered.kept)));
            extra.push(("dropped".to_owned(), PyJson::Int(filtered.dropped)));
            (Some(filtered.body), extra)
        }
    }
}

/// `{:,}`.
fn thousands(n: u64) -> String {
    let digits = n.to_string();
    let mut out = String::new();
    for (i, c) in digits.chars().enumerate() {
        if i > 0 && (digits.len() - i).is_multiple_of(3) {
            out.push(',');
        }
        out.push(c);
    }
    out
}

#[derive(Debug, Clone, Default, PartialEq)]
pub struct ChannelStats {
    pub polls: u64,
    pub errors: u64,
    pub filter_errors: u64,
    pub new_objects: u64,
    pub stored_bytes: u64,
    pub latencies_ms: Vec<f64>,
}

impl ChannelStats {
    pub fn line(&self, name: &str) -> String {
        let latency = if self.latencies_ms.is_empty() {
            "nan".to_owned()
        } else {
            let mut sorted = self.latencies_ms.clone();
            sorted.sort_by(f64::total_cmp);
            let n = sorted.len();
            let median = if n % 2 == 1 {
                sorted[n / 2]
            } else {
                (sorted[n / 2 - 1] + sorted[n / 2]) / 2.0
            };
            format!("{median:.0}")
        };
        let mut text = format!(
            "{name}: {} polls, {} errors, {} new payloads, {} bytes stored, median {latency} ms",
            self.polls,
            self.errors,
            self.new_objects,
            thousands(self.stored_bytes)
        );
        if self.filter_errors > 0 {
            text.push_str(&format!(", {} filter errors", self.filter_errors));
        }
        text
    }
}

/// What a decoder reads of an archived poll: the stored payload's sha256 and the stored body.
pub type ArchivedBody = (Option<String>, Option<Vec<u8>>);

/// Store a poll (filtered if the channel says so); return the stored payload's sha256 and the
/// stored body, which is what a decoder reads, live as in replay (`archive_poll`).
pub fn archive_poll(
    writer: &ArchiveWriter,
    channel: &Channel,
    poll: &Poll,
    stats: &mut ChannelStats,
    filters: &Filters,
    log: &dyn Fn(&str),
) -> std::io::Result<ArchivedBody> {
    let (body, mut extra) = archived_payload(channel, poll, filters);
    if !poll.request.is_empty() {
        let request = poll
            .request
            .iter()
            .map(|(k, v)| (k.clone(), PyJson::str(v.as_str())))
            .collect();
        extra.push(("request".to_owned(), PyJson::Object(request)));
    }
    let stored = writer.append(
        &channel.source,
        &channel.channel,
        poll,
        body.as_deref(),
        &extra,
    )?;
    stats.polls += 1;
    stats
        .latencies_ms
        .push((poll.received_at - poll.requested_at).total_seconds() * 1000.0);
    if stored.new_object_bytes > 0 {
        stats.new_objects += 1;
        stats.stored_bytes += stored.new_object_bytes as u64;
    }
    if let Some((_, PyJson::Str(message))) = extra.iter().find(|(k, _)| k == "filter_error") {
        stats.filter_errors += 1;
        log(&format!(
            "{}: filter error, stored unfiltered: {message}",
            channel.name()
        ));
    }
    if !poll.ok() {
        stats.errors += 1;
        let reason = poll.error.clone().unwrap_or_else(|| {
            format!(
                "HTTP {}",
                poll.status.map_or("None".to_owned(), |s| s.to_string())
            )
        });
        log(&format!("{}: {reason}", channel.name()));
    }
    Ok((stored.sha256, body))
}

/// The recorder's poll handler: archive, count, log.
#[derive(Clone)]
pub struct Recorder {
    pub writer: ArchiveWriter,
    pub stats: Arc<Mutex<IndexMap<String, ChannelStats>>>,
    pub filters: Filters,
    pub log: Log,
}

impl std::fmt::Debug for Recorder {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Recorder")
            .field("writer", &self.writer)
            .finish_non_exhaustive()
    }
}

impl Recorder {
    fn summarize(&self) {
        let mut stats = self.stats.lock().expect("stats");
        for (name, channel_stats) in stats.iter_mut() {
            (self.log)(&channel_stats.line(name));
            channel_stats.latencies_ms.clear();
        }
    }
}

impl OnPoll for Recorder {
    async fn on_poll(&self, channel: &Channel, poll: Poll) -> Result<(), HandlerError> {
        let recorder = self.clone();
        let channel = channel.clone();
        // Compressing a payload is CPU work: off the async workers.
        tokio::task::spawn_blocking(move || {
            let mut stats = recorder.stats.lock().expect("stats");
            let entry = stats.entry(channel.name()).or_default();
            archive_poll(
                &recorder.writer,
                &channel,
                &poll,
                entry,
                &recorder.filters,
                &*recorder.log,
            )
            .map(|_| ())
        })
        .await??;
        Ok(())
    }
}

/// How long and how often a recording runs.
#[derive(Debug, Clone)]
pub struct RecordOptions {
    pub once: bool,
    pub duration_s: Option<f64>,
    pub summary_interval_s: f64,
}

impl Default for RecordOptions {
    fn default() -> Self {
        RecordOptions {
            once: false,
            duration_s: None,
            summary_interval_s: SUMMARY_INTERVAL_S,
        }
    }
}

/// Demand channels' due calls and the boards policy.
#[derive(Debug)]
pub struct DemandSetup<D> {
    pub source: D,
    pub boards: BoardPolicy,
}

/// Poll and archive until stopped (`record`); demand channels need `demand`.
#[allow(clippy::too_many_arguments)]
pub async fn record<F: Fetcher, D: DueSource + 'static>(
    channels: Vec<Channel>,
    archive: &Path,
    options: RecordOptions,
    fetcher: Arc<F>,
    demand: Option<DemandSetup<D>>,
    filters: Filters,
    clock: Clock,
    stop: Stop,
    log: Log,
) -> Result<IndexMap<String, ChannelStats>, HandlerError> {
    let stats: IndexMap<String, ChannelStats> = channels
        .iter()
        .map(|c| (c.name(), ChannelStats::default()))
        .collect();
    let recorder = Arc::new(Recorder {
        writer: ArchiveWriter::new(archive),
        stats: Arc::new(Mutex::new(stats)),
        filters,
        log: Arc::clone(&log),
    });
    let mut helpers = JoinSet::new();
    if !options.once {
        let summaries = Arc::clone(&recorder);
        let every = options.summary_interval_s;
        helpers.spawn(async move {
            loop {
                tokio::time::sleep(std::time::Duration::from_secs_f64(every)).await;
                summaries.summarize();
            }
        });
        if let Some(seconds) = options.duration_s {
            let deadline = stop.clone();
            helpers.spawn(async move {
                tokio::time::sleep(std::time::Duration::from_secs_f64(seconds.max(0.0))).await;
                deadline.set();
            });
        }
    }
    let names: Vec<String> = channels.iter().map(Channel::name).collect();
    log(&format!(
        "recording {} into {}",
        names.join(", "),
        archive.display()
    ));
    let mut pollers: JoinSet<Result<(), HandlerError>> = JoinSet::new();
    let mut demand = demand;
    for channel in channels {
        let channel = Arc::new(channel);
        if channel.demand.is_none() {
            pollers.spawn(run_channel(
                channel,
                stop.clone(),
                Arc::clone(&fetcher),
                Arc::clone(&recorder),
                Arc::clone(&clock),
                options.once,
            ));
        } else if let Some(setup) = demand.take() {
            let (stop, fetcher, recorder, clock) = (
                stop.clone(),
                Arc::clone(&fetcher),
                Arc::clone(&recorder),
                Arc::clone(&clock),
            );
            let once = options.once;
            pollers.spawn(async move {
                let mut source = setup.source;
                run_demand(
                    channel,
                    stop,
                    &mut source,
                    fetcher,
                    recorder,
                    &setup.boards,
                    clock,
                    None,
                    once,
                )
                .await
                .map(|_| ())
            });
        } else {
            log(&format!(
                "{}: a demand channel needs the active release; not recorded",
                channel.name()
            ));
        }
    }
    let mut failure: Option<HandlerError> = None;
    while let Some(joined) = pollers.join_next().await {
        let result = joined
            .map_err(|e| -> HandlerError { Box::new(e) })
            .and_then(|r| r);
        if let Err(error) = result {
            failure = Some(error);
            break;
        }
    }
    pollers.abort_all();
    helpers.abort_all();
    recorder.summarize();
    if let Some(error) = failure {
        return Err(error);
    }
    let stats = recorder.stats.lock().expect("stats").clone();
    Ok(stats)
}

/// `parse_duration`: `90s`, `30m`, `6h`, `2d` (a decimal number allowed) in seconds.
pub fn parse_duration(text: &str) -> Result<f64, String> {
    let invalid = || {
        format!(
            "invalid duration {}; use e.g. 90s, 30m, 6h or 2d",
            repr_str(text)
        )
    };
    let stripped = strip(text);
    let mut chars: Vec<char> = stripped.chars().collect();
    let unit = chars.pop().ok_or_else(invalid)?;
    let factor = match unit {
        's' => 1.0,
        'm' => 60.0,
        'h' => 3600.0,
        'd' => 86400.0,
        _ => return Err(invalid()),
    };
    let number: String = chars.iter().collect();
    let (whole, fraction) = match number.split_once('.') {
        Some((w, f)) => (w, Some(f)),
        None => (number.as_str(), None),
    };
    let digits = |s: &str| !s.is_empty() && s.chars().all(is_decimal_char);
    if !digits(whole) || fraction.is_some_and(|f| !digits(f)) {
        return Err(invalid());
    }
    float_from_str(&number)
        .map(|value| value * factor)
        .ok_or_else(invalid)
}

#[cfg(test)]
mod tests;
