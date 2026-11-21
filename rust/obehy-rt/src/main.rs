use std::process::ExitCode;

use clap::Parser;
use obehy_rt::cli::{Cli, run};

#[cfg(feature = "mimalloc")]
#[global_allocator]
static GLOBAL: mimalloc::MiMalloc = mimalloc::MiMalloc;

fn main() -> ExitCode {
    run(&Cli::parse())
}
