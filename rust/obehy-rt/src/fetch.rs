//! One HTTP request of a channel, as a [`Poll`] (successful or not).
//!
//! A proxied channel is never fetched directly: [`fetch`] refuses it, and only
//! [`crate::egress::Egress`] sends it, through the proxy pool (docs/R2_SLICE.md section 7). A
//! channel with `auth` needs its secret, which only the egress holds: refused here too.
//!
//! `{epoch_ms}` in a URL is filled with the current time at each request (a cache buster,
//! DPMD-Q9). A gzip-encoded response, which a manifest asks for with its own `accept-encoding`
//! header, is stored decompressed; an error response's body is kept as sent.
//!
//! The request carries what urllib's would: the manifest's headers (a later name replaces an
//! earlier one regardless of case), `User-Agent: Obehy/...` unless anonymous, `Accept-Encoding:
//! identity` unless the manifest names an encoding (`http.client`'s default), and
//! `Content-Type: application/x-www-form-urlencoded` on a body without a content type (urllib's
//! default; SŽ's boards rely on it). One difference reqwest imposes: it adds `Accept: */*` to a
//! request without an `Accept` header. Connections are kept alive (urllib closes each).
//!
//! Timeouts are urllib's: `timeout_s` bounds the connect and each read, not the whole request.

use std::collections::{BTreeMap, HashMap};
use std::io::Read as _;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use obehy_core::config::Channel;
use obehy_core::time::Instant;
use reqwest::header::{HeaderMap, HeaderName, HeaderValue};

use crate::archive::Poll;

pub const KEPT_HEADERS: [&str; 4] = ["age", "date", "etag", "last-modified"];
pub const EPOCH_MS: &str = "{epoch_ms}";
/// `pipeline.download.USER_AGENT`, sent on direct requests.
pub const USER_AGENT: &str = "Obehy/0.1 (+https://obehy.cz)";
const FORM: &str = "application/x-www-form-urlencoded";
/// Clients kept per (proxy, timeout), at most 256.
const MAX_CLIENTS: usize = 256;

/// The wall clock of polls (`times.now`); tests step their own.
pub type Clock = Arc<dyn Fn() -> Instant + Send + Sync>;

pub fn system_clock() -> Clock {
    Arc::new(obehy_core::time::now)
}

/// One proxy of the pool, from an `ip:port:username:password` line. Never printed: its
/// `Debug` shows only that it is a proxy.
#[derive(Clone, PartialEq, Eq, Hash)]
pub struct Proxy {
    pub host: String,
    pub port: String,
    pub user: String,
    pub password: String,
}

impl std::fmt::Debug for Proxy {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("Proxy(..)")
    }
}

impl Proxy {
    /// The proxy URL, credentials quoted: `http://user:p%40ss@host:port`.
    pub fn url(&self) -> String {
        format!(
            "http://{}:{}@{}:{}",
            quote(&self.user, false),
            quote(&self.password, false),
            self.host,
            self.port
        )
    }
}

/// `urllib.parse.quote(text, safe="")`, or `quote_plus` (a space becomes `+`).
pub fn quote(text: &str, plus: bool) -> String {
    let mut out = String::with_capacity(text.len());
    for byte in text.bytes() {
        match byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'_' | b'.' | b'-' | b'~' => {
                out.push(byte as char);
            }
            b' ' if plus => out.push('+'),
            _ => out.push_str(&format!("%{byte:02X}")),
        }
    }
    out
}

/// `urllib.parse.urlencode(params)`.
pub fn urlencode(params: &[(String, String)]) -> String {
    params
        .iter()
        .map(|(k, v)| format!("{}={}", quote(k, true), quote(v, true)))
        .collect::<Vec<_>>()
        .join("&")
}

/// A request as built from a channel, before it is sent.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Request {
    pub method: String,
    pub url: String,
    /// Names as given, case-folded duplicates merged (the later value wins in the earlier
    /// place, as urllib's `Request.headers` dict keyed by `name.capitalize()`).
    pub headers: Vec<(String, String)>,
    pub body: Option<Vec<u8>>,
}

