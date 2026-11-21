//! `obehy-rt compare`: the exact record of a replay window (docs/RUST_PORT.md, R0 item 1). The
//! pinned references (`tests/realtime/compare/`, `tests/golden/corpus-stream/`) are written by
//! it; `--check REF` tells where a stream parts from one, and `obehy rt report --from-stream`
//! makes the replay report of a stream written with `--diagnostics`. This is the contract those
//! readers rely on.
//!
//! The window is replay's: the channels, `merged_polls` reception order, `--since`/`--until` and
//! live feeds unless `--all-feeds`. Output is JSON lines in the canonical form of
//! [`crate::canonical`] (sorted keys, fixed float decimals by key, UTC `Z` times: the
//! byte-for-byte contract). A path ending in `.gz` is gzip-compressed with no name and mtime 0,
//! so equal streams give equal files; compare the decompressed text, not the gzip bytes.
//!
//! The first record is the `header`: `compare_version`, `level`, `channels` (sorted
//! `source/channel`), `from`, `to`, `since`, `until`, `all_feeds`, `fact_schema_version`, and for
//! `output` also `feeds`, `gtfs_rt_every_s`, `release` (run ID), `core_version`,
//! `policy_version`, and only when asked `diagnostics: true` and `quality: true` (below).
//!
//! **`--level facts`** (connectors only; needs neither a release nor a database). Per archived
//! poll, in reception order:
//!
//! - `poll`: `at` (reception), `source`, `channel`, `sha256`, `status`, `error` (the transport
//!   error archived), `request` (a demand poll's request, else null), `decoder_version`,
//!   `outcome`: `decoded` (with `observations`: how many after the live-feed filter), `failed`
//!   (no body or a failed status) or `undecodable`, followed then by `payload_error`:
//!   `message` (the decoder's error);
//! - one `facts` per observation: `item`, `feed`, `observed_at`, `decoder_version` and `facts`:
//!   the facts JSON of [`obehy_core::model::json`] (floats canonical, its own time strings
//!   kept). An observation names its poll's source, channel, reception time and payload by
//!   construction.
//!
//! **`--level output`** (needs a release directory; no database). Everything the worker would
//! write, each record tagged with `at` (simulated time), in the order produced:
//!
//! - `effect`: `phase` (`step`: one poll's observations, at its reception; `tick`:
//!   housekeeping and estimates; `finish`: the final tick at the last poll) and `effect` (the
//!   core's effect, generic); a step's or tick's effects first, then the rows the writer made of
//!   them ([`crate::store::RecordingStore`] lists the row kinds);
//! - every `gtfs_rt_every_s` (on round multiples, as replay snapshots): estimates for every
//!   feed, the state tables written (`state` rows, and the vehicle rows that settles), then
//!   `gtfs_rt` per feed: `feed` and `message`, the FeedMessage as protobuf JSON with proto field
//!   names (64-bit integers as strings, enums by name, float32 as their shortest round-trip
//!   decimal, then the canonical float rule), vehicles named as the state tables name them;
//! - after the last poll, the final tick's effects and rows, then the state tables once more.
//!
//! Alerts and compositions are not part of the stream: alert channels are not replayed (they do
//! not pass through the core) and compositions need the lookup broker's database.
//!
//! **`--diagnostics`** (output level) adds what the replay report reads, and **`--quality`**
//! (implies it) also what its quality section reads ([`crate::samples`]). Journeys are journey
//! keys as in effects; times are instants; lists of journeys are in the feed's instance order
//! (insertion order, as `refresh` visits them).
//!
//! - `poll`, every poll before its step's effects (also a failed or undecodable one): the facts
//!   level's `poll` record (no `payload_error`), a decoded one also with `feeds`: its
//!   observations per feed;
//! - `predictor_sample`, on round multiples of 300 s (at the first tick at or after each, with
//!   the sample time as `at`), after that tick's effects and journey samples and before its
//!   snapshot, when the rail feed is replayed: `feed` and `journeys`, each running rail run
//!   (with parts) having an arrival event or a prediction: `journey`, `arrivals` (`[call index,
//!   the arrival interval's middle]` of each call with an arrival event) and `predicted`
//!   (`[predictor, call index, predicted arrival]` per predictor of the policy's `[rail]
//!   predictor` choices, in that order, that predicts: the anchor prediction, `propagate` the
//!   `sz` target with the current lateness, only where the call has a scheduled arrival; the
//!   arrival is that schedule time plus the lateness);
//! - `journey_sample` (`--quality`), after every tick's effects, per feed in sorted order:
//!   `feed` and `journeys`, every journey of the feed with `journey`, `trip_id`, `mode`,
//!   `lifecycle`, `stale`, `lost`, `rail` (has parts), `lead` (has a lead vehicle), `progress`
//!   (null or `call`: the index reached, `along_m`), `track` (null or `seen_at`, `at_stop`: at a
//!   call and not seen away, `lateness_s`, `hypotheses`: has any, `off_path`: the best is held
//!   off the path), `fix` (null unless a track with `seen_at`, a lead vehicle with a position
//!   and a path of two points or more; else distances from that position: `best_m` to the best
//!   hypothesis's point or null, `first_m` and `last_m` to the first and last call's point,
//!   `near`: the calls `call - 1 ..= call + 2` within the policy's departure margin), `n`
//!   (number of calls), `calls` (`[index, call]` of each call that differs from the journey's
//!   previous sample, every call on its first or when `n` changed; a call: `arrival`,
//!   `departure` (the event intervals' middles), `estimated_arrival`, `estimated_departure`,
//!   `passed_arrival`, `passed_departure`, `passenger`, `status`), `plan` (on the first sample
//!   and when it changed, else null: `scheduled` `[[arrival, departure], ...]`, `zones`
//!   `[[arrival_m, departure_m], ...]`, `calls_m`, `shaped`, `points`: how many path points). A
//!   journey missing from its feed's sample is forgotten, so it comes back whole. Floats here
//!   are exact (`canonical`'s exact kinds).

