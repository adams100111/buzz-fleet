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

Check what's installed with `buzz-fleet --version`.

### Updating

Re-run the exact same install command:

```bash
curl -fsSL https://raw.githubusercontent.com/adams100111/buzz-fleet/main/scripts/get.sh | bash
```

`get.sh` always fetches whatever is tagged `latest` on GitHub Releases and
overwrites the installed binaries in place — there's no separate "update"
command, no version diffing, and no confirmation prompt. Run
`buzz-fleet --version` afterward to confirm you're on the new version.

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

Persona templates live in `~/.config/buzz-fleet/personas` — auto-seeded on
first install (see `scripts/get.sh`) with this repo's bundled
[`personas/`](personas/) starter templates (six stack-specific developer
personas), and auto-created empty if that seeding is ever skipped (e.g. a
release with no network access at install time). Seeding is one-time —
updating never touches or overwrites the directory again, so anything you
add or change there is yours to keep.

Place `.persona.md` files (YAML frontmatter + body) or `.agent.json` files
(Buzz Desktop `buzz-agent-snapshot` v1 exports) there yourself to add more.
Each template's fields are optionally pre-filled into the create/update form
in the TUI. `.agent.png` files (PNG exports of agents) are detected but not
parsed (counted as unsupported). A `.persona.md` file's directory may also
contain a sibling `pack_instructions.md` — team-wide instructions shared by
every persona in that directory, pre-filled into the form's separate "Team
instructions" field (`BUZZ_ACP_TEAM_INSTRUCTIONS`) alongside the
persona-specific prompt. The System prompt and Team instructions fields are
scrollable multi-line text areas (not single-line inputs) since persona
content is routinely several paragraphs long.

The new `agent create` and `agent update` flags for harness configuration:

```bash
buzz-fleet agent create --community eltahir --display-name "Advanced Agent" \
  --harness claude --prompt-file ./persona.md \
  --team-instructions "Test-first. Strict typing." \
  --model claude-3-5-sonnet-20241022 \
  --parallelism 4 \
  --idle-timeout-seconds 300 \
  --max-turn-duration-seconds 60 \
  --respond-to-allowlist "npub1...,npub2..."
```

These optional fields map to systemd env vars on the agent's unit:
- `--team-instructions` → `BUZZ_ACP_TEAM_INSTRUCTIONS`
- `--model` → `BUZZ_ACP_MODEL`
- `--parallelism` → `BUZZ_ACP_AGENTS`
- `--idle-timeout-seconds` → `BUZZ_ACP_IDLE_TIMEOUT`
- `--max-turn-duration-seconds` → `BUZZ_ACP_MAX_TURN_DURATION`
- `--respond-to-allowlist` → `BUZZ_ACP_RESPOND_TO_ALLOWLIST` (comma-separated
  pubkeys; also sets `BUZZ_ACP_RESPOND_TO=allowlist`)

`agent create`/`agent update` also take two flags for NIP-29 channel
membership:
- `--channel-ids` → comma-separated NIP-29 channel UUIDs the agent should
  join (e.g. `--channel-ids "11111111-1111-1111-1111-111111111111,22222222-2222-2222-2222-222222222222"`)
- `--channel-add-policy` → who may add this agent to a channel: `anyone`,
  `owner_only`, or `nobody` (default `owner_only`)

#### Desktop/mobile visibility

