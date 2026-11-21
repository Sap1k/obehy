//! The core and the writer against Python, journey by journey (docs/RUST_PORT.md, R6): every
//! corpus journey's output stream (`obehy-rt corpus-stream`) equals the pinned one
//! `tests/realtime/corpus_stream.py` wrote into `tests/golden/corpus-stream/`.

use std::io::Read;

use obehy_core::config::manifest::{self, RECORDER_FILTERS};
use obehy_core::core::Semantics;
use obehy_rt::canonical::line;
use obehy_rt::corpus::stream;
use obehy_rt::settings::Paths;

fn gunzip(path: &std::path::Path) -> String {
    let file = std::fs::File::open(path).unwrap_or_else(|e| panic!("{}: {e}", path.display()));
    let mut text = String::new();
    flate2::read::MultiGzDecoder::new(file)
        .read_to_string(&mut text)
        .expect("a gzip stream");
    text
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
        if text != expected {
            let at = text
                .lines()
                .zip(expected.lines())
                .position(|(a, b)| a != b)
                .unwrap_or_else(|| text.lines().count().min(expected.lines().count()));
            failed.push(format!("{name}: first difference at line {}", at + 1));
        }
    }
    assert!(failed.is_empty(), "{}", failed.join("\n"));
}
