//! Policy and manifests: every shipped file loads, and the refusals of the Python worker's
//! `test_foundation.py` and `test_rt_record.py` hold.

mod common;

use std::collections::BTreeMap;
use std::path::PathBuf;

use obehy_core::config::manifest::{self, Channel, RECORDER_FILTERS};
use obehy_core::config::policy::{POLICY_FILE, WarmReplayMode};
use obehy_core::config::{ConfigError, Policy};
use obehy_core::model::{Feed, SourceSemantics};

fn policy_path() -> PathBuf {
    common::repo()
        .join("src/obehy/data/realtime")
        .join(POLICY_FILE)
}

fn sources_dir() -> PathBuf {
    common::repo().join("src/obehy/data/realtime/sources")
}

fn policy_text() -> String {
    std::fs::read_to_string(policy_path()).unwrap()
}

/// `test_foundation.test_policy_loads_and_resolves_modes` and the journey key test.
#[test]
fn policy_resolves_modes_and_journey_keys() {
    let policy = Policy::from_toml(&policy_text()).unwrap();
    assert_eq!(policy.version, "20");
    assert_eq!(policy.warm_replay.mode, WarmReplayMode::Vehicles);
    assert_eq!(policy.time.max_delay_s.get("rail"), 14400.0);
    assert_eq!(policy.time.max_delay_s.get("bus"), 7200.0);
    assert_eq!(policy.progress.max_speed_mps.get("rail"), 45.0);
    assert_eq!(policy.retention.longest_raw_days(), 14);
    assert_eq!(policy.rail.rank("pid"), 1);
    assert_eq!(policy.rail.rank("mpvnet"), 2);
    assert_eq!(policy.rail.rank("sz-mapa"), 5);
    assert_eq!(policy.rail.rank("dpmd"), 6);
    let keys = |pairs: &[(&str, &str)]| -> BTreeMap<String, String> {
        pairs
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect()
    };
    let journey = &policy.journey;
    let rail = keys(&[
        ("czptt:tr", "Tr:1154:--KADR000607:00:2026"),
        ("czptt:train_number", "607"),
    ]);
    assert_eq!(journey.key_of(&rail), Some(("czptt:train_number", "607")));
    let pid = keys(&[
        ("pid:gtfs_trip_id", "22_101_260901"),
        ("cis:line_trip", "100022:101"),
    ]);
    assert_eq!(journey.key_of(&pid), Some(("cis:line_trip", "100022:101")));
    assert_eq!(journey.key_of(&keys(&[("czptt:tr", "Tr:1")])), None);
    assert_eq!(journey.shared, vec!["cis:line_trip".to_owned()]);
}

fn policy_error(edit: impl FnOnce(&mut toml::Table)) -> String {
    let mut document: toml::Table = policy_text().parse().unwrap();
    edit(&mut document);
    match Policy::from_toml(&toml::to_string(&document).unwrap()) {
        Err(ConfigError::Policy(message)) => message,
        other => panic!("expected a policy error, got {other:?}"),
    }
}

fn section<'a>(document: &'a mut toml::Table, name: &str) -> &'a mut toml::Table {
    document.get_mut(name).unwrap().as_table_mut().unwrap()
}

#[test]
fn a_missing_or_unknown_value_does_not_load() {
    let missing = policy_error(|d| {
        section(d, "progress").remove("chord_k");
    });
    assert!(missing.contains("chord_k"), "{missing}");
    let unknown = policy_error(|d| {
        section(d, "progress").insert("chord_kk".into(), 0.2.into());
    });
    assert!(unknown.contains("unknown field `chord_kk`"), "{unknown}");
    let wrong = policy_error(|d| {
        section(d, "progress").insert("beam".into(), 8.5.into());
    });
    assert!(
        wrong.contains("beam") || wrong.contains("integer"),
        "{wrong}"
    );
    let no_default = policy_error(|d| {
        section(d, "time").insert("pre_trip_s".into(), toml::Value::Table(toml::Table::new()));
    });
    assert!(no_default.contains("default"), "{no_default}");
    let order = policy_error(|d| {
        section(d, "lifecycle").insert("forget_after_s".into(), 1800.into());
    });
    assert!(order.contains("forget_after_s"), "{order}");
    let predictor = policy_error(|d| {
        section(d, "rail").insert("predictor".into(), "magic".into());
    });
    assert!(predictor.contains("predictor"), "{predictor}");
    let shared = policy_error(|d| {
        section(d, "journey").insert("shared".into(), toml::Value::Array(vec!["x".into()]));
    });
    assert!(shared.contains("shared"), "{shared}");
    // Where Python takes extra keys, so does Rust.
    let mut document: toml::Table = policy_text().parse().unwrap();
    section(&mut document, "emit").insert("extra".into(), 1.into());
    document.insert("extra".into(), 1.into());
    assert!(Policy::from_toml(&toml::to_string(&document).unwrap()).is_ok());
}

