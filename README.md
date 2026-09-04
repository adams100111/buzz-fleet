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

## Install (any Linux x86_64/aarch64 machine, no clone)

```bash
curl -fsSL https://raw.githubusercontent.com/adams100111/buzz-fleet/main/scripts/get.sh | bash
```

Detects your architecture, downloads the matching `buzz-fleet` and
`buzz-fleet-signer` binaries from the latest GitHub Release, verifies their
SHA256, and installs both onto `PATH` (`~/.local/bin`, plus `/usr/local/bin`
too if passwordless `sudo` is available) — no Rust, no Python, no `uv`, no
clone. Afterward `buzz-fleet` works from anywhere:

```bash
buzz-fleet tui
```

### Building from source instead

If you're on an architecture the releases don't cover yet, or you're
developing `buzz-fleet` itself:

```bash
git clone https://github.com/adams100111/buzz-fleet.git
cd buzz-fleet
./scripts/install.sh
```

`install.sh` is safe to re-run any time (e.g. after pulling new code). What
it does, step by step:

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

The equivalent manual steps, if you'd rather not run the script:

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

### Releasing a new version

Push a `v*` tag matching `pyproject.toml`'s `version`, `src/buzz_fleet/__init__.py`'s
`__version__`, and `signer/Cargo.toml`'s `version` (`.github/workflows/release.yml`
fails the release if any of the three disagree with the tag):

```bash
git tag v0.1.0
git push origin v0.1.0
```

CI (`checks` → `binary` × {x86_64, aarch64} → `release`) runs the full test
suite, builds both binaries for both architectures, and publishes them as
GitHub Release assets with a combined `checksums.txt` — exactly what
`get.sh` above downloads.

### Lingering

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

#### Persona templates

Persona templates live in `~/.config/buzz-fleet/personas` — this directory is
auto-created on first use. Place `.persona.md` files (YAML frontmatter + body)
or `.agent.json` files (Buzz Desktop `buzz-agent-snapshot` v1 exports) there.
Each template's fields are optionally pre-filled into the create/update form in
the TUI. `.agent.png` files (PNG exports of agents) are detected but not parsed
(counted as unsupported).

The new `agent create` and `agent update` flags for harness configuration:

```bash
buzz-fleet agent create --community eltahir --display-name "Advanced Agent" \
  --harness claude --prompt-file ./persona.md \
  --model claude-3-5-sonnet-20241022 \
  --parallelism 4 \
  --idle-timeout-seconds 300 \
  --max-turn-duration-seconds 60 \
  --respond-to-allowlist "npub1...,npub2..."
```

These optional fields map to systemd env vars on the agent's unit:
- `--model` → `BUZZ_ACP_MODEL`
- `--parallelism` → `BUZZ_ACP_AGENTS`
- `--idle-timeout-seconds` → `BUZZ_ACP_IDLE_TIMEOUT`
- `--max-turn-duration-seconds` → `BUZZ_ACP_MAX_TURN_DURATION`
- `--respond-to-allowlist` → `BUZZ_ACP_RESPOND_TO_ALLOWLIST` (comma-separated
  pubkeys; also sets `BUZZ_ACP_RESPOND_TO=allowlist`)

### Manage agents (TUI)

```bash
buzz-fleet tui
```

Shows a connect screen if no community is set up yet, otherwise a live
dashboard of agents and their systemd status. Bindings: `c` create, `u`
edit (display name and/or prompt — editing a persona-file agent without
touching the prompt field leaves its persona file alone), `x` delete, `l`
view live logs.

When creating an agent (`c`), the form shows a template dropdown that lists
all `.persona.md` and `.agent.json` files from `~/.config/buzz-fleet/personas`.
Selecting a template pre-fills the display name, harness, system prompt, model,
parallelism, and idle/max-turn timeouts — all fields are editable before
submit, and re-selecting a different template overwrites them again. The new
fields for model, parallelism, idle timeout, max turn duration, and respond-to
allowlist are available as blank-by-default inputs on both the create and edit
forms.

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
