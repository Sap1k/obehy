//! The archive and the recorder (ported from `tests/unit/test_rt_record.py`).

use std::collections::BTreeMap;
use std::path::PathBuf;

use obehy_core::time::{Date, Instant, TimeDelta};
use serde_json::{Map, Value, json};

use super::command::filterable;
use super::*;
use crate::archive::{ArchiveWriter, Poll, index_line, iter_polls, prune};
use crate::demand::DueCalls;
use crate::fetch::system_clock;

fn t0() -> Instant {
    Instant::from_utc(Date::ymd(2026, 10, 5).midnight()) + TimeDelta::from_micros(86_390_000_000)
}

fn ms(n: i64) -> TimeDelta {
    TimeDelta::from_micros(n * 1000)
}

fn poll_at(body: Option<&[u8]>, at: Instant, status: Option<u16>) -> Poll {
    Poll {
        requested_at: at,
        received_at: at + ms(120),
        status,
        body: body.map(<[u8]>::to_vec),
        content_type: Some("application/json".into()),
        headers: BTreeMap::from([("date".into(), "Mon, 05 Oct 2026 21:59:50 GMT".into())]),
        error: (status != Some(200)).then(|| format!("HTTP {}", status.unwrap_or(0))),
        request: BTreeMap::new(),
    }
}

fn poll(body: &[u8]) -> Poll {
    poll_at(Some(body), t0(), Some(200))
}