/// `test_rt_record.test_shipped_manifests_load`.
#[test]
fn shipped_manifests_load() {
    let sources = manifest::load_sources(&sources_dir(), &RECORDER_FILTERS).unwrap();
    let channels: Vec<&Channel> = sources.iter().flat_map(|s| &s.channels).collect();
    let names: Vec<String> = channels.iter().map(|c| c.name()).collect();
    assert_eq!(
        names,
        [
            "arriva-express/buses",
            "dpmd/vehicles",
            "duk/vehicles",
            "ids-jmk/vehicles",
            "ids-jmk/alerts",
            "idsok/vehicles",
            "iredo/vehicles",
            "mpvnet/idol",
            "mpvnet/jikord",
            "mpvnet/odis",
            "mpvnet/zlin",
            "pid/vehicles",
            "pid/alerts",
            "pid/infotexts",
            "sz-mapa/trains",
            "sz-tabule/board",
        ]
    );
    let by = |name: &str| *channels.iter().find(|c| c.name() == name).unwrap();
    let (arriva, dpmd, duk, idol) = (
        by("arriva-express/buses"),
        by("dpmd/vehicles"),
        by("duk/vehicles"),
        by("mpvnet/idol"),
    );
    let (pid, boards, sz) = (
        by("pid/vehicles"),
        by("sz-tabule/board"),
        by("sz-mapa/trains"),
    );
    assert_eq!(by("pid/alerts").capabilities, ["alert"]);
    assert_eq!(by("ids-jmk/alerts").capabilities, ["alert"]);
    // IDSJMK-Q1: a trip key czptt holds sends the observation there.
    assert_eq!(
        by("ids-jmk/vehicles").core_semantics,
        SourceSemantics {
            trip_key_feed: Some(Feed::Czptt),
            ..Default::default()
        }
    );
    assert!(
        pid.auth.is_some()
            && pid.cursor.is_some()
            && pid.live_feeds.as_deref() == Some(&["jdf".to_owned(), "czptt".to_owned()][..])
    );
    assert_eq!(
        (dpmd.filter.as_deref(), dpmd.method.as_str()),
        (Some("dpmd"), "GET")
    );
    assert!(dpmd.url.contains("{epoch_ms}"));
    assert_eq!(idol.headers["origin"], "https://www.mpvnet.cz");
    assert_eq!(
        (
            boards.demand.as_deref(),
            boards.proxied,
            boards.method.as_str()
        ),
        (Some("station_boards"), true, "POST")
    );
    assert!(
        String::from_utf8(boards.body.clone().unwrap())
            .unwrap()
            .contains("SR70={sr70}")
    );
    assert_eq!(arriva.headers["x-enviroment"], "client");
    assert!(sz.url.contains("OsVlaky") && sz.proxied && !duk.proxied && !arriva.proxied);
    assert!(duk.live_feeds.is_none() && duk.is_live("czptt"));
    assert!(idol.live_feeds.is_none() && idol.is_live("jdf") && idol.is_live("czptt"));
    assert_eq!(
        (duk.interval_s, duk.backoff_after, duk.max_backoff_s),
        (15.0, 5, 300.0)
    );
    assert_eq!(
        duk.core_semantics,
        SourceSemantics {
            pre_trip_delay_is_elapsed: true,
            pre_trip_after_end_is_stale: true,
            ..Default::default()
        }
    );
    assert_eq!(
        arriva.core_semantics,
        SourceSemantics {
            delay_floor_minute: true,
            ..Default::default()
        }
    );
    assert_eq!(
        sz.core_semantics,
        SourceSemantics {
            unchanged_entry_is_no_news: true,
            delay_floor_minute: true,
            ..Default::default()
        }
    );
    // test_physical_vehicles_by_source_and_mode
    let source = |name: &str| sources.iter().find(|s| s.source == name).unwrap();
    assert_eq!(
        (
            source("pid").physical(Some("bus")),
            source("pid").physical(Some("metro"))
        ),
        (true, false)
    );
    assert_eq!(
        (
            source("pid").physical(Some("rail")),
            source("pid").physical(None)
        ),
        (false, true)
    );
    assert!(
        !source("sz-mapa").physical(Some("rail")) && source("dpmd").physical(Some("trolleybus"))
    );
    assert_eq!(source("mpvnet").live_feeds(), ["jdf", "czptt"]);
    assert!(!source("iredo").physical(None) && !source("idsok").physical(Some("tram")));
    // test_select_channels_rejects_unknown_source
    let all: Vec<Channel> = channels.iter().map(|c| (*c).clone()).collect();
    assert!(
        manifest::select_channels(&all, &["duk"])
            .unwrap()
            .iter()
            .all(|c| c.source == "duk")
    );
    assert!(
        manifest::select_channels(&all, &["no-such-source"])
            .unwrap_err()
            .to_string()
            .contains("no-such-source")
    );
    // The ČD lookup answers for its own trains on the days around today.
    let cd = &source("cd").lookups[0];
    let today = obehy_core::time::Date::ymd(2026, 10, 8);
    let ours = |ns: &str| (ns == "czptt:tr").then(|| "Tr:1154:--KADR000607:00:2026".to_owned());
    assert_eq!(cd.applies.refusal(&ours, today, today), None);
    assert!(
        cd.applies
            .refusal(&ours, today.add_days(2), today)
            .is_some()
    );
    assert!(cd.applies.refusal(&|_| None, today, today).is_some());
}

