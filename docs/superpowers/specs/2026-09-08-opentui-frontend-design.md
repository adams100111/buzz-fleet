# An OpenTUI frontend over the Python core — design spec

Status: draft for review. Not approved, not built.
Date: 2026-09-08
Scope: buzz-fleet (this repo). No changes to the Rust signer's responsibilities.
Vocabulary: `CONTEXT.md`. Decision records: `docs/adr/`.

## 1. Goal

Replace buzz-fleet's Textual TUI with a TypeScript/OpenTUI frontend, keep the
Python core as the backend, and — in the same work — give the product the two
things it lacks today: **multi-community handling with a community toggle**, and
**a real settings/config layer**.

The Textual TUI keeps working until the new frontend reaches parity and is
verified. Nothing is deleted before then.

Owner decisions taken during design:

| Question | Decision |
|---|---|
| Frontend framework | OpenTUI (React bindings), not Ink and not Textual |
| Backend language | **Python stays.** No port of the domain layer |
| Signer | Unchanged. Still the only code that touches Nostr keys (ADR 0001) |
| Cutover | Keep the current TUI until the new one is complete and verified |
| Communities | First-class: a toggle, and correct handling throughout |
| Config | A proper settings/config layer, which does not exist today |

## 2. Verified facts this design rests on

Measured against this checkout and the installed toolchain on 2026-09-08.

1. **Size.** `src/` is 5,284 lines; `tests/` is 7,528 lines across 27 files; the
   Rust signer is 1,692 lines. `src/buzz_fleet/tui/` is **999 lines** and
   `tests/tui/` is 1,286 lines. The UI is 19% of the source.
2. **The signer already isolates Nostr.** `signer_client.py` is a thin
   subprocess wrapper with 25 functions; even relay reads go through it
   (`orchestration/relay.py` calls `signer_client.query`). No Python code
   signs, encrypts, or speaks the wire protocol directly.
3. **PyInstaller startup is ~1.0–1.2 s.** `dist/buzz-fleet --version`, warm,
   measured three times: 1.18 s, 1.01 s, 1.09 s. The binary is 25.3 MB and
   `--onefile` unpacks on every invocation. **One-shot CLI calls cannot back an
   interactive UI.**
4. **The JSON surface is nearly absent.** `--json` exists on three commands, all
   in `cli/fleet_commands.py` (`fleet agents`, `tasks`, `task show`). Everything
   under `agent`, `harness`, and `connect` prints prose only.
5. **The TUI is hardcoded to one community.** `tui/screens/dashboard.py:25`
   defines `CURRENT_COMMUNITY_ID = "eltahir"`, imported by `tui/app.py` and
   `tui/screens/connect.py`. `state.list_community_ids()` exists but is used
   only by `orchestration/identity.py:33`.
6. **Systemd unit names collide across communities.** `manager.py:454` computes
   `existing_ids` from `self.list_agents()`, which is scoped to one community,
   so agent ids are unique *per community*. `systemctl_client.py:19` builds
   `f"buzz-agent@{agent_id}"`, which is **global**. Two communities each with a
   `reviewer` claim the same unit; last write wins and the wrong Nostr key is
   loaded. Masked today only by fact 5 and by `resolve_identity` refusing to
   guess when more than one community exists.
7. **There is no config file and no XDG support.** `state.CONFIG_DIR` is
   `Path.home()/".config"/"buzz-fleet"`, hardcoded. Other paths are scattered:
   `systemd.AGENTS_DIR`, `systemd.WORK_DIR`, `systemd.TEMPLATE_UNIT_PATH`,
   `buzz_acp.BUZZ_ACP_DIR`, `harnesses.PI_AGENT_TEMPLATE_DIR`,
   `personas.DEFAULT_PERSONAS_DIR`. `$XDG_CONFIG_HOME` and friends are ignored.
8. **Secrets live inline in state files.** `state._write_secure` opens at
   `0o600`; `_serialize_with_secrets` patches real `SecretStr` values back into
   the dumped JSON. Key material and ordinary state share a file.