fn merge_header(headers: &mut Vec<(String, String)>, name: &str, value: &str) {
    match headers
        .iter_mut()
        .find(|(k, _)| k.eq_ignore_ascii_case(name))
    {
        Some(slot) => slot.1 = value.to_owned(),
        None => headers.push((name.to_owned(), value.to_owned())),
    }
}

fn epoch_ms() -> String {
    let since = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default();
    since.as_millis().to_string()
}

/// The channel's request, with `headers` (a secret) and query `params` (a cursor) added; an
/// anonymous one carries no header but the manifest's and these (`channel_request`).
pub fn channel_request(
    channel: &Channel,
    anonymous: bool,
    body: Option<&[u8]>,
    headers: &[(String, String)],
    params: &[(String, String)],
) -> Request {
    let data = body.map(<[u8]>::to_vec).or_else(|| channel.body.clone());
    let mut url = channel.url.replace(EPOCH_MS, &epoch_ms());
    if !params.is_empty() {
        url.push(if url.contains('?') { '&' } else { '?' });
        url.push_str(&urlencode(params));
    }
    let mut sent: Vec<(String, String)> = Vec::new();
    if !anonymous {
        sent.push(("User-Agent".to_owned(), USER_AGENT.to_owned()));
    }
    for (name, value) in channel
        .headers
        .iter()
        .chain(headers.iter().map(|(k, v)| (k, v)))
    {
        merge_header(&mut sent, name, value);
    }
    Request {
        method: channel.method.clone(),
        url,
        headers: sent,
        body: data,
    }
}

/// A poll that was never sent.
pub fn refused(clock: &Clock, reason: &str) -> Poll {
    let at = clock();
    Poll::failed(at, at, reason)
}

/// The HTTP clients: one per (proxy, timeout), built on first use and kept.
#[derive(Debug, Default)]
pub struct Http {
    clients: Mutex<HashMap<(Option<Proxy>, u64), reqwest::Client>>,
}

impl Http {
    pub fn new() -> Http {
        Http::default()
    }

    fn client(&self, proxy: Option<&Proxy>, timeout_s: f64) -> Result<reqwest::Client, String> {
        let timeout = Duration::from_secs_f64(timeout_s.max(0.0));
        let key = (proxy.cloned(), timeout.as_micros() as u64);
        let mut clients = self.clients.lock().expect("client cache");
        if let Some(client) = clients.get(&key) {
            return Ok(client.clone());
        }
        let mut builder = reqwest::Client::builder()
            .http1_only()
            .connect_timeout(timeout)
            .read_timeout(timeout);
        builder = match proxy {
            None => builder.no_proxy(),
            Some(p) => {
                let proxy = reqwest::Proxy::all(format!("http://{}:{}", p.host, p.port))
                    .map_err(|_| "invalid: proxy address".to_owned())?
                    .basic_auth(&p.user, &p.password);
                builder.proxy(proxy)
            }
        };
        let client = builder
            .build()
            .map_err(|e| format!("transport: {}", error_chain(&e)))?;
        if clients.len() >= MAX_CLIENTS {
            clients.clear();
        }
        clients.insert(key, client.clone());
        Ok(client)
    }
}

/// An error and its causes, as one line.
fn error_chain(error: &dyn std::error::Error) -> String {
    let mut text = error.to_string();
    let mut source = error.source();
    while let Some(cause) = source {
        let more = cause.to_string();
        if !text.contains(&more) {
            text.push_str(": ");
            text.push_str(&more);
        }
        source = cause.source();
    }
    text
}

fn is_timeout(error: &reqwest::Error) -> bool {
    if error.is_timeout() {
        return true;
    }
    let mut source = std::error::Error::source(error);
    while let Some(cause) = source {
        if let Some(io) = cause.downcast_ref::<std::io::Error>()
            && io.kind() == std::io::ErrorKind::TimedOut
        {
            return true;
        }
        source = cause.source();
    }
    false
}

