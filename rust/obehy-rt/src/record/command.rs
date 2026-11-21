//! The `obehy-rt record` command: flags, channel selection, egress, the demand source and
//! signals around [`super::record`] (`commands._record`, `_egress`, `_demand`).

use std::path::{Path, PathBuf};
use std::process::ExitCode;
use std::sync::Arc;

use obehy_core::config::Channel;
use obehy_core::config::manifest::{RECORDER_FILTERS, load_sources, select_channels};

use super::{DemandSetup, Filters, RecordOptions, SUMMARY_INTERVAL_S, parse_duration, record};
use crate::demand::{StationDemand, active_loads, connect, full_codes};
use crate::egress::build_egress;
use crate::fetch::system_clock;
use crate::logs::{Log, stdout};
use crate::scheduler::{HandlerError, Stop};
use crate::settings::{Env, Paths, ProcessEnv, load_database_url};

/// `obehy-rt record`'s options (`obehy rt record`'s flags).
#[derive(Debug, Clone, Default, clap::Args)]
pub struct RecordArgs {
    #[arg(long, default_value = "data/rt-raw")]
    pub archive: PathBuf,
    /// Comma-separated source IDs.
    #[arg(long, value_parser = parse_list)]
    pub sources: Option<SourceList>,
    /// A connector manifest or a directory of them (default: the shipped ones).
    #[arg(long)]
    pub manifest: Option<PathBuf>,
    /// Poll every channel once and exit.
    #[arg(long)]
    pub once: bool,
    /// For example 30m, 6h or 2d.
    #[arg(long, value_parser = parse_duration)]
    pub duration: Option<f64>,
    /// The local config (default: config/obehy.local.toml).
    #[arg(long)]
    pub config: Option<PathBuf>,
    /// Overrides OBEHY_DATABASE_URL and the config.
    #[arg(long)]
    pub database_url: Option<String>,
}

/// `--sources a,b`: the items stripped, empty ones dropped (`commands._list`).
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct SourceList(pub Vec<String>);

fn parse_list(text: &str) -> Result<SourceList, String> {
    Ok(SourceList(
        text.split(',')
            .map(|s| s.trim().to_owned())
            .filter(|s| !s.is_empty())
            .collect(),
    ))
}

/// The channels with a filter that is not available yet are left out, with a line each.
pub(crate) fn filterable(
    channels: Vec<Channel>,
    filters: &Filters,
    log: &dyn Fn(&str),
) -> Vec<Channel> {
    channels
        .into_iter()
        .filter(|c| match &c.filter {
            Some(name) if filters.get(name).is_none() => {
                log(&format!(
                    "{}: filter '{name}' is not ported to Rust yet; not recorded",
                    c.name()
                ));
                false
            }
            _ => true,
        })
        .collect()
}

/// Run `obehy-rt record`: exit status 1 with `error: ...` on a configuration error.
pub fn run_command(args: &RecordArgs) -> ExitCode {
    match command(args, &ProcessEnv) {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("error: {error}");
            ExitCode::FAILURE
        }
    }
}

fn command(args: &RecordArgs, env: &dyn Env) -> Result<(), HandlerError> {
    let log = stdout();
    let paths = Paths::discover()?;
    let manifest = args.manifest.clone().unwrap_or_else(|| paths.sources());
    let config = args.config.clone().unwrap_or_else(|| paths.config());
    let all: Vec<Channel> = load_sources(&manifest, &RECORDER_FILTERS)?
        .into_iter()
        .flat_map(|s| s.channels)
        .collect();
    let wanted: Vec<&str> = args
        .sources
        .iter()
        .flat_map(|list| list.0.iter())
        .map(String::as_str)
        .collect();
    let channels = select_channels(&all, &wanted)?;
    let policy = paths.load_policy()?;
    let egress = build_egress(&channels, &policy.egress, &config, env, Arc::clone(&log))?;
    let filters = Filters::shipped();
    let usable = filterable(egress.usable(&channels, &*log), &filters, &*log);
    if usable.is_empty() {
        return Err(
            "no channel can poll: configure the egress proxy lists and source secrets".into(),
        );
    }
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()?;
    runtime.block_on(async move {
        let (usable, demand) = demand_setup(usable, args, &config, env, &paths, &log).await;
        let stop = Stop::new();
        let signal = stop.clone();
        tokio::spawn(async move {
            wait_for_signal().await;
            signal.set();
        });
        record(
            usable,
            &args.archive,
            RecordOptions {
                once: args.once,
                duration_s: args.duration,
                summary_interval_s: SUMMARY_INTERVAL_S,
            },
            Arc::new(egress),
            demand,
            filters,
            system_clock(),
            stop,
            log,
        )
        .await
        .map(|_| ())
    })
}

/// SIGINT (Ctrl+C) or, on Unix, SIGTERM: stop after the polls in flight.
async fn wait_for_signal() {
    #[cfg(unix)]
    {
        use tokio::signal::unix::{SignalKind, signal};
        let mut term = match signal(SignalKind::terminate()) {
            Ok(term) => term,
            Err(_) => {
                let _ = tokio::signal::ctrl_c().await;
                return;
            }
        };
        tokio::select! {
            _ = tokio::signal::ctrl_c() => {}
            _ = term.recv() => {}
        }
    }
    #[cfg(not(unix))]
    {
        let _ = tokio::signal::ctrl_c().await;
    }
}

/// Demand channels need the active release's due calls; without a database they are left out
/// of a recording (`commands._demand`).
async fn demand_setup(
    channels: Vec<Channel>,
    args: &RecordArgs,
    config: &Path,
    env: &dyn Env,
    paths: &Paths,
    log: &Log,
) -> (Vec<Channel>, Option<DemandSetup<StationDemand>>) {
    if channels.iter().all(|c| c.demand.is_none()) {
        return (channels, None);
    }
    let setup = async {
        let url = match &args.database_url {
            Some(url) => url.clone(),
            None => load_database_url(config, env)?,
        };
        let client = Arc::new(connect(&url).await?);
        let (_, _, czptt) = active_loads(&client).await?;
        let boards = paths.load_policy()?.boards;
        let codes = full_codes(&paths.catalogue())?;
        Ok::<_, HandlerError>(DemandSetup {
            source: StationDemand::new(client, czptt, boards.clone(), codes),
            boards,
        })
    };
    match setup.await {
        Ok(setup) => (channels, Some(setup)),
        Err(error) => {
            log(&format!(
                "demand channels not recorded (no active release in a database: {error})"
            ));
            let kept = channels
                .into_iter()
                .filter(|c| c.demand.is_none())
                .collect();
            (kept, None)
        }
    }
}
