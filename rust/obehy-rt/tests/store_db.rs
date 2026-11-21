//! The store, warm start's reader and the lookup broker against PostgreSQL (docs/RUST_PORT.md
//! R7/R8; the Python worker's history and broker tests, ported). Each test makes a small migrated database from the test database URL
//! (`OBEHY_TEST_DATABASE_URL` or `[database] test_url`) through `rust/tools/store_db.py` and
//! drops it afterwards; without a URL the tests pass without running. No request leaves
//! 127.0.0.1.

use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};

use obehy_core::config::manifest;
use obehy_core::model::*;
use obehy_core::time::{Date, Instant};
use obehy_rt::archive::{ArchiveWriter, Poll};
use obehy_rt::fetch::{Clock, Http, fetch};
use obehy_rt::lookups::{LookupBroker, connect, run_broker};
use obehy_rt::scheduler::{Fetcher, Stop};
use obehy_rt::settings::{Paths, ProcessEnv, load_test_database_url};
use obehy_rt::store::stored::{Window, stored_observations};
use obehy_rt::store::vehicles::{PlainBook, physical_rows};
use obehy_rt::store::{PgStore, RecordingStore, Writer};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;
use tokio_postgres::{Config, NoTls};

fn repo() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../..")
}

fn tool(args: &[&str]) -> String {
    let out = Command::new("uv")
        .args(["run", "python", "rust/tools/store_db.py"])
        .args(args)
        .current_dir(repo())
        .output()
        .expect("running rust/tools/store_db.py");
    assert!(
        out.status.success(),
        "store_db.py {} failed: {}",
        args[0],
        String::from_utf8_lossy(&out.stderr)
    );
    String::from_utf8_lossy(&out.stdout).trim().to_owned()
}

/// A migrated database of its own, dropped with the value.
struct TestDb {
    name: String,
    config: Config,
}

impl TestDb {
    fn create() -> Option<TestDb> {
        let paths = Paths::discover().ok()?;
        let url = load_test_database_url(&paths.config(), &ProcessEnv)?;
        let mut config: Config = url.parse().expect("the test database URL parses");
        let name = tool(&["create"]);
        config.dbname(&name);
        Some(TestDb { name, config })
    }

    fn store(&self) -> PgStore {
        let paths = Paths::discover().unwrap();
        let sources =
            manifest::load_sources(&paths.sources(), &manifest::RECORDER_FILTERS).unwrap();
        PgStore::connect_config(&self.config, physical_rows(&sources)).unwrap()
    }

    fn query(&self, sql: &str) -> Vec<Vec<String>> {
        tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap()
            .block_on(query(&self.config, sql))
    }
}

/// Every column as text, in UTC.
async fn query(config: &Config, sql: &str) -> Vec<Vec<String>> {
    let (client, connection) = config.connect(NoTls).await.unwrap();
    tokio::spawn(connection);
    client.batch_execute("SET TIME ZONE 'UTC'").await.unwrap();
    client
        .simple_query(sql)
        .await
        .unwrap()
        .into_iter()
        .filter_map(|m| match m {
            tokio_postgres::SimpleQueryMessage::Row(row) => Some(
                (0..row.len())
                    .map(|i| row.get(i).unwrap_or("NULL").to_owned())
                    .collect(),
            ),
            _ => None,
        })
        .collect()
}

impl Drop for TestDb {
    fn drop(&mut self) {
        tool(&["drop", &self.name]);
    }
}

fn at(text: &str) -> Instant {
    Instant::from_isoformat(text).unwrap()
}

const D8: Date = Date::ymd(2026, 10, 8);

fn journey(key: &str) -> JourneyKey {
    JourneyKey {
        feed: Feed::Jdf,
        namespace: "cis:line_trip".into(),
        key: key.into(),
        service_date: D8,
    }
}

fn observation(item: i64, received: &str, vehicle: Option<&str>) -> Arc<Observation> {
    let mut facts: Vec<Fact> = Vec::new();
    if let Some(vehicle) = vehicle {
        facts.push(
            VehicleKey {
                source_vehicle_id: vehicle.into(),
            }
            .into(),
        );
    }
    facts.push(
        TripKey {
            namespace: "cis:line_trip".into(),
            key: "1:1".into(),
        }
        .into(),
    );
    facts.push(
        Position {
            lat: 50.081_234_5,
            lon: 14.420_1,
            bearing: Some(90.5),
        }
        .into(),
    );
    Arc::new(Observation {
        source: "dpmd".into(),
        channel: "vehicles".into(),
        feed: Feed::Jdf,
        received_at: at(received),
        observed_at: Some(at(received) - obehy_core::time::TimeDelta::from_seconds(5)),
        raw: RawRef {
            sha256: "ab".repeat(32),
            item,
        },
        decoder_version: 2,
        facts,
    })
}