9. **OpenTUI is 0.5.11**, first published 2025-08-18 and last published
   2026-09-07. TypeScript over a Zig core via Bun FFI; React and Solid
   bindings; components include box, text, input, select, scrollbox, textarea,
   text-table, tab-select, slider, markdown. Testing harness:
   `createTestRenderer`, `mockInput` (`typeText`, `pressKey`, `pressArrow`,
   `pasteBracketedText`), `captureCharFrame()`.
10. **OpenTUI ships standalone binaries.** `bun build --compile` embeds the Core
    native library; Bun cross-compiles to `bun-linux-arm64`. Eight native
    targets are published. glibc and musl are **separate targets** requiring
    `process.env.OPENTUI_LIBC` as a build-time define. OpenTUI's own docs state
    Bun Core tests run on macOS arm64, Linux x64 and Windows x64 only, and warn
    that "an available artifact does not prove runtime parity on every
    published target" — **buzz-fleet ships aarch64, which is outside that set.**
11. **Release today** builds one PyInstaller binary plus the Rust signer per
    arch (x86_64, aarch64) in a CI matrix, with SHA256 sums, installed by
    `scripts/get.sh` with "no Rust, no Python, no uv, no clone".
12. **`:` needs no escaping in a systemd instance name.** `systemd.unit(5)`'s
    escaping algorithm replaces "/" with "-" and escapes every character that is
    not an ASCII alphanumeric, `:`, `_` or `.`. Confirmed live:
    `systemd-escape --template='buzz-agent@.service' 'eltahir:reviewer'` returns
    the name unchanged and `systemctl --user show` reports
    `Id=buzz-agent@eltahir:reviewer.service`, `LoadState=loaded`. `%i` and `%I`
    are therefore identical for these names.
13. **The CLI handlers write to stdout, 49 times.** `cli/app.py` and
    `cli/fleet_commands.py` contain 23 `typer.echo` calls each, plus three
    `Console().print` calls. Any handler shared with a stdio daemon would emit
    these into the protocol stream.
14. **The secret mask is reusable, and that is a bug.**
    `tui/screens/agent_form.py:276` renders existing env as `KEY=********`, and
    `:497` restores the stored secret for any value equal to `********`. An env
    value a user genuinely sets to `********` is silently replaced by the old
    secret.

## 3. Architecture

Three processes, two subprocess boundaries, each justified:

```
  OpenTUI frontend (Bun/TypeScript)
        │  newline-delimited JSON over stdio       ← new
        ▼
  buzz-fleet core (Python)                          ← unchanged responsibilities
        │  argv + JSON on stdout
        ▼
  buzz-fleet-signer (Rust)                          ← unchanged (ADR 0001)
```

The frontend never touches systemd, state files, the relay, or keys. It renders
and it sends requests. The Python core remains the only writer of state and
units, and the signer remains the only holder of key material.

This mirrors an arrangement the project already runs: `buzz-acp` speaks ACP over
stdio to a harness. The pattern is not new here.

The boundary itself is recorded as
`docs/adr/0003-frontends-talk-to-a-long-lived-core-over-json-rpc.md`.

### 3.1 Why a daemon and not CLI calls

Fact 3. At ~1 s per invocation a one-shot design costs a second per keystroke-
triggered action, and a status-polling dashboard would spend its life spawning
Python. The frontend spawns `buzz-fleet daemon --stdio` **once** and keeps it.

The daemon is also useful beyond the TUI: herdr plugin actions, agent-driven
buzz-fleet use, and any future frontend all get the same interface.

### 3.2 The protocol

**JSON-RPC 2.0**, newline-delimited, one message per line. This is what every
comparable stdio daemon uses — MCP and the Language Server Protocol both — so it
brings off-the-shelf libraries on either side and a wire format anyone who has
touched an LSP or MCP server already reads. An earlier draft invented a bespoke
envelope; familiarity with one other tool was not a good enough reason to design
a protocol.

