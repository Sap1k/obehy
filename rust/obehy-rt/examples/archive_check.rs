//! The Rust raw archive against the Python one (rust/tools/archive_crosscheck.py):
//!
//! ```text
//! cargo run --release -p obehy-rt --example archive_check -- dump ROOT SOURCE CHANNEL FROM TO OUT
//! cargo run --release -p obehy-rt --example archive_check -- write ROOT
//! cargo run --release -p obehy-rt --example archive_check -- recompress ROOT/SOURCE/CHANNEL/DAY
//! ```
//!
//! `dump` writes the canonical poll lines `archive_crosscheck.py dump` writes, from Rust's
//! `iter_polls`; `write` appends the fixed polls `archive_crosscheck.py write` appends;
//! `recompress` compresses every object of a day again and counts those equal to Python's.

use std::collections::BTreeMap;
use std::io::Write as _;
use std::path::Path;
use std::process::ExitCode;

use obehy_core::py::json::{DumpOptions, PyJson, dumps};
use obehy_core::time::{Date, Instant, TimeDelta};
use obehy_rt::archive::{ArchiveWriter, Poll, iter_polls, sha256_hex};
use serde_json::Value;

fn py(value: &Value) -> PyJson {
    match value {
        Value::Null => PyJson::Null,
        Value::Bool(b) => PyJson::Bool(*b),
        Value::Number(n) => n.as_i64().map_or_else(
            || PyJson::Float(n.as_f64().unwrap_or(f64::NAN)),
            PyJson::Int,
        ),
        Value::String(s) => PyJson::str(s.as_str()),
        Value::Array(items) => PyJson::List(items.iter().map(py).collect()),
        Value::Object(map) => PyJson::Object(map.iter().map(|(k, v)| (k.clone(), py(v))).collect()),
    }
}

fn dump(root: &Path, source: &str, channel: &str, from: Date, to: Date, out: &Path) -> usize {
    let mut stream = std::io::BufWriter::new(std::fs::File::create(out).expect("output"));
    let mut count = 0;
    for (number, poll) in iter_polls(root, source, channel, from, to).enumerate() {
        let poll = poll.expect("archived poll");
        let body = poll.body().expect("payload");
        let line = PyJson::object([
            ("n", PyJson::Int(number as i64)),
            (
                "received_at",
                PyJson::Str(poll.received_at().expect("received_at").isoformat()),
            ),
            ("entry", py(&Value::Object(poll.entry.clone()))),
            (
                "body_sha256",
                PyJson::opt(body.as_deref(), |b| PyJson::Str(sha256_hex(b))),
            ),
            (
                "body_bytes",
                PyJson::opt(body.as_deref(), |b| PyJson::Int(b.len() as i64)),
            ),
        ]);
        writeln!(stream, "{}", dumps(&line, DumpOptions::COMPACT.sorted())).expect("write");
        count += 1;
    }
    count
}

/// One append: source, channel, poll, stored body, extra index fields.
type Row<'a> = (
    &'a str,
    &'a str,
    Poll,
    Option<&'a [u8]>,
    Vec<(String, PyJson)>,
);

const DATE: &str = "Mon, 05 Oct 2026 21:59:50 GMT";

fn at(ms: i64) -> Instant {
    let t0 = Instant::from_utc(Date::ymd(2026, 10, 5).midnight())
        + TimeDelta::from_micros(86_390_250_000);
    t0 + TimeDelta::from_micros(ms * 1000)
}

fn poll(
    requested: i64,
    received: i64,
    status: Option<u16>,
    body: Option<&[u8]>,
    content_type: Option<&str>,
    headers: &[(&str, &str)],
    error: Option<&str>,
) -> Poll {
    Poll {
        requested_at: at(requested),
        received_at: at(received),
        status,
        body: body.map(<[u8]>::to_vec),
        content_type: content_type.map(str::to_owned),
        headers: headers
            .iter()
            .map(|(k, v)| ((*k).to_owned(), (*v).to_owned()))
            .collect(),
        error: error.map(str::to_owned),
        request: BTreeMap::new(),
    }
}

