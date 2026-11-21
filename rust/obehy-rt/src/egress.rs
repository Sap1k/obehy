//! Proxied, anonymous egress (docs/R2_SLICE.md section 7).
//!
//! SŽ IP-bans addresses that run services against it, so a channel declaring `proxied = true`
//! (SŽ, ČD) is only ever sent through the proxy pool, with no User-Agent and no header but the
//! manifest's own. There is one pool for every such channel: a saved proxy list (Webshare's
//! format, one `ip:port:username:password` per line), a secret file named by
//! `OBEHY_EGRESS_PROXY_LIST_FILE` or `[realtime.egress] proxy_list_file`, re-read every
//! `egress.refresh_s`; an unreadable, empty or malformed list keeps the pool. The file's
//! contents and the proxies' addresses and credentials are never logged: a proxy is named by
//! its index in the list.
//!
//! Requests go round-robin over the pool. A proxy answering 403, 407 or 429, or failing to
//! connect (no status), is benched for `egress.cooldown_s` and the request is retried once on
//! the next proxy. A proxied channel never polls without the pool: no proxy configured, or
//! every one benched, means no request at all (fail closed).
//!
//! [`Egress`] is the fetcher of every channel, so it also holds what a request needs beyond its
//! manifest: the secret of a channel with `auth` (such a channel without its secret never polls
//! either), and the cursor of a channel with `cursor` (delta polling: the last good poll's
//! header goes back as a query parameter, and is archived with the poll in `request`).

use std::collections::{BTreeMap, HashMap};
use std::path::Path;
use std::sync::{Arc, Mutex};

use obehy_core::config::Channel;
use obehy_core::config::policy::EgressPolicy;
use obehy_core::time::{Instant, TimeDelta};

use crate::archive::Poll;
use crate::fetch::{Clock, Http, Proxy, channel_request, refused, send, system_clock};
use crate::logs::Log;
use crate::scheduler::Fetcher;
use crate::settings::{
    ConfigurationError, Env, LIST_ENV, LIST_SETTING, load_proxy_list, load_secrets, secret_env,
};

pub const BENCH_STATUSES: [u16; 3] = [403, 407, 429];

/// Proxies from `ip:port:username:password` lines; malformed lines are skipped.
pub fn parse_proxy_list(text: &str) -> Vec<Proxy> {
    text.lines()
        .filter_map(|line| {
            let parts: Vec<&str> = line.trim().split(':').collect();
            if parts.len() != 4
                || parts.iter().any(|p| p.is_empty())
                || !parts[1].bytes().all(|b| b.is_ascii_digit())
            {
                return None;
            }
            Some(Proxy {
                host: parts[0].to_owned(),
                port: parts[1].to_owned(),
                user: parts[2].to_owned(),
                password: parts[3].to_owned(),
            })
        })
        .collect()
}

/// Why a list could not be read: the error's kind (never its text, which may quote the secret
/// file).
fn read_failure(error: &std::io::Error) -> String {
    error.kind().to_string()
}

pub type ReadList = Box<dyn Fn(&str) -> std::io::Result<String> + Send + Sync>;
pub type Monotonic = Box<dyn Fn() -> f64 + Send + Sync>;

#[derive(Debug, Default)]
struct PoolState {
    proxies: Vec<Proxy>,
    benched_until: BTreeMap<usize, f64>,
    next: usize,
    refreshed_at: Option<f64>,
}

/// The proxies, shared by every proxied channel.
pub struct ProxyPool {
    list_file: String,
    policy: EgressPolicy,
    read: ReadList,
    monotonic: Monotonic,
    log: Log,
    state: Mutex<PoolState>,
}

impl std::fmt::Debug for ProxyPool {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let state = self.state.lock().expect("pool");
        f.debug_struct("ProxyPool")
            .field("proxies", &state.proxies.len())
            .field("benched", &state.benched_until.len())
            .finish_non_exhaustive()
    }
}

fn process_monotonic() -> Monotonic {
    let start = std::time::Instant::now();
    Box::new(move || start.elapsed().as_secs_f64())
}