fn snapshot(trip: &str, stops: &[&str]) -> SnapshotJourney {
    SnapshotJourney {
        journey: journey("1:1"),
        at: at("2026-10-08T06:00:00+00:00"),
        release_id: "run-a".into(),
        trip_id: trip.into(),
        route_name: "1".into(),
        headsign: Some("Děčín".into()),
        calls: stops
            .iter()
            .enumerate()
            .map(|(i, s)| ScheduledCall {
                ordinal: i as i64 + 1,
                location_id: (*s).into(),
                visit_n: 1,
                passenger_service: true,
                scheduled_arrival: Some(21_600 + 300 * i as i32),
                scheduled_departure: Some(21_600 + 300 * i as i32),
                name: format!("Zastávka {s}"),
            })
            .collect::<Vec<_>>()
            .into(),
        run_key: None,
        route_id: Some("jdf:route:1".into()),
        mode: Some("bus".into()),
        agency_id: None,
    }
}

fn platform(label: PlatformLabel, assigned: &str) -> Effect {
    Effect::RecordPlatform(RecordPlatform {
        journey: journey("1:1"),
        location_id: "B".into(),
        visit_n: 1,
        kind: StopKind::Arrival,
        platform: Platform {
            value: "2".into(),
            label,
            boarding_point_id: None,
            assigned_at: at(assigned),
            source: "sz-tabule".into(),
        },
    })
}

fn effects() -> Vec<Effect> {
    let result = |obs: Arc<Observation>| {
        Effect::ObservationResult(ObservationResult {
            observation: obs,
            journey: Some(journey("1:1")),
            reason: None,
            public: Vec::new(),
            state_since: None,
            method: BindingMethod::Keyed,
        })
    };
    vec![
        result(observation(0, "2026-10-08T06:00:05+00:00", Some("7"))),
        result(observation(1, "2026-10-08T07:00:05+00:00", None)),
        // The same journey twice in one write: a new revision each, the second re-attaching.
        Effect::SnapshotJourney(snapshot("t1", &["A", "B"])),
        Effect::SnapshotJourney(snapshot("t2", &["A", "C"])),
        Effect::WriteEvent(WriteEvent {
            journey: journey("1:1"),
            location_id: "A".into(),
            visit_n: 1,
            kind: EventKind::Departure,
            interval: Interval::new(
                at("2026-10-08T06:00:00+00:00"),
                at("2026-10-08T06:00:05+00:00"),
            ),
            method: EventMethod::Progress,
            source: "dpmd".into(),
        }),
        Effect::AssignVehicle(AssignVehicle {
            vehicle: VehicleId {
                source: "dpmd".into(),
                source_vehicle_id: "7".into(),
            },
            journey: journey("1:1"),
            at: at("2026-10-08T06:01:00+00:00"),
            method: BindingMethod::Keyed,
        }),
        Effect::LinkJourneys(LinkJourneys {
            source: journey("1:1"),
            target: journey("1:2"),
            kind: JourneyLinkKind::ContinuesAs,
        }),
        platform(PlatformLabel::Platform, "2026-10-08T06:02:00+00:00"),
        platform(PlatformLabel::Track, "2026-10-08T05:59:00+00:00"),
        Effect::RecordCancellation(RecordCancellation {
            journey: journey("1:1"),
            source: "pid".into(),
            at: at("2026-10-08T05:00:00+00:00"),
            cancelled: true,
        }),
        Effect::RecordCancellation(RecordCancellation {
            journey: journey("1:1"),
            source: "dpmd".into(),
            at: at("2026-10-08T06:00:00+00:00"),
            cancelled: false,
        }),
    ]
}

fn derivation() -> Derivation {
    Derivation {
        core_version: "r1.0".into(),
        policy_version: "19".into(),
        release_id: "run-a".into(),
    }
}

