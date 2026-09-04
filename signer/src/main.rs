mod events;

use clap::{Parser, Subcommand};
use nostr::Keys;
use nostr::nips::nip19::ToBech32;
use buzz_ws_client::connection::NostrWsConnection;
use serde_json::json;

#[derive(Parser)]
#[command(name = "buzz-fleet-signer", about = "Nostr key/event helper for buzz-fleet")]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Generate a new Nostr keypair, printed as JSON.
    GenerateKey,
    /// Verify a key can authenticate against a relay.
    CheckConnection {
        #[arg(long)]
        relay: String,
        #[arg(long)]
        nsec: String,
    },
}

#[tokio::main]
async fn main() {
    // buzz-ws-client dials wss:// relays via tokio-tungstenite's rustls backend, which
    // requires the process to install a CryptoProvider before the first TLS handshake —
    // it does not select one automatically. Install `ring` (the only backend in our
    // dependency graph) up front so `check-connection` can actually connect.
    rustls::crypto::ring::default_provider()
        .install_default()
        .expect("install rustls crypto provider");

    let cli = Cli::parse();
    let code = match cli.command {
        Command::GenerateKey => {
            let keys = Keys::generate();
            println!(
                "{}",
                json!({
                    "public_key": keys.public_key().to_hex(),
                    "secret_key": keys.secret_key().to_bech32().expect("bech32 encode"),
                })
            );
            0
        }
        Command::CheckConnection { relay, nsec } => match run_check_connection(&relay, &nsec).await {
            Ok(()) => {
                println!("{}", json!({"ok": true}));
                0
            }
            Err(e) => {
                println!("{}", json!({"ok": false, "error": e.to_string()}));
                1
            }
        },
    };
    std::process::exit(code);
}

async fn run_check_connection(relay: &str, nsec: &str) -> anyhow::Result<()> {
    let keys = Keys::parse(nsec)?;
    let conn = NostrWsConnection::connect_authenticated(relay, &keys, None).await?;
    conn.disconnect().await?;
    Ok(())
}
