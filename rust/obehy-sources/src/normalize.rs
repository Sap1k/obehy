//! The named normalizers: the quirks that need logic, each carrying its quirk ID from
//! `docs/sources/` (decode spec §2). Value normalizers make one value of an entity from
//! others; [`block_current`] chooses one entity per group across a payload.

use std::collections::{BTreeSet, HashMap};
use std::sync::{Arc, OnceLock};

use indexmap::IndexMap;
use obehy_core::config::decode::{Normalizer, RecordLayout, Scalar};
use obehy_core::py::text;
use obehy_core::time::{Instant, TimeDelta, read_unless_offset_twice};
use regex::Regex;

use crate::error::PayloadError;
use crate::mapping::{CompileError, Context, Ref, Slots, duration, get, py_regex};
use crate::reader::json::Json;
use crate::value::{Val, py_eq};

/// A record layout, compiled.
#[derive(Debug, Clone)]
pub(crate) struct Layout {
    t: String,
    fields: usize,
    category: Option<Scalar>,
    line: Option<(usize, Regex)>,
}

/// A compiled value normalizer.
#[derive(Debug, Clone)]
pub(crate) enum Norm {
    OffsetAppliedTwice {
        value: Ref,
        text: Ref,
        against: Ref,
        tolerance: TimeDelta,
    },
    PidCancelled {
        state: Ref,
        flag: Ref,
        canceled: String,
    },
    CatalogueCodes {
        name: Ref,
        table: Arc<HashMap<String, Vec<String>>>,
    },
    MinuteMiddle {
        value: Ref,
    },
    PopupLabels {
        value: Ref,
    },
    MovingBearing {
        speed: Ref,
        arrow: Ref,
        needs: Vec<Ref>,
    },
    MpvnetRecord {
        record: Ref,
        kind: Ref,
        category: Ref,
        bus: Layout,
        train: Layout,
    },
    SignedMinSec {
        value: Ref,
        regex: Regex,
    },
}

fn layout(c: &RecordLayout) -> Result<Layout, CompileError> {
    let line = match (c.line_field, &c.line_regex) {
        (Some(f), Some(p)) => Some((f, py_regex(p)?)),
        (None, None) => None,
        _ => return Err(CompileError("line_field and line_regex go together".into())),
    };
    Ok(Layout {
        t: c.t.clone(),
        fields: c.fields,
        category: c.category.clone(),
        line,
    })
}

pub(crate) fn compile(
    c: &mut crate::mapping::Compiler<'_>,
    n: &Normalizer,
    _name: &str,
) -> Result<Norm, CompileError> {
    Ok(match n {
        Normalizer::OffsetAppliedTwice {
            value,
            text,
            against,
            tolerance,
            ..
        } => Norm::OffsetAppliedTwice {
            value: c.reference(value)?,
            text: c.reference(text)?,
            against: c.reference(against)?,
            tolerance: duration(tolerance)?,
        },
        Normalizer::PidCancelled {
            state,
            flag,
            canceled,
            ..
        } => Norm::PidCancelled {
            state: c.reference(state)?,
            flag: c.reference(flag)?,
            canceled: canceled.clone(),
        },
        Normalizer::CatalogueCodes {
            name, catalogue, ..
        } => Norm::CatalogueCodes {
            name: c.reference(name)?,
            table: c.catalogue(catalogue)?,
        },
        Normalizer::MinuteMiddle { value, .. } => Norm::MinuteMiddle {
            value: c.reference(value)?,
        },
        Normalizer::PopupLabels { value, .. } => Norm::PopupLabels {
            value: c.reference(value)?,
        },
        Normalizer::MovingBearing {
            speed,
            arrow,
            needs,
            ..
        } => Norm::MovingBearing {
            speed: c.reference(speed)?,
            arrow: c.reference(arrow)?,
            needs: needs
                .iter()
                .map(|n| c.reference(n))
                .collect::<Result<_, _>>()?,
        },
        Normalizer::MpvnetRecord {
            record,
            kind,
            category,
            bus,
            train,
            ..
        } => Norm::MpvnetRecord {
            record: c.reference(record)?,
            kind: c.reference(kind)?,
            category: c.reference(category)?,
            bus: layout(bus)?,
            train: layout(train)?,
        },
        Normalizer::SignedMinSec { value, regex, .. } => Norm::SignedMinSec {
            value: c.reference(value)?,
            regex: py_regex(regex)?,
        },
    })
}

