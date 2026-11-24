//! Connector manifests, `data/realtime/sources/<source>.toml` (docs/R1_SLICE.md section 4), checked
//! key by key: unknown keys pass everywhere except `[channel.semantics]` (and a stray `egress`).
//! Patterns in `applies.keys` are compiled with the `regex` crate.

use std::path::Path;

use indexmap::IndexMap;
use regex::Regex;
use toml::{Table, Value};

use super::ConfigError;
use super::decode::{Decode, channel_decode, shared_decodes};
use crate::model::SourceSemantics;
use crate::time::Date;

pub const FEED_NAMES: [&str; 2] = ["jdf", "czptt"];
pub const POLL_KINDS: [&str; 2] = ["interval", "demand"];
pub const LOOKUP_KINDS: [&str; 1] = ["composition"];
pub const DEMAND_QUERIES: [&str; 1] = ["station_boards"];
pub const CAPABILITIES: [&str; 12] = [
    "vehicle_key",
    "trip_key",
    "line_ref",
    "destination",
    "position",
    "delay",
    "source_state",
    "stop_event",
    "next_stop",
    "platform",
    "alert",
    "composition",
];
/// The recorder's payload filters (`record.FILTERS`): what a channel's `filter` may name.
pub const RECORDER_FILTERS: [&str; 2] = ["arriva-express", "dpmd"];

/// A request header whose value is a named secret.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Auth {
    pub header: String,
    pub secret: String,
}

/// Delta polling: the last good poll's response header goes back as a query parameter.
#[derive(Debug, Clone, PartialEq)]
pub struct Cursor {
    /// Lower-cased.
    pub header: String,
    pub param: String,
    pub max_age_s: f64,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Channel {
    pub source: String,
    pub channel: String,
    pub method: String,
    pub url: String,
    pub interval_s: f64,
    pub timeout_s: f64,
    /// In document order.
    pub headers: IndexMap<String, String>,
    pub body: Option<Vec<u8>>,
    pub filter: Option<String>,
    pub backoff_after: i64,
    pub max_backoff_s: f64,
    pub feeds: Vec<String>,
    /// None: every feed is live.
    pub live_feeds: Option<Vec<String>>,
    pub capabilities: Vec<String>,
    pub core_semantics: SourceSemantics,
    pub proxied: bool,
    pub demand: Option<String>,
    pub auth: Option<Auth>,
    pub cursor: Option<Cursor>,
    /// `[channel.decode]` (or the shared `[decode.<name>]` it names): Rust only.
    pub decode: Option<Decode>,
}

impl Channel {
    pub fn is_live(&self, feed: &str) -> bool {
        self.live_feeds
            .as_ref()
            .is_none_or(|live| live.iter().any(|f| f == feed))
    }