use std::fs;
use std::io::{self, BufRead, BufReader, BufWriter, Read, Write};
use std::path::{Path, PathBuf};
use std::process::ExitCode;

use clap::Args;
use obehy_core::config::Channel;
use obehy_core::config::manifest::{self, RECORDER_FILTERS};
use obehy_core::model::FACT_SCHEMA_VERSION;
use obehy_core::model::json::facts_to_json;
use obehy_core::time::{Date, Instant, TimeDelta, iso, wall_instant};
use obehy_sources::Decoders;
use serde_json::Value;

use crate::archive::{ArchivedPoll, iter_polls};
use crate::canonical::{line, obj, text, time};
use crate::settings::Paths;

pub const COMPARE_VERSION: i64 = 1;
/// The sources compared when none are named.
pub const SOURCES: [&str; 3] = ["duk", "sz-mapa", "sz-tabule"];

/// The window of a replay: the archive, its channels and the time span, and the release.
#[derive(Debug, Clone, Args)]
pub struct WindowArgs {
    /// Release directory (needed by --level output and replay).
    #[arg(long = "from-files")]
    pub release: Option<PathBuf>,
    #[arg(long = "from")]
    pub start: String,
    #[arg(long = "to")]
    pub end: String,
    #[arg(long, default_value = "data/rt-raw")]
    pub archive: PathBuf,
    /// Comma-separated source IDs (default: duk,sz-mapa,sz-tabule).
    #[arg(long)]
    pub sources: Option<String>,
    #[arg(long, default_value = "jdf,czptt")]
    pub feeds: String,
    /// Connector manifests (default: the repository's).
    #[arg(long)]
    pub manifest: Option<PathBuf>,
    /// From this Prague wall time (`YYYY-MM-DDTHH:MM`; an explicit offset is kept).
    #[arg(long)]
    pub since: Option<String>,
    /// And before this local time.
    #[arg(long)]
    pub until: Option<String>,
    /// Also feeds not live yet.
    #[arg(long = "all-feeds")]
    pub all_feeds: bool,
    /// Threads decoding payloads ahead of the core (1: inline).
    #[arg(long, default_value_t = 1)]
    pub threads: usize,
}

#[derive(Debug, Clone, Args)]
pub struct CompareArgs {
    /// `facts` (connectors only) or `output` (everything the worker writes).
    #[arg(long)]
    pub level: String,
    #[command(flatten)]
    pub window: WindowArgs,
    /// State tables and GTFS-RT every so often (output; 0: only at the end).
    #[arg(long = "gtfs-rt-every", default_value_t = 60)]
    pub gtfs_rt_every: i64,
    /// JSON lines (`.gz`: gzip).
    #[arg(long)]
    pub out: PathBuf,
    /// Then diff against this reference.
    #[arg(long)]
    pub check: Option<PathBuf>,
    #[arg(long, default_value_t = 10)]
    pub limit: usize,
    /// `KIND.FIELD` left out of the --check comparison (e.g. `payload_error.message`).
    #[arg(long)]
    pub ignore: Vec<String>,
    /// Output: also the records the replay report reads (`poll`, `predictor_sample`).
    #[arg(long)]
    pub diagnostics: bool,
    /// Output: also `journey_sample` per tick and feed (implies --diagnostics).
    #[arg(long)]
    pub quality: bool,
}

