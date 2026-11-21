//! The worker end to end (docs/RUST_PORT.md R8): `run_worker` polls a local HTTP stub that
//! serves archived DÚK payloads (`../rt-data/rt-raw`, 2026-10-06 07:00 Prague) on a simulated
//! clock, into a small test database holding a bounded subset of a real release
//! (`rust/tools/store_db.py release`). Checked: observations, history and state rows and the
//! GTFS-RT file appear, and they are what replay of the worker's own archive writes into a
//! second database through the same runner and output; the worker refuses a schema it does not
//! know; a release activated mid-run is switched to. Every database made is dropped; without a
//! test database URL, a release or the archive, the tests pass without running. No request
//! leaves 127.0.0.1.

use std::collections::VecDeque;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use obehy_core::config::manifest::{RECORDER_FILTERS, load_sources};
use obehy_core::config::{Channel, Policy, Source};
use obehy_core::core::CORE_VERSION;
use obehy_core::model::{Derivation, Feed};
use obehy_core::publish::alerts::AlertEntities;
use obehy_core::runner::{Runner, Ticks};
use obehy_core::time::{Date, Instant, TimeDelta};
use obehy_core::vehicles::Vocabulary;
use obehy_core::vehicles::vocabulary::VOCABULARY_FILE;
use obehy_rt::archive::iter_polls;
use obehy_rt::compare::Window;
use obehy_rt::egress::Egress;
use obehy_rt::fetch::Clock;
use obehy_rt::phases::Phases;
use obehy_rt::replay::{self, DecodeTimes, decoded_polls, drive, finish};
use obehy_rt::scheduler::Stop;
use obehy_rt::settings::{Paths, ProcessEnv, load_test_database_url};
use obehy_rt::store::vehicles::{book, physical_rows};
use obehy_rt::store::{PgStore, Writer};
use obehy_rt::worker::loads::{PgLoads, active_loads};
use obehy_rt::worker::pipeline::{Output, ReplaySink};
use obehy_rt::worker::{WorkerError, WorkerOptions, run_worker, semantics_of};
use obehy_sources::Decoders;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;
use tokio_postgres::{Config, NoTls};

const RELEASE: &str = "data/releases/20261014T065758Z-d732b1d05827";
const ARCHIVE: &str = "../rt-data/rt-raw";
const DAY: Date = Date::ymd(2026, 10, 6);
const SINCE: &str = "2026-10-06T05:00:00+00:00";
const UNTIL: &str = "2026-10-06T05:01:30+00:00";

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
    let text = String::from_utf8_lossy(&out.stdout);
    text.trim().lines().last().unwrap_or_default().to_owned()
}

/// `url` naming database `name` (a URL's path, or a conninfo's `dbname`).
fn with_dbname(url: &str, name: &str) -> String {
    if let Some(scheme) = url.find("://") {
        let rest = &url[scheme + 3..];
        let authority_end = rest.rfind('@').map_or(0, |at| at + 1);
        let path = rest[authority_end..]
            .find('/')
            .map(|i| scheme + 3 + authority_end + i);
        let query = url.find('?');
        return match path {
            Some(slash) => {
                let end = query.unwrap_or(url.len());
                format!("{}/{name}{}", &url[..slash], &url[end..])
            }
            None => match query {
                Some(q) => format!("{}/{name}{}", &url[..q], &url[q..]),
                None => format!("{url}/{name}"),
            },
        };
    }
    format!("{url} dbname={name}")
}

/// A migrated database of its own, dropped with the value.
struct TestDb {
    name: String,
    url: String,
    config: Config,
}

impl TestDb {
    fn create() -> Option<TestDb> {
        let paths = Paths::discover().ok()?;
        let base = load_test_database_url(&paths.config(), &ProcessEnv)?;
        let name = tool(&["create"]);
        let url = with_dbname(&base, &name);
        let config: Config = url.parse().expect("the test database URL parses");
        assert_eq!(config.get_dbname(), Some(name.as_str()));
        Some(TestDb { name, url, config })
    }

