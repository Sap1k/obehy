//! Policy and manifests: every shipped file loads, to the same values Python loads
//! (`tools/gen_fixtures.py` dumps Python's dataclasses), and the refusals of
//! `tests/unit/realtime/test_foundation.py` and `tests/unit/test_rt_record.py` hold.

mod common;

use std::collections::BTreeMap;
use std::path::PathBuf;

use obehy_core::config::manifest::{self, Channel, Lookup, RECORDER_FILTERS, Source};
use obehy_core::config::policy::{ByMode, POLICY_FILE, WarmReplayMode};
use obehy_core::config::{ConfigError, Policy};
use obehy_core::model::SourceSemantics;
use serde_json::{Value, json};

fn policy_path() -> PathBuf {
    common::repo()
        .join("src/obehy/data/realtime")
        .join(POLICY_FILE)
}

fn sources_dir() -> PathBuf {
    common::repo().join("src/obehy/realtime/sources")
}

fn policy_text() -> String {
    std::fs::read_to_string(policy_path()).unwrap()
}

fn by_mode(value: &ByMode) -> Value {
    json!({"default": value.default, "overrides": value.overrides.iter().map(|(n, v)| json!([n, v])).collect::<Vec<_>>()})
}

/// Python's `dataclasses.asdict(load_policy())`, from the Rust policy.
fn policy_as_python(p: &Policy) -> Value {
    let t = &p.time;
    let l = &p.lifecycle;
    let g = &p.progress;
    let d = &p.prediction;
    let mut shared = p.journey.shared.clone();
    shared.sort();
    json!({
        "version": p.version,
        "journey": {"namespaces": p.journey.namespaces, "shared": shared},
        "time": {"pre_trip_s": by_mode(&t.pre_trip_s), "max_delay_s": by_mode(&t.max_delay_s),
                 "vehicle_day_gap_s": t.vehicle_day_gap_s, "max_clock_skew_s": t.max_clock_skew_s},
        "lifecycle": {"stale_after_s": by_mode(&l.stale_after_s), "predict_without_data_s": by_mode(&l.predict_without_data_s),
                      "arrival_radius_m": l.arrival_radius_m, "departure_margin_m": l.departure_margin_m,
                      "terminal_radius_m": l.terminal_radius_m, "publish_finished_s": by_mode(&l.publish_finished_s),
                      "forget_after_s": l.forget_after_s},
        "progress": {"gps_sigma_m": g.gps_sigma_m, "shape_sigma_m": g.shape_sigma_m, "chord_min_sigma_m": g.chord_min_sigma_m,
                     "chord_k": g.chord_k, "chord_max_sigma_m": g.chord_max_sigma_m, "reach_sigmas": g.reach_sigmas,
                     "bearing_sigma_deg": g.bearing_sigma_deg, "chord_bearing_sigma_deg": g.chord_bearing_sigma_deg,
                     "jitter_m": g.jitter_m, "off_path_log_p": g.off_path_log_p, "max_speed_mps": by_mode(&g.max_speed_mps),
                     "loss_sigma_base_s": g.loss_sigma_base_s, "loss_sigma_rate": g.loss_sigma_rate,
                     "gain_sigma_base_s": g.gain_sigma_base_s, "gain_sigma_rate": g.gain_sigma_rate,
                     "start_lateness_sigma_s": g.start_lateness_sigma_s, "start_early_sigma_s": g.start_early_sigma_s,
                     "beam": g.beam, "prune": g.prune, "agree_within": g.agree_within, "max_commit_lag_s": g.max_commit_lag_s,
                     "max_event_interval_s": g.max_event_interval_s, "max_inferred_spread_s": g.max_inferred_spread_s,
                     "max_extrapolate_s": g.max_extrapolate_s, "off_route_hold_s": g.off_route_hold_s,
                     "move_sigma_m": g.move_sigma_m, "move_slack_m": g.move_slack_m, "reckon_slack": g.reckon_slack,
                     "standing_m": g.standing_m, "stop_ahead_calls": g.stop_ahead_calls, "stop_off_shape_m": g.stop_off_shape_m,
                     "shape_simplify_deg": g.shape_simplify_deg, "merge_m": g.merge_m},
        "prediction": {"min_dwell_s": by_mode(&d.min_dwell_s), "long_dwell_s": by_mode(&d.long_dwell_s),
                       "min_long_dwell_s": by_mode(&d.min_long_dwell_s), "max_early_departure_s": by_mode(&d.max_early_departure_s),
                       "runtime_decay_s": d.runtime_decay_s, "runtime_prior_weight": d.runtime_prior_weight,
                       "runtime_ratio_max": d.runtime_ratio_max, "runtime_keep_decays": d.runtime_keep_decays,
                       "publish_deadband_s": by_mode(&d.publish_deadband_s), "floor_fresh_s": by_mode(&d.floor_fresh_s),
                       "dwell_credit_s": by_mode(&d.dwell_credit_s), "early_keep": by_mode(&d.early_keep)},
        "delay_discard_below_s": p.delay.discard_below_s,
        "warm_replay_mode": match p.warm_replay.mode { WarmReplayMode::Vehicles => "vehicles", WarmReplayMode::Window => "window" },
        "warm_replay_max_hours": p.warm_replay.max_hours,
        "emit_tick_s": p.emit.tick_s,
        "sweep_index_every_s": p.emit.sweep_index_every_s,
        "keep_index_s": p.emit.keep_index_s,
        "compact_index_at_s": p.emit.compact_index_at_s,
        "retention": {"observation_hours": p.retention.observation_hours, "raw_days": by_mode(&p.retention.raw_days),
                      "history_days": p.retention.history_days},
        "guard": {"free_gb": p.guard.free_gb, "min_raw_days": p.guard.min_raw_days, "min_history_days": p.guard.min_history_days},
        "egress": {"refresh_s": p.egress.refresh_s, "cooldown_s": p.egress.cooldown_s},
        "rail": {"position_preference": p.rail.position_preference, "position_fresh_s": p.rail.position_fresh_s,
                 "position_sigma_m": by_mode(&p.rail.position_sigma_m), "event_tolerance_s": p.rail.event_tolerance_s,
                 "predictor": p.rail.predictor.as_str()},
        "boards": {"min_boarding_points": p.boards.min_boarding_points, "window_min": p.boards.window_min,
                   "due_refresh_s": p.boards.due_refresh_s, "early_min": p.boards.early_min, "near_min": p.boards.near_min,
                   "near_refresh_s": p.boards.near_refresh_s, "imminent_min": p.boards.imminent_min,
                   "imminent_refresh_s": p.boards.imminent_refresh_s, "ceiling_per_min": p.boards.ceiling_per_min,
                   "slowdown_s": p.boards.slowdown_s, "slow_latency_factor": p.boards.slow_latency_factor},
        "keyless": {"candidate_window_s": p.keyless.candidate_window_s, "episode_gap_s": p.keyless.episode_gap_s,
                    "accept_s": p.keyless.accept_s, "gps_accept_s": p.keyless.gps_accept_s, "margin_s": p.keyless.margin_s,
                    "max_path_distance_m": p.keyless.max_path_distance_m, "departure_radius_m": p.keyless.departure_radius_m,
                    "confirm_departures": p.keyless.confirm_departures},
    })
}

