mod agent_events;
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
    /// Compute (never publish) an owner-signed NIP-OA auth tag for an agent pubkey.
    ComputeAuthTag {
        #[arg(long)]
        owner_nsec: String,
        #[arg(long)]
        agent_pubkey: String,
    },
    /// Publish the agent's own kind:0 profile, embedding a pre-computed auth tag.
    PublishAgentProfile {
        #[arg(long)]
        relay: String,
        #[arg(long)]
        agent_nsec: String,
        #[arg(long)]
        display_name: String,
        #[arg(long)]
        auth_tag: String,
    },
    /// Publish (owner-signed) the kind:30177 managed-agent record.
    PublishManagedAgent {
        #[arg(long)]
        relay: String,
        #[arg(long)]
        owner_nsec: String,
        #[arg(long)]
        agent_pubkey: String,
        #[arg(long)]
        content_file: std::path::PathBuf,
    },
    /// Retract (NIP-09 kind:5) the kind:30177 managed-agent record.
    RetractManagedAgent {
        #[arg(long)]
        relay: String,
        #[arg(long)]
        owner_nsec: String,
        #[arg(long)]
        agent_pubkey: String,
    },
    /// Publish (agent-signed) the kind:10100 add-policy record.
    PublishAgentAddPolicy {
        #[arg(long)]
        relay: String,
        #[arg(long)]
        agent_nsec: String,
        #[arg(long)]
        policy: String,
    },
    /// File a NIP-IA archive request (used only at delete time, owner-signed).
    ArchiveAgent {
        #[arg(long)]
        relay: String,
        #[arg(long)]
        owner_nsec: String,
        #[arg(long)]
        agent_pubkey: String,
        #[arg(long)]
        reason: String,
        #[arg(long)]
        auth_tag: String,
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
        Command::ComputeAuthTag { owner_nsec, agent_pubkey } => {
            match run_compute_auth_tag(&owner_nsec, &agent_pubkey) {
                Ok(auth_tag) => { println!("{}", json!({"ok": true, "auth_tag": auth_tag})); 0 }
                Err(e) => { println!("{}", json!({"ok": false, "error": e.to_string()})); 1 }
            }
        }
        Command::PublishAgentProfile { relay, agent_nsec, display_name, auth_tag } => {
            let builder = agent_events::build_agent_profile(&display_name, &auth_tag);
            match run_publish(&relay, &agent_nsec, builder).await {
                Ok(()) => { println!("{}", json!({"ok": true})); 0 }
                Err(e) => { println!("{}", json!({"ok": false, "error": e.to_string()})); 1 }
            }
        }
        Command::PublishManagedAgent { relay, owner_nsec, agent_pubkey, content_file } => {
            let builder = std::fs::read_to_string(&content_file)
                .map_err(anyhow::Error::from)
                .and_then(|content| agent_events::build_managed_agent(&agent_pubkey, &content));
            match run_publish(&relay, &owner_nsec, builder).await {
                Ok(()) => { println!("{}", json!({"ok": true})); 0 }
                Err(e) => { println!("{}", json!({"ok": false, "error": e.to_string()})); 1 }
            }
        }
        Command::RetractManagedAgent { relay, owner_nsec, agent_pubkey } => {
            let builder = Keys::parse(&owner_nsec)
                .map_err(anyhow::Error::from)
                .and_then(|owner_keys| {
                    buzz_sdk::builders::build_delete_addressable(
                        30177,
                        &owner_keys.public_key().to_hex(),
                        &agent_pubkey,
                    )
                    .map_err(|e| anyhow::anyhow!(e))
                });
            match run_publish(&relay, &owner_nsec, builder).await {
                Ok(()) => { println!("{}", json!({"ok": true})); 0 }
                Err(e) => { println!("{}", json!({"ok": false, "error": e.to_string()})); 1 }
            }
        }
        Command::PublishAgentAddPolicy { relay, agent_nsec, policy } => {
            let builder = agent_events::build_agent_add_policy(&policy);
            match run_publish(&relay, &agent_nsec, builder).await {
                Ok(()) => { println!("{}", json!({"ok": true})); 0 }
                Err(e) => { println!("{}", json!({"ok": false, "error": e.to_string()})); 1 }
            }
        }
        Command::ArchiveAgent { relay, owner_nsec, agent_pubkey, reason, auth_tag } => {
            let builder = agent_events::build_archive_agent(&agent_pubkey, &reason, &auth_tag);
            match run_publish(&relay, &owner_nsec, builder).await {
                Ok(()) => { println!("{}", json!({"ok": true})); 0 }
                Err(e) => { println!("{}", json!({"ok": false, "error": e.to_string()})); 1 }
            }
        }
    };
    std::process::exit(code);
}

