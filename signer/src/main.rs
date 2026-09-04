use clap::{Parser, Subcommand};

mod events;

#[derive(Parser)]
#[command(name = "buzz-fleet-signer", about = "Nostr key/event helper for buzz-fleet")]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Placeholder — replaced in Task 3.
    Version,
}

fn main() {
    let cli = Cli::parse();
    match cli.command {
        Command::Version => println!(env!("CARGO_PKG_VERSION")),
    }
}
