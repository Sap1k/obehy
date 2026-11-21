//! Demand polling: request what is needed now, within a budget (docs/R2_SLICE.md section 7).
//!
//! The first demand channel is SŽ's station boards (`station_boards`): one request per station,
//! covering about an hour of its trains. Due passenger calls at stations with at least
//! `boards.min_boarding_points` boarding points come from the active release in SQL (refreshed
//! every `due_refresh_s`); each minute the planner picks the stations to read now:
//!
//! - once when a call comes within `early_min`;
//! - again within `near_min` when the last read is older than `near_refresh_s`;
//! - again within `imminent_min` when it is older than `imminent_refresh_s`.
//!
//! Reads go one at a time, spread over the minute, the soonest due call first (never-read
//! stations before refreshes), at most `ceiling_per_min`; the rest of the minute's demand is
//! dropped and counted. A refusal (403/429), a server error or an answer slower than
//! `slow_latency_factor` times the median halves the ceiling for `slowdown_s`. A 403 also stops
//! reads for the channel's `backoff.max_s` (the circuit), and a fetch that panics is a failed
//! poll (the scheduler's rule). The request's body has `{sr70}` replaced by the station's
//! 6-digit code, kept with the poll for the decoder.

use std::collections::{BTreeMap, BTreeSet};
use std::future::Future;
use std::path::Path;
use std::sync::Arc;

use obehy_core::config::Channel;
use obehy_core::config::policy::BoardPolicy;
use obehy_core::time::{Instant, TimeDelta, local_date};
use tokio_postgres::{Client, NoTls};

use crate::archive::Poll;
use crate::fetch::Clock;
use crate::scheduler::{BLOCKED, Fetcher, HandlerError, OnPoll, Stop, fetch_guarded};

/// 6-digit SR70 -> due times (ascending per location, as the query orders them).
pub type DueCalls = BTreeMap<String, Vec<Instant>>;

/// Where due calls come from: the active release ([`StationDemand`]), or a test's table.
pub trait DueSource: Send {
    fn due(&mut self, now: Instant) -> impl Future<Output = Result<DueCalls, HandlerError>> + Send;
}

/// 5-digit SR70 -> the 6-digit code with check digit, from the catalogue (never computed: the
/// check digit is not always Luhn). Codes the catalogue does not make unique are left out.
pub fn full_codes(catalogue: &Path) -> std::io::Result<BTreeMap<String, String>> {
    let text = std::fs::read_to_string(catalogue)?;
    let text = text.strip_prefix('\u{feff}').unwrap_or(&text);
    let mut found: BTreeMap<String, BTreeSet<String>> = BTreeMap::new();
    for line in text.lines() {
        let first = if let Some(rest) = line.strip_prefix('"') {
            rest.split('"').next().unwrap_or_default()
        } else {
            line.split(',').next().unwrap_or_default()
        };
        if first.len() == 6 && first.bytes().all(|b| b.is_ascii_digit()) {
            found
                .entry(first[..5].to_owned())
                .or_default()
                .insert(first.to_owned());
        }
    }
    Ok(found
        .into_iter()
        .filter(|(_, full)| full.len() == 1)
        .map(|(short, full)| (short, full.into_iter().next().expect("one code")))
        .collect())
}

// --- planning (pure) ---------------------------------------------------------------------------

/// When each station was last read; demand dropped over the ceiling.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct ReadLog {
    pub last: BTreeMap<String, Instant>,
    pub dropped: u64,
}

fn minutes(n: i64) -> TimeDelta {
    TimeDelta::from_micros(n * 60_000_000)
}

fn seconds(n: i64) -> TimeDelta {
    TimeDelta::from_micros(n * 1_000_000)
}