fn check_history(db: &TestDb) {
    assert_eq!(
        db.query("SELECT key, latest_revision FROM history.journey"),
        [["1:1", "2"]]
    );
    assert_eq!(
        db.query("SELECT revision, trip_id, headsign FROM history.journey_schedule ORDER BY 1"),
        [["1", "t1", "Děčín"], ["2", "t2", "Děčín"]]
    );
    assert_eq!(
        db.query(
            "SELECT revision, ordinal, location_id, name FROM history.named_call ORDER BY 1, 2"
        )
        .len(),
        4
    );
    assert_eq!(
        db.query(
            "SELECT e.revision, e.event_type, e.event_time, e.orphaned, t.value \
             FROM history.actual_stop_event e JOIN history.term t ON t.term_id = e.location_term"
        ),
        [["2", "departure", "2026-10-08 06:00:02.5+00", "f", "A"]]
    );
    assert_eq!(
        db.query(
            "SELECT source_vehicle_id, first_seen, last_seen, method, vehicle_id \
             FROM history.vehicle_assignment"
        ),
        [[
            "7",
            "2026-10-08 06:00:00+00",
            "2026-10-08 06:01:00+00",
            "keyed",
            "dpmd:7"
        ]]
    );
    assert_eq!(
        db.query("SELECT source_key, target_key, kind FROM history.journey_link"),
        [["1:1", "1:2", "continues_as"]]
    );
    assert_eq!(
        db.query("SELECT label, first_seen, last_seen FROM history.platform_evidence"),
        [["track", "2026-10-08 05:59:00+00", "2026-10-08 06:02:00+00"]]
    );
    assert_eq!(
        db.query("SELECT source, restored_at FROM history.journey_cancellation"),
        [["pid", "2026-10-08 06:00:00+00"]]
    );
    assert_eq!(db.query("SELECT count(*) FROM rt.observation"), [["2"]]);
    assert!(
        db.query("SELECT count(*) FROM rt.physical_vehicles")[0][0] != "0",
        "the physical policy is written when the writer opens"
    );
}

#[test]
fn effects_become_observations_and_history_and_read_back() {
    for copy_from in [0, usize::MAX] {
        let Some(db) = TestDb::create() else {
            return;
        };
        let mut store = db.store();
        store.copy_from = copy_from;
        let mut writer = Writer::open(store, derivation(), |_| PlainBook).unwrap();
        let effects = effects();
        writer.write(&effects).unwrap();
        check_history(&db);
        // The current schedule again: no new revision (the older t1 would be one).
        writer.write(&effects[3..]).unwrap();
        assert_eq!(
            db.query("SELECT latest_revision FROM history.journey"),
            [["2"]],
            "a schedule already stored is no new revision"
        );
        // Warm start reads the observations back as they were.
        let window = Window {
            since: at("2026-10-08T05:00:00+00:00"),
            until: at("2026-10-08T08:00:00+00:00"),
            alive: None,
        };
        let mut read: Vec<Observation> = Vec::new();
        let count = writer
            .store
            .block_on(stored_observations(
                writer.store.client(),
                &window,
                1,
                |chunk| {
                    read.extend(chunk);
                    true
                },
            ))
            .unwrap();
        assert_eq!(count, 2);
        let written: Vec<Observation> = effects[..2]
            .iter()
            .map(|e| match e {
                Effect::ObservationResult(r) => (*r.observation).clone(),
                _ => unreachable!(),
            })
            .collect();
        assert_eq!(read, written);
    }
}

fn instance(lateness: i64) -> Instance {
    let t = at("2026-10-08T06:00:00+00:00");
    Instance {
        journey: journey("1:1"),
        release_id: "run-a".into(),
        trip_id: "t2".into(),
        mode: "bus".into(),
        lifecycle: Lifecycle::Running,
        calls: vec![CallState::new(0, "A", 1), CallState::new(1, "C", 1)].into(),
        freshness: Freshness {
            updated_at: t,
            heard_at: t,
            stale: false,
            lost: false,
        },
        progress: None,
        track: None,
        reported_delay_s: None,
        lateness_s: Some(lateness),
        off_route_since: None,
        off_route: false,
        lead: None,
        journeys: Vec::new(),
        parts: Vec::new(),
        sources: Vec::new(),
        track_source: None,
        shown_source: None,
        published: None,
        held: Vec::new(),
    }
}

