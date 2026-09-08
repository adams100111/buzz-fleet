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

### Upgrading from 0.8.x

0.9.0 moved every file `buzz-fleet` owns onto the XDG base directories (with
secrets split into a parallel, more restrictively permissioned tree) and
renamed every agent's systemd unit instance from `buzz-agent@<agent-id>` to
`buzz-agent@<community-id>:<agent-id>`, so two communities can never again
collide on one agent id sharing a unit. Existing installs need a one-time,
one-command migration — it does not run automatically, because it stops
units and moves key material:

```bash
# Stop every agent first — the migration refuses to run while any are active.
systemctl --user stop 'buzz-agent@*'

buzz-fleet migrate --dry-run   # review the plan; changes nothing
buzz-fleet migrate             # apply it
buzz-fleet agent list          # confirm every agent is present and running
```

The migration is resumable: if it's interrupted partway, just run
`buzz-fleet migrate` again — every step checks its own postcondition, so
whatever already moved is left alone and only the rest is redone.

Before touching anything, each run that reaches this point copies your
entire pre-migration `~/.config/buzz-fleet` tree to its own
`~/.config/buzz-fleet.bak-<timestamp>` — a fresh, separately timestamped
backup every time, including a retry after a failed attempt (e.g. one that
aborted because a unit failed to start), not just once overall.
**Every one of those backups holds every secret it copied in plaintext** —
the legacy layout kept relay nsecs and agent private keys inline in the
same JSON files as everything else, unlike the split state/secrets tree the
migration moves you onto. Once you've confirmed the fleet is healthy on the
new layout, review and delete all of them: `rm -rf ~/.config/buzz-fleet.bak-*`.