/// `--feeds`: comma-separated feed names, in the order given.
pub fn parse_feeds(text: &str) -> Result<Vec<obehy_core::model::Feed>, String> {
    text.split(',')
        .map(str::trim)
        .filter(|f| !f.is_empty())
        .map(|f| f.parse().map_err(|_| format!("unknown feed {f:?}")))
        .collect()
}

fn fail(message: impl std::fmt::Display) -> ExitCode {
    eprintln!("error: {message}");
    ExitCode::FAILURE
}

/// `_local_time`: a Prague wall time from the command line, or an aware one.
pub fn local_time(text: &str) -> Result<Instant, String> {
    let parsed = iso::parse_datetime(text).map_err(|e| e.0)?;
    match parsed.offset_us {
        Some(_) => parsed.instant().map_err(|e| e.0),
        None => Ok(wall_instant(parsed.wall)),
    }
}

/// The selected window.
#[derive(Debug, Clone)]
pub struct Window {
    pub archive: PathBuf,
    pub start: Date,
    pub end: Date,
    pub channels: Vec<Channel>,
    pub since: Option<Instant>,
    pub until: Option<Instant>,
    pub all_feeds: bool,
}

/// What a replay of a window needs: the policy, every manifest, the decoders and the window.
#[derive(Debug)]
pub struct Setup {
    pub policy: obehy_core::config::Policy,
    pub sources: Vec<obehy_core::config::Source>,
    pub decoders: Decoders,
    pub window: Window,
}

/// The policy, manifests, decoders and window the arguments name.
pub fn setup(args: &WindowArgs) -> Result<Setup, String> {
    let paths = Paths::discover().map_err(|e| e.to_string())?;
    let policy = paths.load_policy().map_err(|e| e.to_string())?;
    let manifest_dir = args.manifest.clone().unwrap_or_else(|| paths.sources());
    let sources =
        manifest::load_sources(&manifest_dir, &RECORDER_FILTERS).map_err(|e| e.to_string())?;
    let package = paths.root.join("src/obehy");
    let files = |name: &str| fs::read_to_string(package.join(name)).map_err(|e| e.to_string());
    let decoders = Decoders::new(&sources, &files).map_err(|e| e.to_string())?;
    let all: Vec<Channel> = sources.iter().flat_map(|s| s.channels.clone()).collect();
    let wanted: Vec<String> = match &args.sources {
        Some(list) => list
            .split(',')
            .map(|s| s.trim().to_owned())
            .filter(|s| !s.is_empty())
            .collect(),
        None => SOURCES.iter().map(|s| (*s).to_owned()).collect(),
    };
    let wanted: Vec<&str> = wanted.iter().map(String::as_str).collect();
    let selected = manifest::select_channels(&all, &wanted).map_err(|e| e.to_string())?;
    let channels: Vec<Channel> = selected
        .into_iter()
        .filter(|c| !decoders.is_alert_channel(&c.source, &c.channel))
        .collect();
    let parse_day = |t: &str| Date::from_isoformat(t).map_err(|e| e.0);
    let (start, end) = (parse_day(&args.start)?, parse_day(&args.end)?);
    let since = args.since.as_deref().map(local_time).transpose()?;
    let until = args.until.as_deref().map(local_time).transpose()?;
    let window = Window {
        archive: args.archive.clone(),
        start,
        end,
        channels,
        since,
        until,
        all_feeds: args.all_feeds,
    };
    Ok(Setup {
        policy,
        sources,
        decoders,
        window,
    })
}

