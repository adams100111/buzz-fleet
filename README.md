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
./scripts/install.sh
```

That's the whole install. It's safe to re-run any time (e.g. after pulling
new code). What it does, step by step:

1. Installs Rust (via `rustup`) if `cargo` isn't already on `PATH`.
2. Installs `uv` (via its official installer) if it isn't already on `PATH`
   — `uv` is only needed to *build* the standalone binary below, not to run
   it afterward.
3. Builds `buzz-fleet-signer` (`cargo build --release` in `signer/`) and
   installs it to `/usr/local/bin` (asks for `sudo` — that directory is
   root-owned by default; the binary itself has no special runtime
   privileges).
4. Builds `buzz-fleet` itself as a standalone PyInstaller binary — no
   Python or `uv` needed to *run* it afterward, on this machine or any
   other of the same OS/architecture — and installs it to `/usr/local/bin`
   the same way.

After it finishes, `buzz-fleet` and `buzz-fleet-signer` are both real
commands on your `PATH`. If you'd rather do it by hand (or the script
doesn't fit your setup), the equivalent manual steps are:

```bash
cd signer && cargo build --release
sudo install -m 0755 target/release/buzz-fleet-signer /usr/local/bin/buzz-fleet-signer
cd ..

uv sync --group dev
uv run pyinstaller --onefile --name buzz-fleet --paths src \
  --collect-all textual --collect-all rich --collect-all pydantic \
  --collect-all typer --collect-all click \
  scripts/pyinstaller_entry.py
sudo install -m 0755 dist/buzz-fleet /usr/local/bin/buzz-fleet
```

`buzz-fleet` needs `loginctl` lingering enabled for your user so `--user`
systemd units survive after you log out (SSH etc.) — otherwise every agent
would die the moment your session ends. This is handled automatically the
first time you create an agent; you don't need to run anything for it
yourself. The only exception is a host whose polkit policy requires
privilege for a non-console session to self-enable lingering — if that
happens, `buzz-fleet` tells you the exact one-time command to run
(`sudo loginctl enable-linger <you>`) instead of failing confusingly later.

## Usage

### Connect to a community

`connect` needs the relay URL and a nsec that already holds `admin` or
`owner` role in that community (the same key you'd use to log into Buzz
Desktop) — it's what lets `buzz-fleet` publish relay-membership events on
your behalf. Omit `--admin-nsec` to be prompted for it with masked input
instead of passing it as a plaintext argument:

```bash
buzz-fleet connect --id eltahir --relay wss://buzz.eltahir.me
```

### Manage agents (CLI)

```bash
# Create — --prompt-file points at a persona .md file or a plain text prompt
buzz-fleet agent create --community eltahir --display-name "Test Echo" \
  --harness claude --prompt-file ./persona.md

# List
buzz-fleet agent list --community eltahir

# Update — display name and/or prompt file; anything else is left untouched
buzz-fleet agent update --community eltahir test-echo --display-name "New Name"

# Delete — revokes relay membership and removes the agent's local files
buzz-fleet agent delete --community eltahir test-echo
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
buzz-fleet tui
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