Other than those backups, the migration never deletes anything from your
*original* `~/.config/buzz-fleet` tree — the one exception is each agent's
legacy system-prompt file, removed only after its copy to the new location
has been verified. `communities/*.json` and `agents/*.env` are left behind
with every relay nsec and agent private key still inline, and any agent
that used an MCP server keeps its old `mcp-*.sh` wrapper (with its own
`export TOKEN=…` lines) under the legacy work directory too. Once you've
confirmed the fleet is healthy on the new layout, delete
`~/.config/buzz-fleet/communities/` and `~/.config/buzz-fleet/agents/`
specifically — **do not delete `~/.config/buzz-fleet` itself**, since
`config.toml` and your `personas/` templates genuinely still live there
under the current layout too. (It's safe to delete `communities/` before
`agents/`, or the reverse, or to re-run `buzz-fleet migrate` in between —
already-migrated agents are never re-derived from what's left in either.)

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

### Configuration

`config.toml` lives at `$XDG_CONFIG_HOME/buzz-fleet/config.toml` (falling back
to `~/.config/buzz-fleet/config.toml` when `XDG_CONFIG_HOME` is unset).
`buzz-fleet` never rewrites this file — that's deliberate, so your comments
and formatting survive, and it's why every field is optional and defaults
apply when the file (or any section in it) is missing:

```toml
# buzz-fleet configuration. Machine-managed data lives in $XDG_STATE_HOME.

[general]
default_community = "eltahir"   # used when nothing else selects one

[ui]
refresh_interval_ms = 2000
default_view       = "agents"   # agents | runs | tasks
theme              = "buzz-fleet"
confirm_destructive = true

[defaults]
harness = "claude"              # pre-selected in the create-agent form

[notifier]                      # reserved for a future ntfy-based alert feature; not wired up yet
ntfy_url   = "https://ntfy.example.org/buzz-fleet"
ntfy_token = "env:BUZZ_FLEET_NTFY_TOKEN"   # never a literal secret

[herdr]
report_agents = false           # reserved for a future fleet-health report; not wired up yet
```

Secrets are never literals in config. A value may be `env:NAME` to indirect
through the environment; anything else is treated as plaintext and rejected
for fields marked secret (currently `notifier.ntfy_token`) — `config.toml` is
a file you're invited to edit and hand around, so a pasted token would be the
one plaintext secret outside the secrets tree.

Run `buzz-fleet config show` to print the effective configuration (defaults
merged with whatever `config.toml` sets) and where it was read from. A secret
value is never printed — you'll see `<set>` or `None` for `ntfy_token`, never
its value. An unrecognised section or key (a typo, or a key a newer
`buzz-fleet` understands that this older binary doesn't) is never rejected —
across a fleet with machines on different versions, a forward-compatible
config key must not brick an older binary mid-upgrade — but `config show`
lists any it found under "unrecognised keys (ignored)" so a typo doesn't go
unnoticed.

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

### Communities

Every command that touches a specific community accepts `--community` and
falls back to the `BUZZ_FLEET_COMMUNITY` environment variable — both
override the active community for that one invocation only, without
changing what's stored. The active community itself — used whenever neither
is given — is a small piece of persisted state, separate from `config.toml`,
and `community use` is how you change it from the command line (the TUI's
connect screen is the only other thing that writes it):

```bash
# List every community connected on this machine; `*` marks the active one
buzz-fleet community list
buzz-fleet community list --json

# Make a community active — validates the id exists first, and leaves the
# previous selection untouched if it doesn't
buzz-fleet community use eltahir

# Show the active community's relay, agent count, and fleet channel
buzz-fleet community show
```

`community show` exits with an error if no community is active yet (run
`community use` first); `community list` never does — several communities
with none selected is simply displayed with no `*` marked, not an error.

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

- **create**: mints a key, publishes the agent's visibility events (kind:0
  profile, kind:9000 channel joins, kind:10100 add-policy, kind:30177
  managed-agent record) with a NIP-OA `auth` tag proving the community
  owner authorized it, writes `~/.config/buzz-fleet/agents/<id>.{env,
  prompt.md}` (including that tag as `BUZZ_AUTH_TAG`, which `buzz-acp`
  attaches to its own relay AUTH event on every connect), and
  `systemctl --user enable --now buzz-agent@<id>`. Deliberately does *not*
  register the agent as a direct relay member (kind:9030) — see "Why agents
  aren't direct relay members" below.
- **update**: rewrites those files and `systemctl --user restart`s the unit
  — there is no live-reload, `buzz-acp` reads its config once at startup.
- **delete**: `systemctl --user disable --now`, best-effort revokes relay
  membership if the agent ever held any (a no-op for any agent created after
  this behavior changed), and deletes the agent's env/prompt files and local
  record.

#### Why agents aren't direct relay members

Earlier versions of `buzz-fleet` published a `kind:9030` event making every
agent a direct relay member — the only way, at the time, for an agent to
connect to a relay that requires membership at all. This actively breaks
`channel_add_policy=owner_only`: the relay's own membership check
(`check_relay_membership` in `buzz-relay`) returns "already a member"
*before* it ever looks at the agent's NIP-OA `auth` tag, so
`agent_owner_pubkey` — the column that policy reads — never gets backfilled,
and a third party adding the agent to a channel from another device fails
with `policy:owner_only — agent has no owner set` no matter what the agent's
env file contains. Relying purely on NIP-OA delegation (the `auth` tag,
verified fresh on every AUTH) for both relay connectivity *and* ownership
avoids this — it's also exactly how Buzz Desktop's own managed-agent
creation flow works; it never adds an agent as a direct relay member either.
This requires the relay to have NIP-OA delegation enabled
(`BUZZ_ALLOW_NIP_OA_AUTH=true`) — confirmed live against the relay this
project was built against, where a direct membership revoke on a running
agent didn't interrupt its connection at all. Pointed at a relay with that
flag off, a newly created agent won't be able to connect at all (rather than
just failing owner_only channel adds) — if agents stop connecting after an
upgrade, that flag is the first thing to check on the relay side.

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
delete, `l` view live logs, `s` switch community. `esc` cancels the
create/edit form or closes the log view without side effects. Delete is
destructive and not undoable, so `x`/`Delete` opens a confirmation dialog
first (`y`/click Delete to confirm, `n`/`esc`/click Cancel to back out)
rather than deleting on the keypress.

`s` opens a picker listing every connected community (marking the active one
with `*`), its relay URL, and its agent count. Enter switches to it and
rebuilds the dashboard around it; `esc` cancels without changing anything.
The picker writes the active community through the same
`state.save_active_community` function `buzz-fleet community use` calls, so
the CLI and the TUI can never disagree about which community is active.

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

## Orchestration