fn labels_regex() -> &'static Regex {
    static LABEL: OnceLock<Regex> = OnceLock::new();
    LABEL.get_or_init(|| Regex::new(r"<br>([^<:]{1,30}): ([^<]*)").expect("a valid pattern"))
}

fn speed_regex() -> &'static Regex {
    static SPEED: OnceLock<Regex> = OnceLock::new();
    SPEED.get_or_init(|| py_regex(r"^(\d+) km/h$").expect("a valid pattern"))
}

pub(crate) fn evaluate(
    n: &Norm,
    slots: &Slots,
    ctx: &Context,
) -> Result<Option<Val>, PayloadError> {
    let _ = ctx;
    Ok(match n {
        // DUK-Q1: the GPS time later than the activity time by its own offset had it twice.
        Norm::OffsetAppliedTwice {
            value,
            text,
            against,
            tolerance,
        } => {
            let gps = get(slots, value).and_then(|v| v.as_instant());
            let activity = get(slots, against).and_then(|v| v.as_instant());
            match (gps, activity) {
                (Some(_), Some(activity)) => {
                    let raw = get(slots, text)
                        .and_then(|v| v.as_text().map(str::to_owned))
                        .unwrap_or_default();
                    read_unless_offset_twice(&raw, activity, *tolerance)
                        .map_err(|e| PayloadError::value(e.0))?
                        .map(Val::Instant)
                }
                (gps, _) => gps.map(Val::Instant),
            }
        }
        // PID-Q4: `state_position == "canceled"` or `is_canceled is True`.
        Norm::PidCancelled {
            state,
            flag,
            canceled,
        } => {
            let by_state = get(slots, state).is_some_and(|v| v.as_text() == Some(canceled));
            let by_flag = get(slots, flag).is_some_and(|v| {
                matches!(v.as_ref(), Val::Json(Json::Bool(true)) | Val::Bool(true))
            });
            Some(Val::Bool(by_state || by_flag))
        }
        // SZ-Q4: every code the catalogue gives the name.
        Norm::CatalogueCodes { name, table } => {
            get(slots, name).map(|v| Val::Codes(table.get(&v.text()).cloned().unwrap_or_default()))
        }
        // SZ-Q12: a truncated minute stands for its middle.
        Norm::MinuteMiddle { value } => get(slots, value)
            .and_then(|v| v.as_instant())
            .map(|at| Val::Instant(at + TimeDelta::from_seconds(30))),
        // DPMD-Q2: `dict(re.findall(r"<br>([^<:]{1,30}): ([^<]*)", body))`.
        Norm::PopupLabels { value } => get(slots, value).and_then(|v| {
            let body = v.as_text()?;
            let mut map = IndexMap::new();
            for found in labels_regex().captures_iter(body) {
                map.insert(found[1].to_owned(), found[2].to_owned());
            }
            Some(Val::Map(map))
        }),
        // DPMD-Q10: a heading only while moving (speed above 0 km/h), from a numeric arrow.
        Norm::MovingBearing {
            speed,
            arrow,
            needs,
        } => {
            if needs.iter().any(|r| get(slots, r).is_none()) {
                return Ok(None);
            }
            let speed = get(slots, speed).map(|v| v.text()).unwrap_or_default();
            let moving = speed_regex()
                .captures(text::strip(&speed))
                .and_then(|c| text::int_from_str(&c[1]))
                .is_some_and(|n| match n {
                    text::PyInt::Small(n) => n > 0,
                    text::PyInt::Big(t) => !t.starts_with('-'),
                });
            match get(slots, arrow).as_deref() {
                Some(Val::Json(Json::Str(a))) if moving && text::isdigit(a) => Some(Val::Float(
                    text::float_from_str(a).ok_or_else(|| PayloadError::float_literal(a))?,
                )),
                _ => None,
            }
        }
        // MPVNET-Q2, Q3: the record by kind; a city bus (category) or a bad line has none.
        Norm::MpvnetRecord {
            record,
            kind,
            category,
            bus,
            train,
        } => {
            let Some(record) = get(slots, record).and_then(|v| v.as_text().map(str::to_owned))
            else {
                return Ok(None);
            };
            let fields: Vec<String> = record.split('|').map(str::to_owned).collect();
            let kind = get(slots, kind);
            let is = |t: &str| {
                kind.as_deref()
                    .is_some_and(|k| py_eq(k, &Scalar::Text(t.into())))
            };
            let chosen = if is(&bus.t) && fields.len() == bus.fields {
                let category_ok = bus
                    .category
                    .as_ref()
                    .is_none_or(|want| get(slots, category).is_some_and(|c| py_eq(&c, want)));
                let line_ok = bus
                    .line
                    .as_ref()
                    .is_none_or(|(f, re)| fields.get(*f).is_some_and(|v| re.is_match(v)));
                if !(category_ok && line_ok) {
                    return Ok(None);
                }
                "bus"
            } else if is(&train.t) && fields.len() == train.fields {
                "train"
            } else {
                return Ok(None);
            };
            Some(Val::Record {
                kind: chosen.to_owned(),
                fields,
            })
        }
        // MPVNET-Q6: `$z-2~min.~5~s` is -125 s; the letter is not a sign.
        Norm::SignedMinSec { value, regex } => get(slots, value).and_then(|v| {
            let found = regex.captures(v.as_text()?)?;
            let minutes = text::int_from_str(&found[2])?.small()?;
            let seconds = text::int_from_str(&found[3])?.small()?;
            let total = minutes * 60 + seconds;
            Some(Val::int(if found[1].is_empty() { total } else { -total }))
        }),
    })
}