impl ProxyPool {
    /// A pool reading `list_file` from disk on the process's monotonic clock.
    pub fn new(list_file: impl Into<String>, policy: EgressPolicy, log: Log) -> ProxyPool {
        ProxyPool::with(
            list_file,
            policy,
            Box::new(|path| std::fs::read_to_string(path)),
            process_monotonic(),
            log,
        )
    }

    pub fn with(
        list_file: impl Into<String>,
        policy: EgressPolicy,
        read: ReadList,
        monotonic: Monotonic,
        log: Log,
    ) -> ProxyPool {
        ProxyPool {
            list_file: list_file.into(),
            policy,
            read,
            monotonic,
            log,
            state: Mutex::new(PoolState::default()),
        }
    }

    fn refresh_locked(&self, state: &mut PoolState) {
        let now = (self.monotonic)();
        if let Some(at) = state.refreshed_at
            && now - at < self.policy.refresh_s as f64
        {
            return;
        }
        state.refreshed_at = Some(now);
        let proxies = match (self.read)(&self.list_file) {
            Ok(text) => parse_proxy_list(&text),
            Err(error) => {
                (self.log)(&format!(
                    "egress: proxy list unreadable ({})",
                    read_failure(&error)
                ));
                return;
            }
        };
        if proxies.is_empty() {
            (self.log)("egress: proxy list has no usable line; pool kept");
            return;
        }
        if proxies != state.proxies {
            (self.log)(&format!("egress: {} proxies", proxies.len()));
            state.proxies = proxies;
            state.benched_until.clear();
            state.next = 0;
        }
    }

    /// Reread the list when due; an unreadable or empty file keeps the current pool.
    pub fn refresh(&self) {
        let mut state = self.state.lock().expect("pool");
        self.refresh_locked(&mut state);
    }

    /// The next proxy not on the bench, round-robin; None if every one is benched.
    pub fn pick(&self) -> Option<(usize, Proxy)> {
        let mut state = self.state.lock().expect("pool");
        self.refresh_locked(&mut state);
        let now = (self.monotonic)();
        let count = state.proxies.len();
        for step in 0..count {
            let index = (state.next + step) % count;
            if state.benched_until.get(&index).copied().unwrap_or(0.0) <= now {
                state.next = (index + 1) % count;
                return Some((index, state.proxies[index].clone()));
            }
        }
        None
    }

    pub fn bench(&self, index: usize, reason: &str) {
        {
            let mut state = self.state.lock().expect("pool");
            let until = (self.monotonic)() + self.policy.cooldown_s as f64;
            state.benched_until.insert(index, until);
        }
        (self.log)(&format!("egress: proxy #{index} benched ({reason})"));
    }

    pub fn proxies(&self) -> Vec<Proxy> {
        self.state.lock().expect("pool").proxies.clone()
    }

    /// The benched proxies' indexes, ascending.
    pub fn benched(&self) -> Vec<usize> {
        self.state
            .lock()
            .expect("pool")
            .benched_until
            .keys()
            .copied()
            .collect()
    }
}

/// Why a proxy is benched after `poll`; None to keep it.
pub fn bench_reason(poll: &Poll) -> Option<String> {
    match (poll.status, &poll.error) {
        (Some(status), _) if BENCH_STATUSES.contains(&status) => Some(format!("HTTP {status}")),
        (None, Some(error)) => Some(error.split(':').next().unwrap_or_default().to_owned()),
        _ => None,
    }
}

/// The fetcher of the recorder and the worker: direct, or through the pool.
pub struct Egress {
    pub pool: Option<Arc<ProxyPool>>,
    pub http: Http,
    pub clock: Clock,
    pub secrets: BTreeMap<String, String>,
    /// Per channel: the cursor value of its last good poll and when that poll was received.
    cursors: Mutex<HashMap<String, (String, Instant)>>,
}

impl std::fmt::Debug for Egress {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Egress")
            .field("pool", &self.pool)
            .field("secrets", &self.secrets.keys().collect::<Vec<_>>())
            .finish_non_exhaustive()
    }
}

impl Egress {
    pub fn new(pool: Option<Arc<ProxyPool>>, secrets: BTreeMap<String, String>) -> Egress {
        Egress::with_clock(pool, secrets, system_clock())
    }