fn scratch(name: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("obehy-rt-record-{name}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

fn channel(source: &str, name: &str, filter: Option<&str>) -> Channel {
    let filter = filter.map_or(String::new(), |f| format!("filter = \"{f}\"\n"));
    let text = format!(
        "source = \"{source}\"\nmanifest_version = 1\ntitle = \"t\"\ncovers = \"c\"\n[[channel]]\n\
         name = \"{name}\"\npoll = {{ kind = \"interval\", seconds = 15 }}\n{filter}\
         backoff = {{ after_failures = 5, max_s = 300 }}\ntimeout_s = 10\n\
         request = {{ method = \"GET\", url = \"https://example.invalid/vehicles\" }}\n"
    );
    let parsed =
        obehy_core::config::manifest::parse_source(&text, source, "test.toml", &RECORDER_FILTERS)
            .unwrap();
    parsed.channels[0].clone()
}

fn day(d: u32) -> Date {
    Date::ymd(2026, 10, d)
}

fn bodies(
    root: &std::path::Path,
    source: &str,
    name: &str,
    from: Date,
    to: Date,
) -> Vec<Option<Vec<u8>>> {
    iter_polls(root, source, name, from, to)
        .map(|p| p.unwrap().body().unwrap())
        .collect()
}

#[test]
fn identical_payloads_share_one_object() {
    let root = scratch("identical");
    let writer = ArchiveWriter::new(&root);
    let body = b"{\"VehicleList\":[]}";
    let first = writer
        .append("duk", "vehicles", &poll(body), Some(body), &Map::new())
        .unwrap();
    let second = writer
        .append("duk", "vehicles", &poll(body), Some(body), &Map::new())
        .unwrap();
    assert_eq!(first.sha256, second.sha256);
    assert!(first.new_object_bytes > 0);
    assert_eq!(second.new_object_bytes, 0);
    let objects = root.join("duk/vehicles/2026-10-05/objects");
    assert_eq!(std::fs::read_dir(objects).unwrap().count(), 1);
    let polls: Vec<_> = iter_polls(&root, "duk", "vehicles", day(5), day(5))
        .map(Result::unwrap)
        .collect();
    assert_eq!(polls.len(), 2);
    assert_eq!(polls[0].body().unwrap().unwrap(), body);
    assert_eq!(polls[0].entry["elapsed_ms"], 120);
    assert_eq!(
        polls[0].entry["headers"]["date"],
        "Mon, 05 Oct 2026 21:59:50 GMT"
    );
    assert_eq!(polls[0].received_at().unwrap(), t0() + ms(120));
}

#[test]
fn error_polls_are_archived() {
    let root = scratch("errors");
    let writer = ArchiveWriter::new(&root);
    let bad = poll_at(Some(b"bad gateway"), t0(), Some(502));
    writer
        .append("duk", "vehicles", &bad, Some(b"bad gateway"), &Map::new())
        .unwrap();
    let start = t0() - TimeDelta::from_micros(3_600_000_000);
    let timeout = Poll::failed(start, start + ms(20_000), "timeout: timed out");
    writer
        .append("duk", "vehicles", &timeout, None, &Map::new())
        .unwrap();
    let polls: Vec<_> = iter_polls(&root, "duk", "vehicles", day(5), day(5))
        .map(Result::unwrap)
        .collect();
    let statuses: Vec<Option<i64>> = polls.iter().map(|p| p.status()).collect();
    assert_eq!(statuses, [Some(502), None]);
    assert_eq!(polls[0].body().unwrap().unwrap(), b"bad gateway");
    assert_eq!(polls[1].error(), Some("timeout: timed out"));
    assert_eq!(polls[1].body().unwrap(), None);
    assert!(!polls[0].succeeded() && !polls[1].succeeded());
}

#[test]
fn index_rolls_over_at_utc_midnight() {
    let root = scratch("rollover");
    let writer = ArchiveWriter::new(&root);
    writer
        .append("duk", "vehicles", &poll(b"a"), Some(b"a"), &Map::new())
        .unwrap();
    let later = poll_at(Some(b"b"), t0() + ms(15_000), Some(200));
    writer
        .append("duk", "vehicles", &later, Some(b"b"), &Map::new())
        .unwrap();
    assert!(root.join("duk/vehicles/2026-10-05/index.jsonl").is_file());
    assert!(root.join("duk/vehicles/2026-10-06/index.jsonl").is_file());
    let got = bodies(&root, "duk", "vehicles", day(5), day(6));
    assert_eq!(got, [Some(b"a".to_vec()), Some(b"b".to_vec())]);
    assert_eq!(crate::archive::days(&root), [day(5), day(6)]);
    assert_eq!(crate::archive::sources(&root), ["duk"]);
}

#[test]
fn truncated_last_index_line_is_skipped_but_a_broken_middle_line_is_an_error() {
    let root = scratch("truncated");
    let writer = ArchiveWriter::new(&root);
    writer
        .append("duk", "vehicles", &poll(b"a"), Some(b"a"), &Map::new())
        .unwrap();
    let index = root.join("duk/vehicles/2026-10-05/index.jsonl");
    let mut text = std::fs::read_to_string(&index).unwrap();
    text.push_str("{\"requested_at\": \"2026-");
    std::fs::write(&index, &text).unwrap();
    assert_eq!(
        bodies(&root, "duk", "vehicles", day(5), day(5)),
        [Some(b"a".to_vec())]
    );
    text.push('\n');
    std::fs::write(&index, &text).unwrap();
    let results: Vec<_> = iter_polls(&root, "duk", "vehicles", day(5), day(5)).collect();
    assert!(results[0].is_ok() && results[1].is_err());
}

#[test]
fn prune_deletes_only_day_directories_before_the_cutoff() {
    let root = scratch("prune");
    let writer = ArchiveWriter::new(&root);
    writer
        .append("duk", "vehicles", &poll(b"a"), Some(b"a"), &Map::new())
        .unwrap();
    let later = poll_at(Some(b"b"), t0() + ms(15_000), Some(200));
    writer
        .append("duk", "vehicles", &later, Some(b"b"), &Map::new())
        .unwrap();
    writer
        .append("pid", "vehicles", &poll(b"c"), Some(b"c"), &Map::new())
        .unwrap();
    std::fs::create_dir(root.join("duk/vehicles/notes")).unwrap();
    let pruned = prune(&root, day(6), None).unwrap();
    let mut names: Vec<String> = pruned
        .iter()
        .map(|p| {
            p.strip_prefix(&root)
                .unwrap()
                .to_string_lossy()
                .replace('\\', "/")
        })
        .collect();
    names.sort();
    assert_eq!(
        names,
        ["duk/vehicles/2026-10-05", "pid/vehicles/2026-10-05"]
    );
    let mut left: Vec<String> = std::fs::read_dir(root.join("duk/vehicles"))
        .unwrap()
        .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
        .collect();
    left.sort();
    assert_eq!(left, ["2026-10-06", "notes"]);
    assert!(
        prune(&root.join("missing"), day(6), None)
            .unwrap()
            .is_empty()
    );
}

#[test]
fn the_index_line_holds_the_poll_and_its_extra_fields() {
    let mut p = poll(b"{\"VehicleList\":[]}");
    p.received_at = t0() + TimeDelta::from_micros(120_500);
    p.content_type = Some("application/json; charset=utf-8".into());
    p.headers.insert("etag".into(), "W/\"x\"".into());
    let mut extra = Map::new();
    extra.insert("filter".into(), json!("dpmd@1"));
    extra.insert("request".into(), json!({"sr70": "534149"}));
    let body = p.body.clone().unwrap();
    let sha = crate::archive::sha256_hex(&body);
    let line: Value =
        serde_json::from_str(&index_line(&p, Some(&sha), Some(&body), &extra)).unwrap();
    assert_eq!(
        line,
        json!({
            "requested_at": "2026-10-05T23:59:50.000000+00:00",
            "received_at": "2026-10-05T23:59:50.120500+00:00",
            "elapsed_ms": 121,
            "status": 200,
            "sha256": sha,
            "bytes": 18,
            "content_type": "application/json; charset=utf-8",
            "headers": {"date": "Mon, 05 Oct 2026 21:59:50 GMT", "etag": "W/\"x\""},
            "error": null,
            "filter": "dpmd@1",
            "request": {"sr70": "534149"},
        })
    );
}

#[test]
fn elapsed_ms_rounds_to_the_nearest() {
    let at = t0();
    let after = |us| crate::archive::elapsed_ms(at, at + TimeDelta::from_micros(us));
    assert_eq!(
        [
            after(499),
            after(500),
            after(2501),
            after(-499),
            after(-1500)
        ],
        [0, 1, 3, 0, -1]
    );
}

fn keep_express(body: &[u8]) -> Result<Filtered, FilterError> {
    let text = std::str::from_utf8(body).map_err(|e| FilterError(e.to_string()))?;
    if !text.starts_with('[') {
        return Err(FilterError("not a GraphQL batch".into()));
    }
    let all: Vec<&str> = text.trim_matches(['[', ']']).split(',').collect();
    let kept: Vec<&str> = all
        .iter()
        .copied()
        .filter(|v| v.contains("EXPRESS"))
        .collect();
    Ok(Filtered {
        body: format!("[{}]", kept.join(",")).into_bytes(),
        kept: kept.len() as i64,
        dropped: (all.len() - kept.len()) as i64,
    })
}

fn filters() -> Filters {
    Filters(BTreeMap::from([(
        "arriva-express".to_owned(),
        PayloadFilter {
            version: 1,
            apply: keep_express,
        },
    )]))
}

#[test]
fn a_filter_keeps_what_it_keeps_and_says_so() {
    let channel = channel("arriva-express", "buses", Some("arriva-express"));
    let source = b"[\"ARRIVA EXPRESS\",\"MHD\",\"ARRIVA EXPRESS\",\"REGIONAL\"]";
    let (body, extra) = archived_payload(&channel, &poll(source), &filters());
    assert_eq!(body.unwrap(), b"[\"ARRIVA EXPRESS\",\"ARRIVA EXPRESS\"]");
    assert_eq!(extra["filter"], json!("arriva-express@1"));
    assert_eq!((&extra["kept"], &extra["dropped"]), (&json!(2), &json!(2)));
    assert_eq!(extra["source_bytes"], json!(source.len()));
    assert!(
        extra["source_sha256"]
            .as_str()
            .is_some_and(|s| s.len() == 64)
    );
}

#[test]
fn a_filter_stores_an_unexpected_shape_unfiltered() {
    let channel = channel("arriva-express", "buses", Some("arriva-express"));
    let source = b"{\"errors\":[{\"message\":\"schema changed\"}]}";
    let (body, extra) = archived_payload(&channel, &poll(source), &filters());
    assert_eq!(body.unwrap(), source);
    assert_eq!(extra["filter_error"], json!("not a GraphQL batch"));
    // A failed poll is stored as sent, labelled with the filter only.
    let failed = poll_at(Some(b"oops"), t0(), Some(500));
    let (body, extra) = archived_payload(&channel, &failed, &filters());
    assert_eq!((body.unwrap(), extra.len()), (b"oops".to_vec(), 1));
}

#[test]
fn a_channel_whose_filter_is_not_ported_is_not_recorded() {
    let channels = vec![
        channel("duk", "vehicles", None),
        channel("dpmd", "vehicles", Some("dpmd")),
    ];
    let logs = Mutex::new(Vec::<String>::new());
    let kept = filterable(channels, &Filters::default(), &|m| {
        logs.lock().unwrap().push(m.into())
    });
    assert_eq!(kept.len(), 1);
    assert!(logs.lock().unwrap()[0].contains("dpmd/vehicles: filter 'dpmd'"));
}

#[test]
fn parse_durations() {
    assert_eq!(parse_duration("90s"), Ok(90.0));
    assert_eq!(parse_duration("6h"), Ok(6.0 * 3600.0));
    assert_eq!(parse_duration(" 2d "), Ok(2.0 * 86400.0));
    assert_eq!(parse_duration("1.5m"), Ok(90.0));
    assert!(parse_duration("6 hours").unwrap_err().contains("duration"));
    assert!(parse_duration(".5s").is_err() && parse_duration("5.s").is_err());
}

#[test]
fn stats_lines_read_as_pythons() {
    let stats = ChannelStats {
        polls: 3,
        errors: 1,
        filter_errors: 2,
        new_objects: 2,
        stored_bytes: 1_234_567,
        latencies_ms: vec![100.0, 300.0],
    };
    assert_eq!(
        stats.line("duk/vehicles"),
        "duk/vehicles: 3 polls, 1 errors, 2 new payloads, 1,234,567 bytes stored, median 200 ms, \
         2 filter errors"
    );
    assert!(ChannelStats::default().line("x").contains("median nan ms"));
}

struct Fixed(BTreeMap<String, Vec<u8>>);

impl Fetcher for Fixed {
    async fn fetch(&self, channel: &Channel, _: Option<Vec<u8>>) -> Poll {
        poll(&self.0[&channel.name()])
    }
}

struct NoDemand;

impl DueSource for NoDemand {
    async fn due(&mut self, _: Instant) -> Result<DueCalls, HandlerError> {
        Ok(DueCalls::new())
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn record_once_writes_one_poll_per_channel() {
    let root = scratch("once");
    let channels = vec![
        channel("duk", "vehicles", None),
        channel("arriva-express", "buses", Some("arriva-express")),
    ];
    let payloads = BTreeMap::from([
        ("duk/vehicles".to_owned(), b"{\"VehicleList\":[]}".to_vec()),
        (
            "arriva-express/buses".to_owned(),
            b"[\"ARRIVA EXPRESS\",\"MHD\"]".to_vec(),
        ),
    ]);
    let stats = record::<_, NoDemand>(
        channels,
        &root,
        RecordOptions {
            once: true,
            ..RecordOptions::default()
        },
        Arc::new(Fixed(payloads.clone())),
        None,
        filters(),
        system_clock(),
        Stop::new(),
        Arc::new(|_| {}),
    )
    .await
    .unwrap();
    let polls: Vec<(String, u64)> = stats.iter().map(|(k, v)| (k.clone(), v.polls)).collect();
    assert_eq!(
        polls,
        [
            ("duk/vehicles".to_owned(), 1),
            ("arriva-express/buses".to_owned(), 1)
        ]
    );
    let arriva: Vec<_> = iter_polls(&root, "arriva-express", "buses", day(5), day(5))
        .map(Result::unwrap)
        .collect();
    assert_eq!(arriva.len(), 1);
    assert_eq!(arriva[0].entry["kept"], 1);
    assert_eq!(
        bodies(&root, "duk", "vehicles", day(5), day(5)),
        [Some(payloads["duk/vehicles"].clone())]
    );
}

#[test]
fn the_command_line_takes_obehy_rt_records_flags() {
    use clap::Parser as _;
    let cli = crate::cli::Cli::try_parse_from([
        "obehy-rt",
        "record",
        "--archive",
        "x",
        "--sources",
        " duk,,pid ",
        "--once",
        "--duration",
        "30m",
        "--config",
        "c.toml",
        "--database-url",
        "postgresql://h/d",
        "--manifest",
        "m",
    ])
    .unwrap();
    let crate::cli::Command::Record(args) = cli.command else {
        panic!("not record");
    };
    assert_eq!(
        args.sources,
        Some(SourceList(vec!["duk".into(), "pid".into()]))
    );
    assert_eq!((args.once, args.duration), (true, Some(1800.0)));
    assert_eq!(args.archive, PathBuf::from("x"));
    let bare = crate::cli::Cli::try_parse_from(["obehy-rt", "record"]).unwrap();
    let crate::cli::Command::Record(args) = bare.command else {
        panic!("not record");
    };
    assert_eq!(args.archive, PathBuf::from("data/rt-raw"));
    assert!(
        crate::cli::Cli::try_parse_from(["obehy-rt", "record", "--duration", "6 hours"]).is_err()
    );
}

fn arriva_body(main_types: &[&str]) -> Vec<u8> {
    let vehicles: Vec<String> = main_types
        .iter()
        .enumerate()
        .map(|(i, kind)| format!("{{\"spz\": \"1AB{i}\", \"mainType\": \"{kind}\"}}"))
        .collect();
    format!(
        "[{{\"data\": {{\"busesCurrentLocations\": [{}]}}}}]",
        vehicles.join(", ")
    )
    .into_bytes()
}

#[test]
fn the_arriva_filter_keeps_only_express() {
    let channel = channel("arriva-express", "buses", Some("arriva-express"));
    let source = arriva_body(&["ARRIVA EXPRESS", "REGIONÁLNÍ BUS", "MHD", "ARRIVA EXPRESS"]);
    let (body, extra) = archived_payload(&channel, &poll(&source), &Filters::shipped());
    let body = body.unwrap();
    let document: serde_json::Value = serde_json::from_slice(&body).unwrap();
    let spz: Vec<&str> = document[0]["data"]["busesCurrentLocations"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| v["spz"].as_str().unwrap())
        .collect();
    assert_eq!(spz, ["1AB0", "1AB3"]);
    assert!(!String::from_utf8_lossy(&body).contains("REGIONÁLNÍ"));
    assert_eq!(extra["filter"], json!("arriva-express@1"));
    assert_eq!((&extra["kept"], &extra["dropped"]), (&json!(2), &json!(2)));
    assert_eq!(extra["source_bytes"], json!(source.len()));
}

#[test]
fn the_arriva_filter_stores_an_unexpected_shape_unfiltered() {
    let channel = channel("arriva-express", "buses", Some("arriva-express"));
    let source = b"{\"errors\":[{\"message\":\"schema changed\"}]}";
    let (body, extra) = archived_payload(&channel, &poll(source), &Filters::shipped());
    assert_eq!(body.unwrap(), source);
    assert!(matches!(&extra["filter_error"], Value::String(m) if m.contains("GraphQL batch")));
}

const DPMD_BODY: &str = concat!(
    "/* {\"TimeStamp\":\"2026-10-11T17:30:16.391\"} */",
    " busstops=[{id:`c8e1d1e7`,coords:`50.1N, 14.2E`}];",
    " buspositions=[{id:`3f2172e3`,line:`201`,vehicle:`842`,driver:`1103`,course:`7115`,",
    "header:`<span>Autobus: 842</span>`,body:`<br>Čas: 11.10.2026 17:30:03<br>Řidič: 1103",
    "<br>Linka: 201<br>Spoj: 351<br><img src=\"`+vehicleImageUrl + `842.jpg\" ",
    "onclick=\"window.open('x')\">`,hint:`Autobus: 842\nLinka: 201\n`,fuel:`C`,arrow:`85`,",
    "coords:`50.78264999N, 14.22869015E`,final1: {name:`Chrochvice`,coords:`50.7N, 14.1E`},",
    " final2: null, shapes:`<gpx></gpx>`,off: 0},",
    " {id:`aa`,line:``,vehicle:`900`,driver:`77`,body:`<br>Autobus: 900`,off: 1}];",
    " legends=[{id:`c5`,text:`Jízda načas`,mark:``}]; datahash='0b221b33';",
);

#[test]
fn the_dpmd_filter_keeps_vehicles_in_service_without_drivers() {
    // DPMD-Q2 (JavaScript literals), DPMD-Q5 (parked vehicles), DPMD-Q8 (driver numbers).
    let channel = channel("dpmd", "vehicles", Some("dpmd"));
    let (body, extra) =
        archived_payload(&channel, &poll(DPMD_BODY.as_bytes()), &Filters::shipped());
    let body = body.unwrap();
    let document: serde_json::Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(document["TimeStamp"], "2026-10-11T17:30:16.391");
    let buses = document["buspositions"].as_array().unwrap();
    assert_eq!(buses.len(), 1);
    assert_eq!(buses[0]["vehicle"], "842");
    let text = String::from_utf8_lossy(&body);
    assert!(!text.contains("1103") && !text.contains("Řidič") && !text.contains("busstops"));
    assert_eq!(extra["filter"], json!("dpmd@1"));
    assert_eq!((&extra["kept"], &extra["dropped"]), (&json!(1), &json!(1)));
}

#[test]
fn the_dpmd_filter_stores_an_unexpected_shape_unfiltered() {
    let channel = channel("dpmd", "vehicles", Some("dpmd"));
    let source = b"/* {\"TimeStamp\":\"2026-10-11T17:30:16\"} */ buspositions=eval(`x`);";
    let (body, extra) = archived_payload(&channel, &poll(source), &Filters::shipped());
    assert_eq!(body.unwrap(), source);
    assert!(matches!(&extra["filter_error"], Value::String(m) if m.contains("eval")));
}
