//! The raw realtime archive: port of `realtime/archive.py`. Content-addressed zstd payloads with
//! a daily append-only index (BASE_PLAN.md section 18.4):
//!
//! ```text
//! <root>/<source>/<channel>/<YYYY-MM-DD>/index.jsonl     one line per poll, UTC date
//! <root>/<source>/<channel>/<YYYY-MM-DD>/objects/<sha256>.zst
//! ```
//!
//! Both implementations read each other's archive while both exist. For the same poll the
//! index line is byte-identical to Python's (`json.dumps(..., ensure_ascii=False,
//! separators=(",", ":"))`, the same key order) and so is the object: zstd level 10 with the
//! content size in the frame and no checksum, as `zstandard.ZstdCompressor(level=10).compress`
//! writes it, from the same libzstd (1.5.7 in both).

use std::collections::BTreeMap;
use std::fs;
use std::io::{self, Write as _};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};

use obehy_core::py::json::{DumpOptions, PyJson, dumps};
use obehy_core::time::{Date, Instant, TimeError};
use serde_json::{Map, Value};
use sha2::{Digest, Sha256};

pub const INDEX: &str = "index.jsonl";
pub const OBJECTS: &str = "objects";
pub const COMPRESSION_LEVEL: i32 = 10;

/// `datetime.isoformat(timespec="microseconds")` of an aware UTC time:
/// `2026-10-05T23:59:50.000000+00:00`.
pub fn timestamp(value: Instant) -> String {
    let utc = value.utc();
    let (hour, minute, second, micro) = utc.hms_micro();
    format!(
        "{}T{hour:02}:{minute:02}:{second:02}.{micro:06}+00:00",
        utc.date().isoformat()
    )
}

/// One request to a channel, successful or not.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Poll {
    pub requested_at: Instant,
    pub received_at: Instant,
    pub status: Option<u16>,
    pub body: Option<Vec<u8>>,
    pub content_type: Option<String>,
    /// The kept response headers (lower-case names); archived sorted.
    pub headers: BTreeMap<String, String>,
    pub error: Option<String>,
    /// What a demand-polled request was for (a board's station: `{"sr70": "534149"}`), or a
    /// delta cursor; archived with the poll, since the payload itself may not say.
    pub request: BTreeMap<String, String>,
}

impl Poll {
    /// A poll that was never sent, or failed without a response.
    pub fn failed(requested_at: Instant, received_at: Instant, error: impl Into<String>) -> Poll {
        Poll {
            requested_at,
            received_at,
            status: None,
            body: None,
            content_type: None,
            headers: BTreeMap::new(),
            error: Some(error.into()),
            request: BTreeMap::new(),
        }
    }

    pub fn ok(&self) -> bool {
        self.error.is_none() && self.status.is_some_and(|s| (200..300).contains(&s))
    }
}