/// The stations to read in the coming minute, most urgent first, at most `budget`.
pub fn plan_reads(
    now: Instant,
    due: &DueCalls,
    log: &mut ReadLog,
    policy: &BoardPolicy,
    budget: usize,
) -> Vec<String> {
    let early = minutes(policy.early_min);
    let near = minutes(policy.near_min);
    let imminent = minutes(policy.imminent_min);
    let mut wanted: Vec<(bool, Instant, &String)> = Vec::new();
    for (station, times) in due {
        let Some(&soonest) = times.iter().find(|&&t| t >= now) else {
            continue;
        };
        if soonest - now > early {
            continue;
        }
        let last = log.last.get(station).copied();
        let unread = last.is_none_or(|l| l < soonest - early);
        let stale = last.is_some_and(|l| {
            (soonest - now <= imminent && now - l > seconds(policy.imminent_refresh_s))
                || (soonest - now <= near && now - l > seconds(policy.near_refresh_s))
        });
        if unread || stale {
            wanted.push((!unread, soonest, station));
        }
    }
    wanted.sort();
    log.dropped += wanted.len().saturating_sub(budget) as u64;
    wanted
        .into_iter()
        .take(budget)
        .map(|(_, _, station)| station.clone())
        .collect()
}

/// `statistics.median`.
fn median(values: &[f64]) -> f64 {
    let mut sorted = values.to_vec();
    sorted.sort_by(f64::total_cmp);
    let n = sorted.len();
    if n % 2 == 1 {
        sorted[n / 2]
    } else {
        (sorted[n / 2 - 1] + sorted[n / 2]) / 2.0
    }
}

/// The budget: halved for `slowdown_s` after a refusal, a server error or a slow answer.
#[derive(Debug, Clone)]
pub struct Pace {
    pub policy: BoardPolicy,
    pub latencies: Vec<f64>,
    pub slow_until: Option<Instant>,
}

impl Pace {
    pub fn new(policy: BoardPolicy) -> Pace {
        Pace {
            policy,
            latencies: Vec::new(),
            slow_until: None,
        }
    }

    pub fn budget(&self, now: Instant) -> usize {
        let ceiling = self.policy.ceiling_per_min.max(0) as usize;
        match self.slow_until {
            Some(until) if now < until => (ceiling / 2).max(1),
            _ => ceiling,
        }
    }

    pub fn note(&mut self, poll: &Poll, now: Instant) {
        let latency = (poll.received_at - poll.requested_at).total_seconds();
        let slow = self.latencies.len() >= 10
            && latency > self.policy.slow_latency_factor * median(&self.latencies);
        if self.latencies.len() > 99 {
            self.latencies.drain(..self.latencies.len() - 99);
        }
        self.latencies.push(latency);
        let refused = matches!(poll.status, Some(429 | BLOCKED)) || poll.status.unwrap_or(0) >= 500;
        if refused || slow {
            self.slow_until = Some(now + seconds(self.policy.slowdown_s));
        }
    }
}

// --- due calls (SQL) ---------------------------------------------------------------------------

/// The active publication's loads (`index_sql.active_loads`): run ID, jdf and czptt load IDs.
pub async fn active_loads(client: &Client) -> Result<(String, i32, i32), HandlerError> {
    let row = client
        .query_opt(
            "SELECT run_id, jdf_load_id, czptt_load_id FROM control.publication",
            &[],
        )
        .await?;
    let Some(row) = row else {
        return Err("no release is active".into());
    };
    match row.try_get::<_, Option<String>>(0)? {
        Some(run_id) => Ok((run_id, row.try_get(1)?, row.try_get(2)?)),
        None => Err("no release is active".into()),
    }
}

/// Connect to PostgreSQL; the connection runs on its own task.
pub async fn connect(url: &str) -> Result<Client, tokio_postgres::Error> {
    let (client, connection) = tokio_postgres::connect(url, NoTls).await?;
    tokio::spawn(async move {
        if let Err(error) = connection.await {
            crate::logs::log(&format!("database connection: {error}"));
        }
    });
    Ok(client)
}

/// Due passenger calls per station of the active rail release, refreshed periodically.
#[derive(Debug)]
pub struct StationDemand {
    client: Arc<Client>,
    load_id: i32,
    policy: BoardPolicy,
    codes: BTreeMap<String, String>,
    /// location -> 5-digit SR70.
    stations: Option<BTreeMap<String, String>>,
    cached: DueCalls,
    until: Option<Instant>,
}

impl StationDemand {
    pub fn new(
        client: Arc<Client>,
        load_id: i32,
        policy: BoardPolicy,
        codes: BTreeMap<String, String>,
    ) -> StationDemand {
        StationDemand {
            client,
            load_id,
            policy,
            codes,
            stations: None,
            cached: DueCalls::new(),
            until: None,
        }
    }