pub fn run_command(args: &CompareArgs) -> ExitCode {
    if args.level != "facts" && args.level != "output" {
        return fail(format!(
            "--level must be facts or output, not {:?}",
            args.level
        ));
    }
    if args.level == "output" && args.window.release.is_none() {
        return fail("--level output needs --from-files <release dir>");
    }
    let Setup {
        policy,
        sources,
        decoders,
        window,
    } = match setup(&args.window) {
        Ok(s) => s,
        Err(e) => return fail(e),
    };
    let written = if args.level == "output" {
        let feeds = match parse_feeds(&args.window.feeds) {
            Ok(f) => f,
            Err(e) => return fail(e),
        };
        let options = crate::output::Options {
            release: args.window.release.clone().expect("checked above"),
            feeds,
            gtfs_rt_every_s: Some(args.gtfs_rt_every).filter(|e| *e != 0),
            threads: args.window.threads,
            diagnostics: args.diagnostics,
            quality: args.quality,
        };
        write_stream(&args.out, |emit| {
            crate::output::output(&window, &options, &decoders, &sources, policy, emit)
        })
    } else {
        write_stream(&args.out, |emit| {
            facts(&window, &decoders, policy.time.max_clock_skew(), emit)
        })
    };
    let count = match written {
        Ok(n) => n,
        Err(e) => return fail(e),
    };
    println!("{count} records in {}", args.out.display());
    let Some(reference) = &args.check else {
        return ExitCode::SUCCESS;
    };
    match diff(reference, &args.out, args.limit, &args.ignore) {
        Ok((same, report)) => {
            println!("{}", report.join("\n"));
            if same {
                ExitCode::SUCCESS
            } else {
                ExitCode::FAILURE
            }
        }
        Err(e) => fail(e),
    }
}

/// `merged_polls`: the archived polls of the channels in reception order, exactly as
/// `heapq.merge` over the per-channel streams (sorted by `(source, channel)`) with the key
/// `(received_at, source, channel)`.
pub fn merged_polls(
    window: &Window,
) -> impl Iterator<Item = io::Result<(Instant, String, String, ArchivedPoll)>> {
    let mut keys: Vec<(String, String)> = window
        .channels
        .iter()
        .map(|c| (c.source.clone(), c.channel.clone()))
        .collect();
    keys.sort();
    let mut streams: Vec<_> = keys
        .into_iter()
        .map(|(source, channel)| {
            let polls = iter_polls(&window.archive, &source, &channel, window.start, window.end);
            (source, channel, polls.peekable_poll())
        })
        .collect();
    std::iter::from_fn(move || {
        let mut best: Option<(usize, Instant)> = None;
        for (i, (_, _, stream)) in streams.iter_mut().enumerate() {
            let head = match stream.head() {
                Ok(Some(at)) => at,
                Ok(None) => continue,
                Err(e) => return Some(Err(e)),
            };
            // Ties go to the earlier stream (heapq.merge's order index); the key's source and
            // channel only tie within one stream.
            if best.is_none_or(|(_, b)| head < b) {
                best = Some((i, head));
            }
        }
        let (i, at) = best?;
        let (source, channel, stream) = &mut streams[i];
        let poll = stream.take().expect("a head");
        Some(Ok((at, source.clone(), channel.clone(), poll)))
    })
}

/// A stream of archived polls with its next poll's reception time read ahead.
#[derive(Debug)]
pub struct Peeked<I> {
    inner: I,
    next: Option<(Instant, ArchivedPoll)>,
    done: bool,
}

pub trait PeekPolls: Sized {
    fn peekable_poll(self) -> Peeked<Self>;
}

impl<I> PeekPolls for I
where
    I: Iterator<Item = Result<ArchivedPoll, crate::archive::ArchiveError>>,
{
    fn peekable_poll(self) -> Peeked<Self> {
        Peeked {
            inner: self,
            next: None,
            done: false,
        }
    }
}

impl<I> Peeked<I>
where
    I: Iterator<Item = Result<ArchivedPoll, crate::archive::ArchiveError>>,
{
    fn head(&mut self) -> io::Result<Option<Instant>> {
        if self.next.is_none() && !self.done {
            match self.inner.next() {
                None => self.done = true,
                Some(Err(e)) => return Err(io::Error::other(e.to_string())),
                Some(Ok(poll)) => {
                    let at = poll
                        .received_at()
                        .map_err(|e| io::Error::other(e.to_string()))?;
                    self.next = Some((at, poll));
                }
            }
        }
        Ok(self.next.as_ref().map(|(at, _)| *at))
    }

    fn take(&mut self) -> Option<ArchivedPoll> {
        self.next.take().map(|(_, p)| p)
    }
}

