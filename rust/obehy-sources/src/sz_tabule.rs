//! SŽ station board: `InfoTabule` HTML → platforms (docs/sources/sz-tabule.md), a code
//! decoder (`decoder = "sz-tabule"`).
//!
//! One poll is one station's board: arrivals and departures. Each row with a platform becomes a
//! vehicle-less observation keyed by train number, carrying a `PlatformAssignment` for the
//! request's station. SZT-Q1 (the 6-digit request code loses its check digit), SZT-Q2/Q9 (the
//! last column's header says platforms, tracks or both), SZT-Q4 (`-` and `ND` are none),
//! SZT-Q5 (bare `HH:mm` resolved near reception). Only desktop cells are read.

use std::sync::LazyLock;

use obehy_core::model::{
    Fact, Feed, Observation, PlatformAssignment, PlatformLabel, RawRef, StopKind, TripKey,
};
use obehy_core::time::{Instant, resolve_clock};
use regex::Regex;
use scraper::{ElementRef, Html, Selector};

use crate::error::PayloadError;
use crate::mapping::hhmm;
use crate::reader::json::{self, Value};

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

fn selector(css: &str) -> Selector {
    Selector::parse(css).expect("a valid selector")
}

static TABLES: LazyLock<Selector> = LazyLock::new(|| selector("div[data-typ]"));
static HEADER: LazyLock<Selector> = LazyLock::new(|| selector("thead th"));
static ROWS: LazyLock<Selector> = LazyLock::new(|| selector("tbody tr"));
static CELLS: LazyLock<Selector> = LazyLock::new(|| selector("td, th"));
static COLUMN: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^inTaCol[PO]-\d+$").expect("a valid pattern"));

fn normalize_space(text: &str) -> String {
    text.split_whitespace().collect::<Vec<_>>().join(" ")
}

fn cell(element: ElementRef<'_>) -> Cell {
    let e = element.value();
    Cell {
        column: e.classes().find(|c| COLUMN.is_match(c)).map(str::to_owned),
        mobile: e.has_class("d-md-none", scraper::CaseSensitivity::CaseSensitive),
        text: normalize_space(&element.text().collect::<String>()),
        train: e.attr("data-trainnumber").map(str::to_owned),
    }
}

/// The board's arrival and departure tables.
fn tables(document: &Html) -> Vec<Table> {
    let mut tables = Vec::new();
    for div in document.select(&TABLES) {
        let kind = match div.value().attr("data-typ") {
            Some("prijezdy") => StopKind::Arrival,
            Some("odjezdy") => StopKind::Departure,
            _ => continue,
        };
        let header = div
            .select(&HEADER)
            .map(cell)
            .filter(|c| !c.mobile)
            .map(|c| (c.column, c.text))
            .collect();
        let rows = div
            .select(&ROWS)
            .map(|row| row.select(&CELLS).map(cell).collect())
            .collect();
        tables.push(Table { kind, header, rows });
    }
    tables
}

/// The 6-digit request code without its check digit (SZT-Q1).
pub fn code5(station: &str) -> Option<String> {
    (station.len() == 6 && station.bytes().all(|b| b.is_ascii_digit()))
        .then(|| station[..5].to_owned())
}

fn labels(table: &Table) -> Option<Vec<PlatformLabel>> {
    let last = &table.header.last()?.1;
    match last.to_lowercase().as_str() {
        "nástupiště" => Some(vec![PlatformLabel::Platform]),
        "kolej" => Some(vec![PlatformLabel::Track]),
        "nást./kolej" => Some(vec![PlatformLabel::Platform, PlatformLabel::Track]),
        _ => None,
    }
}

/// A board's observations; `request` is the poll's archived request (`{"sr70": "534149"}`).
pub fn decode(
    body: &[u8],
    sha256: &str,
    received_at: Instant,
    request: Option<&Value>,
    version: i64,
) -> Result<Vec<Observation>, PayloadError> {
    let sr70 = request
        .and_then(|r| r.get("sr70"))
        .map(json::text)
        .unwrap_or_default();
    let station =
        code5(&sr70).ok_or_else(|| PayloadError::new("poll without the station's SR70 code"))?;
    let text =
        std::str::from_utf8(body).map_err(|e| PayloadError::new(format!("not UTF-8: {e}")))?;
    let tables = tables(&Html::parse_document(text));
    if tables.is_empty() {
        return Err(PayloadError::new("no arrival or departure table"));
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
    if !train.bytes().all(|b| b.is_ascii_digit()) {
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
        .map(|(l, p)| (*l, p.trim().to_owned()))
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
