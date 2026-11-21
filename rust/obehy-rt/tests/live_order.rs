//! Live polls reach the core in the order replay runs them (`worker::order`): archived payloads
//! of three channels (`../rt-data/rt-raw`, 2026-10-06 05:00–05:05 UTC) go through the live
//! poll handler (ticket, archive, decode) on concurrent pollers, one per channel; what reaches
//! the core's queue is shuffled (any decode delays) and put through the reorder buffer. The
//! polls must come out exactly as replay of the handler's own archive runs them. Without the
//! archive the test passes without running. Nothing is fetched.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::mpsc::Receiver;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use indexmap::IndexMap;
use obehy_core::config::Channel;
use obehy_core::config::manifest::{RECORDER_FILTERS, load_sources};
use obehy_core::model::Observation;
use obehy_core::time::{Date, Instant};
use obehy_rt::archive::{ArchiveWriter, Poll, iter_polls};
use obehy_rt::compare::{Window, merged_polls};
use obehy_rt::phases::Phases;
use obehy_rt::record::Filters;
use obehy_rt::replay::{DecodeTimes, decoded_polls};
use obehy_rt::scheduler::{OnPoll, Stop};
use obehy_rt::settings::Paths;
use obehy_rt::worker::core_loop::Message;
use obehy_rt::worker::handler::{PollHandler, Shared};
use obehy_rt::worker::order::{Released, Reorder, Sequencer};
use obehy_sources::Decoders;

const ARCHIVE: &str = "../rt-data/rt-raw";
const DAY: Date = Date::ymd(2026, 10, 6);
const SINCE: &str = "2026-10-06T05:00:00+00:00";
const UNTIL: &str = "2026-10-06T05:05:00+00:00";
const CHANNELS: [(&str, &str); 3] = [
    ("duk", "vehicles"),
    ("sz-mapa", "trains"),
    ("arriva-express", "buses"),
];

fn repo() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../..")
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

/// The channels and their decoders.
fn decoders() -> (Vec<Channel>, Arc<Decoders>) {
    let paths = Paths::discover().unwrap();
    let sources = load_sources(&paths.sources(), &RECORDER_FILTERS).unwrap();
    let package = paths.root.join("src/obehy");
    let files = |name: &str| std::fs::read_to_string(package.join(name)).map_err(|e| e.to_string());
    let decoders = Decoders::new(&sources, &files).unwrap();
    let channels = CHANNELS
        .iter()
        .map(|(source, channel)| {
            sources
                .iter()
                .flat_map(|s| &s.channels)
                .find(|c| c.source == *source && c.channel == *channel)
                .unwrap()
                .clone()
        })
        .collect();
    (channels, Arc::new(decoders))
}

/// The live poll handler archiving into `archive`, before warm replay is done (no poller waits
/// on the core), and the core's end of its queue.
fn handler(archive: &Path, decoders: Arc<Decoders>) -> (Arc<PollHandler>, Receiver<Message>) {
    let (core, messages) = std::sync::mpsc::channel();
    let policy = Paths::discover().unwrap().load_policy().unwrap();
    let handler = PollHandler {
        archive: ArchiveWriter::new(archive),
        decoders,
        filters: Filters::shipped(),
        skew: policy.time.max_clock_skew(),
        stats: Mutex::new(IndexMap::new()),
        phases: Mutex::new(Phases::new()),
        core,
        sequencer: Arc::new(Sequencer::new()),
        ready: Stop::new(),
        alerts: None,
        log: Arc::new(|_: &str| {}),
    };
    (Arc::new(handler), messages)
}

/// A channel's archived polls of the window as its fetch returned them.
fn fetched(channel: &Channel) -> Vec<Poll> {
    let (since, until) = (at(SINCE), at(UNTIL));
    iter_polls(
        &repo().join(ARCHIVE),
        &channel.source,
        &channel.channel,
        DAY,
        DAY,
    )
    .map(|p| p.unwrap())
    .filter_map(|p| {
        let received_at = p.received_at().unwrap();
        if received_at < since || received_at >= until {
            return None;
        }
        Some(Poll {
            requested_at: p.requested_at().unwrap(),
            received_at,
            status: p.status().map(|s| u16::try_from(s).unwrap()),
            body: p.body().unwrap(),
            content_type: p
                .entry
                .get("content_type")
                .and_then(|v| v.as_str())
                .map(str::to_owned),
            headers: BTreeMap::new(),
            error: p.error().map(str::to_owned),
            request: p.request(),
        })
    })
    .collect()
}

/// A small deterministic generator (xorshift).
struct Rng(u64);

impl Rng {
    fn next(&mut self) -> u64 {
        self.0 ^= self.0 << 13;
        self.0 ^= self.0 >> 7;
        self.0 ^= self.0 << 17;
        self.0
    }

    fn shuffle<T>(&mut self, items: &mut [T]) {
        for i in (1..items.len()).rev() {
            let j = (self.next() % (i as u64 + 1)) as usize;
            items.swap(i, j);
        }
    }
}

type Ran = Vec<(Instant, Vec<Arc<Observation>>)>;

