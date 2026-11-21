//! The generic poll scheduler: port of `realtime/runtime/scheduler.py`. Runs a channel at its
//! declared interval, with backoff.
//!
//! Ticks are fixed-rate; missed ticks are skipped rather than bunched after a slow poll. After
//! `backoff.after_failures` consecutive failures the interval doubles per failure up to
//! `backoff.max_s`. A 403 means the source refuses us (SŽ blocks addresses): the circuit opens
//! at once and the next poll waits the full `max_s`. No fetch error ends a channel: one that
//! panics is logged and is a failed poll ([`fetch_guarded`]). An error handling a poll (the
//! database) still ends the channel, and the process with it.
//!
//! The recorder and the worker share it and differ only in what they do with a poll
//! ([`OnPoll`]).

use std::future::Future;
use std::sync::Arc;
use std::time::Duration;

use obehy_core::config::Channel;
use tokio::sync::watch;

use crate::archive::Poll;
use crate::fetch::Clock;

pub const BLOCKED: u16 = 403;
/// Enough doublings to reach `max_s` from any interval the manifests use.
pub const BLOCKED_STEPS: i64 = 32;

/// What ends a poller: an error handling a poll.
pub type HandlerError = Box<dyn std::error::Error + Send + Sync>;

/// Sends one request of a channel (with a demand read's body). Never fails: an error is a
/// failed poll.
pub trait Fetcher: Send + Sync + 'static {
    fn fetch(&self, channel: &Channel, body: Option<Vec<u8>>) -> impl Future<Output = Poll> + Send;
}

/// What the recorder or the worker does with a poll.
pub trait OnPoll: Send + Sync + 'static {
    fn on_poll(
        &self,
        channel: &Channel,
        poll: Poll,
    ) -> impl Future<Output = Result<(), HandlerError>> + Send;
}

/// The stop event (`asyncio.Event`): set once, seen by every poller.
#[derive(Debug, Clone)]
pub struct Stop(Arc<watch::Sender<bool>>);

impl Default for Stop {
    fn default() -> Self {
        Stop::new()
    }
}

impl Stop {
    pub fn new() -> Stop {
        Stop(Arc::new(watch::channel(false).0))
    }

    pub fn set(&self) {
        self.0.send_replace(true);
    }

    pub fn is_set(&self) -> bool {
        *self.0.borrow()
    }

    pub async fn wait(&self) {
        let mut seen = self.0.subscribe();
        let _ = seen.wait_for(|set| *set).await;
    }

    /// Wait up to `seconds` or until set (`wait_for(stop.wait(), timeout)`).
    pub async fn sleep(&self, seconds: f64) {
        let duration = Duration::from_secs_f64(seconds.max(0.0));
        let _ = tokio::time::timeout(duration, self.wait()).await;
    }
}

/// `fetcher.fetch(channel, body)` on its own task; a panic there is logged and is a failed poll.
pub async fn fetch_guarded<F: Fetcher>(
    fetcher: &Arc<F>,
    channel: &Arc<Channel>,
    body: Option<Vec<u8>>,
    clock: &Clock,
) -> Poll {
    let requested_at = clock();
    let task = {
        let fetcher = Arc::clone(fetcher);
        let channel = Arc::clone(channel);
        tokio::spawn(async move { fetcher.fetch(&channel, body).await })
    };
    match task.await {
        Ok(poll) => poll,
        Err(error) => {
            let message = if error.is_panic() {
                let payload = error.into_panic();
                payload
                    .downcast_ref::<String>()
                    .cloned()
                    .or_else(|| payload.downcast_ref::<&str>().map(|s| (*s).to_owned()))
                    .unwrap_or_else(|| "panic".to_owned())
            } else {
                "cancelled".to_owned()
            };
            eprintln!("{}: fetch failed: {message}", channel.name());
            Poll::failed(requested_at, clock(), format!("Panic: {message}"))
        }
    }
}

/// The interval after `consecutive_failures` failures (`backoff_interval`).
pub fn backoff_interval(channel: &Channel, consecutive_failures: i64) -> f64 {
    if consecutive_failures < channel.backoff_after {
        return channel.interval_s;
    }
    // Python's `2 ** n` is an int and the product raises OverflowError past 2**1023 (about
    // 1,000 consecutive failures); here it saturates at `max_s`.
    let exponent = (consecutive_failures - channel.backoff_after + 1).min(2000) as i32;
    let doubled = channel.interval_s * 2f64.powi(exponent);
    channel.max_backoff_s.min(channel.interval_s.max(doubled))
}

/// The consecutive-failure count after `poll`; a 403 opens the circuit at once.
pub fn failures_after(channel: &Channel, failures: i64, poll: &Poll) -> i64 {
    if poll.ok() {
        return 0;
    }
    if poll.status == Some(BLOCKED) {
        return (failures + 1).max(channel.backoff_after + BLOCKED_STEPS);
    }
    failures + 1
}