/// `NÁZEV20` name → the 5-digit SR70 codes the catalogue gives it, sorted (SZ-Q4): Python's
/// `csv.reader` over the file read as `utf-8-sig`.
pub fn name_codes(text: &str) -> HashMap<String, Vec<String>> {
    let text = text.strip_prefix('\u{feff}').unwrap_or(text);
    let mut found: HashMap<String, BTreeSet<String>> = HashMap::new();
    for row in csv_rows(text) {
        if row.len() >= 2 && row[0].chars().count() >= 5 && !text::strip(&row[1]).is_empty() {
            found
                .entry(text::strip(&row[1]).to_owned())
                .or_default()
                .insert(row[0].chars().take(5).collect());
        }
    }
    found
        .into_iter()
        .map(|(name, codes)| (name, codes.into_iter().collect()))
        .collect()
}

/// Python's `csv.reader` (excel dialect) over a text read with `newline=""`.
fn csv_rows(text: &str) -> Vec<Vec<String>> {
    let mut rows = Vec::new();
    let mut row = Vec::new();
    let mut field = String::new();
    let mut chars = text.chars().peekable();
    let mut quoted = false;
    let mut started = false;
    while let Some(c) = chars.next() {
        if quoted {
            if c == '"' {
                if chars.peek() == Some(&'"') {
                    chars.next();
                    field.push('"');
                } else {
                    quoted = false;
                }
            } else {
                field.push(c);
            }
            continue;
        }
        match c {
            '"' if field.is_empty() && !started => {
                quoted = true;
                started = true;
            }
            ',' => {
                row.push(std::mem::take(&mut field));
                started = false;
            }
            '\r' | '\n' => {
                if c == '\r' && chars.peek() == Some(&'\n') {
                    chars.next();
                }
                row.push(std::mem::take(&mut field));
                rows.push(std::mem::take(&mut row));
                started = false;
            }
            _ => {
                field.push(c);
                started = true;
            }
        }
    }
    if !field.is_empty() || !row.is_empty() || started {
        row.push(field);
        rows.push(row);
    }
    rows.retain(|r| !(r.len() == 1 && r[0].is_empty()));
    rows
}