/// Whether a poll succeeded, live or archived: no transport error and a 2xx status.
pub fn succeeded(status: &Value, error: &Value) -> bool {
    error.is_null() && status.as_i64().is_some_and(|s| (200..300).contains(&s))
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Stored {
    pub sha256: Option<String>,
    pub new_object_bytes: usize,
}

pub fn day_directory(root: &Path, source: &str, channel: &str, day: Date) -> PathBuf {
    root.join(source).join(channel).join(day.isoformat())
}

/// The entries of `directory` that glob's `*` matches (hidden names excluded), sorted by name.
fn glob_star(directory: &Path) -> Vec<(String, PathBuf)> {
    let Ok(entries) = fs::read_dir(directory) else {
        return Vec::new();
    };
    let mut found: Vec<(String, PathBuf)> = entries
        .filter_map(Result::ok)
        .filter_map(|e| {
            let name = e.file_name().to_string_lossy().into_owned();
            (!name.starts_with('.')).then(|| (name, e.path()))
        })
        .collect();
    found.sort();
    found
}

/// `root.glob(f"{source}/*/*")`, `source` literal or every source.
fn day_candidates(root: &Path, source: Option<&str>) -> Vec<(String, PathBuf)> {
    let sources: Vec<PathBuf> = match source {
        Some(name) => vec![root.join(name)],
        None => glob_star(root)
            .into_iter()
            .map(|(_, p)| p)
            .filter(|p| p.is_dir())
            .collect(),
    };
    let mut out = Vec::new();
    for source in sources {
        for (_, channel) in glob_star(&source) {
            if channel.is_dir() {
                out.extend(glob_star(&channel));
            }
        }
    }
    out
}

/// The UTC days any source or channel has a directory of, oldest first.
pub fn days(root: &Path) -> Vec<Date> {
    let mut found: Vec<Date> = day_candidates(root, None)
        .into_iter()
        .filter_map(|(name, path)| {
            let day = Date::from_isoformat(&name).ok()?;
            path.is_dir().then_some(day)
        })
        .collect();
    found.sort();
    found.dedup();
    found
}

/// The sources with a directory in the archive.
pub fn sources(root: &Path) -> Vec<String> {
    glob_star(root)
        .into_iter()
        .filter(|(_, p)| p.is_dir())
        .map(|(name, _)| name)
        .collect()
}

/// Delete the day directories of every channel of `source` (every source with None) older than
/// `before`; returns them. Only `<root>/<source>/<channel>/<YYYY-MM-DD>` directories are touched.
pub fn prune(root: &Path, before: Date, source: Option<&str>) -> io::Result<Vec<PathBuf>> {
    let mut candidates = day_candidates(root, source);
    candidates.sort_by(|a, b| a.1.cmp(&b.1));
    let mut pruned = Vec::new();
    for (name, directory) in candidates {
        let Ok(day) = Date::from_isoformat(&name) else {
            continue;
        };
        if directory.is_dir() && day < before {
            fs::remove_dir_all(&directory)?;
            pruned.push(directory);
        }
    }
    Ok(pruned)
}

/// A unique sibling of `destination` for an atomic write (`atomic_output_path`).
fn temporary_sibling(destination: &Path) -> PathBuf {
    static COUNTER: AtomicU64 = AtomicU64::new(0);
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0, |d| d.as_nanos());
    let unique = format!(
        "{:x}{:x}{:x}",
        std::process::id(),
        nanos,
        COUNTER.fetch_add(1, Ordering::Relaxed)
    );
    let name = destination
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default();
    destination.with_file_name(format!(".{name}.{unique}.part"))
}

/// Write `bytes` to `destination` through a temporary sibling, renamed into place.
pub fn write_atomic(destination: &Path, bytes: &[u8]) -> io::Result<()> {
    if let Some(parent) = destination.parent() {
        fs::create_dir_all(parent)?;
    }
    let temporary = temporary_sibling(destination);
    let result = fs::write(&temporary, bytes).and_then(|()| fs::rename(&temporary, destination));
    if result.is_err() {
        let _ = fs::remove_file(&temporary);
    }
    result
}

/// The sha256 of a payload, lower-case hex.
pub fn sha256_hex(bytes: &[u8]) -> String {
    let digest = Sha256::digest(bytes);
    let mut text = String::with_capacity(64);
    for byte in digest {
        text.push_str(&format!("{byte:02x}"));
    }
    text
}

/// `round(microseconds / 1000)`: half to even, as Python rounds the float quotient.
pub fn elapsed_ms(requested_at: Instant, received_at: Instant) -> i64 {
    let micros = received_at.micros() - requested_at.micros();
    let (quotient, remainder) = (micros.div_euclid(1000), micros.rem_euclid(1000));
    match remainder.cmp(&500) {
        std::cmp::Ordering::Greater => quotient + 1,
        std::cmp::Ordering::Equal if quotient % 2 != 0 => quotient + 1,
        _ => quotient,
    }
}

fn string_map(map: &BTreeMap<String, String>) -> PyJson {
    PyJson::Object(
        map.iter()
            .map(|(k, v)| (k.clone(), PyJson::str(v.as_str())))
            .collect(),
    )
}

