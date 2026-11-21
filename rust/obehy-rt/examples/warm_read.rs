//! How fast warm start reads (R7): stored observations of a window read back by
//! `store::stored` (binary COPY on a thread of its own, facts decoded there), as warm replay
//! reads them, with nothing behind the reader. Reports rows, time to the first chunk, the
//! total and the rate. Read-only.
//!
//! ```text
//! cargo run --release -p obehy-rt --example warm_read -- [--until ISO] [--hours H]
//!     [--mode window|vehicles] [--url-env VAR]
//! ```
//!
//! The window is the policy's (`warm_window`) unless `--hours` is given; the database is
//! `[database] url` (or `OBEHY_DATABASE_URL`), or the URL in `--url-env`.

use std::process::ExitCode;
use std::time::Instant as Clock;

use obehy_core::config::policy::WarmReplayMode;
use obehy_core::time::{Instant, TimeDelta, now};
use obehy_rt::settings::{Paths, ProcessEnv, load_database_url};
use obehy_rt::store::stored::{CHUNK, spawn_reader};
use obehy_rt::warm::warm_window;

fn run() -> Result<(), Box<dyn std::error::Error>> {
    let mut until = None;
    let mut hours = None;
    let mut mode = None;
    let mut url_env = None;
    let mut items = std::env::args().skip(1);
    while let Some(flag) = items.next() {
        let value = items.next().ok_or(format!("{flag} needs a value"))?;
        match flag.as_str() {
            "--until" => until = Some(Instant::from_isoformat(&value)?),
            "--hours" => hours = Some(value.parse::<i64>()?),
            "--mode" => {
                mode = Some(match value.as_str() {
                    "window" => WarmReplayMode::Window,
                    "vehicles" => WarmReplayMode::Vehicles,
                    _ => return Err("--mode is window or vehicles".into()),
                });
            }
            "--url-env" => url_env = Some(value),
            _ => return Err(format!("unknown flag {flag}").into()),
        }
    }
    let paths = Paths::discover()?;
    let mut policy = paths.load_policy()?;
    if let Some(hours) = hours {
        policy.warm_replay.max_hours = hours;
    }
    if let Some(mode) = mode {
        policy.warm_replay.mode = mode;
    }
    let url = match url_env {
        Some(name) => std::env::var(name)?,
        None => load_database_url(&paths.config(), &ProcessEnv)?,
    };
    let until = until.unwrap_or_else(now);
    let window = warm_window(&policy, until);
    println!(
        "window {} .. {} ({} h, alive {})",
        window.since,
        window.until,
        (window.until - window.since).total_seconds() / 3600.0,
        window
            .alive
            .map_or_else(|| "-".to_owned(), |a| a.to_string())
    );
    let started = Clock::now();
    let mut first = None;
    let (mut rows, mut facts, mut chunks) = (0usize, 0usize, 0usize);
    let mut last = None;
    for chunk in spawn_reader(&url, window, CHUNK) {
        let chunk = chunk?;
        first.get_or_insert_with(|| started.elapsed().as_secs_f64());
        chunks += 1;
        rows += chunk.len();
        facts += chunk.iter().map(|o| o.facts.len()).sum::<usize>();
        last = chunk.last().map(|o| o.received_at);
    }
    let total = started.elapsed().as_secs_f64();
    let span = last.map_or(TimeDelta::from_seconds(0), |l| l - window.since);
    println!(
        "{rows} observations ({facts} facts) in {chunks} chunks: first chunk {:.2} s, all {total:.2} s, \
         {:.0}/s; last at +{:.0} s",
        first.unwrap_or(total),
        rows as f64 / total.max(1e-9),
        span.total_seconds()
    );
    Ok(())
}

fn main() -> ExitCode {
    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("warm_read: {error}");
            ExitCode::FAILURE
        }
    }
}