/// The polls of `archive_crosscheck.polls()`, in order.
fn write(root: &Path) {
    let writer = ArchiveWriter::new(root);
    let payload = "{\"VehicleList\":[{\"ID\":\"Čáslav ü\"}]}".as_bytes();
    let json = Some("application/json");
    let ok = Some(200);
    let board = "<html>Ž</html>".as_bytes();
    let rows: Vec<Row<'_>> = vec![
        (
            "duk",
            "vehicles",
            poll(
                0,
                120,
                ok,
                Some(payload),
                json,
                &[("date", DATE), ("age", "3")],
                None,
            ),
            Some(payload),
            vec![],
        ),
        (
            "duk",
            "vehicles",
            poll(
                15000,
                15120,
                ok,
                Some(payload),
                json,
                &[("date", DATE)],
                None,
            ),
            Some(payload),
            vec![],
        ),
        (
            "duk",
            "vehicles",
            poll(
                30000,
                50000,
                None,
                None,
                None,
                &[],
                Some("TimeoutError: timed out"),
            ),
            None,
            vec![],
        ),
        (
            "duk",
            "vehicles",
            poll(
                45000,
                45001,
                Some(502),
                Some(b"bad gateway"),
                Some("text/html"),
                &[],
                Some("HTTP 502 Bad Gateway"),
            ),
            Some(b"bad gateway"),
            vec![],
        ),
        (
            "arriva-express",
            "buses",
            poll(1000, 1500, ok, Some(b"[1,2,3]"), json, &[], None),
            Some(b"[1]"),
            vec![
                ("filter".into(), PyJson::str("arriva-express@1")),
                ("source_bytes".into(), PyJson::Int(7)),
                ("source_sha256".into(), PyJson::Str(sha256_hex(b"[1,2,3]"))),
                ("kept".into(), PyJson::Int(1)),
                ("dropped".into(), PyJson::Int(2)),
            ],
        ),
        (
            "arriva-express",
            "buses",
            poll(2000, 2500, ok, Some(b"{}"), None, &[], None),
            Some(b"{}"),
            vec![
                ("filter".into(), PyJson::str("arriva-express@1")),
                (
                    "filter_error".into(),
                    PyJson::str("Expected a \"GraphQL\" batch\n\t\u{1}"),
                ),
            ],
        ),
        (
            "sz-tabule",
            "board",
            poll(
                3000,
                3333,
                ok,
                Some(board),
                Some("text/html; charset=UTF-8"),
                &[],
                None,
            ),
            Some(board),
            vec![(
                "request".into(),
                PyJson::Object(vec![("sr70".into(), PyJson::str("534149"))]),
            )],
        ),
    ];
    for (source, channel, poll, body, extra) in rows {
        writer
            .append(source, channel, &poll, body, &extra)
            .expect("append");
    }
}

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match args
        .iter()
        .map(String::as_str)
        .collect::<Vec<_>>()
        .as_slice()
    {
        ["dump", root, source, channel, from, to, out] => {
            let count = dump(
                Path::new(root),
                source,
                channel,
                Date::from_isoformat(from).expect("from"),
                Date::from_isoformat(to).expect("to"),
                Path::new(out),
            );
            println!("{count} polls");
            ExitCode::SUCCESS
        }
        ["recompress", day] => {
            // Each object recompressed by Rust against the bytes Python stored.
            let (mut same, mut total) = (0, 0);
            for entry in std::fs::read_dir(Path::new(day).join("objects")).expect("objects") {
                let stored = std::fs::read(entry.expect("entry").path()).expect("object");
                let body = zstd::decode_all(stored.as_slice()).expect("zstd");
                let again = zstd::bulk::compress(&body, obehy_rt::archive::COMPRESSION_LEVEL)
                    .expect("compress");
                same += usize::from(again == stored);
                total += 1;
            }
            println!("{same} of {total} objects byte-identical");
            ExitCode::SUCCESS
        }
        ["write", root] => {
            write(Path::new(root));
            ExitCode::SUCCESS
        }
        _ => {
            eprintln!("usage: archive_check dump ROOT SOURCE CHANNEL FROM TO OUT | write ROOT");
            ExitCode::FAILURE
        }
    }
}