/// The index line of a poll (without its newline): Python's text, key for key.
pub fn index_line(
    poll: &Poll,
    sha256: Option<&str>,
    body: Option<&[u8]>,
    extra: &[(String, PyJson)],
) -> String {
    let mut entry = vec![
        (
            "requested_at".to_owned(),
            PyJson::Str(timestamp(poll.requested_at)),
        ),
        (
            "received_at".to_owned(),
            PyJson::Str(timestamp(poll.received_at)),
        ),
        (
            "elapsed_ms".to_owned(),
            PyJson::Int(elapsed_ms(poll.requested_at, poll.received_at)),
        ),
        (
            "status".to_owned(),
            PyJson::opt(poll.status, |s| PyJson::Int(i64::from(s))),
        ),
        ("sha256".to_owned(), PyJson::opt(sha256, PyJson::str)),
        (
            "bytes".to_owned(),
            PyJson::opt(body, |b| PyJson::Int(b.len() as i64)),
        ),
        (
            "content_type".to_owned(),
            PyJson::opt(poll.content_type.as_deref(), PyJson::str),
        ),
        ("headers".to_owned(), string_map(&poll.headers)),
        (
            "error".to_owned(),
            PyJson::opt(poll.error.as_deref(), PyJson::str),
        ),
    ];
    // `{**entry, **extra}`: a repeated key keeps its first position and takes the new value.
    for (key, value) in extra {
        match entry.iter_mut().find(|(k, _)| k == key) {
            Some(slot) => slot.1 = value.clone(),
            None => entry.push((key.clone(), value.clone())),
        }
    }
    dumps(&PyJson::Object(entry), DumpOptions::COMPACT.utf8())
}

/// Appends polls; each write opens the index anew, so days roll over without state.
#[derive(Debug, Clone)]
pub struct ArchiveWriter {
    pub root: PathBuf,
}

impl ArchiveWriter {
    pub fn new(root: impl Into<PathBuf>) -> ArchiveWriter {
        ArchiveWriter { root: root.into() }
    }

    /// Store `body` (the payload as archived) and one index line describing `poll`.
    pub fn append(
        &self,
        source: &str,
        channel: &str,
        poll: &Poll,
        body: Option<&[u8]>,
        extra: &[(String, PyJson)],
    ) -> io::Result<Stored> {
        let directory = day_directory(&self.root, source, channel, poll.received_at.utc().date());
        let mut sha256 = None;
        let mut written = 0;
        if let Some(body) = body {
            let hash = sha256_hex(body);
            let blob = directory.join(OBJECTS).join(format!("{hash}.zst"));
            if !blob.exists() {
                let compressed = zstd::bulk::compress(body, COMPRESSION_LEVEL)?;
                write_atomic(&blob, &compressed)?;
                written = compressed.len();
            }
            sha256 = Some(hash);
        }
        let mut line = index_line(poll, sha256.as_deref(), body, extra);
        line.push('\n');
        fs::create_dir_all(&directory)?;
        let mut index = fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(directory.join(INDEX))?;
        index.write_all(line.as_bytes())?;
        Ok(Stored {
            sha256,
            new_object_bytes: written,
        })
    }
}

/// An archive that cannot be read.
#[derive(Debug, thiserror::Error)]
pub enum ArchiveError {
    #[error("{path}: {source}")]
    Io { path: PathBuf, source: io::Error },
    #[error("{path}: not UTF-8")]
    Utf8 { path: PathBuf },
    #[error("{path} line {line}: {message}")]
    Entry {
        path: PathBuf,
        line: usize,
        message: String,
    },
    #[error("{0}")]
    Time(#[from] TimeError),
}

/// One archived poll: its day directory and its index entry.
#[derive(Debug, Clone, PartialEq)]
pub struct ArchivedPoll {
    pub directory: Arc<Path>,
    pub entry: Map<String, Value>,
}

impl ArchivedPoll {
    fn text(&self, key: &str) -> Option<&str> {
        self.entry.get(key).and_then(Value::as_str)
    }

    pub fn received_at(&self) -> Result<Instant, ArchiveError> {
        Ok(Instant::from_isoformat(
            self.text("received_at").unwrap_or_default(),
        )?)
    }

    pub fn requested_at(&self) -> Result<Instant, ArchiveError> {
        Ok(Instant::from_isoformat(
            self.text("requested_at").unwrap_or_default(),
        )?)
    }

    pub fn sha256(&self) -> Option<&str> {
        self.text("sha256")
    }

    pub fn status(&self) -> Option<i64> {
        self.entry.get("status").and_then(Value::as_i64)
    }

    pub fn error(&self) -> Option<&str> {
        self.text("error")
    }

    pub fn succeeded(&self) -> bool {
        succeeded(
            self.entry.get("status").unwrap_or(&Value::Null),
            self.entry.get("error").unwrap_or(&Value::Null),
        )
    }