    /// `source/channel`.
    pub fn name(&self) -> String {
        format!("{}/{}", self.source, self.channel)
    }
}

/// A token bucket: `per_minute` requests on average, at most `burst` at once.
#[derive(Debug, Clone, PartialEq)]
pub struct Rate {
    pub per_minute: f64,
    pub burst: i64,
}

/// Which trips a lookup answers for.
#[derive(Debug, Clone, Default)]
pub struct Applies {
    /// (namespace, pattern searched in the key), sorted by namespace.
    pub keys: Vec<(String, Regex)>,
    pub days_around: Option<i64>,
}

impl PartialEq for Applies {
    fn eq(&self, other: &Self) -> bool {
        self.days_around == other.days_around
            && self.keys.len() == other.keys.len()
            && self
                .keys
                .iter()
                .zip(&other.keys)
                .all(|(a, b)| a.0 == b.0 && a.1.as_str() == b.1.as_str())
    }
}

impl Applies {
    /// Why the lookup does not answer for a trip with these keys on the date; None if it does.
    pub fn refusal(
        &self,
        keys: &dyn Fn(&str) -> Option<String>,
        service_date: Date,
        today: Date,
    ) -> Option<String> {
        for (namespace, pattern) in &self.keys {
            match keys(namespace) {
                Some(key) if pattern.is_match(&key) => {}
                _ => return Some(format!("not a trip it answers for ({namespace})")),
            }
        }
        if let Some(days) = self.days_around
            && i64::from(service_date.days_since(today)).abs() > days
        {
            return Some(format!("answers only within {days} day(s) of today"));
        }
        None
    }
}

/// A lazy detail (a composition): asked for on demand, never polled.
#[derive(Debug, Clone, PartialEq)]
pub struct Lookup {
    pub source: String,
    pub name: String,
    pub kind: String,
    pub feeds: Vec<String>,
    pub request: Channel,
    pub ttl_s: f64,
    pub not_found_ttl_s: f64,
    pub retry_s: f64,
    pub rate: Rate,
    pub daily_budget: i64,
    pub applies: Applies,
}

/// One connector manifest.
#[derive(Debug, Clone, PartialEq)]
pub struct Source {
    pub source: String,
    pub title: String,
    pub covers: String,
    pub channels: Vec<Channel>,
    pub lookups: Vec<Lookup>,
    /// `{"default": true, **[vehicles].physical}`, in that order.
    pub physical_vehicles: IndexMap<String, bool>,
}

impl Source {
    /// Whether its vehicle IDs on `mode`'s journeys (None: unknown) are physical vehicles.
    pub fn physical(&self, mode: Option<&str>) -> bool {
        mode.and_then(|m| self.physical_vehicles.get(m).copied())
            .unwrap_or_else(|| {
                self.physical_vehicles
                    .get("default")
                    .copied()
                    .unwrap_or(true)
            })
    }

    /// The feeds any of its channels feeds live, in `FEED_NAMES` order.
    pub fn live_feeds(&self) -> Vec<&'static str> {
        FEED_NAMES
            .into_iter()
            .filter(|feed| {
                self.channels
                    .iter()
                    .any(|c| c.feeds.iter().any(|f| f == feed) && c.is_live(feed))
            })
            .collect()
    }
}

fn fail<T>(message: String) -> Result<T, ConfigError> {
    Err(ConfigError::Manifest(message))
}

/// A key name in messages.
fn quoted(key: &str) -> String {
    format!("'{key}'")
}

/// `^[a-z0-9][a-z0-9-]*$`.
fn identifier(text: &str) -> bool {
    let mut chars = text.chars();
    chars
        .next()
        .is_some_and(|c| c.is_ascii_lowercase() || c.is_ascii_digit())
        && chars.all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-')
}

fn table<'a>(t: &'a Table, key: &str, at: &str) -> Result<&'a Table, ConfigError> {
    match t.get(key) {
        Some(Value::Table(inner)) => Ok(inner),
        _ => fail(format!("{at}: {} must be a table", quoted(key))),
    }
}

fn string(t: &Table, key: &str, at: &str) -> Result<String, ConfigError> {
    match t.get(key) {
        Some(Value::String(s)) if !s.is_empty() => Ok(s.clone()),
        _ => fail(format!("{at}: {} must be a non-empty string", quoted(key))),
    }
}

fn positive(t: &Table, key: &str, at: &str) -> Result<f64, ConfigError> {
    let value = match t.get(key) {
        Some(Value::Integer(i)) => *i as f64,
        Some(Value::Float(x)) => *x,
        _ => f64::NAN,
    };
    if value > 0.0 {
        return Ok(value);
    }
    fail(format!("{at}: {} must be a positive number", quoted(key)))
}

fn strings(t: &Table, key: &str, at: &str, allowed: &[&str]) -> Result<Vec<String>, ConfigError> {
    let values: Vec<String> = match t.get(key) {
        None => Vec::new(),
        Some(Value::Array(items)) => {
            let mut out = Vec::new();
            for item in items {
                match item {
                    Value::String(s) => out.push(s.clone()),
                    _ => return fail(format!("{at}: {} must be a list of strings", quoted(key))),
                }
            }
            out
        }
        Some(_) => return fail(format!("{at}: {} must be a list of strings", quoted(key))),
    };
    let mut unknown: Vec<&String> = values
        .iter()
        .filter(|v| !allowed.contains(&v.as_str()))
        .collect();
    unknown.sort();
    unknown.dedup();
    if !unknown.is_empty() {
        let listed: Vec<String> = unknown.iter().map(|v| format!("'{v}'")).collect();
        return fail(format!("{at}: unknown {key} [{}]", listed.join(", ")));
    }
    Ok(values)
}