fn vehicle(id: &str, bound: bool) -> VehicleState {
    let vehicle = VehicleId {
        source: "dpmd".into(),
        source_vehicle_id: id.into(),
    };
    VehicleState {
        binding: bound.then(|| Binding {
            vehicle: vehicle.clone(),
            journey: journey("1:1"),
            trip_id: "t2".into(),
            method: BindingMethod::Keyed,
            bound_at: at("2026-10-08T06:00:00+00:00"),
            confirmed: true,
        }),
        vehicle,
        feed: Feed::Jdf,
        status: VehicleStatus::Running,
        last_seen: at("2026-10-08T06:00:00+00:00"),
        trip_key: None,
        position: Some(Position {
            lat: 50.0,
            lon: 14.0,
            bearing: None,
        }),
        position_at: Some(at("2026-10-08T06:00:00+00:00")),
        reason: None,
        episode: None,
        facts: Vec::new().into(),
        since: None,
    }
}

fn state(vehicles: &[(&str, bool)], lateness: i64) -> FeedState {
    let mut state = FeedState::new(Feed::Jdf);
    for (id, bound) in vehicles {
        let v = vehicle(id, *bound);
        state.vehicles.insert(v.vehicle.clone(), v);
    }
    state.instances.insert(journey("1:1"), instance(lateness));
    state
}

#[test]
fn state_tables_are_replaced_then_kept_in_step() {
    // What a recording writer sends, without a database.
    let mut recording =
        Writer::open(RecordingStore::default(), derivation(), |_| PlainBook).unwrap();
    let first = state(&[("7", true), ("8", false)], 60);
    recording.write_state(&[(Feed::Jdf, &first)]).unwrap();
    let second = state(&[("7", true)], 120);
    recording.write_state(&[(Feed::Jdf, &second)]).unwrap();
    let ops: Vec<String> = recording
        .store
        .take()
        .iter()
        .map(obehy_rt::canonical::line)
        .filter(|l| l.contains("\"kind\":\"state\""))
        .map(|l| {
            let v: serde_json::Value = serde_json::from_str(&l).unwrap();
            format!(
                "{} {}",
                v["table"].as_str().unwrap(),
                v["op"].as_str().unwrap()
            )
        })
        .collect();
    assert_eq!(
        ops,
        [
            "vehicle_state_current replace",
            "vehicle_state_current insert",
            "vehicle_state_current insert",
            "trip_state_current replace",
            "trip_state_current insert",
            "trip_part_current replace",
            "trip_part_current insert",
            "vehicle_state_current delete",
            "trip_state_current upsert",
        ]
    );

    let Some(db) = TestDb::create() else {
        return;
    };
    let mut writer = Writer::open(db.store(), derivation(), |_| PlainBook).unwrap();
    writer.write_state(&[(Feed::Jdf, &first)]).unwrap();
    assert_eq!(
        db.query(
            "SELECT source_vehicle_id, key, trip_id, vehicle_id, represents \
             FROM rt.vehicle_state_current ORDER BY 1"
        ),
        [
            ["7", "1:1", "t2", "dpmd:7", "t"],
            ["8", "NULL", "NULL", "dpmd:8", "t"]
        ]
    );
    writer.write_state(&[(Feed::Jdf, &second)]).unwrap();
    assert_eq!(
        db.query("SELECT source_vehicle_id FROM rt.vehicle_state_current"),
        [["7"]]
    );
    assert_eq!(
        db.query(
            "SELECT delay_s, calls->1->>'location_id', calls->0->>'passed' \
             FROM rt.trip_state_current"
        ),
        [["120", "C", "false"]]
    );
    assert_eq!(
        db.query("SELECT trip_id, first_call, last_call FROM rt.trip_part_current"),
        [["t2", "0", "1"]]
    );
}

// --- the lookup broker --------------------------------------------------------------------

/// A local server answering every request with a ČD composition, counting requests.
async fn stub(body: Vec<u8>, served: Arc<AtomicUsize>) -> String {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    tokio::spawn(async move {
        loop {
            let Ok((mut stream, _)) = listener.accept().await else {
                return;
            };
            served.fetch_add(1, Ordering::SeqCst);
            let body = body.clone();
            tokio::spawn(async move {
                let mut request = vec![0u8; 65_536];
                let _ = stream.read(&mut request).await;
                let head = format!(
                    "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\
                     Connection: close\r\n\r\n",
                    body.len()
                );
                let _ = stream.write_all(head.as_bytes()).await;
                let _ = stream.write_all(&body).await;
            });
        }
    });
    format!("http://{address}/composition")
}