    /// A new release was activated: query its calls from now on.
    pub fn switch(&mut self, load_id: i32) {
        self.load_id = load_id;
        self.stations = None;
        self.until = None;
    }

    async fn stations(&mut self) -> Result<BTreeMap<String, String>, HandlerError> {
        if let Some(stations) = &self.stations {
            return Ok(stations.clone());
        }
        let rows = self
            .client
            .query(
                "SELECT k.public_id, k.identifier FROM static.source_key k \
                 WHERE k.load_id = $1 AND k.namespace = 'sr70' AND k.public_id IN ( \
                  SELECT location_id FROM static.trip_call WHERE load_id = $2 \
                  AND passenger_service AND boarding_point_id IS NOT NULL \
                  AND boarding_point_id NOT LIKE $3 \
                  GROUP BY location_id HAVING count(DISTINCT boarding_point_id) >= $4)",
                &[
                    &self.load_id,
                    &self.load_id,
                    &"%:BUS",
                    &self.policy.min_boarding_points,
                ],
            )
            .await?;
        let stations: BTreeMap<String, String> =
            rows.iter().map(|r| (r.get(0), r.get(1))).collect();
        self.stations = Some(stations.clone());
        Ok(stations)
    }
}

impl DueSource for StationDemand {
    async fn due(&mut self, now: Instant) -> Result<DueCalls, HandlerError> {
        if let Some(until) = self.until
            && now < until
        {
            return Ok(self.cached.clone());
        }
        let horizon = minutes(self.policy.window_min) + seconds(self.policy.due_refresh_s);
        let stations = self.stations().await?;
        let today = local_date(now);
        let dates = vec![today.add_days(-1).isoformat(), today.isoformat()];
        let locations: Vec<&String> = stations.keys().collect();
        // As Python's query; dates and instants cross as text, the due time comes back as
        // epoch microseconds.
        let rows = self
            .client
            .query(
                "SELECT tc.location_id, (extract(epoch FROM control.obehy_instant(sd.service_date, \
                    coalesce(tc.scheduled_departure, tc.scheduled_arrival))) * 1000000)::int8 AS due \
                 FROM static.trip_call tc \
                 JOIN static.trip t ON t.load_id = tc.load_id AND t.trip_id = tc.trip_id \
                 JOIN static.service_date sd ON sd.load_id = t.load_id \
                  AND sd.service_id = t.service_id AND sd.service_date = ANY($1::text[]::date[]) \
                 WHERE tc.load_id = $2 AND tc.passenger_service AND tc.location_id = ANY($3) \
                  AND control.obehy_instant(sd.service_date, \
                   coalesce(tc.scheduled_departure, tc.scheduled_arrival)) \
                   BETWEEN $4::text::timestamptz AND $5::text::timestamptz \
                 ORDER BY 1, 2",
                &[
                    &dates,
                    &self.load_id,
                    &locations,
                    &now.isoformat(),
                    &(now + horizon).isoformat(),
                ],
            )
            .await?;
        let mut cached = DueCalls::new();
        for row in rows {
            let location: String = row.get(0);
            let micros: i64 = row.get(1);
            if let Some(full) = stations.get(&location).and_then(|c| self.codes.get(c)) {
                cached
                    .entry(full.clone())
                    .or_default()
                    .push(Instant::from_micros(micros));
            }
        }
        self.cached = cached.clone();
        self.until = Some(now + seconds(self.policy.due_refresh_s));
        Ok(cached)
    }
}

// --- the loop ----------------------------------------------------------------------------------