`agent create` also publishes a handful of additional Nostr events —
a kind:0 profile, a kind:30177 managed-agent record, a kind:10100
add-policy record, and (if `--channel-ids` was given) a kind:9000
channel-join per channel — so the agent shows up owner-attributed in Buzz
Desktop's and mobile's Agents view. This is automatic: no extra command to
run, no CLI flag to remember, the same self-healing philosophy as the other
runtime concerns documented under "Runtime self-healing" below. `agent
delete` mirrors this on the way out — it also leaves any joined channels,
retracts the managed-agent record, and files an archive request (matching
Desktop's own real delete behavior), so a deleted agent stops appearing in
Desktop's pickers/autocomplete too.

`agent list` (CLI) and the TUI dashboard both show a "Visibility" status
column reflecting this: `—` (an agent created before this feature existed,
not covered by it), `pending` (still publishing), `synced` (every step
succeeded), or `error: <reason>` (a permanent failure, e.g. a malformed
channel UUID).

### Manage agents (TUI)

```bash
buzz-fleet tui
```

Shows a connect screen if no community is set up yet, otherwise a live
dashboard of agents and their systemd status. Bindings: `c` create, `u`
edit (display name and/or prompt — editing a persona-file agent without
touching the prompt field leaves its persona file alone), `x` (or `Delete`)
delete, `l` view live logs. `esc` cancels the create/edit form or closes the
log view without side effects. Delete is destructive and not undoable, so
`x`/`Delete` opens a confirmation dialog first (`y`/click Delete to confirm,
`n`/`esc`/click Cancel to back out) rather than deleting on the keypress.

When creating an agent (`c`), the form shows a template dropdown that lists
all `.persona.md` and `.agent.json` files from `~/.config/buzz-fleet/personas`
(auto-created if missing; shows "No templates found in `<dir>`" instead of an
empty dropdown when there's nothing there yet). Selecting a template pre-fills
the display name, harness, system prompt, model, parallelism, and idle/max-turn
timeouts — all fields are editable before submit, and re-selecting a different
template overwrites them again. The new fields for model, parallelism, idle
timeout, max turn duration, respond-to allowlist, channel IDs, and channel
add-policy are available as blank-by-default inputs on both the create and
edit forms.

The dashboard's agent table (and `agent list` on the CLI) both show a
"Visibility" status column: `—` for an agent created before this feature
existed, `pending` while events are still publishing, `synced` once every
step has succeeded, or `error: <reason>` for a permanent failure.

The harness dropdown auto-detects what's actually usable on this machine and
labels each option accordingly — `available`, `adapter missing` (the base CLI
is installed but not the ACP adapter `buzz-acp` needs), or `not installed`
(neither) — sorting available harnesses first and defaulting new agents to
one of them when possible. `buzz-acp` shells out to a specific adapter binary
per harness, not the bare CLI, so having e.g. `claude` on `PATH` isn't enough
on its own:

| Harness | Checks for | Install if missing |
|---|---|---|
| claude | `claude-agent-acp` (or `claude-code-acp`) | `npm install -g @agentclientprotocol/claude-agent-acp` |
| codex | `codex-acp` | `npm install -g @agentclientprotocol/codex-acp` (must be 1.x) |
| pi | `pi-acp` | `npm install -g --ignore-scripts @earendil-works/pi-coding-agent && npm install -g pi-acp` |
| goose | `goose` | install `goose` itself — no separate adapter |

When the selected harness isn't `available`, an **Install adapter** button
appears next to the dropdown — clicking it runs that harness's install
command(s) directly (blocking while `npm` runs) and hides itself once it
succeeds; on failure it stays visible and shows the error. The same action
is available without the TUI:

```bash
buzz-fleet harness list             # show all four harnesses' detected status
buzz-fleet harness install codex    # run codex's install command(s) now
```

`harness install` has no automated path for `goose` (it isn't an npm
package) — install it yourself, then re-check with `harness list`.

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

### Runtime self-healing

`buzz-fleet` doesn't just create agents — every `agent list`, dashboard
refresh, create, or update also makes sure they can actually run, with no
extra command to remember: installs `buzz-acp` itself the first time it's
needed (a static binary, no separate build/install step of your own),
resolves each harness's adapter to an absolute path so a systemd `--user`
unit can find something installed via mise/nvm/asdf even though systemd's
own `PATH` doesn't include those directories, and derives the connected
community's owner pubkey once so agents don't silently drop every event.
Nothing here needs a manual restart — the next `agent list` or dashboard
load fixes it.

## Development

```bash
cd signer && cargo build && cargo test
uv sync
uv run pytest -v
uv run ruff check .
```