/// Sends every lookup request to the stub instead of its upstream; keeps the URLs asked.
struct StubFetcher {
    url: String,
    asked: Arc<Mutex<Vec<String>>>,
    http: Http,
    clock: Clock,
}

impl Fetcher for StubFetcher {
    async fn fetch(&self, channel: &obehy_core::config::Channel, _: Option<Vec<u8>>) -> Poll {
        self.asked.lock().unwrap().push(channel.url.clone());
        let local = obehy_core::config::Channel {
            url: self.url.clone(),
            proxied: false,
            ..channel.clone()
        };
        fetch(&self.http, &local, &self.clock).await
    }
}

#[test]
fn the_broker_answers_requests_through_the_lookups() {
    let Some(db) = TestDb::create() else {
        return;
    };
    tool(&["broker-fixture", &db.name]);
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
        .unwrap();
    let archive = std::env::temp_dir().join(format!("obehy-broker-{}", db.name));
    runtime.block_on(async {
        let body = std::fs::read(repo().join("tests/realtime/compositions/cd-607.json")).unwrap();
        let served = Arc::new(AtomicUsize::new(0));
        let url = stub(body, Arc::clone(&served)).await;
        let paths = Paths::discover().unwrap();
        let lookups: Vec<_> = manifest::load_sources(&paths.sources(), &manifest::RECORDER_FILTERS)
            .unwrap()
            .into_iter()
            .flat_map(|s| s.lookups)
            .collect();
        let asked = Arc::new(Mutex::new(Vec::new()));
        // The 7th, at 06:10 in Prague: ČD answers for trains around today.
        let on_the_day = at("2026-10-07T04:10:00+00:00");
        let clock: Clock = Arc::new(move || on_the_day);
        let fetcher = StubFetcher {
            url,
            asked: Arc::clone(&asked),
            http: Http::new(),
            clock: Arc::clone(&clock),
        };
        let policy = paths.load_policy().unwrap();
        let logged = Arc::new(Mutex::new(Vec::<String>::new()));
        let log_to = Arc::clone(&logged);
        let mut broker = LookupBroker::new(
            connect(&db.config).await.unwrap(),
            &lookups,
            fetcher,
            ArchiveWriter::new(&archive),
            clock,
            Box::new(|| 0.0),
            Arc::new(move |line: &str| log_to.lock().unwrap().push(line.to_owned())),
            policy.journey.clone(),
        );
        // The run's second part from Zákupy, which the train has not left; a bus (no lookup
        // covers its feed); the first part, left at its only departing call: asked from there.
        assert_eq!(broker.serve().await.unwrap(), 3);
        let asked_now = asked.lock().unwrap().clone();
        assert_eq!(asked_now.len(), 2);
        assert!(asked_now[0].contains("%22SR70%22%3A%225454002%22"), "{}", asked_now[0]);
        assert!(asked_now[1].contains("%22SR70%22%3A%225454000%22"), "{}", asked_now[1]);
        assert_eq!(served.load(Ordering::SeqCst), 2);
        assert_eq!(
            query(&db.config, "SELECT lookup, cache_key, outcome, status FROM rt.lookup_fetch ORDER BY fetch_id").await,
            [
                ["composition", "6600/2026-10-07/5454002", "answered", "200"],
                ["composition", "6600/2026-10-07/5454000", "answered", "200"]
            ]
        );
        assert_eq!(
            query(&db.config,
                "SELECT c.from_location, count(u.*) FROM rt.composition c \
                 JOIN rt.composition_unit u USING (composition_id) GROUP BY 1 ORDER BY 1"
            ).await,
            [["czptt:location:CZ54000", "6"], ["czptt:location:CZ54002", "6"]]
        );
        assert_eq!(
            query(&db.config,
                "SELECT trip_id, pending, answered_at IS NOT NULL, outcomes->'cd/composition'->>'outcome' \
                 FROM rt.lookup_request ORDER BY trip_id"
            ).await,
            [
                ["czptt:trip:1:1", "f", "t", "answered"],
                ["czptt:trip:1:2", "f", "t", "answered"],
                ["jdf:trip:1", "f", "t", "NULL"]
            ]
        );
        assert!(archive.join("cd").join("composition").is_dir());

        // Asked again from a call planned to the same answer: on NOTIFY, served from the cache.
        let stop = Stop::new();
        let stopper = stop.clone();
        let config = db.config.clone();
        let running = tokio::spawn(async move {
            run_broker(&mut broker, &config, &stopper, 3600.0).await.unwrap();
            broker
        });
        let (client, connection) = db.config.connect(NoTls).await.unwrap();
        tokio::spawn(connection);
        tokio::time::sleep(std::time::Duration::from_millis(500)).await;
        client
            .batch_execute(
                "INSERT INTO rt.lookup_request (kind, feed, trip_id, service_date, from_location, \n                 requested_at, pending, outcomes) VALUES ('composition', 'czptt', 'czptt:trip:1:2', \
                 '2026-10-07', 'czptt:location:CZ54002', statement_timestamp(), true, '{}'); \
                 NOTIFY obehy_lookup",
            )
            .await
            .unwrap();
        let mut answered = false;
        for _ in 0..100 {
            let pending = query(&db.config,
                "SELECT pending, outcomes->'cd/composition'->>'cache_key' FROM rt.lookup_request \
                 WHERE from_location = 'czptt:location:CZ54002'",
            ).await;
            if pending == [["f", "6600/2026-10-07/5454002"]] {
                answered = true;
                break;
            }
            tokio::time::sleep(std::time::Duration::from_millis(100)).await;
        }
        stop.set();
        running.await.unwrap();
        assert!(answered, "the NOTIFY woke the broker");
        assert_eq!(served.load(Ordering::SeqCst), 2, "the cached answer was reused");
    });
    let _ = std::fs::remove_dir_all(&archive);
}