/// Read the planned stations minute by minute until `stop`; one pass with `once`.
#[allow(clippy::too_many_arguments)]
pub async fn run_demand<F: Fetcher, H: OnPoll, D: DueSource>(
    channel: Arc<Channel>,
    stop: Stop,
    demand: &mut D,
    fetcher: Arc<F>,
    on_poll: Arc<H>,
    policy: &BoardPolicy,
    clock: Clock,
    log: Option<ReadLog>,
    once: bool,
) -> Result<ReadLog, HandlerError> {
    let mut reads = log.unwrap_or_default();
    let mut pace = Pace::new(policy.clone());
    let mut blocked_until: Option<Instant> = None;
    while !stop.is_set() {
        let started = clock();
        let mut chosen = Vec::new();
        if blocked_until.is_none_or(|until| started >= until) {
            let due = demand.due(started).await?;
            chosen = plan_reads(started, &due, &mut reads, policy, pace.budget(started));
        }
        let spacing = 60.0 / chosen.len().max(1) as f64;
        for station in chosen {
            if stop.is_set() {
                break;
            }
            let template = channel.body.clone().unwrap_or_default();
            let body = replace_bytes(&template, b"{sr70}", station.as_bytes());
            let mut poll = fetch_guarded(&fetcher, &channel, Some(body), &clock).await;
            poll.request = BTreeMap::from([("sr70".to_owned(), station.clone())]);
            let now = clock();
            reads.last.insert(station, now);
            pace.note(&poll, now);
            let blocked = poll.status == Some(BLOCKED);
            on_poll.on_poll(&channel, poll).await?;
            if blocked {
                // SŽ refuses us: stop for the full backoff (the circuit).
                blocked_until = Some(now + TimeDelta::from_secs_f64(channel.max_backoff_s));
                break;
            }
            if !once {
                stop.sleep(spacing).await;
            }
        }
        if once {
            return Ok(reads);
        }
        let rest = 60.0 - (clock() - started).total_seconds();
        if rest > 0.0 {
            stop.sleep(rest).await;
        }
    }
    Ok(reads)
}

