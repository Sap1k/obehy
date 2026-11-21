//! Fetching and egress against local HTTP servers (docs/RUST_PORT.md R8; the port of
//! `tests/unit/realtime/test_egress.py`): the proxy pool's rotation, bench and reload, SŽ's
//! fail-closed rule, the exact request headers, timeouts, gzip, error bodies and delta cursors.
//! No request leaves 127.0.0.1.

use std::collections::BTreeMap;
use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant as Monotonic};

use obehy_core::config::Channel;
use obehy_core::config::manifest::parse_source;
use obehy_core::config::policy::EgressPolicy;
use obehy_core::time::{Instant, TimeDelta};
use obehy_rt::egress::{Egress, ProxyPool};
use obehy_rt::fetch::{Clock, Http, fetch, fetch_body, system_clock};
use obehy_rt::logs::Log;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};

const POLICY: EgressPolicy = EgressPolicy {
    refresh_s: 3600,
    cooldown_s: 600,
};

/// One request a stub received: its request line, headers (names lower-cased) and body.
#[derive(Debug, Clone)]
struct Seen {
    line: String,
    headers: Vec<(String, String)>,
    body: Vec<u8>,
}

impl Seen {
    fn header(&self, name: &str) -> Option<&str> {
        self.headers
            .iter()
            .find(|(k, _)| k == name)
            .map(|(_, v)| v.as_str())
    }
}

#[derive(Debug, Clone)]
enum Reply {
    Answer {
        status: u16,
        headers: Vec<(String, String)>,
        body: Vec<u8>,
    },
    /// Accept, read the request, never answer.
    Hang,
}

fn answer(status: u16, headers: &[(&str, &str)], body: &[u8]) -> Reply {
    Reply::Answer {
        status,
        headers: headers
            .iter()
            .map(|(k, v)| ((*k).to_owned(), (*v).to_owned()))
            .collect(),
        body: body.to_vec(),
    }
}

type Responder = Arc<dyn Fn(&Seen, usize) -> Reply + Send + Sync>;

/// A local HTTP server (or forward proxy: it answers absolute-URI and CONNECT requests
/// itself) recording every request.
struct Stub {
    addr: SocketAddr,
    seen: Arc<Mutex<Vec<Seen>>>,
}

impl Stub {
    async fn start(respond: impl Fn(&Seen, usize) -> Reply + Send + Sync + 'static) -> Stub {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let seen = Arc::new(Mutex::new(Vec::new()));
        let respond: Responder = Arc::new(respond);
        let log = Arc::clone(&seen);
        tokio::spawn(async move {
            loop {
                let Ok((stream, _)) = listener.accept().await else {
                    return;
                };
                let (log, respond) = (Arc::clone(&log), Arc::clone(&respond));
                tokio::spawn(serve(stream, log, respond));
            }
        });
        Stub { addr, seen }
    }

    async fn fixed(reply: Reply) -> Stub {
        Stub::start(move |_, _| reply.clone()).await
    }

    fn seen(&self) -> Vec<Seen> {
        self.seen.lock().unwrap().clone()
    }

    fn proxy_line(&self) -> String {
        format!("127.0.0.1:{}:user:p@ss", self.addr.port())
    }

    fn url(&self, path: &str) -> String {
        format!("http://127.0.0.1:{}{path}", self.addr.port())
    }
}