    pub fn with_clock(
        pool: Option<Arc<ProxyPool>>,
        secrets: BTreeMap<String, String>,
        clock: Clock,
    ) -> Egress {
        Egress {
            pool,
            http: Http::new(),
            clock,
            secrets,
            cursors: Mutex::new(HashMap::new()),
        }
    }

    /// The channels that may poll: a proxied channel needs the pool, an auth channel its
    /// secret (fail closed).
    pub fn usable(&self, channels: &[Channel], log: &dyn Fn(&str)) -> Vec<Channel> {
        let mut kept = Vec::new();
        for channel in channels {
            if channel.proxied && self.pool.is_none() {
                log(&format!(
                    "{}: no proxy list ({LIST_ENV} or {LIST_SETTING}); not polled",
                    channel.name()
                ));
                continue;
            }
            if let Some(auth) = &channel.auth
                && !self.secrets.contains_key(&auth.secret)
            {
                log(&format!(
                    "{}: secret '{}' is not configured ({} or [realtime.secrets] {}); not polled",
                    channel.name(),
                    auth.secret,
                    secret_env(&auth.secret),
                    auth.secret
                ));
                continue;
            }
            kept.push(channel.clone());
        }
        kept
    }

    fn since(&self, channel: &Channel) -> Option<String> {
        let cursor = channel.cursor.as_ref()?;
        let last = self
            .cursors
            .lock()
            .expect("cursors")
            .get(&channel.name())
            .cloned()?;
        let age = (self.clock)() - last.1;
        (age <= TimeDelta::from_secs_f64(cursor.max_age_s)).then_some(last.0)
    }

    fn advance(&self, channel: &Channel, poll: &Poll) {
        let Some(cursor) = &channel.cursor else {
            return;
        };
        if !poll.ok() {
            return; // a failed poll keeps the cursor: the next delta covers the gap
        }
        let mut cursors = self.cursors.lock().expect("cursors");
        match poll.headers.get(&cursor.header).filter(|v| !v.is_empty()) {
            Some(value) => {
                cursors.insert(channel.name(), (value.clone(), poll.received_at));
            }
            None => {
                cursors.remove(&channel.name());
            }
        }
    }

    /// One poll of `channel` (with a demand read's `body`).
    pub async fn fetch(&self, channel: &Channel, body: Option<&[u8]>) -> Poll {
        let mut headers: Vec<(String, String)> = Vec::new();
        if let Some(auth) = &channel.auth {
            let Some(secret) = self.secrets.get(&auth.secret) else {
                return refused(&self.clock, &format!("secret '{}' required", auth.secret));
            };
            headers.push((auth.header.clone(), secret.clone()));
        }
        let params: Vec<(String, String)> = match (&channel.cursor, self.since(channel)) {
            (Some(cursor), Some(since)) => vec![(cursor.param.clone(), since)],
            _ => Vec::new(),
        };
        let keep: Vec<String> = channel.cursor.iter().map(|c| c.header.clone()).collect();
        let mut poll = if channel.proxied {
            self.proxied(channel, body, &headers, &params, &keep).await
        } else {
            let request = channel_request(channel, false, body, &headers, &params);
            send(
                &self.http,
                &request,
                channel.timeout_s,
                &self.clock,
                None,
                &keep,
            )
            .await
        };
        self.advance(channel, &poll);
        poll.request.extend(params);
        poll
    }

    async fn proxied(
        &self,
        channel: &Channel,
        body: Option<&[u8]>,
        headers: &[(String, String)],
        params: &[(String, String)],
        keep: &[String],
    ) -> Poll {
        let Some(pool) = &self.pool else {
            return refused(&self.clock, "proxied: no proxy list configured");
        };
        let mut last: Option<Poll> = None;
        for _attempt in 0..2 {
            let Some((index, proxy)) = pool.pick() else {
                break;
            };
            let request = channel_request(channel, true, body, headers, params);
            let poll = send(
                &self.http,
                &request,
                channel.timeout_s,
                &self.clock,
                Some(&proxy),
                keep,
            )
            .await;
            match bench_reason(&poll) {
                None => return poll,
                Some(reason) => pool.bench(index, &reason),
            }
            last = Some(poll);
        }
        last.unwrap_or_else(|| {
            let state = if pool.proxies().is_empty() {
                "no proxy list"
            } else {
                "every proxy benched"
            };
            refused(&self.clock, &format!("proxied: {state}"))
        })
    }
}

