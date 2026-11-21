//! The `obehy-rt worker` command: the flags and config of `obehy realtime`
//! (`commands._worker`): the channels and lookups of the selected sources that egress can
//! serve, the policy, the database URL, and SIGTERM / Ctrl-C as the stop.

use std::path::PathBuf;
use std::process::ExitCode;
use std::sync::Arc;

use obehy_core::config::manifest::{RECORDER_FILTERS, load_sources, select_channels};
use obehy_core::config::{Channel, Lookup};
use obehy_core::py::text::strip;
use obehy_core::vehicles::Vocabulary;
use obehy_core::vehicles::vocabulary::VOCABULARY_FILE;
use obehy_sources::Decoders;

use super::{WorkerOptions, run_worker};
use crate::compare::parse_feeds;
use crate::egress::build_egress;
use crate::fetch::system_clock;
use crate::logs::stdout;
use crate::record::Filters;
use crate::scheduler::{HandlerError, Stop};
use crate::settings::{Env, Paths, ProcessEnv, load_database_url};

/// `obehy-rt worker`'s options (`obehy realtime`'s flags).
#[derive(Debug, Clone, clap::Args)]
pub struct WorkerArgs {
    #[arg(long, default_value = "data/rt-raw")]
    pub archive: PathBuf,
    /// Where `<feed>.pb` is written (production: /srv/gtfs-rt through the data volume).
    #[arg(long = "gtfs-rt", default_value = "data/gtfs-rt")]
    pub gtfs_rt: PathBuf,
    /// Comma-separated source IDs (default: every source).
    #[arg(long)]
    pub sources: Option<String>,
    /// Comma-separated feeds.
    #[arg(long, default_value = "jdf,czptt")]
    pub feeds: String,
    /// A connector manifest or a directory of them (default: the shipped ones).
    #[arg(long)]
    pub manifest: Option<PathBuf>,
    /// The local config (default: config/obehy.local.toml).
    #[arg(long)]
    pub config: Option<PathBuf>,
    /// Overrides OBEHY_DATABASE_URL and the config.
    #[arg(long)]
    pub database_url: Option<String>,
}

/// Run `obehy-rt worker`: exit status 1 with `error: ...` when it cannot start or fails.
pub fn run_command(args: &WorkerArgs) -> ExitCode {
    match command(args, &ProcessEnv) {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("error: {error}");
            ExitCode::FAILURE
        }
    }
}

fn command(args: &WorkerArgs, env: &dyn Env) -> Result<(), HandlerError> {
    let log = stdout();
    let paths = Paths::discover()?;
    let manifest = args.manifest.clone().unwrap_or_else(|| paths.sources());
    let config = args.config.clone().unwrap_or_else(|| paths.config());
    let sources = load_sources(&manifest, &RECORDER_FILTERS)?;
    let wanted: Vec<String> = args
        .sources
        .iter()
        .flat_map(|list| list.split(','))
        .map(|s| strip(s).to_owned())
        .filter(|s| !s.is_empty())
        .collect();
    let wanted: Vec<&str> = wanted.iter().map(String::as_str).collect();
    let all: Vec<Channel> = sources.iter().flat_map(|s| s.channels.clone()).collect();
    let channels = select_channels(&all, &wanted)?;
    let lookups: Vec<Lookup> = sources
        .iter()
        .flat_map(|s| s.lookups.clone())
        .filter(|l| wanted.is_empty() || wanted.contains(&l.source.as_str()))
        .collect();
    let policy = paths.load_policy()?;
    let requests: Vec<Channel> = channels
        .iter()
        .cloned()
        .chain(lookups.iter().map(|l| l.request.clone()))
        .collect();
    let egress = build_egress(&requests, &policy.egress, &config, env, Arc::clone(&log))?;
    let filters = Filters::shipped();
    let usable: Vec<Channel> = egress
        .usable(&channels, &*log)
        .into_iter()
        .filter(|c| match &c.filter {
            Some(name) if filters.get(name).is_none() => {
                log(&format!(
                    "{}: filter '{name}' is not available; not polled",
                    c.name()
                ));
                false
            }
            _ => true,
        })
        .collect();
    if usable.is_empty() {
        return Err(
            "no channel can poll: configure the egress proxy lists and source secrets".into(),
        );
    }
    let lookups: Vec<Lookup> = lookups
        .into_iter()
        .filter(|l| {
            !egress
                .usable(std::slice::from_ref(&l.request), &*log)
                .is_empty()
        })
        .collect();
    let package = paths.root.join("src/obehy");
    let files = |name: &str| std::fs::read_to_string(package.join(name)).map_err(|e| e.to_string());
    let decoders = Decoders::new(&sources, &files)?;
    let vocabulary = Vocabulary::load(&paths.data().join("..").join(VOCABULARY_FILE))?;
    let database_url = match &args.database_url {
        Some(url) => url.clone(),
        None => load_database_url(&config, env)?,
    };
    let options = WorkerOptions {
        database_url,
        channels: usable,
        lookups,
        sources,
        decoders: Arc::new(decoders),
        policy: Arc::new(policy),
        vocabulary,
        catalogue: paths.catalogue(),
        feeds: parse_feeds(&args.feeds)?,
        archive: args.archive.clone(),
        gtfs_rt_dir: args.gtfs_rt.clone(),
        once: false,
        clock: system_clock(),
        log,
    };
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()?;
    runtime.block_on(async move {
        let stop = Stop::new();
        let signal = stop.clone();
        tokio::spawn(async move {
            wait_for_signal().await;
            signal.set();
        });
        run_worker(options, Arc::new(egress), stop).await?;
        Ok(())
    })
}

/// SIGINT (Ctrl+C) or, on Unix, SIGTERM.
async fn wait_for_signal() {
    #[cfg(unix)]
    {
        use tokio::signal::unix::{SignalKind, signal};
        match signal(SignalKind::terminate()) {
            Ok(mut term) => {
                tokio::select! {
                    _ = tokio::signal::ctrl_c() => {}
                    _ = term.recv() => {}
                }
            }
            Err(_) => {
                let _ = tokio::signal::ctrl_c().await;
            }
        }
    }
    #[cfg(not(unix))]
    {
        let _ = tokio::signal::ctrl_c().await;
    }
}
