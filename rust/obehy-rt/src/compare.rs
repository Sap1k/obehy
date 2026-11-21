//! `obehy-rt compare`: the exact record of a replay window, the same stream as Python's
//! `obehy rt compare` (`realtime/compare.py`; docs/RUST_PORT.md, R0 item 1) from the same
//! arguments. Only `--level facts` so far (R2): every archived poll of the window decoded by
//! its connector, in `merged_polls` reception order, as `poll`, `payload_error` and `facts`
//! records in the canonical JSON of [`crate::canonical`]. `--level output` comes with the core
//! (R6).

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
use obehy_sources::reader::json::{Json, Object};
use serde_json::Value;

use crate::archive::{ArchivedPoll, iter_polls};
use crate::canonical::{C, line};
use crate::settings::Paths;

pub const COMPARE_VERSION: i64 = 1;
/// `replay.SOURCES`: the sources compared when none are named.
pub const SOURCES: [&str; 3] = ["duk", "sz-mapa", "sz-tabule"];

#[derive(Debug, Clone, Args)]
pub struct CompareArgs {
    /// `facts` (connectors only) or `output` (not yet: R6).
    #[arg(long)]
    pub level: String,
    /// Release directory (needed by --level output).
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

pub fn run_command(args: &CompareArgs) -> ExitCode {
    if args.level != "facts" && args.level != "output" {
        return fail(format!(
            "--level must be facts or output, not {:?}",
            args.level
        ));
    }
    if args.level == "output" {
        if args.release.is_none() {
            return fail("--level output needs --from-files <release dir>");
        }
        return fail("--level output is not implemented yet (R6)");
    }
    let paths = match Paths::discover() {
        Ok(p) => p,
        Err(e) => return fail(e),
    };
    let policy = match paths.load_policy() {
        Ok(p) => p,
        Err(e) => return fail(e),
    };
    let manifest_dir = args.manifest.clone().unwrap_or_else(|| paths.sources());
    let sources = match manifest::load_sources(&manifest_dir, &RECORDER_FILTERS) {
        Ok(s) => s,
        Err(e) => return fail(e),
    };
    let package = paths.root.join("src/obehy");
    let files = |name: &str| fs::read_to_string(package.join(name)).map_err(|e| e.to_string());
    let decoders = match Decoders::new(&sources, &files) {
        Ok(d) => d,
        Err(e) => return fail(e),
    };
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
    let selected = match manifest::select_channels(&all, &wanted) {
        Ok(c) => c,
        Err(e) => return fail(e),
    };
    let channels: Vec<Channel> = selected
        .into_iter()
        .filter(|c| !decoders.is_alert_channel(&c.source, &c.channel))
        .collect();
    let parse_day = |t: &str| Date::from_isoformat(t).map_err(|e| e.0);
    let (start, end) = match (parse_day(&args.start), parse_day(&args.end)) {
        (Ok(s), Ok(e)) => (s, e),
        (Err(e), _) | (_, Err(e)) => return fail(e),
    };
    let since = match args.since.as_deref().map(local_time).transpose() {
        Ok(v) => v,
        Err(e) => return fail(e),
    };
    let until = match args.until.as_deref().map(local_time).transpose() {
        Ok(v) => v,
        Err(e) => return fail(e),
    };
    let window = Window {
        archive: args.archive.clone(),
        start,
        end,
        channels,
        since,
        until,
        all_feeds: args.all_feeds,
    };
    let count = match write_stream(&args.out, |emit| {
        facts(&window, &decoders, policy.time.max_clock_skew(), emit)
    }) {
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

/// A JSON value of an index line as the decoders read it.
pub fn json_of(value: &Value) -> Json {
    match value {
        Value::Null => Json::Null,
        Value::Bool(b) => Json::Bool(*b),
        Value::Number(n) => n.as_i64().map_or_else(
            || {
                n.as_u64().map_or_else(
                    || Json::Float(n.as_f64().unwrap_or(f64::NAN)),
                    |u| Json::BigInt(u.to_string().into()),
                )
            },
            Json::Int,
        ),
        Value::String(s) => Json::Str(s.clone()),
        Value::Array(items) => Json::Array(items.iter().map(json_of).collect()),
        Value::Object(map) => {
            let mut object = Object::default();
            for (k, v) in map {
                object.insert(k.clone(), json_of(v));
            }
            Json::Object(object)
        }
    }
}

fn header(window: &Window, level: &str) -> C {
    let mut names: Vec<String> = window.channels.iter().map(Channel::name).collect();
    names.sort();
    C::obj([
        ("kind", C::str("header")),
        ("compare_version", C::Int(i128::from(COMPARE_VERSION))),
        ("level", C::str(level)),
        ("channels", C::List(names.into_iter().map(C::Str).collect())),
        ("from", C::str(window.start.isoformat())),
        ("to", C::str(window.end.isoformat())),
        ("since", C::time(window.since)),
        ("until", C::time(window.until)),
        ("all_feeds", C::Bool(window.all_feeds)),
        (
            "fact_schema_version",
            C::Int(i128::from(FACT_SCHEMA_VERSION)),
        ),
    ])
}

/// The facts level (`compare.facts`).
pub fn facts(
    window: &Window,
    decoders: &Decoders,
    max_clock_skew: TimeDelta,
    emit: &mut dyn FnMut(&C) -> io::Result<()>,
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
        let field = |k: &str| entry.get(k).map_or(C::Null, C::from_json);
        let request = match entry.get("request") {
            Some(r @ Value::Object(_)) => C::from_json(r),
            _ => C::Null,
        };
        let version = decoders.version(&source, &channel).ok_or_else(|| {
            io::Error::other(format!("no realtime connector decodes {source}/{channel}"))
        })?;
        let record = |outcome: &str| -> Vec<(String, C)> {
            vec![
                ("kind".into(), C::str("poll")),
                ("at".into(), C::time(Some(at))),
                ("source".into(), C::str(&source)),
                ("channel".into(), C::str(&channel)),
                ("sha256".into(), field("sha256")),
                ("status".into(), field("status")),
                ("error".into(), field("error")),
                ("request".into(), request.clone()),
                ("decoder_version".into(), C::Int(i128::from(version))),
                ("outcome".into(), C::str(outcome)),
            ]
        };
        let body = poll.body().map_err(|e| io::Error::other(e.to_string()))?;
        let (Some(body), true) = (body, poll.succeeded()) else {
            emit(&C::Obj(record("failed")))?;
            continue;
        };
        let sha256 = entry
            .get("sha256")
            .and_then(Value::as_str)
            .unwrap_or_default();
        let request_json = entry.get("request").map(json_of);
        let decoded = decoders.decode(
            &source,
            &channel,
            &body,
            sha256,
            at,
            max_clock_skew,
            request_json.as_ref(),
        );
        let observations = match decoded {
            Ok(o) => o,
            Err(error) => {
                emit(&C::Obj(record("undecodable")))?;
                emit(&C::obj([
                    ("kind", C::str("payload_error")),
                    ("at", C::time(Some(at))),
                    ("class", C::str(error.class)),
                    ("message", C::str(&error.message)),
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
        fields.push(("observations".into(), C::Int(observations.len() as i128)));
        emit(&C::Obj(fields))?;
        for o in &observations {
            emit(&C::obj([
                ("kind", C::str("facts")),
                ("at", C::time(Some(at))),
                ("item", C::Int(i128::from(o.raw.item))),
                ("feed", C::str(o.feed.as_str())),
                ("observed_at", C::time(o.observed_at)),
                ("decoder_version", C::Int(i128::from(o.decoder_version))),
                ("facts", C::from_py(&facts_to_json(&o.facts))),
            ]))?;
        }
    }
    Ok(())
}

/// Writes a stream of records to `out` (gzip for `.gz`, no name and mtime 0), through a
/// temporary file; the number of records.
pub fn write_stream(
    out: &Path,
    produce: impl FnOnce(&mut dyn FnMut(&C) -> io::Result<()>) -> io::Result<()>,
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
    let mut writer: Box<dyn Write> = if name.ends_with(".gz") {
        Box::new(
            flate2::GzBuilder::new()
                .mtime(0)
                .write(file, flate2::Compression::default()),
        )
    } else {
        Box::new(file)
    };
    let mut count = 0;
    produce(&mut |record: &C| {
        count += 1;
        writer.write_all(line(record).as_bytes())
    })?;
    writer.flush()?;
    drop(writer);
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

/// Where two compare streams part (`compare_check.diff`, simplified): whether they are the
/// same, ignoring `KIND.FIELD`s, and a report.
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