async fn serve(mut stream: TcpStream, log: Arc<Mutex<Vec<Seen>>>, respond: Responder) {
    let mut buffer = Vec::new();
    let mut chunk = [0u8; 4096];
    let head_end = loop {
        if let Some(at) = buffer.windows(4).position(|w| w == b"\r\n\r\n") {
            break at;
        }
        match stream.read(&mut chunk).await {
            Ok(0) | Err(_) => return,
            Ok(n) => buffer.extend_from_slice(&chunk[..n]),
        }
    };
    let head = String::from_utf8_lossy(&buffer[..head_end]).into_owned();
    let mut lines = head.split("\r\n");
    let line = lines.next().unwrap_or_default().to_owned();
    let headers: Vec<(String, String)> = lines
        .filter_map(|l| l.split_once(':'))
        .map(|(k, v)| (k.trim().to_lowercase(), v.trim().to_owned()))
        .collect();
    let length: usize = headers
        .iter()
        .find(|(k, _)| k == "content-length")
        .and_then(|(_, v)| v.parse().ok())
        .unwrap_or(0);
    let mut body = buffer[head_end + 4..].to_vec();
    while body.len() < length {
        match stream.read(&mut chunk).await {
            Ok(0) | Err(_) => break,
            Ok(n) => body.extend_from_slice(&chunk[..n]),
        }
    }
    let seen = Seen {
        line,
        headers,
        body,
    };
    let number = {
        let mut log = log.lock().unwrap();
        log.push(seen.clone());
        log.len() - 1
    };
    match respond(&seen, number) {
        Reply::Hang => {
            tokio::time::sleep(Duration::from_secs(30)).await;
        }
        Reply::Answer {
            status,
            headers,
            body,
        } => {
            let mut out = format!("HTTP/1.1 {status} Reason{status}\r\n");
            for (k, v) in &headers {
                out.push_str(&format!("{k}: {v}\r\n"));
            }
            out.push_str(&format!(
                "Content-Length: {}\r\nConnection: close\r\n\r\n",
                body.len()
            ));
            let _ = stream.write_all(out.as_bytes()).await;
            let _ = stream.write_all(&body).await;
            let _ = stream.shutdown().await;
        }
    }
}

fn channel(manifest: &str, stem: &str) -> Channel {
    let text = format!(
        "source = \"{stem}\"\nmanifest_version = 1\ntitle = \"t\"\ncovers = \"c\"\n{manifest}"
    );
    parse_source(&text, stem, "test.toml", &[])
        .unwrap()
        .channels[0]
        .clone()
}

/// SŽ's map channel, proxied, at `url`.
fn sz(url: &str, timeout_s: f64) -> Channel {
    channel(
        &format!(
            "[[channel]]\nname = \"trains\"\nproxied = true\n\
             poll = {{ kind = \"interval\", seconds = 30 }}\n\
             backoff = {{ after_failures = 5, max_s = 300 }}\ntimeout_s = {timeout_s}\n\
             request = {{ method = \"GET\", url = '{url}', headers = {{ Accept = \"application/json\" }} }}\n"
        ),
        "sz-mapa",
    )
}

fn direct(url: &str, extra: &str) -> Channel {
    channel(
        &format!(
            "[[channel]]\nname = \"vehicles\"\npoll = {{ kind = \"interval\", seconds = 15 }}\n\
             backoff = {{ after_failures = 5, max_s = 300 }}\ntimeout_s = 5\n{extra}\n\
             [channel.request]\nmethod = \"GET\"\nurl = \"{url}\"\n"
        ),
        "duk",
    )
}

struct Logs(Arc<Mutex<Vec<String>>>);

impl Logs {
    fn new() -> (Logs, Log) {
        let lines = Arc::new(Mutex::new(Vec::new()));
        let sink = Arc::clone(&lines);
        (
            Logs(lines),
            Arc::new(move |m: &str| sink.lock().unwrap().push(m.to_owned())),
        )
    }

    fn all(&self) -> String {
        self.0.lock().unwrap().join("\n")
    }
}

/// A pool over `text`, re-read on a clock the test steps.
fn pool(text: &str, log: Log) -> Arc<ProxyPool> {
    let text = text.to_owned();
    let pool = ProxyPool::with(
        "secret.txt",
        POLICY,
        Box::new(move |_| Ok(text.clone())),
        Box::new(|| 0.0),
        log,
    );
    pool.refresh();
    Arc::new(pool)
}

fn egress(pool: Option<Arc<ProxyPool>>) -> Egress {
    Egress::new(pool, BTreeMap::new())
}