/// [`block_current`]'s parameters.
#[derive(Debug, Clone, Copy)]
pub(crate) struct BlockView<'a> {
    pub group_by: &'a Ref,
    pub state: &'a Ref,
    pub start: &'a Ref,
    pub running: &'a [String],
    pub off_route: &'a str,
    pub pre_trip: &'a [String],
    pub out: usize,
}

type Entry = (usize, Vec<Option<Val>>);

/// PID-Q6, PID-Q4: of each vehicle's block (entries grouped by `group_by`, in payload order),
/// the entry naming what it does now, with its trip state in `out` when its trip may bind:
/// the running trip (one on its route over one off it; two still running bind neither), else
/// the next to start (pre-trip), else the first (after its last trip: no state).
pub(crate) fn block_current(view: BlockView<'_>, entries: Vec<Entry>) -> Vec<Entry> {
    let mut groups: IndexMap<String, Vec<Entry>> = IndexMap::new();
    for entry in entries {
        let key = get(&entry.1, view.group_by)
            .map(|v| v.text())
            .unwrap_or_default();
        groups.entry(key).or_default().push(entry);
    }
    let state_of = |e: &Entry| get(&e.1, view.state).and_then(|v| v.as_text().map(str::to_owned));
    let is_in = |s: &Option<String>, set: &[String]| s.as_ref().is_some_and(|s| set.contains(s));
    let mut chosen = Vec::new();
    for (_, block) in groups {
        let running: Vec<usize> = (0..block.len())
            .filter(|&i| is_in(&state_of(&block[i]), view.running))
            .collect();
        let (index, bound) = if !running.is_empty() {
            let on_route: Vec<usize> = running
                .iter()
                .copied()
                .filter(|&i| state_of(&block[i]).as_deref() != Some(view.off_route))
                .collect();
            let on_route = if on_route.is_empty() {
                running
            } else {
                on_route
            };
            (on_route[0], on_route.len() == 1)
        } else {
            let mut upcoming: Vec<(Instant, usize)> = (0..block.len())
                .filter(|&i| is_in(&state_of(&block[i]), view.pre_trip))
                .map(|i| {
                    let start = get(&block[i].1, view.start)
                        .and_then(|v| v.as_instant())
                        .unwrap_or(Instant::from_micros(i64::MAX));
                    (start, i)
                })
                .collect();
            upcoming.sort_by_key(|&(start, _)| start);
            (upcoming.first().map_or(0, |&(_, i)| i), true)
        };
        let state = state_of(&block[index]);
        let mut entry = block.into_iter().nth(index).expect("a chosen entry");
        let code = if !bound {
            None
        } else if is_in(&state, view.pre_trip) {
            Some("pre_trip")
        } else if is_in(&state, view.running) {
            Some("running")
        } else {
            None
        };
        entry.1[view.out] = code.map(|c| Val::Text(c.to_owned()));
        chosen.push(entry);
    }
    chosen
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_catalogue_reads_as_python_csv() {
        let table = name_codes(
            "\u{feff}003012,Milotice n.Op.-AWT,50.0,17.5\r\n330464,\"Bludov km 6,300\",49.9,16.9\r\n003013,Milotice n.Op.-AWT ,1,2\r\n12,short\r\n",
        );
        assert_eq!(table["Milotice n.Op.-AWT"], vec!["00301"]);
        assert_eq!(table["Bludov km 6,300"], vec!["33046"]);
        assert!(!table.contains_key("short"));
    }
}