fn alert(id: &str, header: &str) -> obehy_core::alerts::Alert {
    obehy_core::alerts::Alert {
        source: "pid".into(),
        channel: "alerts".into(),
        alert_id: id.into(),
        cause: "CONSTRUCTION".into(),
        effect: "DETOUR".into(),
        severity: "WARNING".into(),
        periods: vec![obehy_core::alerts::Period {
            start: Some(at("2026-10-06T04:00:00+00:00")),
            end: None,
        }],
        header: vec![("cs".into(), header.into())],
        description: Vec::new(),
        url: Vec::new(),
        cause_detail: Vec::new(),
        effect_detail: Vec::new(),
        entities: vec![obehy_core::alerts::AlertEntity {
            kind: "route".into(),
            namespace: "pid:gtfs_route_id".into(),
            identifier: "L1".into(),
            service_date: None,
        }],
        details: Vec::new(),
    }
}

#[test]
fn an_alert_poll_states_the_channels_whole_set() {
    use obehy_rt::store::alerts::write_alerts;
    let Some(db) = TestDb::create() else {
        return;
    };
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap();
    runtime.block_on(async {
        let client = connect(&db.config).await.unwrap();
        let polls = [
            ("06:00", vec![alert("a", "Výluka"), alert("b", "Objížďka")]),
            (
                "06:01",
                vec![alert("a", "Výluka"), alert("b", "Objížďka změněna")],
            ),
            ("06:02", vec![alert("b", "Objížďka změněna")]),
        ];
        for (time, alerts) in &polls {
            let when = at(&format!("2026-10-06T{time}:00+00:00"));
            write_alerts(&client, "pid", "alerts", alerts, when)
                .await
                .unwrap();
        }
        assert_eq!(
            query(
                &db.config,
                "SELECT alert_id, changed_at, last_seen, withdrawn_at, header->>'cs' \
                 FROM rt.alert ORDER BY 1"
            )
            .await,
            [
                [
                    "a",
                    "2026-10-06 06:00:00+00",
                    "2026-10-06 06:01:00+00",
                    "2026-10-06 06:02:00+00",
                    "Výluka"
                ],
                [
                    "b",
                    "2026-10-06 06:01:00+00",
                    "2026-10-06 06:02:00+00",
                    "NULL",
                    "Objížďka změněna"
                ]
            ]
        );
        // It returns: its content is news again.
        write_alerts(
            &client,
            "pid",
            "alerts",
            &[alert("a", "Výluka")],
            at("2026-10-06T06:03:00+00:00"),
        )
        .await
        .unwrap();
        assert_eq!(
            query(
                &db.config,
                "SELECT alert_id, changed_at, withdrawn_at FROM rt.alert ORDER BY 1"
            )
            .await,
            [
                ["a", "2026-10-06 06:03:00+00", "NULL"],
                ["b", "2026-10-06 06:01:00+00", "2026-10-06 06:03:00+00"]
            ]
        );
    });
}