#[tokio::test]
async fn requests_go_through_the_proxy_without_a_user_agent() {
    let working = Stub::fixed(answer(
        200,
        &[("Content-Type", "application/json")],
        b"{\"ok\": true}",
    ))
    .await;
    let (logs, log) = Logs::new();
    let poll = egress(Some(pool(&working.proxy_line(), log)))
        .fetch(&sz("http://sz.invalid/trains", 5.0), None)
        .await;
    assert!(poll.ok(), "{poll:?}");
    assert_eq!(poll.body.as_deref(), Some(&b"{\"ok\": true}"[..]));
    let seen = working.seen();
    assert_eq!(seen.len(), 1);
    let request = &seen[0];
    assert_eq!(request.line, "GET http://sz.invalid/trains HTTP/1.1"); // absolute URI: proxied
    assert_eq!(request.header("user-agent"), None);
    let auth = request.header("proxy-authorization").unwrap();
    assert_eq!(auth, "Basic dXNlcjpwQHNz"); // base64("user:p@ss")
    assert_eq!(request.header("accept"), Some("application/json"));
    assert_eq!(request.header("accept-encoding"), Some("identity"));
    // Nothing else identifies us: the request carries exactly these headers.
    let mut names: Vec<&str> = request.headers.iter().map(|(k, _)| k.as_str()).collect();
    names.sort_unstable();
    assert_eq!(
        names,
        ["accept", "accept-encoding", "host", "proxy-authorization"]
    );
    let text = logs.all();
    assert!(
        !text.contains("p@ss") && !text.contains("127.0.0.1"),
        "{text}"
    );
}

#[tokio::test]
async fn an_https_channel_tunnels_through_the_proxy_without_a_user_agent() {
    // The proxy refuses the tunnel: the poll fails without a status and the proxy is benched.
    let refusing = Stub::fixed(answer(403, &[], b"")).await;
    let (logs, log) = Logs::new();
    let pool = pool(&refusing.proxy_line(), log);
    let poll = egress(Some(Arc::clone(&pool)))
        .fetch(&sz("https://mapy.spravazeleznic.cz/serverside/request2.php?module=Layers\\OsVlaky&&action=load", 5.0), None)
        .await;
    assert!(!poll.ok() && poll.status.is_none(), "{poll:?}");
    let seen = refusing.seen();
    assert_eq!(seen[0].line, "CONNECT mapy.spravazeleznic.cz:443 HTTP/1.1");
    assert_eq!(seen[0].header("user-agent"), None);
    assert!(
        seen[0]
            .header("proxy-authorization")
            .unwrap()
            .starts_with("Basic ")
    );
    assert_eq!(pool.benched(), [0]);
    assert!(
        logs.all().contains("egress: proxy #0 benched (URLError)"),
        "{}",
        logs.all()
    );
}

#[tokio::test]
async fn a_backslash_in_szs_query_is_sent_as_written() {
    let working = Stub::fixed(answer(200, &[], b"[]")).await;
    let url = "http://sz.invalid/serverside/request2.php?module=Layers\\OsVlaky&&action=load";
    let poll = egress(Some(pool(&working.proxy_line(), Arc::new(|_| {}))))
        .fetch(&sz(url, 5.0), None)
        .await;
    assert!(poll.ok(), "{poll:?}");
    assert_eq!(working.seen()[0].line, format!("GET {url} HTTP/1.1"));
}

#[tokio::test]
async fn a_refusing_proxy_is_benched_and_the_request_retried_on_the_next() {
    let refusing = Stub::fixed(answer(403, &[], b"no")).await;
    let working = Stub::fixed(answer(200, &[], b"{}")).await;
    let (logs, log) = Logs::new();
    let pool = pool(
        &format!("{}\n{}\n", refusing.proxy_line(), working.proxy_line()),
        log,
    );
    let egress = egress(Some(Arc::clone(&pool)));
    let channel = sz("http://sz.invalid/trains", 5.0);
    assert!(egress.fetch(&channel, None).await.ok());
    assert_eq!((refusing.seen().len(), working.seen().len()), (1, 1));
    assert_eq!(pool.benched(), [0]);
    assert!(logs.all().contains("proxy #0 benched (HTTP 403)"));
    assert!(egress.fetch(&channel, None).await.ok());
    assert_eq!(refusing.seen().len(), 1); // still on the bench
    assert_eq!(working.seen().len(), 2);
}