/// A transport failure as `<kind>: <detail>`: the kind is what the egress benches a proxy for
/// (`timeout`, `connect`, `transport`).
fn transport_error(error: &reqwest::Error) -> String {
    if is_timeout(error) {
        "timeout: timed out".to_owned()
    } else if error.is_connect() {
        format!("connect: {}", error_chain(error))
    } else if error.is_builder() {
        format!("invalid: {}", error_chain(error))
    } else {
        format!("transport: {}", error_chain(error))
    }
}

/// A header value, read as Latin-1.
fn latin1(value: &HeaderValue) -> String {
    value.as_bytes().iter().map(|&b| b as char).collect()
}

fn kept_headers(headers: &HeaderMap, extra: &[String]) -> BTreeMap<String, String> {
    let mut kept = BTreeMap::new();
    for name in KEPT_HEADERS
        .iter()
        .copied()
        .chain(extra.iter().map(String::as_str))
    {
        if let Some(value) = headers.get(name) {
            kept.insert(name.to_owned(), latin1(value));
        }
    }
    kept
}

/// `gzip.decompress`: every member, an error for anything else.
fn gunzip(body: &[u8]) -> Result<Vec<u8>, String> {
    if body.len() >= 2 && body[..2] != [0x1f, 0x8b] {
        return Err(format!(
            "BadGzipFile: Not a gzipped file ({:?})",
            &body[..2]
        ));
    }
    let mut out = Vec::new();
    flate2::read::MultiGzDecoder::new(body)
        .read_to_end(&mut out)
        .map_err(|e| format!("EOFError: {e}"))?;
    Ok(out)
}

fn build(client: &reqwest::Client, request: &Request) -> Result<reqwest::RequestBuilder, String> {
    let method = reqwest::Method::from_bytes(request.method.as_bytes())
        .map_err(|_| format!("invalid: method {:?}", request.method))?;
    let url =
        reqwest::Url::parse(&request.url).map_err(|_| format!("invalid: url {:?}", request.url))?;
    let mut headers = HeaderMap::new();
    let mut has_type = false;
    let mut has_encoding = false;
    for (name, value) in &request.headers {
        let name = HeaderName::from_bytes(name.as_bytes())
            .map_err(|_| format!("invalid: header name {name:?}"))?;
        let value = HeaderValue::from_str(value)
            .map_err(|_| "ValueError: Invalid header value".to_owned())?;
        has_type |= name == reqwest::header::CONTENT_TYPE;
        has_encoding |= name == reqwest::header::ACCEPT_ENCODING;
        headers.insert(name, value);
    }
    if !has_encoding {
        headers.insert(
            reqwest::header::ACCEPT_ENCODING,
            HeaderValue::from_static("identity"),
        );
    }
    let mut builder = client.request(method, url);
    if let Some(body) = &request.body {
        if !has_type {
            headers.insert(
                reqwest::header::CONTENT_TYPE,
                HeaderValue::from_static(FORM),
            );
        }
        builder = builder.body(body.clone());
    }
    Ok(builder.headers(headers))
}