/// Poll `channel` until `stop` (one poll with `once`), handing each poll to `on_poll`.
pub async fn run_channel<F: Fetcher, H: OnPoll>(
    channel: Arc<Channel>,
    stop: Stop,
    fetcher: Arc<F>,
    on_poll: Arc<H>,
    clock: Clock,
    once: bool,
) -> Result<(), HandlerError> {
    let base = tokio::time::Instant::now();
    let loop_time = || base.elapsed().as_secs_f64();
    let mut next_tick = loop_time();
    let mut failures = 0;
    while !stop.is_set() {
        let poll = fetch_guarded(&fetcher, &channel, None, &clock).await;
        let after = failures_after(&channel, failures, &poll);
        on_poll.on_poll(&channel, poll).await?;
        failures = after;
        if once {
            return Ok(());
        }
        let interval = backoff_interval(&channel, failures);
        let now = loop_time();
        if interval != channel.interval_s {
            next_tick = now + interval;
        } else {
            next_tick += interval;
            if next_tick <= now {
                next_tick += ((now - next_tick) / interval + 1e-9).ceil() * interval;
            }
        }
        stop.sleep(next_tick - now).await;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use obehy_core::time::Instant;
    use std::sync::Mutex;

    pub(crate) fn channel(interval_s: f64) -> Channel {
        let text = format!(
            "source = \"duk\"\nmanifest_version = 1\ntitle = \"t\"\ncovers = \"c\"\n[[channel]]\n\
             name = \"vehicles\"\npoll = {{ kind = \"interval\", seconds = {interval_s} }}\n\
             backoff = {{ after_failures = 5, max_s = 300 }}\ntimeout_s = 5\n\
             request = {{ method = \"GET\", url = \"https://example.invalid/vehicles\" }}\n"
        );
        let source = obehy_core::config::manifest::parse_source(&text, "duk", "duk.toml", &[])
            .expect("manifest");
        source.channels[0].clone()
    }

    fn poll(status: Option<u16>) -> Poll {
        let at = Instant::from_micros(0);
        let mut poll = Poll::failed(at, at, "x");
        poll.status = status;
        if status == Some(200) {
            poll.error = None;
        }
        poll
    }

    #[test]
    fn a_403_opens_the_circuit_at_once() {
        let channel = channel(30.0);
        let failed = poll(Some(503));
        let refused = poll(Some(403));
        assert_eq!(
            backoff_interval(&channel, failures_after(&channel, 0, &failed)),
            30.0
        );
        assert_eq!(
            backoff_interval(&channel, failures_after(&channel, 0, &refused)),
            300.0
        );
        assert_eq!(failures_after(&channel, 40, &poll(Some(200))), 0);
    }

    #[test]
    fn backoff_doubles_after_repeated_failures() {
        let channel = channel(30.0);
        assert_eq!(backoff_interval(&channel, 4), 30.0);
        assert_eq!(backoff_interval(&channel, 5), 60.0);
        assert_eq!(backoff_interval(&channel, 6), 120.0);
        assert_eq!(backoff_interval(&channel, 20), channel.max_backoff_s);
        assert_eq!(backoff_interval(&channel, 5000), channel.max_backoff_s);
    }

    struct Panicking;

    impl Fetcher for Panicking {
        async fn fetch(&self, _: &Channel, _: Option<Vec<u8>>) -> Poll {
            panic!("boom")
        }
    }

    struct Failing(Mutex<Vec<Poll>>);

    #[derive(Debug, thiserror::Error)]
    #[error("database gone")]
    struct Gone;

    impl OnPoll for Failing {
        async fn on_poll(&self, _: &Channel, poll: Poll) -> Result<(), HandlerError> {
            self.0.lock().unwrap().push(poll);
            Err(Box::new(Gone))
        }
    }

    #[tokio::test]
    async fn a_fetch_panic_is_a_failed_poll_and_a_handler_error_ends_the_channel() {
        let handler = Arc::new(Failing(Mutex::new(Vec::new())));
        let result = run_channel(
            Arc::new(channel(15.0)),
            Stop::new(),
            Arc::new(Panicking),
            Arc::clone(&handler),
            crate::fetch::system_clock(),
            false,
        )
        .await;
        assert_eq!(result.unwrap_err().to_string(), "database gone");
        let seen = handler.0.lock().unwrap();
        assert_eq!(seen.len(), 1);
        assert!(!seen[0].ok());
        assert_eq!(seen[0].error.as_deref(), Some("Panic: boom"));
    }

    struct Counting(Mutex<u32>);

    impl Fetcher for Counting {
        async fn fetch(&self, _: &Channel, _: Option<Vec<u8>>) -> Poll {
            *self.0.lock().unwrap() += 1;
            poll(Some(200))
        }
    }

    struct Collect(Mutex<Vec<Poll>>);

    impl OnPoll for Collect {
        async fn on_poll(&self, _: &Channel, poll: Poll) -> Result<(), HandlerError> {
            self.0.lock().unwrap().push(poll);
            Ok(())
        }
    }

    #[tokio::test]
    async fn a_channel_polls_at_its_interval_until_stopped() {
        let fetcher = Arc::new(Counting(Mutex::new(0)));
        let sink = Arc::new(Collect(Mutex::new(Vec::new())));
        let stop = Stop::new();
        let task = tokio::spawn(run_channel(
            Arc::new(channel(0.05)),
            stop.clone(),
            Arc::clone(&fetcher),
            Arc::clone(&sink),
            crate::fetch::system_clock(),
            false,
        ));
        tokio::time::sleep(Duration::from_millis(230)).await;
        stop.set();
        task.await.unwrap().unwrap();
        let polls = *fetcher.0.lock().unwrap();
        assert!((4..=6).contains(&polls), "{polls} polls in 230 ms at 50 ms");
        assert_eq!(sink.0.lock().unwrap().len() as u32, polls);
    }
}