fn headers(request: &Table, at: &str) -> Result<IndexMap<String, String>, ConfigError> {
    match request.get("headers") {
        None => Ok(IndexMap::new()),
        Some(Value::Table(t)) => t
            .iter()
            .map(|(k, v)| match v {
                Value::String(s) => Ok((k.clone(), s.clone())),
                _ => fail(format!("{at}: request.headers must be a table of strings")),
            })
            .collect(),
        Some(_) => fail(format!("{at}: request.headers must be a table of strings")),
    }
}

fn core_semantics(t: &Table, at: &str) -> Result<SourceSemantics, ConfigError> {
    let semantics = match t.get("semantics") {
        None => return Ok(SourceSemantics::default()),
        Some(Value::Table(s)) => s,
        Some(_) => return fail(format!("{at}: semantics must be a table")),
    };
    let mut unknown: Vec<&String> = semantics
        .keys()
        .filter(|k| !SourceSemantics::FIELDS.contains(&k.as_str()))
        .collect();
    unknown.sort();
    if let Some(first) = unknown.first() {
        return fail(format!("{at}: unknown semantics.{first}"));
    }
    let mut flags = [false; 4];
    for (flag, name) in flags.iter_mut().zip(SourceSemantics::FIELDS) {
        *flag = match semantics.get(name) {
            None => false,
            Some(Value::Boolean(b)) => *b,
            Some(_) => return fail(format!("{at}: semantics.{name} must be true or false")),
        };
    }
    Ok(SourceSemantics {
        pre_trip_delay_is_elapsed: flags[0],
        pre_trip_after_end_is_stale: flags[1],
        unchanged_entry_is_no_news: flags[2],
        delay_floor_minute: flags[3],
        trip_key_feed: None,
    })
}

fn proxied(t: &Table, at: &str) -> Result<bool, ConfigError> {
    if t.contains_key("egress") {
        return fail(format!(
            "{at}: egress was replaced by proxied = true (one proxy pool)"
        ));
    }
    match t.get("proxied") {
        None => Ok(false),
        Some(Value::Boolean(b)) => Ok(*b),
        Some(_) => fail(format!("{at}: proxied must be true or false")),
    }
}

