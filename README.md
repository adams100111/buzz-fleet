# buzz-fleet

A TUI for managing headless [Buzz](https://github.com/block/buzz) agents
(Claude Code / Codex / Pi / goose) on a Linux/systemd box — connect to a
community, then create, update, delete, and run agent identities without
hand-editing env files or systemd units.

See `docs/superpowers/plans/2026-09-04-buzz-fleet-v1.md` for the implementation
plan and its linked design spec for the full "why" behind the architecture.

## Architecture

Two pieces:

- **`signer/`** (Rust) — `buzz-fleet-signer`, the only thing that touches
  Nostr keys or events: generates keypairs, checks relay connectivity, and
  publishes the real `kind:9030`/`kind:9031` relay-membership events. Every
  other part of `buzz-fleet` shells out to this binary rather than handling
  keys itself.
- **`src/buzz_fleet/`** (Python) — Pydantic models for local state, thin
  subprocess wrappers around the signer binary and `systemctl --user`, an
  `AgentManager` orchestrating create/update/delete/list, a Typer CLI, and
  the Textual TUI.

Everything runs as your normal, unprivileged user via `systemctl --user` —
no root, anywhere, except installing the `buzz-fleet-signer` binary itself
to `/usr/local/bin` once (a static binary with no special privileges at
runtime).

## Install

```bash
# One-time: let --user systemd units survive after you log out (SSH etc.)
loginctl enable-linger "$(whoami)"

# Build and install the signer binary
cd signer && cargo build --release
sudo install -m 0755 target/release/buzz-fleet-signer /usr/local/bin/buzz-fleet-signer
cd ..

# Install the Python package
uv sync
```

## Usage

### Connect to a community

`connect` needs the relay URL and a nsec that already holds `admin` or
`owner` role in that community (the same key you'd use to log into Buzz
Desktop) — it's what lets `buzz-fleet` publish relay-membership events on
your behalf. Omit `--admin-nsec` to be prompted for it with masked input
instead of passing it as a plaintext argument:

```bash
uv run buzz-fleet connect --id eltahir --relay wss://buzz.eltahir.me
```

### Manage agents (CLI)

```bash
# Create — --prompt-file points at a persona .md file or a plain text prompt
uv run buzz-fleet agent create --community eltahir --display-name "Test Echo" \
  --harness claude --prompt-file ./persona.md

# List
uv run buzz-fleet agent list --community eltahir

# Update — display name and/or prompt file; anything else is left untouched
uv run buzz-fleet agent update --community eltahir test-echo --display-name "New Name"

# Delete — revokes relay membership and removes the agent's local files
uv run buzz-fleet agent delete --community eltahir test-echo
```

Each command's actual effect on the systemd unit:

- **create**: mints a key, registers it as a relay member, writes
  `~/.config/buzz-fleet/agents/<id>.{env,prompt.md}`, and
  `systemctl --user enable --now buzz-agent@<id>`.
- **update**: rewrites those files and `systemctl --user restart`s the unit
  — there is no live-reload, `buzz-acp` reads its config once at startup.
- **delete**: `systemctl --user disable --now`, revokes relay membership,
  and deletes the agent's env/prompt files and local record.

### Manage agents (TUI)

```bash
uv run buzz-fleet tui
```

Shows a connect screen if no community is set up yet, otherwise a live
dashboard of agents and their systemd status. Bindings: `c` create, `u`
edit (display name and/or prompt — editing a persona-file agent without
touching the prompt field leaves its persona file alone), `x` delete, `l`
view live logs.

### Inspecting a running agent directly

```bash
systemctl --user status buzz-agent@<id>
journalctl --user -u buzz-agent@<id> -f
```

Agents need a real API key for their harness (e.g. `ANTHROPIC_API_KEY` for
`claude`) to actually connect and idle waiting for mentions — pass it at
create time via `AgentManager.create_agent(..., anthropic_api_key=...)`
(not yet exposed as a CLI flag; edit the agent's `.env` file directly under
`~/.config/buzz-fleet/agents/<id>.env` and `systemctl --user restart` it in
the meantime).

## Development

```bash
cd signer && cargo build && cargo test
uv sync
uv run pytest -v
uv run ruff check .
```