Five machines, one owner, agents that hand work to each other across all of
them over a shared Nostr channel — the *fleet channel* — instead of direct
network calls between agents. See
`docs/superpowers/specs/2026-09-06-multi-agent-orchestration-design.md` for
the full design; this section is the operational summary.

### One-time setup, then automatic everywhere else

Once per community — run this on whichever machine you consider primary (a
VPS, say):

```bash
buzz-fleet fleet init --community <id>
```

This mints a retrieval keypair, creates (or, with `--channel <uuid>`, adopts)
a NIP-29 channel named `fleet`, and writes a small JSON record into that
channel's `about` field (limits, recorded component versions, and a
conductor registry that stays empty until plans 2/3 land). The retrieval
secret is printed once and stored nowhere — archive it yourself if you'll
need it again. Never run `fleet init` a second time for the same community:
it refuses when a record already exists anywhere it can see, specifically so
five machines can't each create their own channel.

Every other machine discovers that record automatically — no separate "join"
command. `agent list`, `agent create`, `agent update`, and the TUI
dashboard's own refresh all call `AgentManager.ensure_runtime_ready()`,
which discovers the fleet record (once, then caches it on the `Community`),
joins that machine's agents to the channel, and rewrites their env files
with `BUZZ_FLEET_CHANNEL`/`BUZZ_FLEET_RETRIEVAL_KEY`. Check what a community
currently knows with:

```bash
buzz-fleet fleet status --community <id>
```

### What an agent gets

Every managed agent's systemd unit and prompt now carry:

- `buzz` itself on `PATH` (a symlink to the installed `buzz-acp`/Sprig
  multicall binary — see "Incident 8" below) and an explicit
  `WorkingDirectory` per agent (`~/.local/share/buzz-fleet/work/<agent-id>`),
  so it has somewhere to clone repos into and create worktrees.
- The fleet channel and retrieval key
  (`BUZZ_FLEET_CHANNEL`/`BUZZ_FLEET_RETRIEVAL_KEY`) once the fleet record is
  known.
- A "## Fleet coordination" block appended to `BUZZ_ACP_TEAM_INSTRUCTIONS`:
  how to ack a delegation, work in a worktree at the exact commit given,
  report back, and delegate onward. Kept in sync automatically — a stale
  version already in an agent's instructions is replaced with the current
  one, not appended again.
- `--session-policy` (`thread`, the default, or `channel`),
  `--max-turns-per-session` (default 40), and
  `--heartbeat-interval-seconds` (default 900; `0` disables) — these map to
  `buzz-acp`'s own session-scoping, rotation, and heartbeat behavior.

Override any of these per agent with the matching `agent create`/`agent
update` flag; otherwise the defaults above apply.

### Delegating work

```bash
# Hand a task to another agent, at an exact commit, with a deadline
buzz-fleet task delegate --to "Laravel Backend Developer" \
  --repo git@github.com:you/app.git --commit abc1234 \
  --brief "Add a boost:mcp endpoint" --wait 45m

# What's still open, overdue, or waiting on an ack
buzz-fleet tasks --stuck
buzz-fleet tasks --unacked

# The assignee's side
buzz-fleet task ack --task <id>
buzz-fleet task report --task <id> --status done --summary "..." \
  --input-commit abc1234 --output-commit def5678

# Anyone who can cancel it (requester or owner)
buzz-fleet task cancel <id> --reason "no longer needed"

# Full history of one task
buzz-fleet task show <id>
```

**The artifact rule**: a delegation naming a repo/commit expects the
assignee to work at that exact commit, in its own `git worktree` (never a
shared checkout), and to push before reporting. `task report`'s
`--input-commit` must equal the commit you were handed — the command refuses
a mismatch — and `--output-commit` records what you pushed, if anything.
This is enforced by convention (the coordination block every agent is
given) plus the one hard check (`--input-commit` matching), not by a
server-side gate.

**Ad-hoc limits** (per fleet record, defaults shown; not yet configurable
per community): at most 5 open ad-hoc tasks per requester, and a delegation
chain no deeper than 4. `max_rework: 3` and `max_tasks: 20` are recorded in
the same record but not yet enforced by anything in this plan.

**Unique display names**: `agent create` refuses a display name already used
by another member of the fleet channel; pass `--force` to create a duplicate
anyway.