fn channel(
    source: &str,
    raw: &Value,
    at: &str,
    filters: &[&str],
    shared: &IndexMap<String, Decode>,
) -> Result<Channel, ConfigError> {
    let Value::Table(t) = raw else {
        return fail(format!("{at}: not a table"));
    };
    let name = string(t, "name", at)?;
    if !identifier(&name) {
        return fail(format!("{at}: '{name}' must be lowercase ASCII and dashes"));
    }
    let poll = table(t, "poll", at)?;
    let kind = poll.get("kind").and_then(Value::as_str);
    if !kind.is_some_and(|k| POLL_KINDS.contains(&k)) {
        return fail(format!(
            "{at}: poll.kind must be one of ('interval', 'demand')"
        ));
    }
    let demand = if kind == Some("demand") {
        match poll.get("query").and_then(Value::as_str) {
            Some(q) if DEMAND_QUERIES.contains(&q) => Some(q.to_owned()),
            _ => {
                return fail(format!(
                    "{at}: poll.query must be one of ('station_boards',)"
                ));
            }
        }
    } else {
        None
    };
    let backoff = table(t, "backoff", at)?;
    let request = table(t, "request", at)?;
    let method = string(request, "method", at)?;
    if method != "GET" && method != "POST" {
        return fail(format!("{at}: request.method must be GET or POST"));
    }
    let headers = headers(request, at)?;
    let body = match request.get("body") {
        None => None,
        Some(Value::String(s)) => Some(s.clone()),
        Some(_) => return fail(format!("{at}: request.body must be a string")),
    };
    if body.is_some() && method != "POST" {
        return fail(format!("{at}: only POST requests take a body"));
    }
    let filter = match t.get("filter") {
        None => None,
        Some(Value::String(f)) if filters.contains(&f.as_str()) => Some(f.clone()),
        Some(Value::String(other)) => return fail(format!("{at}: unknown filter '{other}'")),
        Some(other) => return fail(format!("{at}: unknown filter {other}")),
    };
    let mut core_semantics = core_semantics(t, at)?;
    let feeds = strings(t, "feeds", at, &FEED_NAMES)?;
    core_semantics.trip_key_feed = match t.get("trip_key_feed") {
        None => None,
        Some(Value::String(feed)) if feeds.contains(feed) => {
            Some(feed.parse().map_err(ConfigError::Manifest)?)
        }
        Some(_) => return fail(format!("{at}: trip_key_feed must be one of its feeds")),
    };
    let live_feeds = if t.contains_key("live_feeds") {
        let allowed: Vec<&str> = feeds.iter().map(String::as_str).collect();
        Some(strings(t, "live_feeds", at, &allowed)?)
    } else {
        None
    };
    let proxied = proxied(t, at)?;
    let auth = if t.contains_key("auth") {
        let a = table(t, "auth", at)?;
        let auth = Auth {
            header: string(a, "header", at)?,
            secret: string(a, "secret", at)?,
        };
        if !identifier(&auth.secret) {
            return fail(format!("{at}: auth.secret must be a lowercase ASCII name"));
        }
        Some(auth)
    } else {
        None
    };
    let cursor = if t.contains_key("cursor") {
        if method != "GET" || demand.is_some() {
            return fail(format!("{at}: only interval GET channels take a cursor"));
        }
        let c = table(t, "cursor", at)?;
        Some(Cursor {
            header: string(c, "header", at)?.to_lowercase(),
            param: string(c, "param", at)?,
            max_age_s: positive(c, "max_age_s", at)?,
        })
    } else {
        None
    };
    let url = string(request, "url", at)?;
    let decode = channel_decode(t.get("decode"), shared, at).map_err(ConfigError::Manifest)?;
    let interval_s = if demand.is_some() {
        60.0
    } else {
        positive(poll, "seconds", at)?
    };
    Ok(Channel {
        source: source.to_owned(),
        channel: name,
        method,
        url,
        interval_s,
        timeout_s: positive(t, "timeout_s", at)?,
        headers,
        body: body.map(String::into_bytes),
        filter,
        backoff_after: positive(backoff, "after_failures", at)? as i64,
        max_backoff_s: positive(backoff, "max_s", at)?,
        feeds,
        live_feeds,
        capabilities: strings(t, "capabilities", at, &CAPABILITIES)?,
        core_semantics,
        proxied,
        demand,
        auth,
        cursor,
        decode,
    })
}

fn applies(t: &Table, at: &str) -> Result<Applies, ConfigError> {
    if !t.contains_key("applies") {
        return Ok(Applies::default());
    }
    let a = table(t, "applies", at)?;
    let keys = match a.get("keys") {
        None => Table::new(),
        Some(Value::Table(k)) => k.clone(),
        Some(_) => return fail(format!("{at}: applies.keys must be a table of patterns")),
    };
    let mut sorted: Vec<(&String, &Value)> = keys.iter().collect();
    sorted.sort_by(|x, y| x.0.cmp(y.0));
    let mut patterns = Vec::new();
    for (namespace, pattern) in sorted {
        let Value::String(pattern) = pattern else {
            return fail(format!("{at}: applies.keys.{namespace} must be a string"));
        };
        let regex = Regex::new(pattern)
            .map_err(|e| ConfigError::Manifest(format!("{at}: applies.keys.{namespace}: {e}")))?;
        patterns.push((namespace.clone(), regex));
    }
    let days_around = match a.get("days_around") {
        None => None,
        Some(Value::Integer(d)) if *d >= 0 => Some(*d),
        Some(_) => {
            return fail(format!(
                "{at}: applies.days_around must be a whole number of days"
            ));
        }
    };
    Ok(Applies {
        keys: patterns,
        days_around,
    })
}