/// `bytes.replace(old, new)`.
fn replace_bytes(text: &[u8], old: &[u8], new: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(text.len());
    let mut i = 0;
    while i < text.len() {
        if text[i..].starts_with(old) {
            out.extend_from_slice(new);
            i += old.len();
        } else {
            out.push(text[i]);
            i += 1;
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use obehy_core::time::{Date, NaiveDateTime, wall_instant};
    use std::sync::Mutex;

    fn local(day: u32, hour: u32, minute: u32) -> Instant {
        wall_instant(NaiveDateTime::new(Date::ymd(2026, 10, day), hour, minute, 0, 0).unwrap())
    }

    fn policy() -> BoardPolicy {
        let root = crate::settings::Paths::discover().expect("repository");
        root.load_policy().expect("policy").boards
    }

    fn plan() -> BoardPolicy {
        BoardPolicy {
            early_min: 45,
            near_min: 15,
            near_refresh_s: 300,
            imminent_min: 5,
            imminent_refresh_s: 120,
            ..policy()
        }
    }

    #[test]
    fn plan_reads_once_early_then_refreshes_near_and_imminent() {
        let policy = plan();
        let now = local(8, 8, 0);
        let due = DueCalls::from([
            ("100011".to_owned(), vec![local(8, 8, 30)]),
            ("100029".to_owned(), vec![local(8, 9, 30)]),
        ]);
        let mut log = ReadLog::default();
        assert_eq!(plan_reads(now, &due, &mut log, &policy, 60), ["100011"]);
        log.last.insert("100011".into(), now);
        assert!(plan_reads(local(8, 8, 10), &due, &mut log, &policy, 60).is_empty());
        assert_eq!(
            plan_reads(local(8, 8, 16), &due, &mut log, &policy, 60),
            ["100011"]
        );
        log.last.insert("100011".into(), local(8, 8, 22));
        assert_eq!(
            plan_reads(local(8, 8, 25), &due, &mut log, &policy, 60),
            ["100011"]
        );
    }

    #[test]
    fn plan_reads_puts_unread_stations_first_and_drops_over_the_budget() {
        let now = local(8, 8, 0);
        let due = DueCalls::from([
            ("1".to_owned(), vec![local(8, 8, 20)]),
            ("2".to_owned(), vec![local(8, 8, 3)]),
            ("3".to_owned(), vec![local(8, 8, 10)]),
        ]);
        let mut log = ReadLog {
            last: BTreeMap::from([("2".to_owned(), local(8, 7, 55))]),
            dropped: 0,
        };
        assert_eq!(plan_reads(now, &due, &mut log, &plan(), 2), ["3", "1"]);
        assert_eq!(log.dropped, 1);
        let mut log2 = ReadLog::default();
        assert_eq!(plan_reads(now, &due, &mut log2, &plan(), 1), ["2"]);
        assert_eq!(log2.dropped, 2);
    }

    #[test]
    fn a_refusal_halves_the_budget() {
        let boards = policy();
        let mut pace = Pace::new(boards.clone());
        let now = local(8, 8, 0);
        let ceiling = boards.ceiling_per_min as usize;
        assert_eq!(pace.budget(now), ceiling);
        let mut refused = Poll::failed(now, now, "HTTP 429");
        refused.status = Some(429);
        pace.note(&refused, now);
        assert_eq!(pace.budget(now), ceiling / 2);
        assert_eq!(pace.budget(now + seconds(boards.slowdown_s)), ceiling);
    }

    #[test]
    fn a_slow_answer_halves_the_budget_after_ten_reads() {
        let boards = policy();
        let mut pace = Pace::new(boards.clone());
        let now = local(8, 8, 0);
        let mut fast = Poll::failed(now, now + seconds(1), "x");
        fast.status = Some(200);
        fast.error = None;
        for _ in 0..10 {
            pace.note(&fast, now);
        }
        assert_eq!(pace.budget(now), boards.ceiling_per_min as usize);
        let mut slow = fast.clone();
        slow.received_at = now + seconds(10);
        pace.note(&slow, now);
        assert!(pace.budget(now) < boards.ceiling_per_min as usize);
        for _ in 0..200 {
            pace.note(&fast, now);
        }
        assert_eq!(pace.latencies.len(), 100);
    }

    struct Table(DueCalls);

    impl DueSource for Table {
        async fn due(&mut self, _: Instant) -> Result<DueCalls, HandlerError> {
            Ok(self.0.clone())
        }
    }

    struct Sent(Mutex<Vec<Vec<u8>>>, Instant);

    impl Fetcher for Sent {
        async fn fetch(&self, _: &Channel, body: Option<Vec<u8>>) -> Poll {
            self.0.lock().unwrap().push(body.unwrap_or_default());
            let mut poll = Poll::failed(self.1, self.1, "x");
            poll.status = Some(200);
            poll.error = None;
            poll.body = Some(b"<html/>".to_vec());
            poll
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
    async fn run_demand_fills_the_station_into_the_body_and_keeps_it_with_the_poll() {
        let text = "source = \"sz-tabule\"\nmanifest_version = 1\ntitle = \"t\"\ncovers = \"c\"\n\
             [[channel]]\nname = \"board\"\npoll = { kind = \"demand\", query = \"station_boards\" }\n\
             backoff = { after_failures = 3, max_s = 900 }\ntimeout_s = 5\n\
             request = { method = \"POST\", url = \"http://x.invalid\", body = \"SR70={sr70}&x=1\" }\n";
        let source =
            obehy_core::config::manifest::parse_source(text, "sz-tabule", "sz-tabule.toml", &[])
                .unwrap();
        let channel = Arc::new(source.channels[0].clone());
        let now = local(8, 8, 0);
        let fetcher = Arc::new(Sent(Mutex::new(Vec::new()), now));
        let sink = Arc::new(Collect(Mutex::new(Vec::new())));
        let mut due = Table(DueCalls::from([(
            "534149".to_owned(),
            vec![local(8, 8, 10)],
        )]));
        let reads = run_demand(
            channel,
            Stop::new(),
            &mut due,
            Arc::clone(&fetcher),
            Arc::clone(&sink),
            &policy(),
            Arc::new(move || now),
            None,
            true,
        )
        .await
        .unwrap();
        assert_eq!(*fetcher.0.lock().unwrap(), [b"SR70=534149&x=1".to_vec()]);
        let polls = sink.0.lock().unwrap();
        assert_eq!(polls[0].request["sr70"], "534149");
        assert_eq!(reads.last["534149"], now);
    }

    #[test]
    fn the_catalogue_gives_unique_full_codes() {
        let paths = crate::settings::Paths::discover().unwrap();
        let codes = full_codes(&paths.catalogue()).unwrap();
        assert_eq!(codes.get("00301").map(String::as_str), Some("003012"));
        assert!(codes.len() > 1000);
    }
}