/// Send `request` (through `proxy` when given); the poll keeps the response headers of
/// [`KEPT_HEADERS`] and `keep` (`send`). Never fails: any error is a failed poll.
pub async fn send(
    http: &Http,
    request: &Request,
    timeout_s: f64,
    clock: &Clock,
    proxy: Option<&Proxy>,
    keep: &[String],
) -> Poll {
    let requested_at = clock();
    let builder = match http
        .client(proxy, timeout_s)
        .and_then(|client| build(&client, request))
    {
        Ok(builder) => builder,
        Err(error) => return Poll::failed(requested_at, clock(), error),
    };
    let response = match builder.send().await {
        Ok(response) => response,
        Err(error) => return Poll::failed(requested_at, clock(), transport_error(&error)),
    };
    let status = response.status();
    let headers = response.headers().clone();
    let reason = response
        .extensions()
        .get::<hyper::ext::ReasonPhrase>()
        .map(|r| String::from_utf8_lossy(r.as_bytes()).into_owned())
        .or_else(|| status.canonical_reason().map(str::to_owned))
        .unwrap_or_default();
    let body = match response.bytes().await {
        Ok(body) => body.to_vec(),
        Err(error) => return Poll::failed(requested_at, clock(), transport_error(&error)),
    };
    let content_type = headers.get(reqwest::header::CONTENT_TYPE).map(latin1);
    if !status.is_success() {
        // urllib's HTTPError: the body as sent (None when empty), only the usual headers.
        return Poll {
            requested_at,
            received_at: clock(),
            status: Some(status.as_u16()),
            body: (!body.is_empty()).then_some(body),
            content_type,
            headers: kept_headers(&headers, &[]),
            error: Some(format!("HTTP {} {reason}", status.as_u16())),
            request: BTreeMap::new(),
        };
    }
    let gzipped = headers
        .get(reqwest::header::CONTENT_ENCODING)
        .is_some_and(|v| v.as_bytes() == b"gzip");
    let body = if gzipped {
        match gunzip(&body) {
            Ok(plain) => plain,
            Err(error) => return Poll::failed(requested_at, clock(), error),
        }
    } else {
        body
    };
    Poll {
        requested_at,
        received_at: clock(),
        status: Some(status.as_u16()),
        body: Some(body),
        content_type,
        headers: kept_headers(&headers, keep),
        error: None,
        request: BTreeMap::new(),
    }
}

fn refusal(channel: &Channel) -> Option<String> {
    if channel.proxied {
        return Some("proxied: not sent directly".to_owned());
    }
    channel
        .auth
        .as_ref()
        .map(|auth| format!("secret '{}' required", auth.secret))
}

/// A direct request; refused for a channel that must go through the proxies or needs a
/// secret (`fetch`).
pub async fn fetch(http: &Http, channel: &Channel, clock: &Clock) -> Poll {
    if let Some(reason) = refusal(channel) {
        return refused(clock, &reason);
    }
    let request = channel_request(channel, false, None, &[], &[]);
    send(http, &request, channel.timeout_s, clock, None, &[]).await
}

/// A direct request with a per-read body (demand channels); refused like [`fetch`].
pub async fn fetch_body(http: &Http, channel: &Channel, body: &[u8], clock: &Clock) -> Poll {
    if let Some(reason) = refusal(channel) {
        return refused(clock, &reason);
    }
    let request = channel_request(channel, false, Some(body), &[], &[]);
    send(http, &request, channel.timeout_s, clock, None, &[]).await
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn quoting_and_urlencode_follow_urllib() {
        assert_eq!(quote("p@ss:w rd~", false), "p%40ss%3Aw%20rd~");
        assert_eq!(
            urlencode(&[("updatedSince".into(), "2026-10-14T22:00:01.000Z".into())]),
            "updatedSince=2026-10-14T22%3A00%3A01.000Z"
        );
        assert_eq!(urlencode(&[("a b".into(), "č d".into())]), "a+b=%C4%8D+d");
    }

    #[test]
    fn a_proxy_never_prints_its_credentials() {
        let proxy = Proxy {
            host: "10.0.0.2".into(),
            port: "8080".into(),
            user: "bob".into(),
            password: "p@ss".into(),
        };
        assert_eq!(format!("{proxy:?}"), "Proxy(..)");
        assert_eq!(proxy.url(), "http://bob:p%40ss@10.0.0.2:8080");
    }

    #[test]
    fn gzip_bodies_decompress_and_garbage_fails() {
        let mut encoder = flate2::write::GzEncoder::new(Vec::new(), flate2::Compression::default());
        std::io::Write::write_all(&mut encoder, b"plain").unwrap();
        let packed = encoder.finish().unwrap();
        assert_eq!(gunzip(&packed).unwrap(), b"plain");
        assert!(gunzip(b"nope").unwrap_err().starts_with("BadGzipFile"));
        assert!(gunzip(&packed[..packed.len() - 4]).is_err());
    }
}