fn lookup(
    source: &str,
    raw: &Value,
    at: &str,
    shared: &IndexMap<String, Decode>,
) -> Result<Lookup, ConfigError> {
    let Value::Table(t) = raw else {
        return fail(format!("{at}: not a table"));
    };
    let name = string(t, "name", at)?;
    if !identifier(&name) {
        return fail(format!("{at}: '{name}' must be lowercase ASCII and dashes"));
    }
    let kind = match t.get("kind").and_then(Value::as_str) {
        Some(k) if LOOKUP_KINDS.contains(&k) => k.to_owned(),
        _ => return fail(format!("{at}: kind must be one of ('composition',)")),
    };
    let request = table(t, "request", at)?;
    if request.get("method").and_then(Value::as_str) != Some("GET") {
        return fail(format!("{at}: a lookup's request.method must be GET"));
    }
    let headers = headers(request, at)?;
    let proxied = proxied(t, at)?;
    let applies = applies(t, at)?;
    let rate = table(t, "rate", at)?;
    let feeds = strings(t, "feeds", at, &FEED_NAMES)?;
    let decode = channel_decode(t.get("decode"), shared, at).map_err(ConfigError::Manifest)?;
    let channel = Channel {
        source: source.to_owned(),
        channel: name.clone(),
        method: "GET".to_owned(),
        url: string(request, "url", at)?,
        interval_s: 0.0,
        timeout_s: positive(t, "timeout_s", at)?,
        headers,
        body: None,
        filter: None,
        backoff_after: 5,
        max_backoff_s: 300.0,
        feeds: feeds.clone(),
        live_feeds: None,
        capabilities: strings(t, "capabilities", at, &CAPABILITIES)?,
        core_semantics: SourceSemantics::default(),
        proxied,
        demand: None,
        auth: None,
        cursor: None,
        decode,
    };
    Ok(Lookup {
        source: source.to_owned(),
        name,
        kind,
        feeds,
        request: channel,
        ttl_s: positive(t, "ttl_s", at)?,
        not_found_ttl_s: positive(t, "not_found_ttl_s", at)?,
        retry_s: positive(t, "retry_s", at)?,
        rate: Rate {
            per_minute: positive(rate, "per_minute", at)?,
            burst: positive(rate, "burst", at)? as i64,
        },
        daily_budget: positive(t, "daily_budget", at)? as i64,
        applies,
    })
}

fn physical_vehicles(document: &Table, at: &str) -> Result<IndexMap<String, bool>, ConfigError> {
    let vehicles = match document.get("vehicles") {
        None => return Ok(IndexMap::from([("default".to_owned(), true)])),
        Some(Value::Table(v)) => v,
        Some(_) => return fail(format!("{at}: [vehicles] must be a table")),
    };
    let mut out = IndexMap::from([("default".to_owned(), true)]);
    match vehicles.get("physical") {
        None => {}
        Some(Value::Table(raw)) => {
            for (mode, value) in raw {
                let Value::Boolean(b) = value else {
                    return fail(format!(
                        "{at}: vehicles.physical must map modes to true or false"
                    ));
                };
                out.insert(mode.clone(), *b);
            }
        }
        Some(_) => {
            return fail(format!(
                "{at}: vehicles.physical must map modes to true or false"
            ));
        }
    }
    Ok(out)
}

