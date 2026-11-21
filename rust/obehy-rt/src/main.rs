use std::process::ExitCode;

use clap::Parser;
use obehy_rt::cli::{Cli, run};

fn main() -> ExitCode {
    run(&Cli::parse())
}