#[tokio::test]
async fn every_proxy_refusing_returns_the_refusal_then_sends_nothing() {
    let refusing = Stub::fixed(answer(403, &[], b"")).await;
    let pool = pool(&refusing.proxy_line(), Arc::new(|_| {}));
    let egress = egress(Some(pool));
    let channel = sz("http://sz.invalid/trains", 5.0);
    assert_eq!(egress.fetch(&channel, None).await.status, Some(403));
    let poll = egress.fetch(&channel, None).await;
    assert!(poll.status.is_none());
    assert!(poll.error.unwrap().contains("every proxy benched"));
    assert_eq!(refusing.seen().len(), 1);
}

#[tokio::test]
async fn sz_fails_closed_no_proxy_no_request() {
    // The channel's own URL is a live local server: nothing may reach it.
    let target = Stub::fixed(answer(200, &[], b"{}")).await;
    let channel = sz(&target.url("/trains"), 5.0);
    let http = Http::new();
    let clock = system_clock();
    let refused = fetch(&http, &channel, &clock).await;
    assert!(
        refused
            .error
            .unwrap()
            .contains("proxied: not sent directly")
    );
    let refused = fetch_body(&http, &channel, b"x", &clock).await;
    assert!(refused.status.is_none());
    let (_, log) = Logs::new();
    let none = egress(None);
    let lines = Mutex::new(Vec::<String>::new());
    assert!(
        none.usable(std::slice::from_ref(&channel), &|m| lines
            .lock()
            .unwrap()
            .push(m.into()))
            .is_empty()
    );
    assert!(lines.lock().unwrap()[0].contains("not polled"));
    let poll = none.fetch(&channel, None).await;
    assert_eq!(
        poll.error.as_deref(),
        Some("proxied: no proxy list configured")
    );
    // A list without a usable line is no pool either.
    let empty = egress(Some(pool("not a proxy\n", log)));
    let poll = empty.fetch(&channel, None).await;
    assert_eq!(poll.error.as_deref(), Some("proxied: no proxy list"));
    assert!(target.seen().is_empty());
}

