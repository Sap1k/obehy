//! SŽ station board: `InfoTabule` HTML → platforms (docs/sources/sz-tabule.md); port of
//! `realtime/sources/sz_tabule.py`, a code decoder (`decoder = "sz-tabule"`).
//!
//! One poll is one station's board: arrivals and departures. Each row with a platform becomes a
//! vehicle-less observation keyed by train number, carrying a `PlatformAssignment` for the
//! request's station. SZT-Q1 (the 6-digit request code loses its check digit), SZT-Q2/Q9 (the
//! last column's header says platforms, tracks or both), SZT-Q4 (`-` and `ND` are none),
//! SZT-Q5 (bare `HH:mm` resolved near reception). Only desktop cells are read.

use std::sync::OnceLock;

use obehy_core::model::{
    Fact, Feed, Observation, PlatformAssignment, PlatformLabel, RawRef, StopKind, TripKey,
};
use obehy_core::py::text;
use obehy_core::time::{Instant, resolve_clock};
use regex::Regex;

use crate::error::PayloadError;
use crate::reader::html::{Event, parse};
use crate::reader::json::{self, Json};

pub const SOURCE: &str = "sz-tabule";
pub const CHANNEL: &str = "board";

#[derive(Debug, Default)]
struct Cell {
    column: Option<String>,
    mobile: bool,
    text: String,
    train: Option<String>,
}

#[derive(Debug)]
struct Table {
    kind: StopKind,
    header: Vec<(Option<String>, String)>,
    rows: Vec<Vec<Cell>>,
}

fn column_regex() -> &'static Regex {
    static COLUMN: OnceLock<Regex> = OnceLock::new();
    COLUMN.get_or_init(|| crate::mapping::py_regex(r"^inTaCol[PO]-\d+$").expect("a valid pattern"))
}

/// The `_Board` handler over the parser's events.
fn tables(events: Vec<Event>) -> Vec<Table> {
    let mut tables: Vec<Table> = Vec::new();
    let mut in_table = false;
    let mut cell: Option<Cell> = None;
    let mut head = false;
    // Python keeps `_row` across tables: a cell before the next `tr` joins the last row seen.
    let mut row: Option<(usize, usize)> = None;
    for event in events {
        match event {
            Event::Start(tag, attrs) => {
                // `{name: value or ""}`: the last duplicate wins.
                let value = |name: &str| {
                    attrs
                        .iter()
                        .rev()
                        .find(|(n, _)| n == name)
                        .map(|(_, v)| v.clone().unwrap_or_default())
                };
                let class = value("class").unwrap_or_default();
                let classes = text::split(&class);
                let typ = value("data-typ");
                let kind = match typ.as_deref() {
                    Some("prijezdy") => Some(StopKind::Arrival),
                    Some("odjezdy") => Some(StopKind::Departure),
                    _ => None,
                };
                if let (true, Some(kind)) = (tag == "div", kind) {
                    tables.push(Table {
                        kind,
                        header: Vec::new(),
                        rows: Vec::new(),
                    });
                    in_table = true;
                } else if !in_table {
                    continue;
                } else if tag == "thead" {
                    head = true;
                } else if tag == "tbody" {
                    head = false;
                } else if tag == "tr" && !head {
                    let t = tables.len() - 1;
                    tables[t].rows.push(Vec::new());
                    row = Some((t, tables[t].rows.len() - 1));
                } else if tag == "td" || tag == "th" {
                    let column = classes
                        .iter()
                        .find(|c| column_regex().is_match(c))
                        .map(|c| (*c).to_owned());
                    cell = Some(Cell {
                        column,
                        mobile: classes.contains(&"d-md-none"),
                        text: String::new(),
                        train: value("data-trainnumber"),
                    });
                }
            }
            Event::End(tag) => {
                if (tag != "td" && tag != "th") || cell.is_none() || !in_table {
                    continue;
                }
                let mut done = cell.take().expect("checked");
                done.text = text::normalize_space(&done.text);
                if tag == "th" && head {
                    if !done.mobile {
                        let table = tables.last_mut().expect("a table");
                        table.header.push((done.column, done.text));
                    }
                } else if let Some((t, r)) = row {
                    tables[t].rows[r].push(done);
                }
            }
            Event::Data(data) => {
                if let Some(c) = cell.as_mut() {
                    c.text.push_str(&data);
                }
            }
        }
    }
    tables
}

/// `code5`: the 6-digit request code without its check digit (SZT-Q1).
pub fn code5(station: &str) -> Option<String> {
    (station.chars().count() == 6 && text::isdigit(station))
        .then(|| station.chars().take(5).collect())
}

fn labels(table: &Table) -> Option<Vec<PlatformLabel>> {
    let last = &table.header.last()?.1;
    match text::normalize_space(last).to_lowercase().as_str() {
        "nástupiště" => Some(vec![PlatformLabel::Platform]),
        "kolej" => Some(vec![PlatformLabel::Track]),
        "nást./kolej" => Some(vec![PlatformLabel::Platform, PlatformLabel::Track]),
        _ => None,
    }
}