/// `load_source` of a manifest's text; `stem` is its file name without `.toml`, `at` names it
/// in messages.
pub fn parse_source(
    text: &str,
    stem: &str,
    at: &str,
    filters: &[&str],
) -> Result<Source, ConfigError> {
    let document: Table = text
        .parse()
        .map_err(|e| ConfigError::Manifest(format!("Invalid TOML in {at}: {e}")))?;
    if !matches!(document.get("manifest_version"), Some(Value::Integer(1))) {
        return fail(format!("{at} must contain manifest_version = 1"));
    }
    let source = string(&document, "source", at)?;
    if !identifier(&source) || stem != source {
        return fail(format!("{at}: source '{source}' must match the file name"));
    }
    let empty = Vec::new();
    let tables = match document.get("channel") {
        None => &empty,
        Some(Value::Array(a)) => a,
        Some(_) => {
            return fail(format!(
                "{at}: [[channel]] and [[lookup]] must be arrays of tables"
            ));
        }
    };
    let lookup_tables = match document.get("lookup") {
        None => &empty,
        Some(Value::Array(a)) => a,
        Some(_) => {
            return fail(format!(
                "{at}: [[channel]] and [[lookup]] must be arrays of tables"
            ));
        }
    };
    if tables.is_empty() && lookup_tables.is_empty() {
        return fail(format!("{at} defines no [[channel]] and no [[lookup]]"));
    }
    let shared = shared_decodes(&document, at).map_err(ConfigError::Manifest)?;
    let mut channels: Vec<Channel> = Vec::new();
    for (number, raw) in tables.iter().enumerate() {
        let c = channel(
            &source,
            raw,
            &format!("{at} channel {}", number + 1),
            filters,
            &shared,
        )?;
        if channels.iter().any(|existing| existing.name() == c.name()) {
            return fail(format!("{at}: duplicate channel {}", c.name()));
        }
        channels.push(c);
    }
    let mut lookups: Vec<Lookup> = Vec::new();
    for (number, raw) in lookup_tables.iter().enumerate() {
        let l = lookup(
            &source,
            raw,
            &format!("{at} lookup {}", number + 1),
            &shared,
        )?;
        let taken = channels.iter().any(|c| c.channel == l.name)
            || lookups.iter().any(|e| e.name == l.name);
        if taken {
            return fail(format!("{at}: duplicate channel or lookup {}", l.name));
        }
        lookups.push(l);
    }
    Ok(Source {
        source,
        title: string(&document, "title", at)?,
        covers: string(&document, "covers", at)?,
        channels,
        lookups,
        physical_vehicles: physical_vehicles(&document, at)?,
    })
}

/// `load_source(path, filters)`.
pub fn load_source(path: &Path, filters: &[&str]) -> Result<Source, ConfigError> {
    let text = std::fs::read_to_string(path)
        .map_err(|e| ConfigError::Manifest(format!("{}: {e}", path.display())))?;
    let stem = path
        .file_stem()
        .and_then(|s| s.to_str())
        .unwrap_or_default();
    parse_source(&text, stem, &path.display().to_string(), filters)
}

/// `load_sources(path, filters)`: every `*.toml` of a directory, by file name, or one file.
pub fn load_sources(path: &Path, filters: &[&str]) -> Result<Vec<Source>, ConfigError> {
    let files = if path.is_dir() {
        let mut files: Vec<_> = std::fs::read_dir(path)
            .map_err(|e| ConfigError::Manifest(format!("{}: {e}", path.display())))?
            .filter_map(|entry| entry.ok().map(|e| e.path()))
            .filter(|p| p.extension().is_some_and(|x| x == "toml"))
            .collect();
        files.sort();
        files
    } else {
        vec![path.to_path_buf()]
    };
    if files.is_empty() {
        return fail(format!("no connector manifests in {}", path.display()));
    }
    files
        .iter()
        .map(|file| load_source(file, filters))
        .collect()
}

/// `select_channels`: the channels of the named sources; an unknown source is an error.
pub fn select_channels(
    channels: &[Channel],
    sources: &[&str],
) -> Result<Vec<Channel>, ConfigError> {
    if sources.is_empty() {
        return Ok(channels.to_vec());
    }
    let mut known: Vec<&str> = channels.iter().map(|c| c.source.as_str()).collect();
    known.sort();
    known.dedup();
    let mut unknown: Vec<&str> = sources
        .iter()
        .copied()
        .filter(|s| !known.contains(s))
        .collect();
    unknown.sort();
    unknown.dedup();
    if !unknown.is_empty() {
        let listed: Vec<String> = known.iter().map(|k| format!("'{k}'")).collect();
        return fail(format!(
            "Unknown source(s) {}; known: [{}]",
            unknown.join(", "),
            listed.join(", ")
        ));
    }
    Ok(channels
        .iter()
        .filter(|c| sources.contains(&c.source.as_str()))
        .cloned()
        .collect())
}