fn run_compute_auth_tag(owner_nsec: &str, agent_pubkey: &str) -> anyhow::Result<String> {
    let owner_keys = Keys::parse(owner_nsec)?;
    let agent_pk = nostr::PublicKey::from_hex(agent_pubkey)?;
    // conditions is always "" — matches Desktop's own kind:0-embedding call
    // site (`relay.rs`'s call to compute_auth_tag(&owner, &agent, "")).
    Ok(buzz_sdk::nip_oa::compute_auth_tag(&owner_keys, &agent_pk, "")?)
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
    // Only referenced from test code below — kept scoped to this module so
    // a plain (non-test) `cargo build` never warns about an unused import.
    use nostr::Kind;

    #[test]
    fn join_channel_builds_self_add_with_bot_role() {
        let keys = Keys::generate();
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
    fn leave_channel_builds_self_remove() {
        let keys = Keys::generate();
        let channel_id = uuid::Uuid::new_v4();
        let builder = buzz_sdk::builders::build_remove_member(channel_id, &keys.public_key().to_hex()).unwrap();
        let event = builder.sign_with_keys(&keys).unwrap();
        assert_eq!(event.kind, Kind::Custom(9001));
        assert!(event.tags.iter().any(|t| {
            let v: Vec<&str> = t.as_slice().iter().map(String::as_str).collect();
            v == ["h", channel_id.to_string().as_str()]
        }));
    }

    #[test]
    fn leave_channel_rejects_malformed_channel_id() {
        assert!(uuid::Uuid::parse_str("not-a-uuid").is_err());
    }

    #[test]
    fn retract_managed_agent_builds_correct_coordinate() {
        let owner = Keys::generate();
        let agent_pubkey = "d".repeat(64);
        let event = buzz_sdk::builders::build_delete_addressable(30177, &owner.public_key().to_hex(), &agent_pubkey)
            .unwrap()
            .sign_with_keys(&owner)
            .unwrap();
        assert_eq!(event.kind, Kind::Custom(5));
        let expected_coord = format!("30177:{}:{}", owner.public_key().to_hex(), agent_pubkey);
        assert!(event.tags.iter().any(|t| {
            let v: Vec<&str> = t.as_slice().iter().map(String::as_str).collect();
            v == ["a", expected_coord.as_str()]
        }));
    }

    #[test]
    fn compute_auth_tag_round_trips_through_verify() {
        let owner = Keys::generate();
        let agent = Keys::generate();
        let tag_json = run_compute_auth_tag(
            &owner.secret_key().to_bech32().unwrap(),
            &agent.public_key().to_hex(),
        )
        .unwrap();
        // verify_auth_tag(auth_tag_json: &str, agent_pubkey: &PublicKey) -> Result<PublicKey, SdkError>
        // (confirmed against crates/buzz-sdk/src/nip_oa.rs — it takes the raw JSON-array
        // string directly, not a parsed `Tag`, and returns the owner's pubkey on success).
        let verified_owner = buzz_sdk::nip_oa::verify_auth_tag(&tag_json, &agent.public_key()).unwrap();
        assert_eq!(verified_owner, owner.public_key());
    }
}