```jsonc
// request
{"jsonrpc":"2.0","id":1,"method":"agent.list","params":{"community":"eltahir"}}
// success
{"jsonrpc":"2.0","id":1,"result":{"agents":[]}}
// failure — numeric code, JSON-RPC reserved range for protocol errors,
// application errors above -32000 with detail in `data`
{"jsonrpc":"2.0","id":1,"error":{"code":-32001,"message":"no such community","data":{"community":"x"}}}
// notification — no `id`, no response expected
{"jsonrpc":"2.0","method":"agent.status_changed","params":{"community":"eltahir","agent":"reviewer","status":"failed"}}
```

Notifications are what keep the UI live without polling: the daemon watches
systemd and pushes changes, so the frontend renders on events rather than on a
timer.

**stdout carries protocol and nothing else.** This is the standard failure mode
for a stdio server, and fact 13 says buzz-fleet would hit it immediately: 49
existing calls print to stdout in the very handlers section 3.2 proposes to
share. All diagnostics go to stderr. See 3.3 for the structural fix.

**No secret value ever crosses this boundary.** The daemon redacts on the way
out; the frontend expresses "leave this unchanged" by **omitting the field**,
never by echoing a mask back. Omission cannot be forged by a user typing a magic
string, which is precisely the failure in fact 14 — the pattern Terraform calls
a write-only attribute. Key material therefore never enters the Bun process, and
so cannot appear in a frontend crash dump or log.

Method groups mirror the existing CLI so there is one mental model:
`community.*`, `agent.*`, `harness.*`, `fleet.*`, `task.*`, `logs.*`, `config.*`.

**Every daemon method must also be reachable as a `--json` CLI command.** The
daemon and the CLI are two front doors onto the same handlers, not two
implementations. This closes fact 4 as a side effect, which is work worth doing
regardless of this project's fate.

### 3.3 Handlers must stop printing

"Two front doors onto the same handlers" is not true of the code as it stands
(fact 13). A handler layer is extracted that **returns values and never writes
to a stream**; the CLI formats those values for a terminal, the daemon
serialises them as JSON-RPC. Auditing the 49 existing call sites instead would
work exactly until the next `typer.echo`, and capturing stdout in the daemon
would silently swallow real error output.

This refactor is the load-bearing part of Phase B, not a tidy-up.

### 3.4 Healing reports what it did

`ensure_runtime_ready()` is called from `create_agent`, `update_agent`, the
dashboard's every refresh, **and `agent list`** — so listing agents is a write.
`CLAUDE.md` documents **nine** incidents behind it, found by running a live
agent end to end rather than by review: a `buzz-acp` that was never installed
(775+ `status=203/EXEC` crash loops), systemd's minimal PATH missing
version-manager install dirs, no `BUZZ_ACP_AGENT_OWNER` (every agent silently
dropping 100% of events), agents invisible to Desktop's Agents view, the
relay's `agent_owner_pubkey` never populated without `BUZZ_AUTH_TAG`, direct
`kind:9030` membership defeating that fix, agent-signed signer connections
failing once it was removed, no `buzz` on an agent's PATH, and multi-line env
values truncated at the first newline.

Concerns 1–5, 7 and 8 self-heal here; 6 and 9 are preventive fixes elsewhere
and have nothing to detect.

It keeps healing automatically. Regressing that reintroduces nine real
incidents, and a banner asking the owner to authorise routine repairs is exactly
the alert fatigue drift-detection tooling is criticised for. What is missing is
the fourth verb in *observe, compare, act, **report***: the method returns what
it changed, and the UI surfaces it. Repairs become visible events instead of
invisible ones.

A frontend phase that wants to be genuinely read-only simply does not call it,
and says so — which also means it cannot call `agent.list` naively.

### 3.5 What replaces the Textual code

Only `src/buzz_fleet/tui/` (999 lines) and `tests/tui/` (1,286 lines) are
retired, and only at cutover. The other 4,285 lines of Python and 6,242 lines of
non-TUI tests are untouched by the frontend work.

## 4. Settings and configuration