    /// The archived request (`{"sr70": ...}`, a cursor), empty when none was kept.
    pub fn request(&self) -> BTreeMap<String, String> {
        match self.entry.get("request") {
            Some(Value::Object(map)) => map
                .iter()
                .map(|(k, v)| {
                    let text = v.as_str().map_or_else(|| v.to_string(), str::to_owned);
                    (k.clone(), text)
                })
                .collect(),
            _ => BTreeMap::new(),
        }
    }

    pub fn object_path(&self) -> Option<PathBuf> {
        self.sha256()
            .map(|sha| self.directory.join(OBJECTS).join(format!("{sha}.zst")))
    }

    /// The stored payload, None for a poll without one.
    pub fn body(&self) -> Result<Option<Vec<u8>>, ArchiveError> {
        let Some(blob) = self.object_path() else {
            return Ok(None);
        };
        let compressed = fs::read(&blob).map_err(|source| ArchiveError::Io {
            path: blob.clone(),
            source,
        })?;
        zstd::decode_all(compressed.as_slice())
            .map(Some)
            .map_err(|source| ArchiveError::Io { path: blob, source })
    }
}

/// Archived polls of the UTC days `start..=end`, in recording order (`iter_polls`). A truncated
/// last index line (the recorder was killed mid-write) is skipped; any other unreadable line is
/// an error.
pub fn iter_polls(root: &Path, source: &str, channel: &str, start: Date, end: Date) -> IterPolls {
    IterPolls {
        root: root.to_path_buf(),
        source: source.to_owned(),
        channel: channel.to_owned(),
        day: start,
        end,
        current: None,
    }
}

#[derive(Debug)]
struct DayLines {
    directory: Arc<Path>,
    index: PathBuf,
    lines: Vec<String>,
    next: usize,
}

#[derive(Debug)]
pub struct IterPolls {
    root: PathBuf,
    source: String,
    channel: String,
    day: Date,
    end: Date,
    current: Option<DayLines>,
}

impl IterPolls {
    /// The next day's index lines, read whole (`Path.read_text`: universal newlines).
    fn open_day(&mut self) -> Result<Option<DayLines>, ArchiveError> {
        while self.day <= self.end {
            let directory = day_directory(&self.root, &self.source, &self.channel, self.day);
            self.day = self.day.add_days(1);
            let index = directory.join(INDEX);
            if !index.is_file() {
                continue;
            }
            let bytes = fs::read(&index).map_err(|source| ArchiveError::Io {
                path: index.clone(),
                source,
            })?;
            let text = String::from_utf8(bytes).map_err(|_| ArchiveError::Utf8 {
                path: index.clone(),
            })?;
            let text = if text.contains('\r') {
                text.replace("\r\n", "\n").replace('\r', "\n")
            } else {
                text
            };
            let lines = text.split('\n').map(str::to_owned).collect();
            return Ok(Some(DayLines {
                directory: Arc::from(directory.as_path()),
                index,
                lines,
                next: 0,
            }));
        }
        Ok(None)
    }
}

impl Iterator for IterPolls {
    type Item = Result<ArchivedPoll, ArchiveError>;

    fn next(&mut self) -> Option<Self::Item> {
        loop {
            if self.current.is_none() {
                match self.open_day() {
                    Ok(Some(day)) => self.current = Some(day),
                    Ok(None) => return None,
                    Err(error) => return Some(Err(error)),
                }
            }
            let day = self.current.as_mut().expect("a day is open");
            while day.next < day.lines.len() {
                let number = day.next;
                day.next += 1;
                let line = &day.lines[number];
                if line.is_empty() {
                    continue;
                }
                match serde_json::from_str::<Value>(line) {
                    Ok(Value::Object(entry)) => {
                        return Some(Ok(ArchivedPoll {
                            directory: Arc::clone(&day.directory),
                            entry,
                        }));
                    }
                    Ok(_) => {
                        return Some(Err(ArchiveError::Entry {
                            path: day.index.clone(),
                            line: number + 1,
                            message: "not a JSON object".to_owned(),
                        }));
                    }
                    Err(_) if number == day.lines.len() - 1 => break,
                    Err(error) => {
                        return Some(Err(ArchiveError::Entry {
                            path: day.index.clone(),
                            line: number + 1,
                            message: error.to_string(),
                        }));
                    }
                }
            }
            self.current = None;
        }
    }
}