const CHANNEL: &str = "source = \"duk\"\nmanifest_version = 1\n[[channel]]\nname = \"vehicles\"\nbackoff = { after_failures = 5, max_s = 300 }\ntimeout_s = 5\n";
const GET: &str = "request = { method = \"GET\", url = \"https://x.invalid\" }\n";
const EVERY_5S: &str = "poll = { kind = \"interval\", seconds = 5 }\n";

fn manifest_error(text: &str, stem: &str) -> String {
    manifest::parse_source(text, stem, "duk.toml", &RECORDER_FILTERS)
        .unwrap_err()
        .to_string()
}

/// `test_rt_record.test_manifest_errors` and its neighbours.
#[test]
fn manifest_errors() {
    let cases = [
        (
            format!("poll = {{ kind = \"interval\", seconds = 0 }}\n{GET}"),
            "seconds",
        ),
        (
            format!("poll = {{ kind = \"cron\", seconds = 5 }}\n{GET}"),
            "poll.kind",
        ),
        (
            format!("{EVERY_5S}filter = \"nope\"\n{GET}"),
            "unknown filter",
        ),
        (
            format!(
                "{EVERY_5S}request = {{ method = \"GET\", url = \"https://x.invalid\", body = \"x\" }}\n"
            ),
            "only POST",
        ),
        (
            format!("{EVERY_5S}feeds = [\"gtfs\"]\n{GET}"),
            "unknown feeds",
        ),
        (
            format!("{EVERY_5S}feeds = [\"jdf\"]\nlive_feeds = [\"czptt\"]\n{GET}"),
            "unknown live_feeds",
        ),
        (
            format!("{EVERY_5S}{GET}semantics = {{ pre_trip_after_end_is_stale = 1 }}\n"),
            "true or false",
        ),
        (
            format!("{EVERY_5S}{GET}semantics = {{ nope = true }}\n"),
            "unknown semantics.nope",
        ),
        (
            format!("{EVERY_5S}feeds = [\"jdf\"]\ntrip_key_feed = \"czptt\"\n{GET}"),
            "trip_key_feed must be one of its feeds",
        ),
        (
            format!("{EVERY_5S}{GET}egress = \"sz\"\n"),
            "replaced by proxied",
        ),
        (
            format!("{EVERY_5S}{GET}proxied = \"yes\"\n"),
            "proxied must be",
        ),
        (
            format!("{EVERY_5S}{GET}auth = {{ header = \"X-Key\", secret = \"Golemio Key\" }}\n"),
            "auth.secret",
        ),
        (
            format!(
                "{EVERY_5S}{GET}cursor = {{ header = \"x-last-modified\", param = \"since\" }}\n"
            ),
            "max_age_s",
        ),
        (
            format!(
                "{EVERY_5S}request = {{ method = \"POST\", url = \"https://x.invalid\" }}\ncursor = {{ header = \"h\", param = \"p\", max_age_s = 60 }}\n"
            ),
            "cursor",
        ),
    ];
    for (snippet, message) in cases {
        let error = manifest_error(&format!("{CHANNEL}{snippet}"), "duk");
        assert!(error.contains(message), "{message:?} not in {error:?}");
    }
    assert!(manifest_error(&format!("{CHANNEL}{EVERY_5S}{GET}"), "other").contains("file name"));
    let table = format!(
        "[[channel]]\nname = \"vehicles\"\n{EVERY_5S}backoff = {{ after_failures = 5, max_s = 300 }}\ntimeout_s = 5\n{GET}"
    );
    let twice = format!("source = \"duk\"\nmanifest_version = 1\n{table}{table}");
    assert!(manifest_error(&twice, "duk").contains("duplicate"));
    // Without a title the channel parses and the title is what fails, as in Python.
    assert!(manifest_error(&format!("{CHANNEL}{EVERY_5S}{GET}"), "duk").contains("'title'"));
}

#[test]
fn every_shipped_decode_section_loads() {
    let dir = common::repo().join("src/obehy/data/realtime/sources");
    let sources = manifest::load_sources(&dir, &RECORDER_FILTERS).expect("manifests load");
    for source in &sources {
        for channel in &source.channels {
            assert!(channel.decode.is_some(), "{} has no decode", channel.name());
        }
        for lookup in &source.lookups {
            assert!(
                lookup.request.decode.is_some(),
                "{} has no decode",
                lookup.name
            );
        }
    }
}