**Per-machine prerequisite**: SSH access to every repository your pipelines
will name. `task delegate` and an assignee's own worktree checkout both need
it; `buzz-fleet` does not set it up for you.

### The agent directory

```bash
buzz-fleet fleet agents
buzz-fleet fleet agents --json
```

Lists every member of the fleet channel: display name, `role`,
`capabilities`, and `description` (set with `--role`, `--capability`
(repeatable), and `--description` on `agent create`/`agent update`),
harness, host, online status, live task count, and the `buzz-fleet` version
that published the agent's record. It's built by joining channel
membership, each agent's published managed-agent record, best-effort
presence, and open tasks from the reducer. See "Known limitations" below:
`online`/last-seen are not populated in this release.

### Per-agent secrets and one MCP server

```bash
buzz-fleet agent create --community <id> --display-name "..." --harness claude \
  --prompt-file ./persona.md \
  --env DATABASE_URL=postgres://... --env API_KEY=... \
  --env-file ./secrets.env \
  --mcp-name boost --mcp-command php --mcp-arg artisan --mcp-arg boost:mcp \
  --mcp-env SOME_TOKEN=...
```

`--env`/`--env-file` (repeatable `KEY=VALUE`, or a file of `KEY=VALUE`
lines) write arbitrary environment variables into the agent's env file,
masked at rest like every other secret `buzz-fleet` stores.
`--mcp-name`/`--mcp-command`/`--mcp-arg`/`--mcp-env` configure the one MCP
server `buzz-acp` supports per agent: `buzz-fleet` generates a small `0700`
wrapper script
(`~/.local/share/buzz-fleet/work/<agent-id>/mcp-<name>.sh`) that exports the
server's own env vars and `exec`s its command, then points
`BUZZ_ACP_MCP_COMMAND` at that wrapper. A persona file can declare the same
via an `env:` block and a single-entry `mcp_servers:` block — `buzz-acp`
supports one server per agent, so a persona declaring more than one is
refused at import.

Pi agents (`--harness pi`) additionally get a private
`PI_CODING_AGENT_DIR`
(`~/.local/share/buzz-fleet/work/<agent-id>/.pi-agent/`), seeded with
`settings.json` (`defaultProjectTrust: always`, the pinned `pi-mcp-adapter`
package) and, when an MCP server is set, `mcp.json` — copied from a shared,
pre-installed template the first time so the agent's first turn needs no
network access.

### Known limitations

- **Presence is not readable over the protocol the signer uses.** The relay
  only ever synthesizes presence (kinds 40902/20001) inside its HTTP bridge,
  never in the plain-websocket `REQ` handler `buzz-fleet-signer` speaks —
  verified against the pinned upstream commit and confirmed live. `fleet
  agents` therefore always shows `online`/`last_seen` as empty, and the
  design spec's live check 12 isn't achievable as designed with the current
  transport. Closing this needs an HTTP-bridge client in the signer — a
  follow-up, not a bug in this release.
- **An already-published managed-agent record does not refresh
  automatically** when `buzz-fleet` gains new record fields. After
  upgrading, `role`, `capabilities`, `description`, `harness`, and `version`
  stay empty in `fleet agents` for any agent created before the upgrade.
  Force a republish by giving that agent an actual (not just repeated)
  `--role`/`--capability`/`--description` value via `agent update` — the
  record only republishes when one of those fields genuinely changes, not
  on every `agent update` call — then run `agent list` (or wait for the TUI
  dashboard's next refresh) to publish it.
- **`agent update` has no flag to explicitly clear an `env` entry or
  `mcp_server` once set** — only to replace it with a new non-empty value.
  The TUI can clear both (blank the relevant fields and save); the CLI
  cannot yet.
- **Per-agent secrets provide no isolation between agents on the same
  host.** All units run as one user, so any agent's harness process can
  read every other agent's env file and MCP wrapper on that machine,
  including their private keys — spec 5.13 presents these as per-agent, but
  on one machine they are effectively fleet-wide.

### Conductor and pipelines

Multi-step pipelines, run proposals, and the always-on conductor process
(failover, notifications, metrics, session recycling, purge) are plans 2 and
3 — not built by this plan. See the spec, section 4, for what they'll add.

## Development

```bash
cd signer && cargo build && cargo test
uv sync
uv run pytest -v
uv run ruff check .
```