impl Fetcher for Egress {
    async fn fetch(&self, channel: &Channel, body: Option<Vec<u8>>) -> Poll {
        Egress::fetch(self, channel, body.as_deref()).await
    }
}

/// The egress for `channels` (and lookups' requests): the pool when any is proxied and a list
/// is configured, and the secrets the channels name (`build_egress`).
pub fn build_egress(
    channels: &[Channel],
    policy: &EgressPolicy,
    config: &Path,
    env: &dyn Env,
    log: Log,
) -> Result<Egress, ConfigurationError> {
    let mut pool = None;
    if channels.iter().any(|c| c.proxied)
        && let Some(list_file) = load_proxy_list(config, env, &*log)?
    {
        let created = ProxyPool::new(list_file, policy.clone(), Arc::clone(&log));
        created.refresh();
        pool = Some(Arc::new(created));
    }
    let secrets = load_secrets(
        channels
            .iter()
            .filter_map(|c| c.auth.as_ref().map(|a| a.secret.as_str())),
        config,
        env,
    )?;
    Ok(Egress::new(pool, secrets))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn proxy_list_lines_become_proxies() {
        let text =
            "10.0.0.1:8080:alice:s3cr:et\n10.0.0.2:8080:bob:p@ss\n\nnot a proxy\n10.0.0.3:x:a:b\n";
        let proxies = parse_proxy_list(text);
        let urls: Vec<String> = proxies.iter().map(Proxy::url).collect();
        assert_eq!(urls, ["http://bob:p%40ss@10.0.0.2:8080"]);
        let crlf = parse_proxy_list(" 10.0.0.1:1:a:b \r\n10.0.0.2:2:c:d\n10.0.0.3:3:e:f");
        assert_eq!(crlf.len(), 3);
    }

    #[test]
    fn a_failed_list_read_keeps_the_pool() {
        let texts = Mutex::new(vec!["".to_owned(), "10.0.0.1:1:a:b".to_owned()]);
        let clock = Mutex::new(vec![4000.0, 0.0]);
        let pool = ProxyPool::with(
            "secret.txt",
            EgressPolicy {
                refresh_s: 3600,
                cooldown_s: 600,
            },
            Box::new(move |_| Ok(texts.lock().unwrap().pop().unwrap())),
            Box::new(move || clock.lock().unwrap().pop().unwrap()),
            Arc::new(|_| {}),
        );
        pool.refresh();
        pool.refresh();
        let urls: Vec<String> = pool.proxies().iter().map(Proxy::url).collect();
        assert_eq!(urls, ["http://a:b@10.0.0.1:1"]);
    }

    #[test]
    fn an_unreadable_list_is_logged_by_error_name_only() {
        let logs = Arc::new(Mutex::new(Vec::<String>::new()));
        let sink = Arc::clone(&logs);
        let pool = ProxyPool::with(
            "D:/secret/list.txt",
            EgressPolicy {
                refresh_s: 3600,
                cooldown_s: 600,
            },
            Box::new(|path| std::fs::read_to_string(path)),
            Box::new(|| 0.0),
            Arc::new(move |m| sink.lock().unwrap().push(m.to_owned())),
        );
        pool.refresh();
        assert_eq!(
            logs.lock().unwrap().as_slice(),
            ["egress: proxy list unreadable (entity not found)"]
        );
        assert!(pool.pick().is_none());
    }

    #[test]
    fn bench_reasons() {
        let at = Instant::from_micros(0);
        let mut poll = Poll::failed(at, at, "timeout: timed out");
        assert_eq!(bench_reason(&poll).as_deref(), Some("timeout"));
        poll.error = Some("HTTP 407 Proxy Authentication Required".into());
        poll.status = Some(407);
        assert_eq!(bench_reason(&poll).as_deref(), Some("HTTP 407"));
        poll.status = Some(503);
        assert_eq!(bench_reason(&poll), None);
    }
}