/// What the core runs of `messages` arriving in this order, and the tickets given up.
fn through_the_buffer(messages: Vec<Message>) -> (Ran, Vec<u64>) {
    let now = std::time::Instant::now();
    let mut order: Reorder<Message> = Reorder::new(Duration::from_secs(30));
    let mut ran = Vec::new();
    let mut stalled = Vec::new();
    let mut take = |released: Released<Message>| match released {
        Released::Next(Message::Poll {
            at, observations, ..
        }) => ran.push((at, observations)),
        Released::Stalled(ticket) => stalled.push(ticket),
        other => panic!("unexpected {other:?}"),
    };
    for message in messages {
        match message {
            Message::Poll { ticket, .. } => order.push(ticket, Some(message), now),
            Message::Skip { ticket } => order.push(ticket, None, now),
            other => panic!("unexpected {other:?}"),
        }
        while let Some(released) = order.pop(now) {
            take(released);
        }
    }
    order.drain().into_iter().for_each(take);
    (ran, stalled)
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn live_polls_reach_the_core_in_replay_order() {
    if !repo().join(ARCHIVE).join("duk").is_dir() {
        eprintln!("skipped: {ARCHIVE} is missing");
        return;
    }
    let (channels, decoders) = decoders();
    let scratch = Scratch::new("live-order");
    let (handler, messages) = handler(&scratch.0, Arc::clone(&decoders));

    // One poller per channel, all at once, each handling its polls one after another (as
    // `run_channel` does), with jitter between them.
    let mut fetched_total = 0;
    let mut pollers = tokio::task::JoinSet::new();
    for (n, channel) in channels.iter().enumerate() {
        let polls = fetched(channel);
        assert!(
            polls.len() >= 5,
            "{}: {} polls",
            channel.name(),
            polls.len()
        );
        fetched_total += polls.len();
        let (on_poll, channel) = (Shared(Arc::clone(&handler)), channel.clone());
        pollers.spawn(async move {
            let mut rng = Rng(0x9e37_79b9_7f4a_7c15 ^ n as u64);
            for poll in polls {
                tokio::time::sleep(Duration::from_micros(rng.next() % 300)).await;
                on_poll.on_poll(&channel, poll).await.unwrap();
            }
        });
    }
    while let Some(joined) = pollers.join_next().await {
        joined.unwrap();
    }
    drop(handler);
    let sent: Vec<Message> = messages.try_iter().collect();
    assert_eq!(sent.len(), fetched_total, "one resolution per ticket");

    // Replay of the live archive.
    let window = Window {
        archive: scratch.0.clone(),
        start: DAY,
        end: DAY,
        channels: channels.clone(),
        since: None,
        until: None,
        all_feeds: false,
    };
    let policy = Paths::discover().unwrap().load_policy().unwrap();
    let mut replayed: Ran = Vec::new();
    decoded_polls(
        &window,
        &decoders,
        policy.time.max_clock_skew(),
        1,
        |poll| {
            replayed.push((poll.at, poll.observations.unwrap_or_default()));
            Ok(())
        },
        &DecodeTimes::default(),
    )
    .unwrap();
    assert_eq!(replayed.len(), fetched_total);
    let observations: usize = replayed.iter().map(|(_, o)| o.len()).sum();
    assert!(observations > 100, "{observations} observations");
    // Reception times strictly increase in replay order: no ties left to break.
    assert!(replayed.windows(2).all(|w| w[0].0 < w[1].0));
    assert_eq!(merged_polls(&window).count(), fetched_total);

    // Whatever order the decoded polls reach the core in, it runs them in replay's.
    let mut rng = Rng(0x2545_f491_4f6c_dd1d);
    let mut arrivals: Vec<Vec<Message>> = vec![sent];
    for _ in 0..3 {
        let mut shuffled: Vec<Message> = arrivals[0]
            .iter()
            .map(|m| match m {
                Message::Poll {
                    ticket,
                    at,
                    observations,
                    ..
                } => Message::Poll {
                    ticket: *ticket,
                    at: *at,
                    observations: observations.clone(),
                    done: None,
                },
                Message::Skip { ticket } => Message::Skip { ticket: *ticket },
                other => panic!("unexpected {other:?}"),
            })
            .collect();
        rng.shuffle(&mut shuffled);
        arrivals.push(shuffled);
    }
    for arrival in arrivals {
        let (ran, stalled) = through_the_buffer(arrival);
        assert!(stalled.is_empty(), "tickets given up: {stalled:?}");
        assert_eq!(ran.len(), replayed.len());
        for (i, (live, replay)) in ran.iter().zip(&replayed).enumerate() {
            assert_eq!(live.0, replay.0, "poll {i}: reception time");
            assert!(live.1 == replay.1, "poll {i}: observations differ");
        }
    }
}

#[tokio::test]
async fn every_ticket_is_resolved_once() {
    let (channels, decoders) = decoders();
    let scratch = Scratch::new("live-order-tickets");
    let (handler, messages) = handler(&scratch.0, decoders);
    let at = Instant::from_isoformat(SINCE).unwrap();
    // A ticket dropped unused (an error, a cancelled poller) is a skip.
    let mut poll = Poll::failed(at, at, "x");
    drop(handler.ticket(&mut poll));
    // A failed fetch is a poll with no observations (replay runs the ticks before it too).
    let on_poll = Shared(Arc::clone(&handler));
    on_poll
        .on_poll(&channels[0], Poll::failed(at, at, "timed out"))
        .await
        .unwrap();
    let sent: Vec<Message> = messages.try_iter().collect();
    assert!(matches!(sent[0], Message::Skip { ticket: 0 }), "{sent:?}");
    match &sent[1] {
        Message::Poll {
            ticket,
            at: received,
            observations,
            done,
        } => {
            assert_eq!(*ticket, 1);
            // Stamped at the same microsecond as the first: just after it.
            assert_eq!(received.micros(), at.micros() + 1);
            assert!(observations.is_empty() && done.is_none());
        }
        other => panic!("unexpected {other:?}"),
    }
    assert_eq!(sent.len(), 2);
}