/// The stream's first record; the output level adds its fields.
pub fn header(window: &Window, level: &str) -> Value {
    let mut names: Vec<String> = window.channels.iter().map(Channel::name).collect();
    names.sort();
    obj([
        ("kind", text("header")),
        ("compare_version", Value::from(COMPARE_VERSION)),
        ("level", text(level)),
        (
            "channels",
            Value::Array(names.into_iter().map(Value::String).collect()),
        ),
        ("from", text(window.start.isoformat())),
        ("to", text(window.end.isoformat())),
        ("since", time(window.since)),
        ("until", time(window.until)),
        ("all_feeds", Value::Bool(window.all_feeds)),
        ("fact_schema_version", Value::from(FACT_SCHEMA_VERSION)),
    ])
}

/// The facts level.
pub fn facts(
    window: &Window,
    decoders: &Decoders,
    max_clock_skew: TimeDelta,
    emit: &mut dyn FnMut(&Value) -> io::Result<()>,
) -> io::Result<()> {
    emit(&header(window, "facts"))?;
    for found in merged_polls(window) {
        let (at, source, channel, poll) = found?;
        if window.until.is_some_and(|u| at >= u) {
            return Ok(());
        }
        if window.since.is_some_and(|s| at < s) {
            continue;
        }
        let entry = &poll.entry;
        let field = |k: &str| entry.get(k).map_or(Value::Null, Value::clone);
        let request = match entry.get("request") {
            Some(r @ Value::Object(_)) => r.clone(),
            _ => Value::Null,
        };
        let version = decoders.version(&source, &channel).ok_or_else(|| {
            io::Error::other(format!("no realtime connector decodes {source}/{channel}"))
        })?;
        let record = |outcome: &str| {
            obj([
                ("kind", text("poll")),
                ("at", time(Some(at))),
                ("source", text(&source)),
                ("channel", text(&channel)),
                ("sha256", field("sha256")),
                ("status", field("status")),
                ("error", field("error")),
                ("request", request.clone()),
                ("decoder_version", Value::from(version)),
                ("outcome", text(outcome)),
            ])
        };
        let body = poll.body().map_err(|e| io::Error::other(e.to_string()))?;
        let (Some(body), true) = (body, poll.succeeded()) else {
            emit(&record("failed"))?;
            continue;
        };
        let sha256 = entry
            .get("sha256")
            .and_then(Value::as_str)
            .unwrap_or_default();
        let decoded = decoders.decode(
            &source,
            &channel,
            &body,
            sha256,
            at,
            max_clock_skew,
            entry.get("request"),
        );
        let observations = match decoded {
            Ok(o) => o,
            Err(error) => {
                emit(&record("undecodable"))?;
                emit(&obj([
                    ("kind", text("payload_error")),
                    ("at", time(Some(at))),
                    ("message", text(&error.message)),
                ]))?;
                continue;
            }
        };
        let live = window
            .channels
            .iter()
            .find(|c| c.source == source && c.channel == channel)
            .expect("a selected channel");
        let observations: Vec<_> = observations
            .into_iter()
            .filter(|o| window.all_feeds || live.is_live(o.feed.as_str()))
            .collect();
        let mut fields = record("decoded");
        fields["observations"] = Value::from(observations.len());
        emit(&fields)?;
        for o in &observations {
            emit(&obj([
                ("kind", text("facts")),
                ("at", time(Some(at))),
                ("item", Value::from(o.raw.item)),
                ("feed", text(o.feed.as_str())),
                ("observed_at", time(o.observed_at)),
                ("decoder_version", Value::from(o.decoder_version)),
                ("facts", facts_to_json(&o.facts)),
            ]))?;
        }
    }
    Ok(())
}

