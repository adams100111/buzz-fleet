mod events;

use clap::{Parser, Subcommand};
use nostr::{Keys, Kind};
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
    /// Derive the hex public key for an existing nsec, printed as JSON.
    PubkeyFromNsec {
        #[arg(long)]
        nsec: String,
    },
    /// Verify a key can authenticate against a relay.
    CheckConnection {
        #[arg(long)]
        relay: String,
        #[arg(long)]
        nsec: String,
    },
    /// Publish a kind:9030 relay-membership add event.
    AddMember {
        #[arg(long)]
        relay: String,
        #[arg(long)]
        admin_nsec: String,
        #[arg(long)]
        pubkey: String,
        #[arg(long)]
        role: Option<String>,
    },
    /// Publish a kind:9031 relay-membership remove event.
    RemoveMember {
        #[arg(long)]
        relay: String,
        #[arg(long)]
        admin_nsec: String,
        #[arg(long)]
        pubkey: String,
    },
    /// Self-join one NIP-29 channel as role=bot.
    JoinChannel {
        #[arg(long)]
        relay: String,
        #[arg(long)]
        agent_nsec: String,
        #[arg(long)]
        channel_id: String,
    },
    /// Self-leave one NIP-29 channel.
    LeaveChannel {
        #[arg(long)]
        relay: String,
        #[arg(long)]
        agent_nsec: String,
        #[arg(long)]
        channel_id: String,
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
        Command::PubkeyFromNsec { nsec } => match Keys::parse(&nsec) {
            Ok(keys) => {
                println!("{}", json!({"ok": true, "public_key": keys.public_key().to_hex()}));
                0
            }
            Err(e) => {
                println!("{}", json!({"ok": false, "error": e.to_string()}));
                1
            }
        },
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
        Command::AddMember { relay, admin_nsec, pubkey, role } => {
            match run_publish(&relay, &admin_nsec, events::build_add_member(&pubkey, role.as_deref())).await {
                Ok(()) => { println!("{}", json!({"ok": true})); 0 }
                Err(e) => { println!("{}", json!({"ok": false, "error": e.to_string()})); 1 }
            }
        }
        Command::RemoveMember { relay, admin_nsec, pubkey } => {
            match run_publish(&relay, &admin_nsec, events::build_remove_member(&pubkey)).await {
                Ok(()) => { println!("{}", json!({"ok": true})); 0 }
                Err(e) => { println!("{}", json!({"ok": false, "error": e.to_string()})); 1 }
            }
        }
        Command::JoinChannel { relay, agent_nsec, channel_id } => {
            let builder = uuid::Uuid::parse_str(&channel_id)
                .map_err(anyhow::Error::from)
                .and_then(|id| {
                    let agent_pubkey = Keys::parse(&agent_nsec)?.public_key().to_hex();
                    buzz_sdk::builders::build_add_member(id, &agent_pubkey, Some(buzz_sdk::MemberRole::Bot))
                        .map_err(|e| anyhow::anyhow!(e))
                });
            match run_publish(&relay, &agent_nsec, builder).await {
                Ok(()) => { println!("{}", json!({"ok": true})); 0 }
                Err(e) => { println!("{}", json!({"ok": false, "error": e.to_string()})); 1 }
            }
        }
        Command::LeaveChannel { relay, agent_nsec, channel_id } => {
            let builder = uuid::Uuid::parse_str(&channel_id)
                .map_err(anyhow::Error::from)
                .and_then(|id| {
                    let agent_pubkey = Keys::parse(&agent_nsec)?.public_key().to_hex();
                    buzz_sdk::builders::build_remove_member(id, &agent_pubkey)
                        .map_err(|e| anyhow::anyhow!(e))
                });
            match run_publish(&relay, &agent_nsec, builder).await {
                Ok(()) => { println!("{}", json!({"ok": true})); 0 }
                Err(e) => { println!("{}", json!({"ok": false, "error": e.to_string()})); 1 }
            }
        }
    };
    std::process::exit(code);
}

async fn run_check_connection(relay: &str, nsec: &str) -> anyhow::Result<()> {
    let keys = Keys::parse(nsec)?;
    let conn = NostrWsConnection::connect_authenticated(relay, &keys, None).await?;
    conn.disconnect().await?;
    Ok(())
}

async fn run_publish(
    relay: &str,
    admin_nsec: &str,
    builder: anyhow::Result<nostr::EventBuilder>,
) -> anyhow::Result<()> {
    let keys = Keys::parse(admin_nsec)?;
    let event = builder?.sign_with_keys(&keys)?;
    let mut conn = NostrWsConnection::connect_authenticated(relay, &keys, None).await?;
    let response = conn.send_event(event).await?;
    conn.disconnect().await?;
    if !response.accepted {
        anyhow::bail!("relay rejected event: {}", response.message);
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn join_channel_builds_self_add_with_bot_role() {
        let keys = Keys::generate();
        let nsec = keys.secret_key().to_bech32().unwrap();
        let channel_id = uuid::Uuid::new_v4();
        let builder = buzz_sdk::builders::build_add_member(
            channel_id,
            &keys.public_key().to_hex(),
            Some(buzz_sdk::MemberRole::Bot),
        )
        .unwrap();
        let event = builder.sign_with_keys(&keys).unwrap();
        assert_eq!(event.kind, Kind::Custom(9000));
        assert!(event.tags.iter().any(|t| {
            let v: Vec<&str> = t.as_slice().iter().map(String::as_str).collect();
            v == ["role", "bot"]
        }));
        assert!(event.tags.iter().any(|t| {
            let v: Vec<&str> = t.as_slice().iter().map(String::as_str).collect();
            v == ["h", channel_id.to_string().as_str()]
        }));
    }

    #[test]
    fn leave_channel_rejects_malformed_channel_id() {
        assert!(uuid::Uuid::parse_str("not-a-uuid").is_err());
    }
}
