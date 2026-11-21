//! The `obehy-rt` command line.

use std::process::ExitCode;

use clap::{Parser, Subcommand};

#[derive(Debug, Parser)]
#[command(name = "obehy-rt", version, about = "Oběhy realtime worker")]
pub struct Cli {
    #[command(subcommand)]
    pub command: Command,
}

// Parsed once per process: the variants' sizes do not matter.
#[allow(clippy::large_enum_variant)]
#[derive(Debug, Clone, Subcommand)]
pub enum Command {
    /// Run the live worker (R8).
    Worker(crate::worker::command::WorkerArgs),
    /// Record raw polls to the archive without processing them (R8).
    Record(crate::record::RecordArgs),
    /// Replay archived polls through the core (R6).
    Replay(crate::replay::ReplayArgs),
    /// Warm-start the core from stored observations (R7).
    Warm(crate::warm::WarmArgs),
    /// Measure decode, core and writer speed over a window (R6).
    Bench(crate::bench::BenchArgs),
    /// Write the comparison harness output for a window (R2, R6).
    Compare(crate::compare::CompareArgs),
    /// Write each corpus journey's output stream, and check it against the pinned one (R6).
    #[command(name = "corpus-stream")]
    CorpusStream(crate::corpus::CorpusArgs),
}

impl Command {
    pub fn name(&self) -> &'static str {
        match self {
            Command::Worker(_) => "worker",
            Command::Record(_) => "record",
            Command::Replay(_) => "replay",
            Command::Warm(_) => "warm",
            Command::Bench(_) => "bench",
            Command::Compare(_) => "compare",
            Command::CorpusStream(_) => "corpus-stream",
        }
    }
}

/// Runs a parsed command line.
pub fn run(cli: &Cli) -> ExitCode {
    if let Command::Worker(args) = &cli.command {
        return crate::worker::command::run_command(args);
    }
    if let Command::Record(args) = &cli.command {
        return crate::record::run_command(args);
    }
    if let Command::Warm(args) = &cli.command {
        return crate::warm::run_command(args);
    }
    if let Command::Bench(args) = &cli.command {
        return crate::bench::run_command(args);
    }
    if let Command::Replay(args) = &cli.command {
        return crate::replay::run_command(args);
    }
    if let Command::CorpusStream(args) = &cli.command {
        return crate::corpus::run_command(args);
    }
    if let Command::Compare(args) = &cli.command {
        return crate::compare::run_command(args);
    }
    eprintln!("obehy-rt {}: not implemented", cli.command.name());
    ExitCode::FAILURE
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_subcommand_parses() {
        for name in ["worker", "record"] {
            let cli = Cli::try_parse_from(["obehy-rt", name]).expect(name);
            assert_eq!(cli.command.name(), name);
        }
        let warm = [
            "obehy-rt",
            "warm",
            "--from-files",
            "r",
            "--until",
            "2026-10-06T08:00",
        ];
        assert_eq!(
            Cli::try_parse_from(warm).expect("warm").command.name(),
            "warm"
        );
        let bench = [
            "obehy-rt",
            "bench",
            "--from",
            "2026-10-06",
            "--to",
            "2026-10-06",
        ];
        let cli = Cli::try_parse_from(bench.into_iter().chain(["--out", "x"])).expect("bench");
        assert_eq!(cli.command.name(), "bench");
        let compare = [
            "obehy-rt",
            "compare",
            "--level",
            "facts",
            "--from",
            "2026-10-06",
        ];
        let cli =
            Cli::try_parse_from(
                compare
                    .into_iter()
                    .chain(["--to", "2026-10-06", "--out", "x"]),
            )
            .expect("compare");
        assert_eq!(cli.command.name(), "compare");
        assert!(Cli::try_parse_from(["obehy-rt", "nope"]).is_err());
    }
}