/// Writes a stream of records to `out` (gzip for `.gz`, no name and mtime 0), through a
/// temporary file; the number of records.
pub fn write_stream(
    out: &Path,
    produce: impl FnOnce(&mut dyn FnMut(&Value) -> io::Result<()>) -> io::Result<()>,
) -> io::Result<usize> {
    if let Some(parent) = out.parent()
        && !parent.as_os_str().is_empty()
    {
        fs::create_dir_all(parent)?;
    }
    let name = out
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default();
    let temporary = out.with_file_name(format!(".partial-{name}"));
    let file = BufWriter::new(fs::File::create(&temporary)?);
    let mut writer: Box<dyn Write + Send> = if name.ends_with(".gz") {
        Box::new(
            flate2::GzBuilder::new()
                .mtime(0)
                .write(file, flate2::Compression::default()),
        )
    } else {
        Box::new(file)
    };
    // The lines are made here and compressed and written on a thread of their own, a buffer
    // at a time, in order.
    const BUFFER: usize = 1 << 20;
    let (send, receive) = std::sync::mpsc::sync_channel::<String>(8);
    let compressor = std::thread::spawn(move || -> io::Result<()> {
        for buffer in receive {
            writer.write_all(buffer.as_bytes())?;
        }
        writer.flush()
    });
    let mut count = 0;
    let mut buffer = String::with_capacity(BUFFER + 4096);
    let produced = produce(&mut |record: &Value| {
        count += 1;
        buffer.push_str(&line(record));
        if buffer.len() >= BUFFER {
            let full = std::mem::replace(&mut buffer, String::with_capacity(BUFFER + 4096));
            send.send(full)
                .map_err(|_| io::Error::other("the stream writer stopped"))?;
        }
        Ok(())
    });
    if produced.is_ok() && !buffer.is_empty() {
        // A failed send leaves the writer's own error to report below.
        let _ = send.send(buffer);
    }
    drop(send);
    let written = compressor
        .join()
        .map_err(|_| io::Error::other("the stream writer panicked"))?;
    produced?;
    written?;
    fs::rename(&temporary, out)?;
    Ok(count)
}

fn open_lines(path: &Path) -> io::Result<Box<dyn BufRead>> {
    let file = fs::File::open(path)?;
    let reader: Box<dyn Read> = if path.extension().is_some_and(|e| e == "gz") {
        Box::new(flate2::read::MultiGzDecoder::new(file))
    } else {
        Box::new(file)
    };
    Ok(Box::new(BufReader::new(reader)))
}

fn kind_of(record: &Value) -> String {
    let kind = record.get("kind").and_then(Value::as_str).unwrap_or("?");
    match record.get("table").and_then(Value::as_str) {
        Some(t) => format!("{kind}/{t}"),
        None => kind.to_owned(),
    }
}

/// Where two compare streams part: whether they are the same, ignoring `KIND.FIELD`s, and a
/// report.
pub fn diff(
    reference: &Path,
    candidate: &Path,
    limit: usize,
    ignore: &[String],
) -> io::Result<(bool, Vec<String>)> {
    let ignored: Vec<(String, String)> = ignore
        .iter()
        .filter_map(|i| i.split_once('.').map(|(k, f)| (k.to_owned(), f.to_owned())))
        .collect();
    let strip = |text: &str| -> Option<Value> {
        let mut v: Value = serde_json::from_str(text).ok()?;
        let kind = v
            .get("kind")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_owned();
        if let Value::Object(map) = &mut v {
            for (k, f) in &ignored {
                if *k == kind {
                    map.remove(f);
                }
            }
        }
        Some(v)
    };
    let mut a = open_lines(reference)?.lines();
    let mut b = open_lines(candidate)?.lines();
    let mut report = Vec::new();
    let (mut total, mut number, mut ra, mut rb) = (0_usize, 0_usize, 0_usize, 0_usize);
    loop {
        let (x, y) = (a.next().transpose()?, b.next().transpose()?);
        if x.is_none() && y.is_none() {
            break;
        }
        number += 1;
        ra += usize::from(x.is_some());
        rb += usize::from(y.is_some());
        if x == y {
            continue;
        }
        let (sx, sy) = (x.as_deref().and_then(strip), y.as_deref().and_then(strip));
        if sx.is_some() && sx == sy {
            continue;
        }
        total += 1;
        if total <= limit {
            let kind = sx.as_ref().or(sy.as_ref()).map(kind_of).unwrap_or_default();
            let short = |t: Option<&String>| match t {
                None => "(end of stream)".to_owned(),
                Some(t) if t.chars().count() > 400 => {
                    t.chars().take(400).collect::<String>() + "..."
                }
                Some(t) => t.clone(),
            };
            report.push(format!("line {number} ({kind})"));
            report.push(format!("  reference: {}", short(x.as_ref())));
            report.push(format!("  candidate: {}", short(y.as_ref())));
            report.push(String::new());
        }
    }
    report.insert(0, format!("records: reference {ra}, candidate {rb}"));
    report.push(if total == 0 {
        "same".to_owned()
    } else {
        format!("{total} records differ")
    });
    Ok((total == 0, report))
}
