//! The `obehy-rt` command line: one subcommand per Python `obehy rt` command it replaces.

use std::process::ExitCode;

use clap::{Parser, Subcommand};

#[derive(Debug, Parser)]
#[command(name = "obehy-rt", version, about = "Oběhy realtime worker")]
pub struct Cli {
    #[command(subcommand)]
    pub command: Command,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Subcommand)]
pub enum Command {
    /// Run the live worker (R8).
    Worker,
    /// Record raw polls to the archive without processing them (R8).
    Record,
    /// Replay archived polls through the core (R6).
    Replay,
    /// Warm-start the core from stored observations (R7).
    Warm,
    /// Measure decode, core and writer speed over a window (R6).
    Bench,
    /// Write the comparison harness output for a window (R2, R6).
    Compare,
}

impl Command {
    pub fn name(self) -> &'static str {
        match self {
            Command::Worker => "worker",
            Command::Record => "record",
            Command::Replay => "replay",
            Command::Warm => "warm",
            Command::Bench => "bench",
            Command::Compare => "compare",
        }
    }
}

/// Runs a parsed command line.
pub fn run(cli: &Cli) -> ExitCode {
    eprintln!("obehy-rt {}: not implemented", cli.command.name());
    ExitCode::FAILURE
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_subcommand_parses() {
        for name in ["worker", "record", "replay", "warm", "bench", "compare"] {
            let cli = Cli::try_parse_from(["obehy-rt", name]).expect(name);
            assert_eq!(cli.command.name(), name);
        }
        assert!(Cli::try_parse_from(["obehy-rt", "nope"]).is_err());
    }
}