fn semantics(s: &SourceSemantics) -> Value {
    json!({"pre_trip_delay_is_elapsed": s.pre_trip_delay_is_elapsed, "pre_trip_after_end_is_stale": s.pre_trip_after_end_is_stale,
           "unchanged_entry_is_no_news": s.unchanged_entry_is_no_news, "delay_floor_minute": s.delay_floor_minute})
}

fn channel(c: &Channel) -> Value {
    json!({
        "source": c.source, "channel": c.channel, "method": c.method, "url": c.url,
        "interval_s": c.interval_s, "timeout_s": c.timeout_s,
        "headers": c.headers.iter().map(|(k, v)| (k.clone(), json!(v))).collect::<serde_json::Map<_, _>>(),
        "body": c.body.as_ref().map(|b| String::from_utf8(b.clone()).unwrap()),
        "filter": c.filter, "backoff_after": c.backoff_after, "max_backoff_s": c.max_backoff_s,
        "feeds": c.feeds, "live_feeds": c.live_feeds, "capabilities": c.capabilities,
        "core_semantics": semantics(&c.core_semantics), "proxied": c.proxied, "demand": c.demand,
        "auth": c.auth.as_ref().map(|a| json!({"header": a.header, "secret": a.secret})),
        "cursor": c.cursor.as_ref().map(|k| json!({"header": k.header, "param": k.param, "max_age_s": k.max_age_s})),
    })
}