fn scratch(name: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("obehy-rt-egress-{name}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

#[tokio::test]
async fn the_proxy_list_is_reread_when_due_and_a_broken_list_keeps_the_pool() {
    let first = Stub::fixed(answer(200, &[], b"1")).await;
    let second = Stub::fixed(answer(200, &[], b"2")).await;
    let dir = scratch("reload");
    let list = dir.join("proxies.txt");
    std::fs::write(&list, format!("{}\n", first.proxy_line())).unwrap();
    let clock = Arc::new(AtomicU64::new(0));
    let now = Arc::clone(&clock);
    let (logs, log) = Logs::new();
    let pool = Arc::new(ProxyPool::with(
        list.to_string_lossy().into_owned(),
        POLICY,
        Box::new(|path| std::fs::read_to_string(path)),
        Box::new(move || now.load(Ordering::SeqCst) as f64),
        log,
    ));
    pool.refresh();
    let egress = egress(Some(pool));
    let channel = sz("http://sz.invalid/trains", 5.0);
    assert_eq!(egress.fetch(&channel, None).await.body.unwrap(), b"1");
    std::fs::write(&list, format!("{}\n", second.proxy_line())).unwrap();
    clock.store(1000, Ordering::SeqCst); // not due yet: the old pool
    assert_eq!(egress.fetch(&channel, None).await.body.unwrap(), b"1");
    clock.store(3600, Ordering::SeqCst); // due: the new list
    assert_eq!(egress.fetch(&channel, None).await.body.unwrap(), b"2");
    std::fs::write(&list, "garbage\n").unwrap();
    clock.store(7200, Ordering::SeqCst);
    assert_eq!(egress.fetch(&channel, None).await.body.unwrap(), b"2");
    std::fs::remove_file(&list).unwrap();
    clock.store(10800, Ordering::SeqCst);
    assert_eq!(egress.fetch(&channel, None).await.body.unwrap(), b"2");
    assert_eq!((first.seen().len(), second.seen().len()), (2, 3));
    let text = logs.all();
    assert!(
        text.contains("proxy list has no usable line; pool kept"),
        "{text}"
    );
    assert!(
        text.contains("proxy list unreadable (FileNotFoundError)"),
        "{text}"
    );
    assert!(!text.contains("p@ss") && !text.contains(&list.to_string_lossy().into_owned()));
}

#[tokio::test]
async fn a_hanging_server_times_out_and_a_hanging_proxy_is_benched() {
    let hanging = Stub::fixed(Reply::Hang).await;
    let started = Monotonic::now();
    let poll = fetch(
        &Http::new(),
        &direct_timeout(&hanging.url("/x"), 0.3),
        &system_clock(),
    )
    .await;
    let took = started.elapsed();
    assert_eq!(
        poll.error.as_deref(),
        Some("TimeoutError: timed out"),
        "{poll:?}"
    );
    assert!(poll.status.is_none());
    assert!(
        took >= Duration::from_millis(250) && took < Duration::from_secs(5),
        "{took:?}"
    );
    let (logs, log) = Logs::new();
    let pool = pool(&hanging.proxy_line(), log);
    let poll = egress(Some(Arc::clone(&pool)))
        .fetch(&sz("http://sz.invalid/trains", 0.3), None)
        .await;
    assert_eq!(poll.error.as_deref(), Some("TimeoutError: timed out"));
    assert_eq!(pool.benched(), [0]);
    assert!(logs.all().contains("proxy #0 benched (TimeoutError)"));
}

#[tokio::test]
async fn an_unreachable_proxy_is_benched_and_the_next_one_used() {
    let working = Stub::fixed(answer(200, &[], b"ok")).await;
    // A port nothing listens on: bind, note it, close.
    let closed = TcpListener::bind("127.0.0.1:0")
        .await
        .unwrap()
        .local_addr()
        .unwrap();
    let (logs, log) = Logs::new();
    let pool = pool(
        &format!(
            "127.0.0.1:{}:a:b\n{}\n",
            closed.port(),
            working.proxy_line()
        ),
        log,
    );
    let poll = egress(Some(Arc::clone(&pool)))
        .fetch(&sz("http://sz.invalid/trains", 5.0), None)
        .await;
    assert!(poll.ok(), "{poll:?}");
    assert_eq!(pool.benched(), [0]);
    assert!(
        logs.all().contains("proxy #0 benched (URLError)"),
        "{}",
        logs.all()
    );
}

fn direct_timeout(url: &str, timeout_s: f64) -> Channel {
    channel(
        &format!(
            "[[channel]]\nname = \"vehicles\"\npoll = {{ kind = \"interval\", seconds = 15 }}\n\
             backoff = {{ after_failures = 5, max_s = 300 }}\ntimeout_s = {timeout_s}\n\
             request = {{ method = \"GET\", url = \"{url}\" }}\n"
        ),
        "duk",
    )
}

#[tokio::test]
async fn a_direct_poll_names_us_fills_the_cache_buster_and_stores_gzip_decompressed() {
    let mut packed = flate2::write::GzEncoder::new(Vec::new(), flate2::Compression::default());
    std::io::Write::write_all(&mut packed, b"plain").unwrap();
    let packed = packed.finish().unwrap();
    let server = Stub::start(move |_, _| {
        answer(
            200,
            &[
                ("Content-Encoding", "gzip"),
                ("Content-Type", "text/javascript"),
                ("ETag", "\"v1\""),
                ("Date", "Mon, 05 Oct 2026 21:59:50 GMT"),
                ("X-Other", "dropped"),
            ],
            &packed,
        )
    })
    .await;
    let channel = direct(&server.url("/TrafficState.js?v={epoch_ms}"), "");
    let mut channel = channel;
    channel
        .headers
        .insert("accept-encoding".into(), "gzip".into());
    let poll = fetch(&Http::new(), &channel, &system_clock()).await;
    assert!(poll.ok(), "{poll:?}");
    assert_eq!(poll.body.as_deref(), Some(&b"plain"[..]));
    assert_eq!(poll.content_type.as_deref(), Some("text/javascript"));
    let kept: Vec<&str> = poll.headers.keys().map(String::as_str).collect();
    assert_eq!(kept, ["date", "etag"]);
    let seen = &server.seen()[0];
    let stamp = seen
        .line
        .split("v=")
        .nth(1)
        .unwrap()
        .split(' ')
        .next()
        .unwrap();
    assert!(
        stamp.len() == 13 && stamp.bytes().all(|b| b.is_ascii_digit()),
        "{}",
        seen.line
    );
    assert_eq!(
        seen.header("user-agent"),
        Some("Obehy/0.1 (+https://obehy.cz)")
    );
    assert_eq!(seen.header("accept-encoding"), Some("gzip"));
}

#[tokio::test]
async fn an_error_response_keeps_its_body_and_reason() {
    let server = Stub::fixed(answer(
        503,
        &[("Content-Encoding", "gzip"), ("Age", "1")],
        b"busy",
    ))
    .await;
    let poll = fetch(
        &Http::new(),
        &direct(&server.url("/x"), ""),
        &system_clock(),
    )
    .await;
    assert_eq!(poll.status, Some(503));
    assert_eq!(poll.error.as_deref(), Some("HTTP 503 Reason503"));
    assert_eq!(poll.body.as_deref(), Some(&b"busy"[..])); // as sent, not gunzipped
    assert_eq!(poll.headers.get("age").map(String::as_str), Some("1"));
    let empty = Stub::fixed(answer(404, &[], b"")).await;
    let poll = fetch(&Http::new(), &direct(&empty.url("/x"), ""), &system_clock()).await;
    assert_eq!((poll.status, poll.body), (Some(404), None));
}

#[tokio::test]
async fn a_demand_body_is_posted_as_a_form_unless_the_manifest_says_otherwise() {
    let server = Stub::fixed(answer(200, &[], b"<html/>")).await;
    let board = channel(
        &format!(
            "[[channel]]\nname = \"board\"\npoll = {{ kind = \"demand\", query = \"station_boards\" }}\n\
             backoff = {{ after_failures = 3, max_s = 900 }}\ntimeout_s = 5\n\
             request = {{ method = \"POST\", url = \"{}\", body = \"SR70={{sr70}}&x=1\" }}\n",
            server.url("/request2.php")
        ),
        "sz-tabule",
    );
    let poll = egress(None).fetch(&board, Some(b"SR70=534149&x=1")).await;
    assert!(poll.ok(), "{poll:?}");
    let seen = &server.seen()[0];
    assert_eq!(seen.line, "POST /request2.php HTTP/1.1");
    assert_eq!(seen.body, b"SR70=534149&x=1");
    assert_eq!(
        seen.header("content-type"),
        Some("application/x-www-form-urlencoded")
    );
    // urllib's set, plus the `Accept: */*` reqwest adds to a request without one.
    let mut names: Vec<(&str, &str)> = seen
        .headers
        .iter()
        .filter(|(k, _)| k != "host" && k != "content-length")
        .map(|(k, v)| (k.as_str(), v.as_str()))
        .collect();
    names.sort_unstable();
    assert_eq!(
        names,
        [
            ("accept", "*/*"),
            ("accept-encoding", "identity"),
            ("content-type", "application/x-www-form-urlencoded"),
            ("user-agent", "Obehy/0.1 (+https://obehy.cz)"),
        ]
    );
}

#[tokio::test]
async fn a_cursor_channel_polls_deltas_with_its_secret() {
    let status = Arc::new(Mutex::new(200u16));
    let flaky = Arc::clone(&status);
    let failing = Stub::start(move |_, number| {
        let code = *flaky.lock().unwrap();
        answer(
            code,
            &[(
                "x-last-modified",
                &format!("2026-10-14T22:00:0{}.000Z", number + 1),
            )],
            b"{}",
        )
    })
    .await;
    let t0 = Instant::from_micros(1_760_479_200_000_000);
    let now = Arc::new(Mutex::new(t0));
    let reading = Arc::clone(&now);
    let clock: Clock = Arc::new(move || *reading.lock().unwrap());
    let egress = Egress::with_clock(
        None,
        BTreeMap::from([("golemio".to_owned(), "k3y".to_owned())]),
        clock,
    );
    let channel = direct(
        &failing.url("/v2/vehiclepositions?limit=5"),
        "auth = { header = \"X-Access-Token\", secret = \"golemio\" }\n\
         cursor = { header = \"x-last-modified\", param = \"updatedSince\", max_age_s = 120 }",
    );
    let first = egress.fetch(&channel, None).await;
    let second = egress.fetch(&channel, None).await;
    *status.lock().unwrap() = 503;
    let failed = egress.fetch(&channel, None).await;
    *status.lock().unwrap() = 200;
    let after_gap = egress.fetch(&channel, None).await; // a failed poll keeps the cursor
    *now.lock().unwrap() = t0 + TimeDelta::from_micros(121_000_000);
    let stale = egress.fetch(&channel, None).await; // the last good poll is too old: a full list
    let seen = failing.seen();
    assert!(
        seen.iter()
            .all(|s| s.header("x-access-token") == Some("k3y"))
    );
    let paths: Vec<&str> = seen
        .iter()
        .map(|s| s.line.split(' ').nth(1).unwrap())
        .collect();
    assert_eq!(paths[0], "/v2/vehiclepositions?limit=5");
    assert!(first.request.is_empty());
    assert!(
        paths[1].ends_with("&updatedSince=2026-10-14T22%3A00%3A01.000Z"),
        "{}",
        paths[1]
    );
    assert_eq!(second.request["updatedSince"], "2026-10-14T22:00:01.000Z");
    assert!(!failed.ok() && paths[2] == paths[3]);
    assert!(after_gap.ok() && paths[3].ends_with("updatedSince=2026-10-14T22%3A00%3A02.000Z"));
    assert!(!paths[4].contains("updatedSince") && stale.request.is_empty());
}

#[tokio::test]
async fn an_auth_channel_needs_its_secret() {
    let target = Stub::fixed(answer(200, &[], b"{}")).await;
    let channel = direct(
        &target.url("/"),
        "auth = { header = \"X-Access-Token\", secret = \"golemio\" }",
    );
    let lines = Mutex::new(Vec::<String>::new());
    let log = |m: &str| lines.lock().unwrap().push(m.into());
    assert!(
        egress(None)
            .usable(std::slice::from_ref(&channel), &log)
            .is_empty()
    );
    let first = lines.lock().unwrap()[0].clone();
    assert!(
        first.contains("OBEHY_SECRET_GOLEMIO") && first.contains("not polled"),
        "{first}"
    );
    let keyed = Egress::new(
        None,
        BTreeMap::from([("golemio".to_owned(), "k".to_owned())]),
    );
    assert_eq!(keyed.usable(std::slice::from_ref(&channel), &log).len(), 1);
    // Without the egress there is no secret: never sent.
    let poll = fetch(&Http::new(), &channel, &system_clock()).await;
    assert_eq!(poll.error.as_deref(), Some("secret 'golemio' required"));
    let poll = egress(None).fetch(&channel, None).await;
    assert_eq!(poll.error.as_deref(), Some("secret 'golemio' required"));
    assert!(target.seen().is_empty());
}
