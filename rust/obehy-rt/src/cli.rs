//! The `obehy-rt` command line: one subcommand per Python `obehy rt` command it replaces.

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
    Worker,
    /// Record raw polls to the archive without processing them (R8).
    Record(crate::record::RecordArgs),
    /// Replay archived polls through the core (R6).
    Replay,
    /// Warm-start the core from stored observations (R7).
    Warm,
    /// Measure decode, core and writer speed over a window (R6).
    Bench,
    /// Write the comparison harness output for a window (R2, R6).
    Compare(crate::compare::CompareArgs),
}

impl Command {
    pub fn name(&self) -> &'static str {
        match self {
            Command::Worker => "worker",
            Command::Record(_) => "record",
            Command::Replay => "replay",
            Command::Warm => "warm",
            Command::Bench => "bench",
            Command::Compare(_) => "compare",
        }
    }
}

/// Runs a parsed command line.
pub fn run(cli: &Cli) -> ExitCode {
    if let Command::Record(args) = &cli.command {
        return crate::record::run_command(args);
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
        for name in ["worker", "record", "replay", "warm", "bench"] {
            let cli = Cli::try_parse_from(["obehy-rt", name]).expect(name);
            assert_eq!(cli.command.name(), name);
        }
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