fn lookup(l: &Lookup) -> Value {
    json!({
        "source": l.source, "name": l.name, "kind": l.kind, "feeds": l.feeds, "request": channel(&l.request),
        "ttl_s": l.ttl_s, "not_found_ttl_s": l.not_found_ttl_s, "retry_s": l.retry_s,
        "rate": {"per_minute": l.rate.per_minute, "burst": l.rate.burst}, "daily_budget": l.daily_budget,
        "applies": {"keys": l.applies.keys.iter().map(|(n, r)| json!([n, r.as_str()])).collect::<Vec<_>>(),
                    "days_around": l.applies.days_around},
    })
}

fn source(s: &Source) -> Value {
    json!({
        "source": s.source, "title": s.title, "covers": s.covers,
        "channels": s.channels.iter().map(channel).collect::<Vec<_>>(),
        "lookups": s.lookups.iter().map(lookup).collect::<Vec<_>>(),
        "physical_vehicles": s.physical_vehicles.iter().map(|(k, v)| (k.clone(), json!(v))).collect::<serde_json::Map<_, _>>(),
    })
}

#[test]
fn the_policy_and_every_manifest_load_as_in_python() {
    let expected = common::json("config.json");
    assert_eq!(expected["policy_file"], POLICY_FILE);
    let policy = Policy::load(&policy_path()).unwrap();
    assert_eq!(policy_as_python(&policy), expected["policy"]);
    let filters: Vec<&str> = expected["filters"]
        .as_array()
        .unwrap()
        .iter()
        .map(|f| f.as_str().unwrap())
        .collect();
    assert_eq!(filters, RECORDER_FILTERS);
    let sources = manifest::load_sources(&sources_dir(), &RECORDER_FILTERS).unwrap();
    let got: Vec<Value> = sources.iter().map(source).collect();
    assert_eq!(Value::Array(got), expected["sources"]);
}

/// `test_foundation.test_policy_loads_and_resolves_modes` and the journey key test.
#[test]
fn policy_resolves_modes_and_journey_keys() {
    let policy = Policy::from_toml(&policy_text()).unwrap();
    assert_eq!(policy.version, "18");
    assert_eq!(policy.warm_replay.mode, WarmReplayMode::Vehicles);
    assert_eq!(policy.time.max_delay_s.get("rail"), 14400.0);
    assert_eq!(policy.time.max_delay_s.get("bus"), 7200.0);
    assert_eq!(policy.progress.max_speed_mps.get("rail"), 45.0);
    assert_eq!(policy.retention.longest_raw_days(), 14);
    assert_eq!(policy.rail.rank("pid"), 1);
    assert_eq!(policy.rail.rank("dpmd"), 3);
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
    assert!(idol.is_live("jdf") && !idol.is_live("czptt"));
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
    assert_eq!(source("mpvnet").live_feeds(), ["jdf"]);
    // test_select_channels_rejects_unknown_source
    let all: Vec<Channel> = channels.iter().map(|c| (*c).clone()).collect();
    assert!(
        manifest::select_channels(&all, &["duk"])
            .unwrap()
            .iter()
            .all(|c| c.source == "duk")
    );
    assert!(
        manifest::select_channels(&all, &["ids-jmk"])
            .unwrap_err()
            .to_string()
            .contains("ids-jmk")
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
    let dir = common::repo().join("src/obehy/realtime/sources");
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
