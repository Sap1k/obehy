//! The core and the writer, journey by journey (docs/RUST_PORT.md, R6): every corpus journey's
//! output stream (`obehy-rt corpus-stream`) matches the pinned one in
//! `tests/golden/corpus-stream/` record for record. Within a record, numbers may differ by the
//! platform's floating point (a rounded last decimal, a whole second or count off by one at a
//! rounding boundary); anything else is a difference.

use std::io::Read;

use obehy_core::config::manifest::{self, RECORDER_FILTERS};
use obehy_core::core::Semantics;
use obehy_core::time::{Instant, TimeDelta};
use obehy_rt::canonical::line;
use obehy_rt::corpus::stream;
use obehy_rt::settings::Paths;
use serde_json::Value;

fn gunzip(path: &std::path::Path) -> String {
    let file = std::fs::File::open(path).unwrap_or_else(|e| panic!("{}: {e}", path.display()));
    let mut text = String::new();
    flate2::read::MultiGzDecoder::new(file)
        .read_to_string(&mut text)
        .expect("a gzip stream");
    text
}

fn close_lines(a: &str, b: &str) -> bool {
    match (serde_json::from_str(a), serde_json::from_str(b)) {
        (Ok(a), Ok(b)) => close(&a, &b),
        _ => false,
    }
}

/// Equal up to floating point: floats within 0.002 (or 1e-6 relative), integers and integer
/// texts within 1, ISO times within a second.
fn close(a: &Value, b: &Value) -> bool {
    match (a, b) {
        (Value::Number(x), Value::Number(y)) => match (x.as_i64(), y.as_i64()) {
            (Some(x), Some(y)) => x.abs_diff(y) <= 1,
            _ => {
                let (x, y) = (x.as_f64().unwrap(), y.as_f64().unwrap());
                (x - y).abs() <= 0.002_f64.max(1e-6 * x.abs())
            }
        },
        (Value::String(x), Value::String(y)) => x == y || close_texts(x, y),
        (Value::Array(x), Value::Array(y)) => {
            x.len() == y.len() && x.iter().zip(y).all(|(a, b)| close(a, b))
        }
        (Value::Object(x), Value::Object(y)) => {
            x.len() == y.len() && x.iter().all(|(k, v)| y.get(k).is_some_and(|w| close(v, w)))
        }
        _ => a == b,
    }
}

fn close_texts(a: &str, b: &str) -> bool {
    if let (Ok(x), Ok(y)) = (a.parse::<i64>(), b.parse::<i64>()) {
        return x.abs_diff(y) <= 1;
    }
    let time = |t: &str| {
        let t = t
            .strip_suffix('Z')
            .map_or_else(|| t.to_owned(), |t| format!("{t}+00:00"));
        Instant::from_isoformat(&t).ok()
    };
    match (time(a), time(b)) {
        (Some(x), Some(y)) => (x - y).abs() <= TimeDelta::from_seconds(1),
        _ => false,
    }
}

#[test]
fn every_corpus_journey_gives_its_pinned_stream() {
    let paths = Paths::discover().expect("the repository");
    let policy = paths.load_policy().expect("the policy");
    let sources =
        manifest::load_sources(&paths.sources(), &RECORDER_FILTERS).expect("the manifests");
    let semantics: Semantics = sources
        .iter()
        .flat_map(|s| s.channels.iter())
        .map(|c| ((c.source.clone(), c.channel.clone()), c.core_semantics))
        .collect();
    let corpus = paths.root.join("tests/realtime/corpus");
    let pinned = paths.root.join("tests/golden/corpus-stream");
    let mut names: Vec<String> = std::fs::read_dir(&corpus)
        .expect("the corpus")
        .filter_map(|e| e.ok())
        .filter_map(|e| {
            e.file_name()
                .to_str()
                .and_then(|n| n.strip_suffix(".json.gz"))
                .map(str::to_owned)
        })
        .collect();
    names.sort();
    assert!(
        names.len() >= 100,
        "the corpus has about a hundred journeys"
    );
    let mut failed = Vec::new();
    for name in &names {
        let mut text = String::new();
        stream(
            name,
            &corpus.join(format!("{name}.json.gz")),
            &policy,
            &semantics,
            &sources,
            &mut |record| {
                text.push_str(&line(record));
                Ok(())
            },
        )
        .unwrap_or_else(|e| panic!("{name}: {e}"));
        let expected = gunzip(&pinned.join(format!("{name}.jsonl.gz")));
        let (got, wanted): (Vec<&str>, Vec<&str>) =
            (text.lines().collect(), expected.lines().collect());
        let first = got
            .iter()
            .zip(&wanted)
            .position(|(a, b)| a != b && !close_lines(a, b));
        match first {
            Some(at) => failed.push(format!("{name}: first difference at line {}", at + 1)),
            None if got.len() != wanted.len() => failed.push(format!(
                "{name}: {} records, {} pinned",
                got.len(),
                wanted.len()
            )),
            None => {}
        }
    }
    assert!(failed.is_empty(), "{}", failed.join("\n"));
}