Today there is no config file, no XDG support, and secrets share files with
ordinary state (facts 7 and 8). This design separates four concerns that are
currently one directory.

### 4.1 Layout

| Kind | Location | Owner | Hand-editable |
|---|---|---|---|
| **Config** | `$XDG_CONFIG_HOME/buzz-fleet/config.toml` | the user | **yes** |
| **State** | `$XDG_STATE_HOME/buzz-fleet/` | the app | no |
| **Secrets** | `$XDG_STATE_HOME/buzz-fleet/secrets/` (`0600`, dir `0700`) | the app | no |
| **Data** | `$XDG_DATA_HOME/buzz-fleet/` | the app | no |
| **Runtime** | `$XDG_RUNTIME_DIR/buzz-fleet/` | the app | no |

Each variable falls back to its XDG default (`~/.config`, `~/.local/state`,
`~/.local/share`) when unset. Personas move to
`$XDG_CONFIG_HOME/buzz-fleet/personas/` — they are user-authored content, so
config is the right home.

Splitting secrets out of state files means a state dump is safe to read, diff,
and paste into a bug report without redaction machinery. Key material lives in
`secrets/<community>.json` and `secrets/<community>/<agent>.json`, mirroring the
state tree.

**A third secret store moves with them.** `systemd.AGENTS_DIR`
(`~/.config/buzz-fleet/agents/<id>.env`) holds `BUZZ_PRIVATE_KEY` at `0600` in
the *config* tree, with `<id>.prompt.md` beside it and
`systemd.WORK_DIR/<id>` as the unit's working directory. All three are keyed on
the unit's `%i`, and the template unit hard-codes their paths
(`EnvironmentFile={AGENTS_DIR}/%i.env`, `WorkingDirectory={WORK_DIR}/%i`).
Env files therefore move to `secrets/`, prompt files to state, and workdirs
under data — and because `%i` itself is changing (5.3), all of it is **one
migration, not two**. Doing it in two passes means restarting every agent on the
fleet twice.

### 4.2 config.toml

Read-only from the application's point of view. The app never rewrites it, so
comments and formatting survive and no TOML *writer* dependency is needed.

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

[notifier]                      # orchestration spec 5.4
ntfy_url   = "https://ntfy.example.org/buzz-fleet"
ntfy_token = "env:BUZZ_FLEET_NTFY_TOKEN"   # never a literal secret

