//! The shipped `[channel.decode]` sections: every quirk a manifest names (a normalizer's
//! `quirk`, or an ID in a comment) has its row in the source's dossier
//! (`docs/sources/<source>.md`, Quirks), every section compiles, and the shape is read
//! strictly (an unknown key does not load).

use std::path::PathBuf;

use obehy_core::config::decode::Decode;
use obehy_core::config::manifest::{self, RECORDER_FILTERS};
use obehy_sources::Decoders;
use regex::Regex;

fn repo() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..")
}

/// The dossier a quirk ID's prefix belongs to.
fn dossier(prefix: &str) -> &'static str {
    match prefix {
        "DUK" => "duk.md",
        "PID" => "pid.md",
        "SZ" => "sz.md",
        "SZT" => "sz-tabule.md",
        "DPMD" => "dpmd.md",
        "MPVNET" => "mpvnet.md",
        "ARRIVA" => "arriva-express.md",
        "CD" => "cd.md",
        "IREDO" => "iredo.md",
        "IDSOK" => "idsok.md",
        other => panic!("no dossier for quirk prefix {other}"),
    }
}

#[test]
fn every_quirk_a_manifest_names_is_in_its_dossier() {
    let id = Regex::new(r"\b([A-Z]+)-Q\d+\b").unwrap();
    let mut missing = Vec::new();
    let mut seen = 0;
    let dir = repo().join("src/obehy/data/realtime/sources");
    for entry in std::fs::read_dir(&dir).unwrap() {
        let path = entry.unwrap().path();
        if path.extension().is_none_or(|e| e != "toml") {
            continue;
        }
        let text = std::fs::read_to_string(&path).unwrap();
        for found in id.captures_iter(&text) {
            seen += 1;
            let doc = repo().join("docs/sources").join(dossier(&found[1]));
            let rows = std::fs::read_to_string(&doc).unwrap();
            if !rows.contains(&format!("| {} |", &found[0])) {
                missing.push(format!(
                    "{}: {} not in {}",
                    path.display(),
                    &found[0],
                    doc.display()
                ));
            }
        }
    }
    assert!(seen > 30, "only {seen} quirk IDs found");
    assert!(missing.is_empty(), "{}", missing.join("\n"));
}

#[test]
fn every_shipped_decode_section_compiles() {
    let sources = manifest::load_sources(
        &repo().join("src/obehy/data/realtime/sources"),
        &RECORDER_FILTERS,
    )
    .unwrap();
    let package = repo().join("src/obehy");
    let files = |name: &str| std::fs::read_to_string(package.join(name)).map_err(|e| e.to_string());
    let decoders = Decoders::new(&sources, &files).unwrap();
    for (source, channel, version) in [
        ("duk", "vehicles", Some(4)),
        ("pid", "vehicles", Some(4)),
        ("pid", "alerts", None),
        ("sz-mapa", "trains", Some(3)),
        ("sz-tabule", "board", Some(2)),
        ("dpmd", "vehicles", Some(2)),
        ("arriva-express", "buses", Some(2)),
        ("mpvnet", "idol", Some(4)),
        ("mpvnet", "zlin", Some(4)),
        ("cd", "composition", None),
        ("iredo", "vehicles", Some(1)),
        ("idsok", "vehicles", Some(1)),
    ] {
        assert!(
            decoders.get(source, channel).is_some(),
            "{source}/{channel}"
        );
        assert_eq!(
            decoders.version(source, channel),
            version,
            "{source}/{channel}"
        );
    }
    assert!(decoders.is_alert_channel("pid", "infotexts"));
    assert!(decoders.decodes_facts("sz-tabule", "board"));
}

fn parse(text: &str) -> Result<Decode, String> {
    let table: toml::Table = text.parse().map_err(|e| format!("{e}"))?;
    table
        .get("decode")
        .cloned()
        .unwrap()
        .try_into()
        .map_err(|e: toml::de::Error| e.to_string())
}

#[test]
fn the_decode_shape_is_read_strictly() {
    let base = r#"
[decode]
version = 1
reader = "json"
entities = "list"
feed = "jdf"
fact = [{ t = "vehicle_key", id = "{id}" }]
[decode.values]
id = { path = "id", as = "text", required = true }
"#;
    assert!(parse(base).is_ok());
    for (bad, what) in [
        (
            "fact = [{ t = \"vehicle_key\", id = \"{id}\", colour = 1 }]",
            "unknown fact key",
        ),
        ("fact = [{ t = \"no_such_fact\" }]", "unknown fact type"),
        ("skipp = { a = 1 }", "unknown section key"),
        ("reader = \"xml\"", "unknown reader"),
    ] {
        let key = bad.split(' ').next().unwrap();
        let text = base
            .lines()
            .map(|l| {
                if l.starts_with(&format!("{key} =")) {
                    bad
                } else {
                    l
                }
            })
            .collect::<Vec<_>>()
            .join("\n");
        let text = if text.contains(bad) {
            text
        } else {
            text.replace("feed = \"jdf\"", &format!("feed = \"jdf\"\n{bad}"))
        };
        assert!(parse(&text).is_err(), "{what} loaded");
    }
    let unknown_value = base.replace("required = true", "required = true, colour = 1");
    assert!(parse(&unknown_value).is_err(), "unknown value key loaded");
    let unknown_fn = base.replace(
        "[decode.values]",
        "[decode.values]\nx = { fn = \"minute_middle\", quirk = \"SZ-Q12\", value = \"id\", extra = 1 }",
    );
    assert!(parse(&unknown_fn).is_err(), "unknown normalizer key loaded");
}