    async fn query(&self, sql: &str) -> Vec<Vec<String>> {
        let (client, connection) = self.config.connect(NoTls).await.unwrap();
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

    async fn count(&self, table: &str) -> usize {
        self.query(&format!("SELECT count(*) FROM {table}")).await[0][0]
            .parse()
            .unwrap()
    }
}

impl Drop for TestDb {
    fn drop(&mut self) {
        tool(&["drop", &self.name]);
    }
}

/// The bounded subset release loaded and activated in each database (by name); its run ID.
fn load_release(names: &[String]) -> String {
    let mut args = vec!["release"];
    args.extend(names.iter().map(String::as_str));
    args.extend([
        "--release",
        RELEASE,
        "--archive",
        ARCHIVE,
        "--source",
        "duk",
        "--channel",
        "vehicles",
        "--since",
        SINCE,
        "--until",
        UNTIL,
        "--feed",
        "jdf",
    ]);
    tool(&args)
}

/// A directory of its own under the system's temporary directory, removed with the value.
struct Scratch(PathBuf);

impl Scratch {
    fn new(what: &str) -> Scratch {
        static N: AtomicUsize = AtomicUsize::new(0);
        let dir = std::env::temp_dir().join(format!(
            "obehy-{what}-{}-{}",
            std::process::id(),
            N.fetch_add(1, Ordering::SeqCst)
        ));
        std::fs::create_dir_all(&dir).unwrap();
        Scratch(dir)
    }
}

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

fn at(text: &str) -> Instant {
    Instant::from_isoformat(text).unwrap()
}

/// What the tests need of the archive and the release; None (skip) when either is missing.
fn inputs_present() -> bool {
    let present = repo().join(RELEASE).is_dir() && repo().join(ARCHIVE).join("duk").is_dir();
    if !present {
        eprintln!("skipped: {RELEASE} or {ARCHIVE}/duk is missing");
    }
    present
}

/// One archived poll as the stub serves it.
#[derive(Debug, Clone)]
struct Served {
    received_at: Instant,
    body: Vec<u8>,
}

fn archived_polls() -> Vec<Served> {
    let (since, until) = (at(SINCE), at(UNTIL));
    iter_polls(&repo().join(ARCHIVE), "duk", "vehicles", DAY, DAY)
        .map(|p| p.unwrap())
        .filter_map(|p| {
            let received_at = p.received_at().unwrap();
            let body = p.body().unwrap()?;
            (received_at >= since && received_at < until && p.succeeded())
                .then_some(Served { received_at, body })
        })
        .collect()
}

/// What the stub does next.
enum Step {
    Serve(Served),
    /// Wait for the test before serving on.
    Gate(Arc<tokio::sync::Notify>),
}

/// An HTTP stub on 127.0.0.1: each request gets the next archived payload, after setting the
/// simulated clock to the payload's reception time; after the last it sets `stop`. `asked`
/// counts requests (a request for step n means step n-1 was run by the worker: one channel
/// polls again only after its poll was handled).
async fn stub(
    steps: Vec<Step>,
    clock: Arc<Mutex<Instant>>,
    stop: Stop,
    asked: Arc<AtomicUsize>,
) -> String {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let steps = Arc::new(tokio::sync::Mutex::new(VecDeque::from(steps)));
    tokio::spawn(async move {
        loop {
            let Ok((mut socket, _)) = listener.accept().await else {
                return;
            };
            let mut seen = Vec::new();
            let mut buffer = [0_u8; 4096];
            while !seen.windows(4).any(|w| w == b"\r\n\r\n") {
                match socket.read(&mut buffer).await {
                    Ok(0) | Err(_) => break,
                    Ok(n) => seen.extend_from_slice(&buffer[..n]),
                }
            }
            asked.fetch_add(1, Ordering::SeqCst);
            let mut queue = steps.lock().await;
            let served = loop {
                match queue.pop_front() {
                    Some(Step::Gate(gate)) => gate.notified().await,
                    Some(Step::Serve(served)) => break Some(served),
                    None => break None,
                }
            };
            let response = match served {
                Some(served) => {
                    *clock.lock().unwrap() = served.received_at;
                    if queue.is_empty() {
                        stop.set();
                    }
                    let mut head = format!(
                        "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\
                         Content-Length: {}\r\nConnection: close\r\n\r\n",
                        served.body.len()
                    )
                    .into_bytes();
                    head.extend(served.body);
                    head
                }
                None => {
                    b"HTTP/1.1 503 Gone\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".to_vec()
                }
            };
            drop(queue);
            let _ = socket.write_all(&response).await;
            let _ = socket.shutdown().await;
        }
    });
    format!("http://{address}/duk")
}

struct Setup {
    paths: Paths,
    policy: Arc<Policy>,
    sources: Vec<Source>,
    channels: Vec<Channel>,
    decoders: Arc<Decoders>,
    vocabulary: Vocabulary,
}

/// DÚK's manifest pointed at the stub, polled every 50 ms.
fn setup(dir: &Path, url: &str) -> Setup {
    let paths = Paths::discover().unwrap();
    let text = std::fs::read_to_string(paths.sources().join("duk.toml")).unwrap();
    let text = text
        .replace(
            "https://tabule.portabo.cz/api/v1-tabule/cis/GetTraffic/0",
            url,
        )
        .replace("seconds = 15 }", "seconds = 0.05 }")
        .replace("timeout_s = 20", "timeout_s = 120");
    assert!(text.contains(url));
    let manifest = dir.join("duk.toml");
    std::fs::write(&manifest, text).unwrap();
    let sources = load_sources(&manifest, &RECORDER_FILTERS).unwrap();
    let channels: Vec<Channel> = sources.iter().flat_map(|s| s.channels.clone()).collect();
    let package = paths.root.join("src/obehy");
    let files = |name: &str| std::fs::read_to_string(package.join(name)).map_err(|e| e.to_string());
    let decoders = Decoders::new(&sources, &files).unwrap();
    let vocabulary = Vocabulary::load(&paths.data().join("..").join(VOCABULARY_FILE)).unwrap();
    Setup {
        policy: Arc::new(paths.load_policy().unwrap()),
        paths,
        sources,
        channels,
        decoders: Arc::new(decoders),
        vocabulary,
    }
}

type Lines = Arc<Mutex<Vec<String>>>;

/// Wait (up to a minute) until the worker logged a line starting with `start`.
async fn wait_for_line(lines: &Lines, start: &str) {
    let deadline = std::time::Instant::now() + Duration::from_secs(60);
    while !lines.lock().unwrap().iter().any(|l| l.starts_with(start)) {
        assert!(std::time::Instant::now() < deadline, "no line {start:?}");
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
}

fn options(setup: &Setup, db: &TestDb, dir: &Path, clock: Clock, lines: &Lines) -> WorkerOptions {
    let lines = Arc::clone(lines);
    WorkerOptions {
        database_url: db.url.clone(),
        channels: setup.channels.clone(),
        lookups: Vec::new(),
        sources: setup.sources.clone(),
        decoders: Arc::clone(&setup.decoders),
        policy: Arc::clone(&setup.policy),
        vocabulary: setup.vocabulary.clone(),
        catalogue: setup.paths.catalogue(),
        feeds: vec![Feed::Jdf],
        archive: dir.join("archive"),
        gtfs_rt_dir: dir.join("gtfs-rt"),
        once: false,
        clock,
        log: Arc::new(move |line: &str| {
            eprintln!("worker: {line}");
            lines.lock().unwrap().push(line.to_owned());
        }),
    }
}

fn simulated(start: Instant) -> (Arc<Mutex<Instant>>, Clock) {
    let now = Arc::new(Mutex::new(start));
    let read = Arc::clone(&now);
    (now, Arc::new(move || *read.lock().unwrap()))
}

/// Replay of the worker's archive into `db` through the same runner and output: each step's
/// effects written, each tick written with the state tables and GTFS-RT, a last tick at the
/// last poll. On a thread of its own (the store blocks on its own runtime).
fn replay_into(setup: &Setup, db: &TestDb, archive: &Path, gtfs_rt: &Path) {
    let config = db.config.clone();
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap();
    let loads = runtime.block_on(async {
        let (client, connection) = config.connect(NoTls).await.unwrap();
        tokio::spawn(connection);
        active_loads(&client).await.unwrap()
    });
    drop(runtime);
    let policy = Arc::clone(&setup.policy);
    let mut runner = Runner::new(
        PgLoads {
            config: config.clone(),
            loads: loads.clone(),
        },
        Arc::clone(&policy),
        &[Feed::Jdf],
        semantics_of(&setup.channels),
        Phases::new(),
    )
    .unwrap();
    let store = PgStore::connect_config(&config, physical_rows(&setup.sources)).unwrap();
    let derivation = Derivation {
        core_version: CORE_VERSION.to_owned(),
        policy_version: policy.version.clone(),
        release_id: loads.run_id.clone(),
    };
    let vocabulary = setup.vocabulary.clone();
    let writer = Writer::open(store, derivation, |stored| book(stored, vocabulary)).unwrap();
    let mut output = Output {
        writer: Some(writer),
        gtfs_rt_dir: Some(gtfs_rt.to_path_buf()),
        lifecycle: policy.lifecycle.clone(),
        emitters: Default::default(),
        alerts: Some(AlertEntities::new()),
    };
    let window = Window {
        archive: archive.to_path_buf(),
        start: DAY,
        end: DAY,
        channels: setup.channels.clone(),
        since: None,
        until: None,
        all_feeds: false,
    };
    let mut clock = replay::Clock::new(Ticks::new(&policy), None);
    let times = DecodeTimes::default();
    let skew = policy.time.max_clock_skew();
    let last = drive(
        &mut runner,
        |each| decoded_polls(&window, &setup.decoders, skew, 1, each, &times),
        &mut clock,
        &mut ReplaySink {
            output: &mut output,
        },
    )
    .unwrap()
    .expect("polls replayed");
    let effects = finish(&mut runner, last).unwrap();
    output.ticked(&mut runner, last, &effects).unwrap();
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn the_worker_writes_what_replay_of_its_archive_writes() {
    if !inputs_present() {
        return;
    }
    let Some(live) = TestDb::create() else {
        return;
    };
    let replayed = TestDb::create().unwrap();
    let names = vec![live.name.clone(), replayed.name.clone()];
    let run_id = tokio::task::spawn_blocking(move || load_release(&names))
        .await
        .unwrap();
    assert!(run_id.starts_with("subset-"), "{run_id}");

    let scratch = Scratch::new("worker");
    let polls = archived_polls();
    assert!(polls.len() >= 4, "{} polls in the window", polls.len());
    let (now, clock) = simulated(polls[0].received_at - TimeDelta::from_seconds(30));
    let stop = Stop::new();
    let asked = Arc::new(AtomicUsize::new(0));
    // Two polls arrive during warm replay (the backlog), the rest once the worker is ready.
    let ready = Arc::new(tokio::sync::Notify::new());
    let mut steps: Vec<Step> = polls[..2].iter().cloned().map(Step::Serve).collect();
    steps.push(Step::Gate(Arc::clone(&ready)));
    steps.extend(polls[2..].iter().cloned().map(Step::Serve));
    let url = stub(steps, now, stop.clone(), asked).await;
    let setup = setup(&scratch.0, &url);
    let lines: Lines = Arc::default();
    let egress = Egress::with_clock(None, Default::default(), Arc::clone(&clock));
    let options = options(&setup, &live, &scratch.0, clock, &lines);
    let worker = tokio::spawn(run_worker(options, Arc::new(egress), stop));
    wait_for_line(&lines, &format!("realtime worker on release {run_id}")).await;
    ready.notify_one();
    tokio::time::timeout(Duration::from_secs(300), worker)
        .await
        .expect("the worker stops after the last payload")
        .unwrap()
        .expect("the worker runs");

    // Rows and the feed appeared.
    let observations = live.count("rt.observation").await;
    assert!(observations > 100, "{observations} observations");
    assert!(live.count("rt.trip_state_current").await > 0);
    assert!(live.count("rt.vehicle_state_current").await > 0);
    assert!(
        live.count("history.journey_schedule").await > 0
            && live.count("history.vehicle_assignment").await > 0
    );
    let releases = live
        .query("SELECT DISTINCT release_id FROM rt.observation")
        .await;
    assert_eq!(releases, [[run_id.clone()]]);
    let feed = std::fs::read(scratch.0.join("gtfs-rt/jdf.pb")).expect("the jdf feed");
    assert!(feed.len() > 100);
    assert!(
        !scratch.0.join("gtfs-rt/czptt.pb").exists(),
        "only the jdf feed runs"
    );
    let text = lines.lock().unwrap().join("\n");
    assert!(
        text.contains(&format!("realtime worker on release {run_id}")),
        "{text}"
    );
    assert!(
        text.contains(&format!("duk/vehicles: {} polls", polls.len())),
        "{text}"
    );

    // Replay of what the worker archived writes the same.
    let archive = scratch.0.join("archive");
    let replay_feed = scratch.0.join("gtfs-rt-replay");
    {
        let setup = &setup;
        let replayed = &replayed;
        std::thread::scope(|scope| {
            scope
                .spawn(|| replay_into(setup, replayed, &archive, &replay_feed))
                .join()
                .unwrap();
        });
    }
    let dumps: Vec<String> = [&live, &replayed]
        .iter()
        .map(|db| {
            let out = scratch.0.join(format!("{}.dump", db.name));
            tool(&["dump", &db.name, out.to_str().unwrap()]);
            std::fs::read_to_string(out).unwrap()
        })
        .collect();
    let (a, b): (Vec<&str>, Vec<&str>) = (dumps[0].lines().collect(), dumps[1].lines().collect());
    let differ: Vec<(&str, &str)> = a
        .iter()
        .zip(&b)
        .filter(|(x, y)| x != y)
        .map(|(x, y)| (*x, *y))
        .take(5)
        .collect();
    assert!(
        differ.is_empty() && a.len() == b.len(),
        "{} vs {} rows; first differences: {differ:#?}",
        a.len(),
        b.len()
    );
    assert_eq!(
        feed,
        std::fs::read(replay_feed.join("jdf.pb")).unwrap(),
        "the last GTFS-RT feed"
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn the_worker_refuses_a_schema_it_does_not_know() {
    let Some(db) = TestDb::create() else {
        return;
    };
    let scratch = Scratch::new("schema");
    db.query(
        "INSERT INTO control.schema_migration (version, name, sha256) \
         SELECT max(version) + 1, 'from_the_future', '' FROM control.schema_migration",
    )
    .await;
    let setup = setup(&scratch.0, "http://127.0.0.1:9/unused");
    let (_, clock) = simulated(at(SINCE));
    let lines: Lines = Arc::default();
    let egress = Egress::with_clock(None, Default::default(), Arc::clone(&clock));
    let options = options(&setup, &db, &scratch.0, clock, &lines);
    let error = run_worker(options, Arc::new(egress), Stop::new())
        .await
        .expect_err("a newer schema is refused");
    assert!(matches!(error, WorkerError::Schema(_)), "{error}");
    assert!(error.to_string().contains("from_the_future"), "{error}");
    assert!(
        error.to_string().contains("newer than this worker"),
        "{error}"
    );
    assert!(!scratch.0.join("archive").exists(), "nothing polled");
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_release_activated_mid_run_is_switched_to() {
    if !inputs_present() {
        return;
    }
    let Some(db) = TestDb::create() else {
        return;
    };
    let names = vec![db.name.clone()];
    let first = {
        let names = names.clone();
        tokio::task::spawn_blocking(move || load_release(&names))
            .await
            .unwrap()
    };
    let scratch = Scratch::new("switch");
    let polls = archived_polls();
    let half = polls.len() / 2;
    let gate = Arc::new(tokio::sync::Notify::new());
    let mut steps: Vec<Step> = polls[..half].iter().cloned().map(Step::Serve).collect();
    steps.push(Step::Gate(Arc::clone(&gate)));
    steps.extend(polls[half..].iter().cloned().map(Step::Serve));
    let (now, clock) = simulated(polls[0].received_at - TimeDelta::from_seconds(30));
    let stop = Stop::new();
    let asked = Arc::new(AtomicUsize::new(0));
    let url = stub(steps, now, stop.clone(), Arc::clone(&asked)).await;
    let setup = setup(&scratch.0, &url);
    let lines: Lines = Arc::default();
    let egress = Egress::with_clock(None, Default::default(), Arc::clone(&clock));
    let options = options(&setup, &db, &scratch.0, clock, &lines);
    let worker = tokio::spawn(run_worker(options, Arc::new(egress), stop));

    // The poll after the first half is asked for: the first half has run.
    while asked.load(Ordering::SeqCst) <= half {
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    let second = tokio::task::spawn_blocking(move || load_release(&names))
        .await
        .unwrap();
    assert_ne!(first, second);
    wait_for_line(&lines, &format!("rebased onto release {second}")).await;
    gate.notify_one();
    tokio::time::timeout(Duration::from_secs(300), worker)
        .await
        .expect("the worker stops after the last payload")
        .unwrap()
        .expect("the worker runs");

    let by_release = db
        .query(
            "SELECT release_id, min(received_at), max(received_at) FROM rt.observation \
             GROUP BY release_id ORDER BY 2",
        )
        .await;
    assert_eq!(by_release.len(), 2, "{by_release:?}");
    assert_eq!(by_release[0][0], first);
    assert_eq!(by_release[1][0], second);
    assert!(by_release[0][2] < by_release[1][1], "{by_release:?}");
    let states = db
        .query("SELECT DISTINCT release_id FROM rt.trip_state_current")
        .await;
    assert_eq!(states, [[second.clone()]]);
}