[herdr]
report_agents = false           # see section 9
```

Secrets are never literals in config. A value may be `env:NAME` to indirect
through the environment; anything else is treated as plaintext and rejected for
fields marked secret.

### 4.3 Precedence

For every setting, first match wins:

1. explicit CLI flag / daemon request parameter
2. `BUZZ_FLEET_*` environment variable
3. machine state (only for things the UI sets, e.g. the active community)
4. `config.toml`
5. built-in default

Agent identity keeps its existing, separate rule (`resolve_identity`):
`BUZZ_PRIVATE_KEY` + `BUZZ_RELAY_URL` in the environment means "I am a fleet
agent running under my own unit" and wins over all local state. That rule is
correct and unchanged.

### 4.4 Migration

`buzz-fleet migrate` moves the existing `~/.config/buzz-fleet` tree into the new
layout, splits secrets out of state files, and renames units (section 5.3). It
is idempotent, backs up to `~/.config/buzz-fleet.bak-<timestamp>` first, and
refuses to run while any `buzz-agent@*` unit is active.

### 4.5 Writes must be atomic, and serialised

`state._write_secure` and `systemd.py:124` both `os.open(..., O_TRUNC)` and
write in place: no temp file, no `fsync`, no `os.replace`, no lock anywhere in
the codebase. One Textual process made that survivable. A daemon does not — a
TUI and a CLI (`buzz-fleet task ack`, run by an agent inside its own unit) can
write the same community file at once, and a crash mid-write leaves truncated
JSON that fails `model_validate_json` on next load, with the agent's private key
in it.

Every write becomes the full four-step atomic sequence:

1. write to a temporary file **in the target's own directory**, so the rename
   cannot cross a filesystem;
2. `flush()`, then `os.fsync()` the file — `flush` only reaches the OS, `fsync`
   reaches the disk;
3. `os.replace()` — documented atomic on POSIX, and unlike `os.rename()` on
   Windows too;
4. **`fsync()` the containing directory.** Without this the rename itself can be
   lost on power failure even though the file's contents were synced. This is
   the step most implementations omit.

Read-modify-write sequences additionally take an advisory `fcntl.flock` scoped
per community, which atomic writes alone do not prevent from losing updates.

The daemon deliberately does **not** become the sole writer: agents run
`buzz-fleet task ack` inside their own systemd units, where no daemon exists, so
the CLI must keep working standalone.

## 5. Communities

### 5.1 The toggle

Hybrid, because the two halves answer different questions — and the glossary
draws exactly this line. `CONTEXT.md` defines a **Fleet** as agents sharing one
owner *and one community*, so a cross-community list is not a fleet view at all.
It is a **Machine view**, a term added to the glossary as part of this design:

- **The agent list is a Machine view.** Every agent whose unit runs on this
  host, across all communities, with a community column. A fleet spans machines
  and stops at one community; a machine view spans communities and stops at one
  host. It answers *what is running on this box?*
- **Everything fleet-shaped is scoped to one community.** Fleet channel, runs,
  tasks and the fleet record belong to one community by construction — each has
  its own relay, owner key and `fleet_channel_id`. These follow the active
  community.

The toggle sets the active community. In the Machine view it acts as a filter;
everywhere else it selects.

### 5.2 Resolving the active community

Extends fact 5's resolution rule rather than replacing it:

1. `--community` / request parameter
2. `BUZZ_FLEET_COMMUNITY`
3. active community in state (what the toggle writes)
4. `[general] default_community` in config.toml
5. if exactly one community exists, that one
6. otherwise: error naming the available communities

Steps 1, 2, 5 and 6 are today's `resolve_identity` behaviour, preserved. Steps 3
and 4 are new and are what make a toggle possible. Note the split: the *toggle*
writes state, the *config* holds a default — so config.toml stays read-only to
the app (section 4.2).

### 5.3 Fixing the unit-name collision

Fact 6 is a live correctness bug that the toggle would trigger on first use. It
is fixed in the Python core, independently of the frontend:

- Unit instance names become `buzz-agent@<community>:<agent>`. Per fact 12 this
  needs no escaping — `systemd.unit(5)` lists `:` among the characters its
  escaping algorithm leaves alone — and `:` is excluded from the slug charset
  (`[^a-z0-9-]` in `slug.py`), so the name cannot be ambiguous the way
  `<community>-<agent>` would be when either half contains a dash.
- Names are **not** escaped. Verified live: `systemd-escape` renders a literal
  `-` as `\x2d`, because `-` is systemd's escape for `/` — so escaping
  `eltahir:my-lara-cdx` yields `eltahir:my\x2dlara\x2dcdx`, and since the
  template uses `%i`, the env file would be named with backslashes in it. A
  literal-dash instance name is meanwhile perfectly valid and loadable, and
  every real agent on this fleet has dashes. Keys are therefore used verbatim
  and *validated* against the unit-name charset (`[A-Za-z0-9:_.-]`, no leading
  `.`), so a malformed key fails loudly rather than being silently mangled.
- The template unit must keep `%i` and never `%I`: they differ for dashed names,
  because `%I` unescapes `-` back to `/`.
- **Convention, inherited by everything that follows: an instance name is
  community-first.** `buzz-fleet-conductor@<community>` in the orchestration
  design already conforms. Recording it here stops a third convention appearing
  when that work lands.
- `buzz-fleet migrate` stops each old unit, rewrites it under the new name,
  updates the env-file path, and starts it again.
- `slug.agent_slug` keeps its per-community uniqueness; global uniqueness now
  comes from the qualified unit name rather than from the slug.

This ships **before** the frontend, together with the config/state relayout
(section 4) as a single core change, so `buzz-fleet migrate` runs exactly once
and does both jobs. The existing Textual TUI benefits from both immediately.

## 6. The frontend

Bun + TypeScript + `@opentui/react`, pinned to an exact version (fact 9 — 0.5.11
is pre-1.0 and moving; pin it and upgrade deliberately).

Screens, mapping to what exists today:

| Screen | Replaces | Notes |
|---|---|---|
| Agent list | `screens/dashboard.py` | unified across communities; community column; status and visibility columns kept |
| Community switcher | *(new)* | the toggle; lists communities, shows active |
| Connect | `screens/connect.py` | now creates an *additional* community rather than overwriting one |
| Agent form | `screens/agent_form.py` | the largest screen (533 lines); ~20 fields |
| Logs | `screens/logs.py` | streamed as daemon notifications, not a polled tail |
| Confirm | `screens/confirm_delete.py` | gated by `[ui] confirm_destructive` |

`scrollbox` covers the log view, `select` and `input` the form, `text-table` the
agent list. All are marked Supported in OpenTUI's API index.

## 7. Packaging

Three artifacts per arch instead of two: the Python core binary, the signer, and
the frontend binary. `scripts/get.sh` and the release matrix gain one entry each.

Constraints from fact 10:

- Build with `process.env.OPENTUI_LIBC` defined. glibc and musl are separate
  targets; leaving it unset retains both branches and demands both native
  packages be present.
- **aarch64 must be tested on real aarch64 hardware**, not merely built. It is
  outside OpenTUI's tested matrix and buzz-fleet ships it.
- The frontend locates the core binary on `PATH`, falling back to a path from
  `BUZZ_FLEET_BIN`. It refuses to start with a clear message rather than
  spawning something unexpected.

## 8. Testing and verification

The Python core keeps its 7,528 lines of tests; nothing about this design
invalidates them. Three additions:

1. **Daemon protocol tests** (Python). Every method: happy path, error envelope,
   unknown method, malformed line, and notification delivery. This is the
   contract both front doors share, so it carries the heaviest coverage.
2. **Frontend tests** (Bun). OpenTUI's `createTestRenderer` + `mockInput`, with
   `captureCharFrame()` assertions, against a scripted fake daemon — the
   frontend never needs a real fleet to be tested.
3. **Parity checks during overlap.** For each screen, the same operation
   performed through the Textual TUI and through the new frontend must produce
   byte-identical state files and unit bodies. Both drive the same Python
   handlers, so a divergence means the frontend sent the wrong request — which
   is exactly the class of bug this catches.

Cutover criterion: every screen in section 6 at parity, migration verified on a
throwaway machine, and one week of daily use on a single non-critical host.

## 9. herdr integration (deliberately deferred)

Separate work, gated on one unverified question: whether `pane.report_agent`
accepts a custom `--source` on a pane hosting no herdr-detected agent. If it
does, each `buzz-agent@` unit can be surfaced as a first-class herdr agent row —
sidebar, `agent wait --until blocked`, notifications, mobile — via a small
plugin. The `[herdr] report_agents` config key reserves the switch.

This is unrelated to the frontend choice: herdr sees any TUI as a process
writing to a PTY and cannot tell OpenTUI from Textual. It is listed here only so
the config key is not invented twice.

## 10. Sequencing

Three changes, each independently shippable and independently useful. They want
separate implementation plans rather than one:

**Phase A — core config, state and units.** The XDG relayout (4.1), the secrets
split including env files, prompt files and workdirs (4.1), atomic writes and
per-community locking (4.5), `buzz-fleet migrate` (4.4), and the unit-name
collision fix (5.3) — one migration, one fleet-wide restart. Touches Python
only. Ships to the existing Textual TUI, which gets a correct multi-community
foundation, a real config file, and durable writes.

**Phase B — the handler split, the daemon, and the JSON surface.** Extracting
handlers that return values instead of printing (3.3) comes **first** and is the
substantial part; `buzz-fleet daemon --stdio` (3.2), notifications, the healing
report (3.4), and `--json` on every command that lacks it (fact 4) follow from
it. Python only. Independently valuable: it is what lets agents drive
buzz-fleet and what any future frontend or herdr action would use, whether or
not the OpenTUI work ever happens.

**Phase C — the OpenTUI frontend.** Sections 6 and 7, plus the community toggle
UI (5.1). The only phase that introduces TypeScript. Cutover per section 8.

A and B are worth doing on their own merits. C is the only phase that is a bet.

## 11. Risks

| Risk | Mitigation |
|---|---|
| OpenTUI 0.5.11 is pre-1.0 and moving fast | Pin exactly; upgrade deliberately; frontend is 999 lines and re-writable |
| aarch64 outside OpenTUI's tested matrix | Test on real hardware before any release ships it |
| Daemon becomes a second source of truth | It holds no state; it is a front door onto existing handlers |
| Migration corrupts a working fleet | Backup first, refuse while units are active, idempotent, dry-run mode |
| Three-language stack raises the contribution bar | Boundaries are narrow and each is justified; the signer boundary already exists |
| Scope creep from "port" into "rewrite" | The Python domain is explicitly out of scope (section 12) |
| The handler split (3.3) touches every CLI command | It is Phase B's first task and its own change, gated by the existing 7,528 lines of tests before any daemon exists |
| One migration doing config, secrets, workdirs and unit renames at once | Dry-run mode, backup first, refuse while units are active, idempotent and resumable — see 4.4 and open question 2 |

## 12. Out of scope

- Porting the Python domain layer to TypeScript.
- Any change to the Rust signer's responsibilities.
- A web frontend.
- The herdr plugin itself (section 9).
- Implementing the orchestration design (`2026-09-06-multi-agent-orchestration-design.md`)
  — that remains approved-but-unbuilt and is independent of this work.

## 13. Resolved during review

1. **Daemon lifetime — one per frontend process.** The frontend spawns
   `buzz-fleet daemon --stdio` as a child and owns it. A shared socket in
   `$XDG_RUNTIME_DIR` buys a warm cache shared with herdr plugin actions, and
   costs socket lifecycle, permissions, and stale-socket recovery — none of
   which anything needs today. Revisit only if section 9 ships.

2. **The daemon exits on stdin EOF.** Standard for a stdio server, and with a
   direct pipe the frontend's death closes it, so the ordinary case needs no
   watchdog. The frontend still terminates the child explicitly on clean
   shutdown rather than relying on EOF alone.

3. **Log streaming is bounded and drops loudly.** A `journalctl -f` on a busy
   agent can outrun the frontend's render loop. Each stream gets a bounded ring
   buffer in the daemon; on overflow the oldest lines go and a
   `{"dropped": N}` marker is delivered in their place. Silently losing lines
   from a log view is worse than saying so, and unbounded buffering turns a
   chatty agent into a memory leak.

4. **`migrate` is explicit, and resumes.** It is never run automatically on
   first launch — it stops units and moves key material, which is not something
   to do to a working fleet without being asked. It must survive being
   interrupted halfway, because interrupting it leaves some agents on old unit
   names and some on new:

   - Every step is **idempotent and checks its own postcondition**, so resuming
     is just running it again.
   - Migration is **per-agent**, not all-or-nothing: an agent whose files and
     unit are already at the new names is skipped.
   - A layout-version marker in state records which schema the tree is on.
   - It refuses to start while any `buzz-agent@*` unit is active, and takes a
     backup to `~/.config/buzz-fleet.bak-<timestamp>` before touching anything.
   - `--dry-run` prints the plan — units to rename, files to move — and changes
     nothing.

## 14. Still open

Nothing blocking Phase A. Deferred until their phase is reached:

- Whether the frontend needs a reconnect path if the daemon dies mid-session,
  or should simply exit with the error (Phase C).
- Whether `agent.list` gets a `heal: false` parameter or a separate read-only
  method, given section 3.4 (Phase B).