fn hhmm(scheduled: &str) -> Option<i64> {
    let (h, m) = scheduled
        .strip_suffix('\n')
        .unwrap_or(scheduled)
        .split_once(':')?;
    let ok = |s: &str, lo: usize, hi: usize| {
        (lo..=hi).contains(&s.chars().count()) && s.chars().all(text::is_decimal_char)
    };
    if !ok(h, 1, 2) || !ok(m, 2, 2) {
        return None;
    }
    let hours = text::int_from_str(h)?.small()?;
    let minutes = text::int_from_str(m)?.small()?;
    if hours > 23 || minutes > 59 {
        return None;
    }
    Some(((hours * 60 + minutes) * 60_000_000) as i64)
}

/// A board's observations; `request` is the poll's archived request (`{"sr70": "534149"}`).
pub fn decode(
    body: &[u8],
    sha256: &str,
    received_at: Instant,
    request: Option<&Json>,
    version: i64,
) -> Result<Vec<Observation>, PayloadError> {
    let sr70 = request
        .and_then(|r| r.get("sr70"))
        .map(Json::py_str)
        .unwrap_or_default();
    let station = code5(&sr70)
        .ok_or_else(|| PayloadError::payload("poll without the station's SR70 code"))?;
    let text = std::str::from_utf8(body).map_err(|e| {
        PayloadError::payload(format!(
            "not UTF-8: {}",
            json::utf8_error_text(body, &e, "utf-8")
        ))
    })?;
    let tables = tables(parse(text));
    if tables.is_empty() {
        return Err(PayloadError::payload("no arrival or departure table"));
    }
    let mut observations = Vec::new();
    let mut item = 0_i64;
    for table in &tables {
        let labels = labels(table);
        let scheduled_column = table
            .header
            .iter()
            .find(|(_, name)| name == "Pravidelný")
            .map(|(column, _)| column.clone());
        let platform_column = table.header.last().map(|(column, _)| column.clone());
        let (Some(labels), Some(Some(scheduled_column)), Some(Some(platform_column))) =
            (labels, scheduled_column, platform_column)
        else {
            continue;
        };
        for row in &table.rows {
            for (key, assignment) in row_facts(
                row,
                table.kind,
                &station,
                &labels,
                &scheduled_column,
                &platform_column,
                received_at,
            ) {
                observations.push(Observation {
                    source: SOURCE.into(),
                    channel: CHANNEL.into(),
                    feed: Feed::Czptt,
                    received_at,
                    observed_at: None,
                    raw: RawRef {
                        sha256: sha256.into(),
                        item,
                    },
                    decoder_version: version,
                    facts: vec![key, assignment],
                });
            }
            item += 1;
        }
    }
    Ok(observations)
}

fn row_facts(
    row: &[Cell],
    kind: StopKind,
    station: &str,
    labels: &[PlatformLabel],
    scheduled_column: &str,
    platform_column: &str,
    received_at: Instant,
) -> Vec<(Fact, Fact)> {
    let desktop: Vec<&Cell> = row.iter().filter(|c| !c.mobile).collect();
    let train = row
        .iter()
        .find_map(|c| c.train.as_ref().filter(|t| !t.is_empty()));
    let scheduled = desktop
        .iter()
        .find(|c| c.column.as_deref() == Some(scheduled_column))
        .map(|c| c.text.as_str());
    let value = desktop
        .iter()
        .find(|c| c.column.as_deref() == Some(platform_column))
        .map(|c| c.text.as_str());
    let (Some(train), Some(scheduled), Some(value)) = (train, scheduled, value) else {
        return Vec::new();
    };
    if !text::isdigit(train) {
        return Vec::new();
    }
    let parts: Vec<&str> = if labels.len() > 1 {
        value.split(" / ").collect()
    } else {
        vec![value]
    };
    if parts.len() != labels.len() {
        return Vec::new();
    }
    let shown: Vec<(PlatformLabel, String)> = labels
        .iter()
        .zip(parts)
        .map(|(l, p)| (*l, text::strip(p).to_owned()))
        .filter(|(_, p)| !matches!(p.as_str(), "" | "-" | "ND"))
        .collect();
    if shown.is_empty() {
        return Vec::new();
    }
    let Some(clock) = hhmm(scheduled) else {
        return Vec::new();
    };
    let Some(when) = resolve_clock(clock, received_at) else {
        return Vec::new();
    };
    let key = Fact::TripKey(TripKey {
        namespace: "czptt:train_number".into(),
        key: train.clone(),
    });
    shown
        .into_iter()
        .map(|(label, part)| {
            (
                key.clone(),
                Fact::PlatformAssignment(PlatformAssignment {
                    station: station.into(),
                    kind,
                    scheduled: when,
                    value: part,
                    label,
                }),
            )
        })
        .collect()
}
