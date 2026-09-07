# Orchestration Foundation Implementation Plan (plan 1 of 3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make fleet agents able to reply, delegate work to each other with an exact code revision, ack and report reliably, and let any machine reconstruct the complete state of every delegation from the relay.

**Architecture:** Spec layers 0 and 1. Agents publish kind 9 channel messages carrying fleet tags through `buzz-fleet task` commands; every fleet event mentions a retrieval key so one indexed relay filter plus `until` paging reads the complete history; a pure reducer turns events into task state for the views. The Rust signer gains I/O-only subcommands. A fleet channel and an owner-signed fleet record are created once and discovered by every machine. Plan 2 is the conductor (outbox, timers, failover, notifier, metrics, recycling); plan 3 is pipelines (runs, groups, human steps, proposals and the `Fleet PM` planner, workspace files, purge, TUI screen, self-update). The agent directory (spec 5.10) is in this plan: Tasks 17 and 18; environment secrets and the single MCP server (spec 5.13) are Task 19.

**Tech Stack:** Python 3.12+ (Typer, Pydantic, Rich, pytest), Rust (clap, nostr 0.44, buzz-sdk, buzz-ws-client, tokio), systemd `--user` units.

**Spec:** `docs/superpowers/specs/2026-09-06-multi-agent-orchestration-design.md` (revision 3). Vocabulary: `CONTEXT.md`. Decisions: `docs/adr/0001`, `docs/adr/0002`.

## Global Constraints

- No Claude/Anthropic attribution in commits (`~/.claude/CLAUDE.md`).
- No root anywhere: `~/.config/buzz-fleet`, `~/.local/share/buzz-fleet`, `--user` units.
- Python never signs or opens WebSockets; all relay I/O goes through `buzz-fleet-signer` via `CommandRunner`.
- Tags: `["t","fleet"]`, `["t","fleet:task:<id>"]`, `["t","fleet:run:<id>"]` are labels only; completeness comes from `["p", <retrieval key>]` on every fleet event. Payload tag `["fleet", <json>]`, `v: 1`, at most 8 KiB. Channel message kind is 9.
- Ids: `task`, `attempt`, `run` are UUIDv4 strings (`uuid.uuid4()`); views show the first 8 characters; lookups accept a unique prefix.
- Env defaults per agent: `BUZZ_ACP_SESSION_POLICY=thread`, `BUZZ_ACP_MAX_TURNS_PER_SESSION=40`, `BUZZ_ACP_HEARTBEAT_INTERVAL=900`, `WorkingDirectory=~/.local/share/buzz-fleet/work/<agent>`.
- Fleet channel name `fleet`. Fleet record lives in the channel `about`: first line `buzz-fleet orchestration channel — do not edit`, second line the JSON.
- Ad-hoc limits: 5 open tasks per requester, parent chain depth 4.
- Coordination block markers `<!-- buzz-fleet:coordination v1 -->` / `<!-- /buzz-fleet:coordination -->`.
- Before every commit: `uv run pytest -q`, `uv run ruff check .`, `uv run mypy`, and `cargo test` in `signer/` for Rust tasks.
- Tests never touch the network or the real home: monkeypatch `buzz_fleet.state.CONFIG_DIR`, `buzz_fleet.systemd.AGENTS_DIR`, `buzz_fleet.systemd.TEMPLATE_UNIT_PATH`, `buzz_fleet.buzz_acp.BUZZ_ACP_DIR`/`BUZZ_ACP_PATH`/`BUZZ_CLI_PATH`, and use a `FakeRunner`.

## Before starting

```bash
cd /home/dev/repos/buzz-fleet && uv sync --group dev && uv run pytest -q && (cd signer && cargo test)
```

Expected: 196 tests pass; the signer's tests pass (first run compiles the buzz git dependencies, several minutes).

## File map

| File | Responsibility |
|---|---|
| `src/buzz_fleet/buzz_acp.py` | (modify) link `buzz` next to `buzz-acp` |
| `src/buzz_fleet/systemd.py` | (modify) PATH and WorkingDirectory in the template, quoted env values, new env vars, work dir creation |
| `src/buzz_fleet/models.py` | (modify) `Agent.session_policy`, `max_turns_per_session`, `heartbeat_interval_seconds`; `Community.fleet_channel_id`, `retrieval_key`, `fleet_record` |
| `src/buzz_fleet/state.py` | (modify) `list_community_ids()` |
| `src/buzz_fleet/visibility.py` | (modify) `host` in the managed-agent record |
| `src/buzz_fleet/manager.py` | (modify) template-change restart, unique-name check, fleet record discovery, auto-join, coordination block refresh |
| `src/buzz_fleet/signer_client.py` | (modify) wrappers for the new signer subcommands |
| `src/buzz_fleet/orchestration/__init__.py` | (create) package |
| `src/buzz_fleet/orchestration/instructions.py` | (create) coordination block |
| `src/buzz_fleet/orchestration/ids.py` | (create) id minting and prefix matching |
| `src/buzz_fleet/orchestration/durations.py` | (create) `parse_duration` |
| `src/buzz_fleet/orchestration/record.py` | (create) fleet record model, encode/decode from channel `about` |
| `src/buzz_fleet/orchestration/protocol.py` | (create) build outgoing messages, parse incoming events |
| `src/buzz_fleet/orchestration/reducer.py` | (create) pure `reduce(events, record) -> State` |
| `src/buzz_fleet/orchestration/identity.py` | (create) agent vs owner identity |
| `src/buzz_fleet/orchestration/relay.py` | (create) paged reads, deletions, member resolution, post |
| `src/buzz_fleet/orchestration/git_artifact.py` | (create) detect repo/commit from a checkout, refuse dirty or unpushed |
| `src/buzz_fleet/cli/fleet_commands.py` | (create) `fleet` and `task` groups, `tasks` command |
| `src/buzz_fleet/cli/app.py` | (modify) register groups; new agent flags |
| `src/buzz_fleet/tui/screens/agent_form.py` | (modify) three new inputs |
| `signer/src/fleet.rs` | (create) message builder, filter parsing, collect/paged query, members, channel metadata |
| `signer/src/main.rs` | (modify) subcommands `post-message`, `query`, `channel-members`, `create-channel`, `read-channel-meta`, `write-channel-about` |
| `README.md`, `CLAUDE.md` | (modify) docs |

---

### Task 1: `buzz` on every agent's PATH, restart on template change

**Files:**
- Modify: `src/buzz_fleet/buzz_acp.py`, `src/buzz_fleet/systemd.py`, `src/buzz_fleet/manager.py`
- Test: `tests/test_buzz_acp.py`, `tests/test_systemd.py`, `tests/test_manager.py`

**Interfaces:**
- Produces: `buzz_acp.BUZZ_CLI_PATH: Path`, `buzz_acp.ensure_buzz_cli_link() -> bool`, `systemd.ensure_template_unit_installed(runner) -> bool`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_buzz_acp.py`:

```python
def test_ensure_buzz_cli_link_creates_and_is_idempotent(tmp_path, monkeypatch) -> None:
    acp_dir = tmp_path / "bin"
    acp_dir.mkdir()
    acp = acp_dir / "buzz-acp"
    acp.write_bytes(b"stub")
    acp.chmod(0o755)
    monkeypatch.setattr(buzz_acp, "BUZZ_ACP_DIR", acp_dir)
    monkeypatch.setattr(buzz_acp, "BUZZ_ACP_PATH", acp)
    monkeypatch.setattr(buzz_acp, "BUZZ_CLI_PATH", acp_dir / "buzz")

    assert buzz_acp.ensure_buzz_cli_link() is True
    assert (acp_dir / "buzz").is_symlink()
    assert (acp_dir / "buzz").resolve() == acp.resolve()
    assert buzz_acp.ensure_buzz_cli_link() is False


def test_ensure_buzz_cli_link_replaces_dangling_link(tmp_path, monkeypatch) -> None:
    acp_dir = tmp_path / "bin"
    acp_dir.mkdir()
    acp = acp_dir / "buzz-acp"
    acp.write_bytes(b"stub")
    link = acp_dir / "buzz"
    link.symlink_to(acp_dir / "missing")
    monkeypatch.setattr(buzz_acp, "BUZZ_ACP_DIR", acp_dir)
    monkeypatch.setattr(buzz_acp, "BUZZ_ACP_PATH", acp)
    monkeypatch.setattr(buzz_acp, "BUZZ_CLI_PATH", link)

    assert buzz_acp.ensure_buzz_cli_link() is True
    assert link.resolve() == acp.resolve()
```

Append to `tests/test_systemd.py`:

```python
def test_template_unit_sets_path_and_workdir() -> None:
    from buzz_fleet.buzz_acp import BUZZ_ACP_DIR
    from buzz_fleet.systemd import WORK_DIR

    assert f"Environment=PATH={BUZZ_ACP_DIR}:/usr/local/bin:/usr/bin:/bin" in TEMPLATE_UNIT
    assert f"WorkingDirectory={WORK_DIR}/%i" in TEMPLATE_UNIT


def test_ensure_template_unit_installed_returns_changed_flag(tmp_path: Path, monkeypatch) -> None:
    unit_path = tmp_path / "buzz-agent@.service"
    monkeypatch.setattr("buzz_fleet.systemd.TEMPLATE_UNIT_PATH", unit_path)
    calls: list[list[str]] = []

    class Runner:
        def run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    assert ensure_template_unit_installed(Runner()) is True
    assert calls == [["systemctl", "--user", "daemon-reload"]]
    assert ensure_template_unit_installed(Runner()) is False
```

Append to `tests/test_manager.py` (uses its existing `FakeRunner`, `_community`, autouse fixture):

```python
def test_template_change_restarts_every_agent(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.WORK_DIR", tmp_path / "work")
    monkeypatch.setattr("buzz_fleet.systemd.TEMPLATE_UNIT_PATH", tmp_path / "unit" / "buzz-agent@.service")
    monkeypatch.setattr("buzz_fleet.systemd.ensure_linger_enabled", lambda runner: None)
    runner = FakeRunner()
    manager = AgentManager(runner, _community())
    agent = manager.create_agent(
        display_name="Restart Me", harness="claude",
        system_prompt_source=SystemPromptSource(kind="inline", text="hi"),
    )
    (tmp_path / "unit" / "buzz-agent@.service").write_text("[Unit]\nDescription=old\n")
    runner.calls.clear()

    manager.ensure_runtime_ready()

    assert any(a[:3] == ["systemctl", "--user", "restart"] and agent.id in a[3] for a in runner.calls)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_buzz_acp.py tests/test_systemd.py tests/test_manager.py -q`
Expected: FAIL on missing `BUZZ_CLI_PATH`, `WORK_DIR`, the PATH assertion, and `None` returned.

- [ ] **Step 3: Implement**

`src/buzz_fleet/buzz_acp.py`, after `BUZZ_ACP_PATH`:

```python
# Sprig is also the `buzz` CLI when invoked by that name. buzz-acp never posts
# the agent's reply itself; it tells the agent to run `buzz messages send`,
# so without this link a fleet agent wakes on a mention and cannot answer.
# Real incident (2026-09-06): `which buzz` was empty on every fleet machine.
BUZZ_CLI_PATH = BUZZ_ACP_DIR / "buzz"


def ensure_buzz_cli_link() -> bool:
    """Make BUZZ_CLI_PATH a symlink to BUZZ_ACP_PATH. Returns True when it changed anything."""
    if (
        BUZZ_CLI_PATH.is_symlink()
        and BUZZ_CLI_PATH.exists()
        and BUZZ_CLI_PATH.resolve() == BUZZ_ACP_PATH.resolve()
    ):
        return False
    BUZZ_ACP_DIR.mkdir(parents=True, exist_ok=True)
    if BUZZ_CLI_PATH.is_symlink() or BUZZ_CLI_PATH.exists():
        BUZZ_CLI_PATH.unlink()
    BUZZ_CLI_PATH.symlink_to(BUZZ_ACP_PATH)
    return True
```

`src/buzz_fleet/systemd.py`: import `BUZZ_ACP_DIR, BUZZ_ACP_PATH`, add `WORK_DIR = Path.home() / ".local" / "share" / "buzz-fleet" / "work"`, and set the template to:

```python
TEMPLATE_UNIT = f"""[Unit]
Description=Buzz headless agent (%i)
After=network-online.target

[Service]
EnvironmentFile={AGENTS_DIR}/%i.env
Environment=PATH={BUZZ_ACP_DIR}:/usr/local/bin:/usr/bin:/bin
WorkingDirectory={WORK_DIR}/%i
ExecStart={BUZZ_ACP_PATH}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""
```

Change `ensure_template_unit_installed` to return `False` when unchanged and `True` after writing and reloading (docstring: a `--user` unit picks up `Environment=`/`WorkingDirectory=` only on restart, so the caller restarts units when this returns True). In `write_agent_files`, before writing the env file, add `(WORK_DIR / agent.id).mkdir(parents=True, exist_ok=True)` so the unit's `WorkingDirectory` exists.

`src/buzz_fleet/manager.py` `ensure_runtime_ready`:

```python
        systemd.ensure_linger_enabled(self._runner)
        template_changed = systemd.ensure_template_unit_installed(self._runner)
        buzz_acp_just_installed = buzz_acp.ensure_buzz_acp_installed()
        buzz_acp.ensure_buzz_cli_link()
        needs_full_refresh = buzz_acp_just_installed or owner_pubkey_just_backfilled or template_changed
```

Add incident 8 to the docstring: the `buzz` CLI was never on the unit PATH, so agents woke and could not reply.

- [ ] **Step 4: Run all tests**

Run: `uv run pytest -q`
Expected: PASS. If an existing manager test asserts the exact call sequence of `create_agent`, add the one `daemon-reload` that a fresh tmp dir now triggers.

- [ ] **Step 5: Commit**

```bash
git add src/buzz_fleet/buzz_acp.py src/buzz_fleet/systemd.py src/buzz_fleet/manager.py tests
git commit -m "Put the buzz CLI on every agent unit's PATH and give each agent a working directory"
```

---

### Task 2: Quote multi-line env values

**Files:**
- Modify: `src/buzz_fleet/systemd.py`
- Test: `tests/test_systemd.py`

**Interfaces:**
- Produces: `systemd.env_line(key: str, value: str) -> str`.

- [ ] **Step 1: Write the failing tests**

```python
def test_env_line_single_line_unquoted() -> None:
    from buzz_fleet.systemd import env_line

    assert env_line("BUZZ_RELAY_URL", "wss://buzz.example") == "BUZZ_RELAY_URL=wss://buzz.example"


def test_env_line_quotes_and_escapes_multiline() -> None:
    from buzz_fleet.systemd import env_line

    value = 'line one\nsays "hi" \\ back\nline three'
    assert env_line("K", value) == 'K="line one\nsays \\"hi\\" \\\\ back\nline three"'


def test_write_agent_files_quotes_multiline_team_instructions(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.WORK_DIR", tmp_path / "work")
    monkeypatch.setattr("buzz_fleet.systemd.resolve_adapter_command", lambda harness: "/usr/bin/claude-agent-acp")
    agent = _agent().model_copy(update={"team_instructions": "# Rules\n\n- one\n- two"})

    write_agent_files(agent, _community(), None, None)

    assert 'BUZZ_ACP_TEAM_INSTRUCTIONS="# Rules\n\n- one\n- two' in agent_env_path(agent.id).read_text()
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_systemd.py -q` → ImportError on `env_line`.

- [ ] **Step 3: Implement**

```python
def env_line(key: str, value: str) -> str:
    """One KEY=value line for a systemd EnvironmentFile.

    systemd stops an unquoted value at the first newline (real incident: a
    multi-paragraph BUZZ_ACP_TEAM_INSTRUCTIONS reached the agent as its first
    line only, 47 of 3,625 bytes). A double-quoted value may span lines; inside
    it `\\` escapes `\\` and `"`. Single-line values stay unquoted so existing
    env files are byte-identical.
    """
    if "\n" not in value:
        return f"{key}={value}"
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'{key}="{escaped}"'
```

Rewrite every `lines.append(f"...")` in `write_agent_files` as `lines.append(env_line(KEY, value))`.

- [ ] **Step 4: Run tests, then verify live on this machine (required)**

```bash
uv run pytest -q
uv run buzz-fleet agent update --community <id> laravel-backend-developer-claude \
  --team-instructions "$(cat ~/.config/buzz-fleet/personas/developers/pack_instructions.md)"
pid=$(systemctl --user show -p MainPID --value buzz-agent@laravel-backend-developer-claude)
tr '\0' '\n' < /proc/$pid/environ | grep -c . ; tr '\0' '\n' < /proc/$pid/environ | grep -A3 '^BUZZ_ACP_TEAM_INSTRUCTIONS='
```

Expected: the value now spans lines. If `journalctl --user -u buzz-agent@... | grep -i invalid` shows systemd rejecting it, stop and report before Task 8.

- [ ] **Step 5: Commit**

```bash
git add src/buzz_fleet/systemd.py tests/test_systemd.py
git commit -m "Quote multi-line env values so team instructions survive systemd parsing"
```

---

### Task 3: Agent settings: session policy, turn cap, heartbeat; hostname in the managed-agent record

**Files:**
- Modify: `src/buzz_fleet/models.py`, `src/buzz_fleet/systemd.py`, `src/buzz_fleet/manager.py`, `src/buzz_fleet/visibility.py`, `src/buzz_fleet/cli/app.py`, `src/buzz_fleet/tui/screens/agent_form.py`
- Test: `tests/test_models.py`, `tests/test_systemd.py`, `tests/test_visibility.py`, `tests/test_cli.py`

**Interfaces:**
- Produces: `Agent.session_policy: Literal["thread","channel"] | None`, `Agent.max_turns_per_session: int | None`, `Agent.heartbeat_interval_seconds: int | None`; env `BUZZ_ACP_SESSION_POLICY`, `BUZZ_ACP_MAX_TURNS_PER_SESSION`, `BUZZ_ACP_HEARTBEAT_INTERVAL`; `visibility.managed_agent_content(agent)` includes `"host": socket.gethostname()`.

- [ ] **Step 1: Write the failing tests**

`tests/test_models.py`:

```python
def test_session_fields_default_none_and_round_trip() -> None:
    agent = Agent(**_base_kwargs())
    assert (agent.session_policy, agent.max_turns_per_session, agent.heartbeat_interval_seconds) == (None, None, None)
    again = Agent.model_validate_json(
        Agent(**_base_kwargs(), session_policy="channel", max_turns_per_session=12, heartbeat_interval_seconds=300).model_dump_json()
    )
    assert (again.session_policy, again.max_turns_per_session, again.heartbeat_interval_seconds) == ("channel", 12, 300)
```

`tests/test_systemd.py`:

```python
def test_write_agent_files_session_and_heartbeat_defaults(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.WORK_DIR", tmp_path / "work")
    monkeypatch.setattr("buzz_fleet.systemd.resolve_adapter_command", lambda harness: "/usr/bin/x")

    write_agent_files(_agent(), _community(), None, None)

    env = agent_env_path("laravel-backend-dev").read_text()
    for line in ("BUZZ_ACP_SESSION_POLICY=thread\n", "BUZZ_ACP_MAX_TURNS_PER_SESSION=40\n", "BUZZ_ACP_HEARTBEAT_INTERVAL=900\n"):
        assert line in env


def test_write_agent_files_session_overrides(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.WORK_DIR", tmp_path / "work")
    monkeypatch.setattr("buzz_fleet.systemd.resolve_adapter_command", lambda harness: "/usr/bin/x")
    agent = _agent().model_copy(update={"session_policy": "channel", "max_turns_per_session": 5, "heartbeat_interval_seconds": 0})

    write_agent_files(agent, _community(), None, None)

    env = agent_env_path("laravel-backend-dev").read_text()
    for line in ("BUZZ_ACP_SESSION_POLICY=channel\n", "BUZZ_ACP_MAX_TURNS_PER_SESSION=5\n", "BUZZ_ACP_HEARTBEAT_INTERVAL=0\n"):
        assert line in env
```

`tests/test_visibility.py` (model the agent fixture on that file's existing one):

```python
def test_managed_agent_content_includes_host(monkeypatch) -> None:
    monkeypatch.setattr("buzz_fleet.visibility.socket.gethostname", lambda: "mod-sol")
    content = managed_agent_content(_agent())
    assert content["host"] == "mod-sol"
```

`tests/test_cli.py`:

```python
def test_agent_create_passes_session_flags(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeManager:
        def create_agent(self, **kwargs):
            captured.update(kwargs)
            return _agent()

    monkeypatch.setattr("buzz_fleet.cli.app._load_manager", lambda community: FakeManager())
    result = runner_cli.invoke(app, [
        "agent", "create", "--community", "e", "--display-name", "X", "--harness", "claude",
        "--prompt-file", "/dev/null", "--session-policy", "channel", "--max-turns-per-session", "7",
        "--heartbeat-interval-seconds", "0",
    ])
    assert result.exit_code == 0, result.output
    assert (captured["session_policy"], captured["max_turns_per_session"], captured["heartbeat_interval_seconds"]) == ("channel", 7, 0)


def test_agent_create_rejects_bad_session_policy(monkeypatch) -> None:
    monkeypatch.setattr("buzz_fleet.cli.app._load_manager", lambda community: object())
    result = runner_cli.invoke(app, ["agent", "create", "--community", "e", "--display-name", "X",
                                     "--harness", "claude", "--prompt-file", "/dev/null", "--session-policy", "bogus"])
    assert result.exit_code == 1 and "thread, channel" in result.output
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_models.py tests/test_systemd.py tests/test_visibility.py tests/test_cli.py -q`

- [ ] **Step 3: Implement**

`models.py` `Agent`, after `respond_to_allowlist`:

```python
    # buzz-acp session scoping (spec 5.8): `thread` (default) isolates each
    # channel thread into its own provider session so a run is a shared,
    # memory-keeping session while the owner can DM the agent in parallel.
    # `channel` is buzz-acp's legacy one-session-per-channel, the rollback.
    session_policy: Literal["thread", "channel"] | None = None
    # Rotate an *active* session after N turns. Dormant sessions are handled
    # by the recycle timer (plan 2), not by this cap.
    max_turns_per_session: int | None = None
    # Seconds between buzz-acp heartbeat prompts; the agent-side delivery
    # recovery path (spec fact 6). 0 disables.
    heartbeat_interval_seconds: int | None = None
```

`systemd.py` `write_agent_files`, after the max-turn-duration block:

```python
    lines.append(env_line("BUZZ_ACP_SESSION_POLICY", agent.session_policy or "thread"))
    lines.append(env_line("BUZZ_ACP_MAX_TURNS_PER_SESSION",
                          str(40 if agent.max_turns_per_session is None else agent.max_turns_per_session)))
    lines.append(env_line("BUZZ_ACP_HEARTBEAT_INTERVAL",
                          str(900 if agent.heartbeat_interval_seconds is None else agent.heartbeat_interval_seconds)))
```

`visibility.py`: `import socket` and add `"host": socket.gethostname()` to the dict returned by `managed_agent_content`. Check `tests/test_visibility.py` for an exact-dict assertion and extend it.

`manager.py` `create_agent`: add keyword params `session_policy: str | None = None`, `max_turns_per_session: int | None = None`, `heartbeat_interval_seconds: int | None = None`, passed into `Agent(...)`.

`cli/app.py`: in `agent_create` and `agent_update` add

```python
    session_policy: Annotated[str | None, typer.Option(help="buzz-acp session scoping: thread (default) or channel")] = None,
    max_turns_per_session: Annotated[int | None, typer.Option(help="Rotate an active session after N turns (default 40)")] = None,
    heartbeat_interval_seconds: Annotated[int | None, typer.Option(help="Heartbeat prompt interval (default 900, 0 disables)")] = None,
```

with `if session_policy is not None and session_policy not in ("thread", "channel"): typer.echo("--session-policy must be one of: thread, channel", err=True); raise typer.Exit(code=1)`; pass through to `create_agent`; in `agent_update` add each to `changes` when not None.

`tui/screens/agent_form.py`: in the Limits section add two `Input`s (`#max-turns-per-session-input`, placeholder "Max turns per session (default 40)"; `#heartbeat-interval-input`, placeholder "Heartbeat interval seconds (default 900)") and a `Select([("thread (default)", "thread"), ("channel", "channel")], value=..., allow_blank=False, id="session-policy-select")`; in the save handler parse both ints with `_parse_optional_int`, read the select value, and pass all three into `changes` and `create_agent`. Update the error notify text.

- [ ] **Step 4: Run tests and commit**

```bash
uv run pytest -q && uv run ruff check . && uv run mypy
git add src/buzz_fleet tests
git commit -m "Add per-agent session policy, turn cap, heartbeat interval, and host in the managed-agent record"
```

---

### Task 4: Signer `post-message`

**Files:**
- Create: `signer/src/fleet.rs`
- Modify: `signer/src/main.rs`

**Interfaces:**
- CLI: `buzz-fleet-signer post-message --relay <url> --nsec <nsec> [--auth-tag <json>] --channel <uuid> --content <text|-> [--mention <hex>]... [--root <hex64>] [--parent <hex64>] [--tag <name>=<value>]...` → `{"ok":true,"event_id":"<hex64>"}` exit 0, or `{"ok":false,"error":"..."}` exit 1.
- Rust: `fleet::build_fleet_message(channel: Uuid, content: &str, mentions: &[String], root: Option<&str>, parent: Option<&str>, extra_tags: &[(String, String)]) -> anyhow::Result<EventBuilder>`.

- [ ] **Step 1: Write the failing tests**

Create `signer/src/fleet.rs`:

```rust
//! Builders and relay helpers for the orchestration subcommands.

use std::time::Duration;

use buzz_ws_client::connection::NostrWsConnection;
use buzz_ws_client::message::RelayMessage;
use nostr::{EventBuilder, JsonUtil};
use uuid::Uuid;

#[cfg(test)]
mod tests {
    use super::*;
    use nostr::{Keys, Kind};

    fn tags_of(event: &nostr::Event) -> Vec<Vec<String>> {
        event.tags.iter().map(|t| t.as_slice().to_vec()).collect()
    }

    fn has(tags: &[Vec<String>], want: &[&str]) -> bool {
        tags.iter().any(|t| t.iter().map(String::as_str).eq(want.iter().copied()))
    }

    #[test]
    fn fleet_message_carries_channel_mentions_thread_and_extra_tags() {
        let channel = Uuid::new_v4();
        let to = "b".repeat(64);
        let root = "c".repeat(64);
        let event = build_fleet_message(
            channel, "@Reviewer ▶ task a1b2c3d4", &[to.clone()], Some(&root), Some(&root),
            &[("t".into(), "fleet".into()), ("fleet".into(), "{\"type\":\"delegate\"}".into())],
        ).unwrap().sign_with_keys(&Keys::generate()).unwrap();

        assert_eq!(event.kind, Kind::Custom(9));
        let tags = tags_of(&event);
        assert!(has(&tags, &["h", &channel.to_string()]));
        assert!(has(&tags, &["p", &to]));
        assert!(has(&tags, &["e", &root, "", "reply"]));
        assert!(has(&tags, &["t", "fleet"]));
        assert!(has(&tags, &["fleet", "{\"type\":\"delegate\"}"]));
    }

    #[test]
    fn fleet_message_nested_reply_emits_root_and_reply_markers() {
        let root = "c".repeat(64);
        let parent = "d".repeat(64);
        let event = build_fleet_message(Uuid::new_v4(), "x", &[], Some(&root), Some(&parent), &[])
            .unwrap().sign_with_keys(&Keys::generate()).unwrap();
        let tags = tags_of(&event);
        assert!(has(&tags, &["e", &root, "", "root"]));
        assert!(has(&tags, &["e", &parent, "", "reply"]));
    }

    #[test]
    fn fleet_message_rejects_bad_mention_and_parent_without_root() {
        assert!(build_fleet_message(Uuid::new_v4(), "x", &["nope".into()], None, None, &[]).is_err());
        assert!(build_fleet_message(Uuid::new_v4(), "x", &[], None, Some(&"d".repeat(64)), &[]).is_err());
    }
}
```

Add `mod fleet;` to `signer/src/main.rs`.

- [ ] **Step 2: Run to verify failure**

Run: `cd signer && cargo test fleet` → compile error, `build_fleet_message` missing.

- [ ] **Step 3: Implement the builder**

In `fleet.rs` above the tests:

```rust
/// A kind 9 channel message with optional NIP-10 thread markers, `p`
/// mentions, and extra tags. The relay stores tags verbatim; only single-
/// letter tags are filterable, which is why completeness rests on the
/// retrieval-key `p` tag the caller passes in `mentions`.
pub fn build_fleet_message(
    channel: Uuid,
    content: &str,
    mentions: &[String],
    root: Option<&str>,
    parent: Option<&str>,
    extra_tags: &[(String, String)],
) -> anyhow::Result<EventBuilder> {
    for m in mentions {
        nostr::PublicKey::from_hex(m).map_err(|e| anyhow::anyhow!("invalid mention {m}: {e}"))?;
    }
    let thread_ref = match (root, parent) {
        (Some(r), p) => {
            let root_id = nostr::EventId::from_hex(r).map_err(|e| anyhow::anyhow!("invalid root: {e}"))?;
            let parent_id = match p {
                Some(p) => nostr::EventId::from_hex(p).map_err(|e| anyhow::anyhow!("invalid parent: {e}"))?,
                None => root_id,
            };
            Some(buzz_sdk::ThreadRef { root_event_id: root_id, parent_event_id: parent_id })
        }
        (None, Some(_)) => anyhow::bail!("--parent requires --root"),
        (None, None) => None,
    };
    let mention_refs: Vec<&str> = mentions.iter().map(String::as_str).collect();
    let mut builder = buzz_sdk::builders::build_message(
        channel, content, thread_ref.as_ref(), &mention_refs, false, &[], &[],
    ).map_err(|e| anyhow::anyhow!(e))?;
    for (name, value) in extra_tags {
        let tag = nostr::Tag::parse([name.as_str(), value.as_str()])
            .map_err(|e| anyhow::anyhow!("invalid tag {name}: {e}"))?;
        builder = builder.tag(tag);
    }
    Ok(builder)
}
```

`EventBuilder::tag` appends (nostr 0.44 `event/builder.rs:206`). `buzz_sdk::ThreadRef` is the path buzz-acp's `pool.rs` uses.

- [ ] **Step 4: Wire the subcommand**

In `main.rs` `enum Command`:

```rust
    /// Publish one kind 9 channel message with mentions, thread markers, and extra tags.
    PostMessage {
        #[arg(long)] relay: String,
        #[arg(long)] nsec: String,
        #[arg(long)] auth_tag: Option<String>,
        #[arg(long)] channel: String,
        /// Message text; `-` reads stdin.
        #[arg(long)] content: String,
        #[arg(long = "mention")] mentions: Vec<String>,
        #[arg(long)] root: Option<String>,
        #[arg(long)] parent: Option<String>,
        /// Extra tag as name=value (repeatable; value may contain '=').
        #[arg(long = "tag")] tags: Vec<String>,
    },
```

Helpers near `run_publish`:

```rust
async fn run_publish_id(
    relay: &str, signer_nsec: &str, builder: anyhow::Result<nostr::EventBuilder>, auth_tag: Option<&nostr::Tag>,
) -> anyhow::Result<String> {
    let keys = Keys::parse(signer_nsec)?;
    let event = builder?.sign_with_keys(&keys)?;
    let id = event.id.to_hex();
    let mut conn = NostrWsConnection::connect_authenticated(relay, &keys, auth_tag).await?;
    let response = conn.send_event(event).await?;
    conn.disconnect().await?;
    if !response.accepted {
        anyhow::bail!("relay rejected event: {}", response.message);
    }
    Ok(id)
}

fn parse_optional_auth_tag(auth_tag: Option<&str>) -> anyhow::Result<Option<nostr::Tag>> {
    match auth_tag {
        None => Ok(None),
        Some(s) => buzz_sdk::nip_oa::parse_auth_tag(s).map(Some).map_err(|e| anyhow::anyhow!("invalid: auth_tag {e}")),
    }
}

fn read_content_arg(content: &str) -> anyhow::Result<String> {
    if content != "-" {
        return Ok(content.to_string());
    }
    let mut s = String::new();
    std::io::Read::read_to_string(&mut std::io::stdin(), &mut s)?;
    Ok(s)
}

fn parse_tag_args(raw: &[String]) -> anyhow::Result<Vec<(String, String)>> {
    raw.iter().map(|s| {
        s.split_once('=').map(|(k, v)| (k.to_string(), v.to_string()))
            .ok_or_else(|| anyhow::anyhow!("invalid --tag {s:?}: expected name=value"))
    }).collect()
}

fn ok_json(value: serde_json::Value) -> i32 { println!("{value}"); 0 }
fn err_json(e: anyhow::Error, code: i32) -> i32 { println!("{}", json!({"ok": false, "error": e.to_string()})); code }
```

Match arm:

```rust
        Command::PostMessage { relay, nsec, auth_tag, channel, content, mentions, root, parent, tags } => {
            let result = async {
                let channel = uuid::Uuid::parse_str(&channel).map_err(|e| anyhow::anyhow!("invalid: channel {e}"))?;
                let content = read_content_arg(&content)?;
                let extra = parse_tag_args(&tags)?;
                let builder = fleet::build_fleet_message(channel, &content, &mentions, root.as_deref(), parent.as_deref(), &extra);
                let tag = parse_optional_auth_tag(auth_tag.as_deref())?;
                run_publish_id(&relay, &nsec, builder, tag.as_ref()).await
            }.await;
            match result { Ok(id) => ok_json(json!({"ok": true, "event_id": id})), Err(e) => err_json(e, 1) }
        }
```

Test in `main.rs`'s test module:

```rust
    #[test]
    fn parse_tag_args_splits_on_first_equals_only() {
        let parsed = parse_tag_args(&["t=fleet".into(), "fleet={\"a\":\"b=c\"}".into()]).unwrap();
        assert_eq!(parsed[0], ("t".into(), "fleet".into()));
        assert_eq!(parsed[1], ("fleet".into(), "{\"a\":\"b=c\"}".into()));
        assert!(parse_tag_args(&["novalue".into()]).is_err());
    }
```

- [ ] **Step 5: Build, test, live smoke**

```bash
cd signer && cargo test && cargo build --release
./target/release/buzz-fleet-signer post-message --relay wss://buzz.eltahir.me --nsec "$ADMIN_NSEC" \
  --channel "$CHANNEL" --content "fleet smoke test" --tag t=fleet --tag 'fleet={"v":1,"type":"smoke"}'
```

Expected: `{"ok":true,"event_id":...}` and the message visible in Desktop. (`ADMIN_NSEC` from `~/.config/buzz-fleet/communities/<id>.json`; `CHANNEL` any channel the owner is in.)

- [ ] **Step 6: Commit**

```bash
git add signer/src && git commit -m "signer: add post-message for tagged, threaded channel messages"
```

---

### Task 5: Signer `query` (one page) and `collect_events`

**Files:**
- Modify: `signer/src/fleet.rs`, `signer/src/main.rs`

**Interfaces:**
- CLI: `buzz-fleet-signer query --relay --nsec [--auth-tag] --filter <json>` → one JSON event per stdout line, exit 0 at EOSE; on CLOSED/connection error `{"ok":false,"error":...}` exit 2. Paging (`until`, `limit`) is driven by the Python caller through the filter JSON.
- Rust: `fleet::parse_filter(json: &str) -> anyhow::Result<nostr::Filter>` (refuses multi-letter `#xx` keys); `fleet::collect_events(conn: &mut NostrWsConnection, filter: nostr::Filter) -> anyhow::Result<Vec<nostr::Event>>`; `fleet::run_query(relay, nsec, auth_tag, filter) -> anyhow::Result<Vec<nostr::Event>>`.

- [ ] **Step 1: Write the failing tests**

```rust
    #[test]
    fn parse_filter_accepts_single_letter_tag_filters() {
        let f = parse_filter(r#"{"kinds":[9],"#p":["ab"],"#h":["6f1c0000-0000-4000-8000-000000000000"],"until":5,"limit":1000}"#).unwrap();
        assert!(f.kinds.as_ref().unwrap().contains(&Kind::Custom(9)));
        let p = nostr::SingleLetterTag::lowercase(nostr::Alphabet::P);
        assert!(f.generic_tags.get(&p).unwrap().contains("ab"));
        assert_eq!(f.until.unwrap().as_u64(), 5);
        assert_eq!(f.limit, Some(1000));
    }

    #[test]
    fn parse_filter_rejects_multi_letter_tag_keys() {
        let err = parse_filter(r#"{"kinds":[9],"#fleet":["x"]}"#).unwrap_err();
        assert!(err.to_string().contains("single-letter"));
    }
```

- [ ] **Step 2: Run to verify failure**

`cd signer && cargo test fleet` → `parse_filter` missing.

- [ ] **Step 3: Implement**

```rust
pub fn parse_filter(json: &str) -> anyhow::Result<nostr::Filter> {
    let raw: serde_json::Value = serde_json::from_str(json)?;
    if let Some(obj) = raw.as_object() {
        for key in obj.keys() {
            if key.starts_with('#') && key.chars().count() != 2 {
                anyhow::bail!("filter key {key:?} is not a single-letter tag filter; nostr would silently ignore it");
            }
        }
    }
    nostr::Filter::from_json(json).map_err(|e| anyhow::anyhow!("invalid filter: {e}"))
}

/// One REQ on an authenticated connection; collect until EOSE, then CLOSE.
pub async fn collect_events(conn: &mut NostrWsConnection, filter: nostr::Filter) -> anyhow::Result<Vec<nostr::Event>> {
    let sub_id = format!("fleet-{}", Uuid::new_v4().simple());
    conn.send_raw(&serde_json::json!(["REQ", sub_id, filter])).await?;
    let mut out = Vec::new();
    loop {
        match conn.next_event(Duration::from_secs(30)).await? {
            RelayMessage::Event { event, .. } => out.push(*event),
            RelayMessage::Eose { .. } => break,
            RelayMessage::Closed { message, .. } => anyhow::bail!("relay closed subscription: {message}"),
            _ => {}
        }
    }
    let _ = conn.send_raw(&serde_json::json!(["CLOSE", sub_id])).await;
    Ok(out)
}

pub async fn run_query(relay: &str, nsec: &str, auth_tag: Option<&nostr::Tag>, filter: nostr::Filter) -> anyhow::Result<Vec<nostr::Event>> {
    let keys = nostr::Keys::parse(nsec)?;
    let mut conn = NostrWsConnection::connect_authenticated(relay, &keys, auth_tag).await?;
    let events = collect_events(&mut conn, filter).await;
    let _ = conn.disconnect().await;
    events
}
```

`main.rs`:

```rust
    /// One-shot REQ; prints each event as a JSON line and exits at EOSE.
    Query { #[arg(long)] relay: String, #[arg(long)] nsec: String, #[arg(long)] auth_tag: Option<String>, #[arg(long)] filter: String },
```

```rust
        Command::Query { relay, nsec, auth_tag, filter } => {
            let result = async {
                let tag = parse_optional_auth_tag(auth_tag.as_deref())?;
                let filter = fleet::parse_filter(&filter)?;
                fleet::run_query(&relay, &nsec, tag.as_ref(), filter).await
            }.await;
            match result {
                Ok(events) => { for e in events { println!("{}", e.as_json()); } 0 }
                Err(e) => err_json(e, 2),
            }
        }
```

- [ ] **Step 4: Test and live smoke**

```bash
cd signer && cargo test && cargo build --release
./target/release/buzz-fleet-signer query --relay wss://buzz.eltahir.me --nsec "$ADMIN_NSEC" \
  --filter '{"kinds":[9],"#h":["'$CHANNEL'"],"limit":5}'
```

Expected: up to five JSON lines, exit 0.

- [ ] **Step 5: Commit**

```bash
git add signer/src && git commit -m "signer: add query subcommand"
```

---

### Task 6: Signer `channel-members`, `create-channel`, `read-channel-meta`, `write-channel-about`

**Files:**
- Modify: `signer/src/fleet.rs`, `signer/src/main.rs`

**Interfaces:**
- `channel-members --relay --nsec [--auth-tag] --channel <uuid>` → `{"ok":true,"members":[{"pubkey","display_name"|null}]}`.
- `create-channel --relay --owner-nsec --name <str> [--about <str>]` → `{"ok":true,"channel_id":"<uuid>"}`.
- `read-channel-meta --relay --nsec [--auth-tag] [--channel <uuid>]` → `{"ok":true,"channels":[{"channel_id","name","about","archived":bool}]}` for one channel or every accessible channel (kind 39000).
- `write-channel-about --relay --owner-nsec --channel <uuid> --about <text|->` → `{"ok":true}` (`build_update_channel(channel, None, Some(about), None, None)`).
- Rust: `fleet::members_from_events(members: &[Event], profiles: &[Event]) -> Vec<(String, Option<String>)>`, `fleet::channel_meta(metadata: &[Event]) -> Vec<ChannelMeta>` with `pub struct ChannelMeta { channel_id, name, about, archived }`.

- [ ] **Step 1: Write the failing tests**

```rust
    fn signed(builder: EventBuilder, keys: &Keys) -> nostr::Event { builder.sign_with_keys(keys).unwrap() }

    #[test]
    fn members_from_events_joins_membership_with_profiles() {
        let a = Keys::generate();
        let b = Keys::generate();
        let channel = Uuid::new_v4();
        let members = signed(EventBuilder::new(Kind::Custom(39002), "").tags([
            nostr::Tag::parse(["d", &channel.to_string()]).unwrap(),
            nostr::Tag::parse(["p", &a.public_key().to_hex()]).unwrap(),
            nostr::Tag::parse(["p", &b.public_key().to_hex()]).unwrap(),
        ]), &Keys::generate());
        let profile_a = signed(EventBuilder::new(Kind::Metadata, r#"{"display_name":"Reviewer"}"#), &a);
        let out = members_from_events(&[members], &[profile_a]);
        assert!(out.contains(&(a.public_key().to_hex(), Some("Reviewer".into()))));
        assert!(out.contains(&(b.public_key().to_hex(), None)));
    }

    #[test]
    fn channel_meta_reads_name_about_and_archived() {
        let keys = Keys::generate();
        let id1 = Uuid::new_v4();
        let id2 = Uuid::new_v4();
        let live = signed(EventBuilder::new(Kind::Custom(39000), "").tags([
            nostr::Tag::parse(["d", &id1.to_string()]).unwrap(),
            nostr::Tag::parse(["name", "fleet"]).unwrap(),
            nostr::Tag::parse(["about", "line one\n{\"buzz-fleet\":1}"]).unwrap(),
        ]), &keys);
        let archived = signed(EventBuilder::new(Kind::Custom(39000), "").tags([
            nostr::Tag::parse(["d", &id2.to_string()]).unwrap(),
            nostr::Tag::parse(["name", "old"]).unwrap(),
            nostr::Tag::parse(["archived", "true"]).unwrap(),
        ]), &keys);
        let metas = channel_meta(&[live, archived]);
        assert_eq!(metas.len(), 2);
        let m = metas.iter().find(|m| m.channel_id == id1.to_string()).unwrap();
        assert_eq!(m.name, "fleet");
        assert_eq!(m.about.as_deref(), Some("line one\n{\"buzz-fleet\":1}"));
        assert!(!m.archived);
        assert!(metas.iter().find(|m| m.channel_id == id2.to_string()).unwrap().archived);
    }
```

Before implementing `archived`, confirm the exact tag buzz-acp checks: `grep -n "archived" /home/dev/repos/buzz/crates/buzz-acp/src/relay.rs | head` and copy that check.

- [ ] **Step 2: Run to verify failure**

`cd signer && cargo test fleet`

- [ ] **Step 3: Implement**

```rust
fn tag_value<'a>(event: &'a nostr::Event, name: &str) -> Option<&'a str> {
    event.tags.iter().find_map(|t| {
        let s = t.as_slice();
        if s.first().map(String::as_str) == Some(name) { s.get(1).map(String::as_str) } else { None }
    })
}

fn tag_values<'a>(event: &'a nostr::Event, name: &str) -> Vec<&'a str> {
    event.tags.iter().filter_map(|t| {
        let s = t.as_slice();
        if s.first().map(String::as_str) == Some(name) { s.get(1).map(String::as_str) } else { None }
    }).collect()
}

pub fn members_from_events(members: &[nostr::Event], profiles: &[nostr::Event]) -> Vec<(String, Option<String>)> {
    let mut names = std::collections::HashMap::new();
    for p in profiles {
        if let Ok(v) = serde_json::from_str::<serde_json::Value>(&p.content) {
            let name = v.get("display_name").and_then(|x| x.as_str())
                .or_else(|| v.get("name").and_then(|x| x.as_str())).map(str::to_string);
            names.insert(p.pubkey.to_hex(), name);
        }
    }
    let mut seen = std::collections::BTreeSet::new();
    let mut out = Vec::new();
    for m in members {
        for pk in tag_values(m, "p") {
            if seen.insert(pk.to_string()) {
                out.push((pk.to_string(), names.get(pk).cloned().flatten()));
            }
        }
    }
    out
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct ChannelMeta {
    pub channel_id: String,
    pub name: String,
    pub about: Option<String>,
    pub archived: bool,
}

pub fn channel_meta(metadata: &[nostr::Event]) -> Vec<ChannelMeta> {
    metadata.iter().filter_map(|e| Some(ChannelMeta {
        channel_id: tag_value(e, "d")?.to_string(),
        name: tag_value(e, "name").unwrap_or("").to_string(),
        about: tag_value(e, "about").map(str::to_string),
        archived: tag_value(e, "archived") == Some("true"),
    })).collect()
}

pub async fn run_channel_members(relay: &str, nsec: &str, auth_tag: Option<&nostr::Tag>, channel: Uuid) -> anyhow::Result<Vec<(String, Option<String>)>> {
    let keys = nostr::Keys::parse(nsec)?;
    let mut conn = NostrWsConnection::connect_authenticated(relay, &keys, auth_tag).await?;
    let d = nostr::SingleLetterTag::lowercase(nostr::Alphabet::D);
    let members = collect_events(&mut conn, nostr::Filter::new().kind(nostr::Kind::Custom(39002)).custom_tags(d, [channel.to_string()])).await?;
    let pubkeys: Vec<nostr::PublicKey> = members.iter()
        .flat_map(|m| tag_values(m, "p").into_iter().filter_map(|p| nostr::PublicKey::from_hex(p).ok()).collect::<Vec<_>>())
        .collect();
    let profiles = if pubkeys.is_empty() { vec![] } else {
        collect_events(&mut conn, nostr::Filter::new().kind(nostr::Kind::Metadata).authors(pubkeys)).await?
    };
    let _ = conn.disconnect().await;
    Ok(members_from_events(&members, &profiles))
}

pub async fn run_read_channel_meta(relay: &str, nsec: &str, auth_tag: Option<&nostr::Tag>, channel: Option<Uuid>) -> anyhow::Result<Vec<ChannelMeta>> {
    let keys = nostr::Keys::parse(nsec)?;
    let mut conn = NostrWsConnection::connect_authenticated(relay, &keys, auth_tag).await?;
    let mut filter = nostr::Filter::new().kind(nostr::Kind::Custom(39000));
    if let Some(id) = channel {
        filter = filter.custom_tags(nostr::SingleLetterTag::lowercase(nostr::Alphabet::D), [id.to_string()]);
    }
    let metadata = collect_events(&mut conn, filter).await?;
    let _ = conn.disconnect().await;
    Ok(channel_meta(&metadata))
}

pub fn build_create_channel(name: &str, about: Option<&str>) -> anyhow::Result<(Uuid, EventBuilder)> {
    let id = Uuid::new_v4();
    let builder = buzz_sdk::builders::build_create_channel(id, name, None, None, about, None).map_err(|e| anyhow::anyhow!(e))?;
    Ok((id, builder))
}

pub fn build_write_about(channel: Uuid, about: &str) -> anyhow::Result<EventBuilder> {
    buzz_sdk::builders::build_update_channel(channel, None, Some(about), None, None).map_err(|e| anyhow::anyhow!(e))
}
```

Commands and arms in `main.rs`:

```rust
    /// List a channel's members with display names.
    ChannelMembers { #[arg(long)] relay: String, #[arg(long)] nsec: String, #[arg(long)] auth_tag: Option<String>, #[arg(long)] channel: String },
    /// Create a channel (owner-signed) and print its id.
    CreateChannel { #[arg(long)] relay: String, #[arg(long)] owner_nsec: String, #[arg(long)] name: String, #[arg(long)] about: Option<String> },
    /// Read kind 39000 metadata for one channel or every accessible channel.
    ReadChannelMeta { #[arg(long)] relay: String, #[arg(long)] nsec: String, #[arg(long)] auth_tag: Option<String>, #[arg(long)] channel: Option<String> },
    /// Owner-signed update of a channel's about text (`-` reads stdin).
    WriteChannelAbout { #[arg(long)] relay: String, #[arg(long)] owner_nsec: String, #[arg(long)] channel: String, #[arg(long)] about: String },
```

```rust
        Command::ChannelMembers { relay, nsec, auth_tag, channel } => {
            let result = async {
                let tag = parse_optional_auth_tag(auth_tag.as_deref())?;
                let channel = uuid::Uuid::parse_str(&channel).map_err(|e| anyhow::anyhow!("invalid: channel {e}"))?;
                fleet::run_channel_members(&relay, &nsec, tag.as_ref(), channel).await
            }.await;
            match result {
                Ok(members) => ok_json(json!({"ok": true, "members": members.into_iter()
                    .map(|(pubkey, display_name)| json!({"pubkey": pubkey, "display_name": display_name})).collect::<Vec<_>>()})),
                Err(e) => err_json(e, 1),
            }
        }
        Command::CreateChannel { relay, owner_nsec, name, about } => {
            let result = async {
                let (id, builder) = fleet::build_create_channel(&name, about.as_deref())?;
                run_publish(&relay, &owner_nsec, Ok(builder), None).await?;
                Ok::<_, anyhow::Error>(id)
            }.await;
            match result { Ok(id) => ok_json(json!({"ok": true, "channel_id": id.to_string()})), Err(e) => err_json(e, 1) }
        }
        Command::ReadChannelMeta { relay, nsec, auth_tag, channel } => {
            let result = async {
                let tag = parse_optional_auth_tag(auth_tag.as_deref())?;
                let channel = match channel {
                    Some(c) => Some(uuid::Uuid::parse_str(&c).map_err(|e| anyhow::anyhow!("invalid: channel {e}"))?),
                    None => None,
                };
                fleet::run_read_channel_meta(&relay, &nsec, tag.as_ref(), channel).await
            }.await;
            match result { Ok(metas) => ok_json(json!({"ok": true, "channels": metas})), Err(e) => err_json(e, 1) }
        }
        Command::WriteChannelAbout { relay, owner_nsec, channel, about } => {
            let result = async {
                let channel = uuid::Uuid::parse_str(&channel).map_err(|e| anyhow::anyhow!("invalid: channel {e}"))?;
                let about = read_content_arg(&about)?;
                run_publish(&relay, &owner_nsec, fleet::build_write_about(channel, &about), None).await
            }.await;
            match result { Ok(()) => ok_json(json!({"ok": true})), Err(e) => err_json(e, 1) }
        }
```

- [ ] **Step 4: Test and live smoke (read-only)**

```bash
cd signer && cargo test && cargo build --release
./target/release/buzz-fleet-signer channel-members --relay wss://buzz.eltahir.me --nsec "$ADMIN_NSEC" --channel "$CHANNEL"
./target/release/buzz-fleet-signer read-channel-meta --relay wss://buzz.eltahir.me --nsec "$ADMIN_NSEC"
```

Expected: members with display names; every accessible channel with name and about. Do not run `create-channel`/`write-channel-about` here; Task 9's `fleet init` does that once.

- [ ] **Step 5: Commit**

```bash
git add signer/src && git commit -m "signer: add channel-members, create-channel, read-channel-meta, write-channel-about"
```

---

### Task 7: Python wrappers for the new signer subcommands

**Files:**
- Modify: `src/buzz_fleet/signer_client.py`
- Test: `tests/test_signer_client.py`

**Interfaces (all keyword-only after the positional runner/relay/nsec):**
- `post_message(runner, relay_url, nsec, channel_id, content, *, mentions, root, parent, tags, auth_tag) -> str`
- `query(runner, relay_url, nsec, filter, *, auth_tag) -> list[dict]`
- `channel_members(runner, relay_url, nsec, channel_id, *, auth_tag) -> list[tuple[str, str | None]]`
- `create_channel(runner, relay_url, owner_nsec, name, *, about) -> str`
- `read_channel_meta(runner, relay_url, nsec, *, channel_id, auth_tag) -> list[dict]` (keys `channel_id`, `name`, `about`, `archived`)
- `write_channel_about(runner, relay_url, owner_nsec, channel_id, about) -> None`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_signer_client.py`:

```python
import pytest  # noqa: E402

from buzz_fleet.signer_client import (  # noqa: E402
    channel_members, create_channel, post_message, query, read_channel_meta, write_channel_about,
)

CH = "6f1c0000-0000-4000-8000-000000000000"


def test_post_message_argv_and_event_id() -> None:
    runner = FakeRunner(json.dumps({"ok": True, "event_id": "e" * 64}))
    event_id = post_message(runner, "wss://r", "nsec1a", CH, "hello", mentions=["b" * 64], root="c" * 64,
                            parent="d" * 64, tags=[("t", "fleet"), ("fleet", '{"type":"x"}')], auth_tag='["auth","a","","b"]')
    assert event_id == "e" * 64
    assert runner.calls == [[
        "buzz-fleet-signer", "post-message", "--relay", "wss://r", "--nsec", "nsec1a", "--auth-tag", '["auth","a","","b"]',
        "--channel", CH, "--content", "hello", "--mention", "b" * 64, "--root", "c" * 64, "--parent", "d" * 64,
        "--tag", "t=fleet", "--tag", 'fleet={"type":"x"}',
    ]]


def test_post_message_raises_on_rejection() -> None:
    runner = FakeRunner(json.dumps({"ok": False, "error": "restricted"}), returncode=1)
    with pytest.raises(RuntimeError, match="restricted"):
        post_message(runner, "wss://r", "nsec1a", CH, "x", mentions=[], root=None, parent=None, tags=[], auth_tag=None)


def test_query_parses_json_lines_and_errors() -> None:
    runner = FakeRunner(json.dumps({"id": "1", "kind": 9}) + "\n" + json.dumps({"id": "2", "kind": 9}) + "\n")
    events = query(runner, "wss://r", "nsec1a", {"kinds": [9], "#p": ["a" * 64]}, auth_tag=None)
    assert [e["id"] for e in events] == ["1", "2"]
    assert json.loads(runner.calls[0][runner.calls[0].index("--filter") + 1]) == {"kinds": [9], "#p": ["a" * 64]}

    runner = FakeRunner(json.dumps({"ok": False, "error": "closed"}) + "\n", returncode=2)
    with pytest.raises(RuntimeError, match="closed"):
        query(runner, "wss://r", "nsec1a", {"kinds": [9]}, auth_tag=None)


def test_channel_members_and_meta_and_about() -> None:
    runner = FakeRunner(json.dumps({"ok": True, "members": [{"pubkey": "a" * 64, "display_name": "Reviewer"},
                                                          {"pubkey": "b" * 64, "display_name": None}]}))
    assert channel_members(runner, "wss://r", "nsec1a", CH, auth_tag=None) == [("a" * 64, "Reviewer"), ("b" * 64, None)]

    runner = FakeRunner(json.dumps({"ok": True, "channels": [{"channel_id": CH, "name": "fleet", "about": "x", "archived": False}]}))
    assert read_channel_meta(runner, "wss://r", "nsec1a", channel_id=None, auth_tag=None)[0]["name"] == "fleet"
    assert "--channel" not in runner.calls[0]

    runner = FakeRunner(json.dumps({"ok": True}))
    write_channel_about(runner, "wss://r", "nsec1owner", CH, "line\n{}")
    assert runner.calls == [["buzz-fleet-signer", "write-channel-about", "--relay", "wss://r", "--owner-nsec", "nsec1owner",
                             "--channel", CH, "--about", "line\n{}"]]


def test_create_channel() -> None:
    runner = FakeRunner(json.dumps({"ok": True, "channel_id": CH}))
    assert create_channel(runner, "wss://r", "nsec1owner", "fleet", about="Fleet") == CH
    assert runner.calls[0][-4:] == ["--name", "fleet", "--about", "Fleet"]
```

- [ ] **Step 2: Run to verify failure**

`uv run pytest tests/test_signer_client.py -q` → ImportError.

- [ ] **Step 3: Implement**

Append to `signer_client.py` (add `import subprocess`):

```python
def _auth_args(auth_tag: str | None) -> list[str]:
    return ["--auth-tag", auth_tag] if auth_tag else []


def _check_ok(result: subprocess.CompletedProcess[str], what: str) -> dict:
    payload = json.loads(result.stdout)
    if not payload.get("ok"):
        raise RuntimeError(f"{what} failed: {payload.get('error')}")
    return payload


def post_message(runner: CommandRunner, relay_url: str, nsec: str, channel_id: str, content: str, *,
                 mentions: list[str], root: str | None, parent: str | None, tags: list[tuple[str, str]],
                 auth_tag: str | None) -> str:
    args = [BINARY, "post-message", "--relay", relay_url, "--nsec", nsec, *_auth_args(auth_tag),
            "--channel", channel_id, "--content", content]
    for m in mentions:
        args += ["--mention", m]
    if root:
        args += ["--root", root]
    if parent:
        args += ["--parent", parent]
    for name, value in tags:
        args += ["--tag", f"{name}={value}"]
    event_id: str = _check_ok(runner.run(args), "post-message")["event_id"]
    return event_id


def query(runner: CommandRunner, relay_url: str, nsec: str, filter: dict, *, auth_tag: str | None) -> list[dict]:
    args = [BINARY, "query", "--relay", relay_url, "--nsec", nsec, *_auth_args(auth_tag),
            "--filter", json.dumps(filter, separators=(",", ":"))]
    result = runner.run(args)
    events: list[dict] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        if "ok" in obj and not obj["ok"]:
            raise RuntimeError(f"query failed: {obj.get('error')}")
        events.append(obj)
    if result.returncode != 0:
        raise RuntimeError(f"query failed with exit code {result.returncode}: {result.stderr.strip()}")
    return events


def channel_members(runner: CommandRunner, relay_url: str, nsec: str, channel_id: str, *,
                    auth_tag: str | None) -> list[tuple[str, str | None]]:
    args = [BINARY, "channel-members", "--relay", relay_url, "--nsec", nsec, *_auth_args(auth_tag), "--channel", channel_id]
    return [(m["pubkey"], m.get("display_name")) for m in _check_ok(runner.run(args), "channel-members")["members"]]


def create_channel(runner: CommandRunner, relay_url: str, owner_nsec: str, name: str, *, about: str | None) -> str:
    args = [BINARY, "create-channel", "--relay", relay_url, "--owner-nsec", owner_nsec, "--name", name]
    if about:
        args += ["--about", about]
    channel_id: str = _check_ok(runner.run(args), "create-channel")["channel_id"]
    return channel_id


def read_channel_meta(runner: CommandRunner, relay_url: str, nsec: str, *, channel_id: str | None,
                      auth_tag: str | None) -> list[dict]:
    args = [BINARY, "read-channel-meta", "--relay", relay_url, "--nsec", nsec, *_auth_args(auth_tag)]
    if channel_id:
        args += ["--channel", channel_id]
    channels: list[dict] = _check_ok(runner.run(args), "read-channel-meta")["channels"]
    return channels


def write_channel_about(runner: CommandRunner, relay_url: str, owner_nsec: str, channel_id: str, about: str) -> None:
    args = [BINARY, "write-channel-about", "--relay", relay_url, "--owner-nsec", owner_nsec, "--channel", channel_id, "--about", about]
    _check_ok(runner.run(args), "write-channel-about")
```

- [ ] **Step 4: Run tests and commit**

```bash
uv run pytest -q && uv run ruff check . && uv run mypy
git add src/buzz_fleet/signer_client.py tests/test_signer_client.py
git commit -m "Add signer client wrappers for messages, queries, members, and channel metadata"
```

---

### Task 8: Pure helpers: ids, durations, fleet record

**Files:**
- Create: `src/buzz_fleet/orchestration/__init__.py` (empty), `ids.py`, `durations.py`, `record.py`
- Test: `tests/test_orch_helpers.py`

**Interfaces:**
- `ids.new_id() -> str` (UUIDv4 string), `ids.short(id) -> str` (first 8 chars), `ids.match_prefix(prefix: str, candidates: Iterable[str]) -> str` (raises `ValueError("unknown ...")` / `ValueError("ambiguous ...")`).
- `durations.parse_duration(text) -> int` seconds: `90`, `90s`, `45m`, `2h`, `1h30m`, `12h`.
- `record.FleetRecord` (Pydantic): `version: int = 1`, `retrieval_key: str`, `conductors: dict[str, ConductorEntry]` (`ConductorEntry(pubkey: str, host: str)`, keys `primary`/`standby`), `limits: Limits(open_adhoc_per_requester=5, chain_depth=4, max_rework=3, max_tasks=20)`, `budget: dict | None`, `retention: str = "keep"`, `versions: dict[str, str]`, `created_at: int`.
- `record.ABOUT_HEADER = "buzz-fleet orchestration channel — do not edit"`, `record.encode_about(rec) -> str`, `record.decode_about(about: str | None) -> FleetRecord | None`.

- [ ] **Step 1: Write the failing tests**

```python
import pytest

from buzz_fleet.orchestration import ids, record
from buzz_fleet.orchestration.durations import parse_duration


def test_ids_new_short_and_prefix() -> None:
    a = ids.new_id()
    assert len(a) == 36 and ids.short(a) == a[:8]
    assert ids.match_prefix(a[:8], [a, ids.new_id()]) == a
    with pytest.raises(ValueError, match="unknown"):
        ids.match_prefix("zzzzzzzz", [a])
    twin = a[:8] + "-0000-4000-8000-000000000000"
    with pytest.raises(ValueError, match="ambiguous"):
        ids.match_prefix(a[:8], [a, twin])


@pytest.mark.parametrize("text,expected", [("90", 90), ("90s", 90), ("45m", 2700), ("2h", 7200), ("1h30m", 5400), (" 12h ", 43200)])
def test_parse_duration(text: str, expected: int) -> None:
    assert parse_duration(text) == expected


@pytest.mark.parametrize("text", ["", "abc", "5x", "-5m", "1m1h", "0m"])
def test_parse_duration_rejects(text: str) -> None:
    with pytest.raises(ValueError):
        parse_duration(text)


def test_record_round_trips_through_about() -> None:
    rec = record.FleetRecord(
        retrieval_key="r" * 64,
        conductors={"primary": record.ConductorEntry(pubkey="p" * 64, host="vps")},
        versions={"buzz-fleet": "0.8.0"}, created_at=1_800_000_000,
    )
    about = record.encode_about(rec)
    assert about.startswith(record.ABOUT_HEADER + "\n")
    again = record.decode_about(about)
    assert again == rec
    assert again.limits.open_adhoc_per_requester == 5 and again.retention == "keep"


def test_record_decode_ignores_foreign_about() -> None:
    assert record.decode_about(None) is None
    assert record.decode_about("just a channel description") is None
    assert record.decode_about(record.ABOUT_HEADER + "\n{not json") is None
```

- [ ] **Step 2: Run to verify failure**

`uv run pytest tests/test_orch_helpers.py -q`

- [ ] **Step 3: Implement**

`ids.py`:

```python
"""Stable ids for tasks, attempts, runs; short display; prefix lookup."""

from __future__ import annotations

import uuid
from collections.abc import Iterable


def new_id() -> str:
    return str(uuid.uuid4())


def short(full: str) -> str:
    return full[:8]


def match_prefix(prefix: str, candidates: Iterable[str]) -> str:
    matches = [c for c in candidates if c.startswith(prefix)]
    if not matches:
        raise ValueError(f"unknown id {prefix!r}")
    if len(matches) > 1:
        raise ValueError(f"ambiguous id {prefix!r}: " + ", ".join(short(m) for m in matches))
    return matches[0]
```

`durations.py`:

```python
"""Parse human durations like 45m or 1h30m into seconds."""

from __future__ import annotations

import re

_UNIT = {"h": 3600, "m": 60, "s": 1}
_PART = re.compile(r"(\d+)([hms])")


def parse_duration(text: str) -> int:
    s = text.strip()
    if s.isdigit():
        total = int(s)
    else:
        total, pos, last = 0, 0, -1
        for m in _PART.finditer(s):
            if m.start() != pos:
                raise ValueError(f"invalid duration {text!r}")
            idx = "hms".index(m.group(2))
            if idx <= last:
                raise ValueError(f"invalid duration {text!r}: units must go h, m, s")
            last = idx
            total += int(m.group(1)) * _UNIT[m.group(2)]
            pos = m.end()
        if pos != len(s):
            raise ValueError(f"invalid duration {text!r}")
    if total <= 0:
        raise ValueError(f"duration must be positive: {text!r}")
    return total
```

`record.py`:

```python
"""The owner-signed fleet record stored in the fleet channel's `about` (spec 5.9)."""

from __future__ import annotations

import json

from pydantic import BaseModel, Field

ABOUT_HEADER = "buzz-fleet orchestration channel — do not edit"


class ConductorEntry(BaseModel):
    pubkey: str
    host: str


class Limits(BaseModel):
    open_adhoc_per_requester: int = 5
    chain_depth: int = 4
    max_rework: int = 3
    max_tasks: int = 20


class FleetRecord(BaseModel):
    version: int = 1
    retrieval_key: str
    conductors: dict[str, ConductorEntry] = Field(default_factory=dict)
    limits: Limits = Field(default_factory=Limits)
    budget: dict | None = None
    retention: str = "keep"
    versions: dict[str, str] = Field(default_factory=dict)
    created_at: int


def encode_about(rec: FleetRecord) -> str:
    return ABOUT_HEADER + "\n" + json.dumps(rec.model_dump(mode="json"), separators=(",", ":"), sort_keys=True)


def decode_about(about: str | None) -> FleetRecord | None:
    if not about:
        return None
    lines = about.split("\n", 1)
    if lines[0].strip() != ABOUT_HEADER or len(lines) < 2:
        return None
    try:
        return FleetRecord.model_validate_json(lines[1])
    except ValueError:
        return None
```

- [ ] **Step 4: Run tests and commit**

```bash
uv run pytest -q && uv run ruff check . && uv run mypy
git add src/buzz_fleet/orchestration tests/test_orch_helpers.py
git commit -m "Add orchestration helpers: ids, durations, fleet record"
```

---

### Task 9: Fleet channel and record: init once, discover everywhere, auto-join, env, unique names

**Files:**
- Modify: `src/buzz_fleet/models.py` (`Community.fleet_channel_id`, `Community.fleet_record`), `src/buzz_fleet/state.py`, `src/buzz_fleet/systemd.py`, `src/buzz_fleet/manager.py`
- Create: `src/buzz_fleet/cli/fleet_commands.py` (`fleet_app`: `init`, `status`)
- Modify: `src/buzz_fleet/cli/app.py`
- Test: `tests/test_models.py`, `tests/test_state.py`, `tests/test_systemd.py`, `tests/test_manager.py`, `tests/test_cli.py`

**Interfaces:**
- `Community.fleet_channel_id: str | None`, `Community.fleet_record: FleetRecord | None`.
- `state.list_community_ids() -> list[str]`.
- Env: `BUZZ_FLEET_CHANNEL=<uuid>`, `BUZZ_FLEET_RETRIEVAL_KEY=<hex>` when known.
- `manager.FLEET_CHANNEL_NAME = "fleet"`; `AgentManager.init_fleet_channel(existing: str | None, host: str) -> tuple[str, FleetRecord]`; `AgentManager.ensure_fleet_record() -> FleetRecord | None` (discover + cache, never raises); `AgentManager.display_name_taken(name) -> bool`; `create_agent(..., force: bool = False)` raises `ValueError` on a taken name unless forced.
- The retrieval keypair is generated by `init_fleet_channel`; its secret is printed once and stored nowhere (spec 5.1). The conductor keys are generated in plan 2; `init` records only the retrieval key and `conductors={}`.

- [ ] **Step 1: Write the failing tests**

`tests/test_models.py`:

```python
def test_community_fleet_fields_default_none() -> None:
    from buzz_fleet.models import Community

    c = Community(id="e", relay_url="wss://r", relay_admin_nsec="nsec1a")
    assert c.fleet_channel_id is None and c.fleet_record is None
```

`tests/test_state.py`:

```python
def test_list_community_ids(tmp_path, monkeypatch) -> None:
    from buzz_fleet import state
    from buzz_fleet.models import Community

    monkeypatch.setattr(state, "CONFIG_DIR", tmp_path)
    assert state.list_community_ids() == []
    for cid in ("b", "a"):
        state.save_community(Community(id=cid, relay_url="wss://r", relay_admin_nsec="nsec1a"))
    assert state.list_community_ids() == ["a", "b"]
```

`tests/test_systemd.py`:

```python
def test_write_agent_files_exports_fleet_env_when_known(tmp_path: Path, monkeypatch) -> None:
    from buzz_fleet.orchestration.record import FleetRecord

    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.WORK_DIR", tmp_path / "work")
    monkeypatch.setattr("buzz_fleet.systemd.resolve_adapter_command", lambda harness: "/usr/bin/x")
    community = _community().model_copy(update={
        "fleet_channel_id": "6f1c0000-0000-4000-8000-000000000000",
        "fleet_record": FleetRecord(retrieval_key="r" * 64, created_at=1),
    })

    write_agent_files(_agent(), community, None, None)

    env = agent_env_path("laravel-backend-dev").read_text()
    assert "BUZZ_FLEET_CHANNEL=6f1c0000-0000-4000-8000-000000000000\n" in env
    assert f"BUZZ_FLEET_RETRIEVAL_KEY={'r' * 64}\n" in env
```

`tests/test_manager.py` — extend `FakeRunner`: add `self.channels: list[dict] = []` and `self.members: list[dict] = []` in `__init__`, and dispatch:

```python
        elif args[:2] == ["buzz-fleet-signer", "read-channel-meta"]:
            stdout = json.dumps({"ok": True, "channels": self.channels})
        elif args[:2] == ["buzz-fleet-signer", "create-channel"]:
            stdout = json.dumps({"ok": True, "channel_id": FLEET})
        elif args[:2] == ["buzz-fleet-signer", "write-channel-about"]:
            stdout = json.dumps({"ok": True})
        elif args[:2] == ["buzz-fleet-signer", "channel-members"]:
            stdout = json.dumps({"ok": True, "members": self.members})
```

with `FLEET = "6f1c0000-0000-4000-8000-000000000000"` at module level, then:

```python
from buzz_fleet.orchestration.record import ABOUT_HEADER, FleetRecord, encode_about


def _fresh_manager(tmp_path: Path, monkeypatch, runner: FakeRunner) -> AgentManager:
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.WORK_DIR", tmp_path / "work")
    monkeypatch.setattr("buzz_fleet.systemd.TEMPLATE_UNIT_PATH", tmp_path / "unit" / "buzz-agent@.service")
    monkeypatch.setattr("buzz_fleet.systemd.ensure_linger_enabled", lambda runner: None)
    from buzz_fleet import state
    community = _community()
    state.save_community(community)
    return AgentManager(runner, community)


def _record_about() -> str:
    return encode_about(FleetRecord(retrieval_key="r" * 64, created_at=1))


def test_init_fleet_channel_creates_writes_record_and_persists(tmp_path: Path, monkeypatch) -> None:
    runner = FakeRunner()
    manager = _fresh_manager(tmp_path, monkeypatch, runner)

    channel_id, rec = manager.init_fleet_channel(existing=None, host="vps")

    assert channel_id == FLEET and len(rec.retrieval_key) == 64
    from buzz_fleet import state
    saved = state.load_community("eltahir")
    assert saved.fleet_channel_id == FLEET and saved.fleet_record.retrieval_key == rec.retrieval_key
    about_call = next(a for a in runner.calls if a[1] == "write-channel-about")
    assert about_call[about_call.index("--about") + 1].startswith(ABOUT_HEADER)


def test_init_fleet_channel_refuses_when_record_exists(tmp_path: Path, monkeypatch) -> None:
    runner = FakeRunner()
    runner.channels = [{"channel_id": FLEET, "name": "fleet", "about": _record_about(), "archived": False}]
    manager = _fresh_manager(tmp_path, monkeypatch, runner)
    with pytest.raises(RuntimeError, match="already exists"):
        manager.init_fleet_channel(existing=None, host="vps")


def test_init_fleet_channel_adopts_and_writes_record(tmp_path: Path, monkeypatch) -> None:
    runner = FakeRunner()
    runner.channels = [{"channel_id": FLEET, "name": "ops", "about": "plain", "archived": False}]
    manager = _fresh_manager(tmp_path, monkeypatch, runner)
    channel_id, _ = manager.init_fleet_channel(existing=FLEET, host="vps")
    assert channel_id == FLEET
    assert not any(a[1] == "create-channel" for a in runner.calls)
    assert any(a[1] == "write-channel-about" for a in runner.calls)


def test_ensure_runtime_ready_discovers_record_joins_and_rewrites_env(tmp_path: Path, monkeypatch) -> None:
    runner = FakeRunner()
    manager = _fresh_manager(tmp_path, monkeypatch, runner)
    agent = manager.create_agent(display_name="Reviewer", harness="claude",
                                 system_prompt_source=SystemPromptSource(kind="inline", text="hi"))
    runner.channels = [{"channel_id": FLEET, "name": "fleet", "about": _record_about(), "archived": False}]
    runner.calls.clear()

    manager.ensure_runtime_ready()

    from buzz_fleet import state
    assert state.load_community("eltahir").fleet_channel_id == FLEET
    assert len([a for a in runner.calls if a[1] == "join-channel" and FLEET in a]) == 1
    env = agent_env_path(agent.id).read_text()
    assert f"BUZZ_FLEET_CHANNEL={FLEET}\n" in env and f"BUZZ_FLEET_RETRIEVAL_KEY={'r' * 64}\n" in env
    assert state.load_agents("eltahir")[0].visibility_state.channels[FLEET] == "joined"


def test_create_agent_refuses_duplicate_display_name_unless_forced(tmp_path: Path, monkeypatch) -> None:
    runner = FakeRunner()
    runner.channels = [{"channel_id": FLEET, "name": "fleet", "about": _record_about(), "archived": False}]
    runner.members = [{"pubkey": "d" * 64, "display_name": "Reviewer"}]
    manager = _fresh_manager(tmp_path, monkeypatch, runner)
    src = SystemPromptSource(kind="inline", text="hi")
    with pytest.raises(ValueError, match="already used"):
        manager.create_agent(display_name="Reviewer", harness="claude", system_prompt_source=src)
    assert manager.create_agent(display_name="Reviewer", harness="claude", system_prompt_source=src, force=True)
```

`tests/test_cli.py`:

```python
def test_fleet_init_prints_channel_and_record(monkeypatch) -> None:
    from buzz_fleet.orchestration.record import FleetRecord

    class FakeManager:
        def init_fleet_channel(self, existing, host):
            return "6f1c0000-0000-4000-8000-000000000000", FleetRecord(retrieval_key="r" * 64, created_at=1)

    monkeypatch.setattr("buzz_fleet.cli.fleet_commands._load_manager", lambda community: FakeManager())
    result = runner_cli.invoke(app, ["fleet", "init", "--community", "e"])
    assert result.exit_code == 0, result.output
    assert "6f1c0000-0000-4000-8000-000000000000" in result.output and "r" * 64 in result.output
```

- [ ] **Step 2: Run to verify failure**

`uv run pytest tests/test_models.py tests/test_state.py tests/test_systemd.py tests/test_manager.py tests/test_cli.py -q`

- [ ] **Step 3: Implement**

`models.py` `Community`:

```python
    # Spec 5.9: the community's orchestration channel and the owner-signed
    # fleet record cached from its metadata. Created once by `fleet init`;
    # discovered everywhere else by `AgentManager.ensure_fleet_record`.
    fleet_channel_id: str | None = None
    fleet_record: FleetRecord | None = None
```

with `from buzz_fleet.orchestration.record import FleetRecord` (the record module imports only pydantic, so no cycle).

`state.py`:

```python
def list_community_ids() -> list[str]:
    directory = CONFIG_DIR / "communities"
    return sorted(p.stem for p in directory.glob("*.json")) if directory.exists() else []
```

`systemd.py` `write_agent_files`, after the owner line:

```python
    if community.fleet_channel_id:
        lines.append(env_line("BUZZ_FLEET_CHANNEL", community.fleet_channel_id))
    if community.fleet_record:
        lines.append(env_line("BUZZ_FLEET_RETRIEVAL_KEY", community.fleet_record.retrieval_key))
```

`manager.py`:

```python
import socket
import time

from buzz_fleet.orchestration.record import FleetRecord, decode_about, encode_about

FLEET_CHANNEL_NAME = "fleet"


def _agent_env_has(agent_id: str, key: str, value: str) -> bool:
    path = systemd.agent_env_path(agent_id)
    return path.exists() and f"{key}={value}\n" in path.read_text()
```

Methods on `AgentManager`:

```python
    def _owner_nsec(self) -> str:
        return self._community.relay_admin_nsec.get_secret_value()

    def _find_fleet_record(self) -> tuple[str, FleetRecord] | None:
        found = []
        for meta in signer_client.read_channel_meta(self._runner, self._community.relay_url, self._owner_nsec(),
                                                    channel_id=None, auth_tag=None):
            if meta.get("archived"):
                continue
            rec = decode_about(meta.get("about"))
            if rec:
                found.append((meta["channel_id"], rec))
        if len(found) > 1:
            raise RuntimeError("more than one channel carries a fleet record: " + ", ".join(c for c, _ in found)
                               + "; archive all but one")
        return found[0] if found else None

    def _save_fleet(self, channel_id: str, rec: FleetRecord) -> None:
        self._community = self._community.model_copy(update={"fleet_channel_id": channel_id, "fleet_record": rec})
        state.save_community(self._community)

    def init_fleet_channel(self, existing: str | None, host: str) -> tuple[str, FleetRecord]:
        """Create (or adopt) the fleet channel and write the fleet record, once per community.

        Never automatic: five machines auto-creating would produce five channels.
        Refuses when a record already exists anywhere the owner can see.
        """
        if self._find_fleet_record() is not None:
            raise RuntimeError("a fleet record already exists on this relay; other machines discover it automatically "
                               "(`buzz-fleet agent list`). Use `fleet status` to see it.")
        retrieval_pub, retrieval_secret = signer_client.generate_key(self._runner)
        rec = FleetRecord(
            retrieval_key=retrieval_pub, conductors={},
            versions={"buzz-fleet": __version__}, created_at=int(time.time()),
        )
        if existing:
            channel_id = existing
        else:
            channel_id = signer_client.create_channel(self._runner, self._community.relay_url, self._owner_nsec(),
                                                      FLEET_CHANNEL_NAME, about=None)
        signer_client.write_channel_about(self._runner, self._community.relay_url, self._owner_nsec(), channel_id, encode_about(rec))
        self._save_fleet(channel_id, rec)
        # The retrieval secret is deliberately not stored: nothing ever signs
        # with it (spec 5.1). Returned once so the owner can archive it.
        self._last_retrieval_secret = retrieval_secret
        return channel_id, rec

    def ensure_fleet_record(self) -> FleetRecord | None:
        if self._community.fleet_record:
            return self._community.fleet_record
        try:
            found = self._find_fleet_record()
        except (RuntimeError, json.JSONDecodeError, KeyError, OSError):
            return None
        if found:
            self._save_fleet(*found)
            return found[1]
        return None

    def display_name_taken(self, display_name: str) -> bool:
        channel = self._community.fleet_channel_id
        if not channel:
            return False
        try:
            members = signer_client.channel_members(self._runner, self._community.relay_url, self._owner_nsec(),
                                                    channel, auth_tag=None)
        except (RuntimeError, json.JSONDecodeError, KeyError, OSError):
            return False
        return any(n and n.strip().lower() == display_name.strip().lower() for _, n in members)
```

`from buzz_fleet import __version__` at the top. In `create_agent`, add `force: bool = False` and, right after `self.ensure_runtime_ready()`:

```python
        if not force and self.display_name_taken(display_name):
            raise ValueError(f"display name {display_name!r} is already used by an agent in the fleet channel; "
                             f"pick another or pass --force")
```

In `_sync_visibility`, replace the channel loop header:

```python
        wanted = list(agent.channel_ids or [])
        if self._community.fleet_channel_id and self._community.fleet_channel_id not in wanted:
            wanted.append(self._community.fleet_channel_id)
        for channel_id in wanted:
```

In `ensure_runtime_ready`, after `needs_full_refresh = ...`: `rec = self.ensure_fleet_record()`, and in the per-agent loop add

```python
            needs_fleet_env = rec is not None and not (
                _agent_env_has(agent.id, "BUZZ_FLEET_CHANNEL", self._community.fleet_channel_id or "")
                and _agent_env_has(agent.id, "BUZZ_FLEET_RETRIEVAL_KEY", rec.retrieval_key)
            )
```

and `and not needs_fleet_env` in the skip condition. No restart is needed for the join itself (spec fact 9); the env rewrite path already restarts.

`cli/fleet_commands.py`:

```python
"""Typer groups for orchestration: `buzz-fleet fleet ...`, `buzz-fleet task ...`, `buzz-fleet tasks`."""

from __future__ import annotations

import socket
from typing import Annotated

import typer

from buzz_fleet import state
from buzz_fleet.manager import AgentManager
from buzz_fleet.proc import RealCommandRunner

fleet_app = typer.Typer(help="Fleet channel, fleet record, and status")


def _load_manager(community_id: str) -> AgentManager:
    community = state.load_community(community_id)
    if community is None:
        typer.echo(f"No community '{community_id}'. Run `buzz-fleet connect` first.", err=True)
        raise typer.Exit(code=1)
    return AgentManager(RealCommandRunner(), community)


@fleet_app.command("init")
def fleet_init(
    community: Annotated[str, typer.Option()],
    channel: Annotated[str | None, typer.Option(help="Adopt an existing channel UUID instead of creating one")] = None,
) -> None:
    manager = _load_manager(community)
    try:
        channel_id, rec = manager.init_fleet_channel(existing=channel, host=socket.gethostname())
    except RuntimeError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1) from e
    typer.echo(f"Fleet channel: {channel_id}")
    typer.echo(f"Retrieval key: {rec.retrieval_key}")
    typer.echo("Retrieval secret (archive it; nothing signs with it and buzz-fleet does not store it):")
    typer.echo(manager._last_retrieval_secret)
    typer.echo("Agents on every machine join the channel on their next buzz-fleet command.")
    typer.echo("Prerequisite per machine: SSH access to every repository your pipelines name.")


@fleet_app.command("status")
def fleet_status(community: Annotated[str, typer.Option()]) -> None:
    manager = _load_manager(community)
    rec = manager.ensure_fleet_record()
    if rec is None:
        typer.echo("No fleet record found. Run `buzz-fleet fleet init` once on the conductor host.")
        raise typer.Exit(code=1)
    typer.echo(f"Channel: {manager._community.fleet_channel_id}")
    typer.echo(f"Retrieval key: {rec.retrieval_key}")
    typer.echo(f"Conductors: {', '.join(f'{k}={v.host} ({v.pubkey[:8]})' for k, v in rec.conductors.items()) or 'none yet'}")
    typer.echo(f"Versions recorded: {rec.versions}")
```

Register in `cli/app.py`: `from buzz_fleet.cli.fleet_commands import fleet_app` and `app.add_typer(fleet_app, name="fleet")`. Add `--force` to `agent create` and pass `force=force`.

- [ ] **Step 4: Run tests, then the one-time live init on the VPS**

```bash
uv run pytest -q && uv run ruff check . && uv run mypy
cp signer/target/release/buzz-fleet-signer ~/.local/share/buzz-fleet/bin/buzz-fleet-signer
uv run buzz-fleet fleet init --community <id>     # on the VPS only, once
uv run buzz-fleet agent list --community <id>     # on every machine: discovers, joins, rewrites env
uv run buzz-fleet fleet status --community <id>
```

Expected: the channel and record printed; Desktop shows a `fleet` channel with the description header and every agent as a member.

- [ ] **Step 5: Commit**

```bash
git add src/buzz_fleet tests && git commit -m "Add the fleet channel and fleet record: init once, discover everywhere, auto-join"
```

---

### Task 10: Coordination instructions block

**Files:**
- Create: `src/buzz_fleet/orchestration/instructions.py`
- Modify: `src/buzz_fleet/systemd.py`, `src/buzz_fleet/manager.py`
- Test: `tests/test_instructions.py`, `tests/test_systemd.py`, `tests/test_manager.py`

**Interfaces:**
- `instructions.COORDINATION_VERSION = "v1"`, `BLOCK_START`, `BLOCK_END`, `apply_coordination_block(text: str | None) -> str`, `has_current_block(text: str | None) -> bool`.

- [ ] **Step 1: Write the failing tests**

`tests/test_instructions.py`:

```python
from buzz_fleet.orchestration import instructions as ins


def test_apply_to_empty_and_idempotent() -> None:
    once = ins.apply_coordination_block(None)
    assert once.startswith(ins.BLOCK_START) and once.rstrip().endswith(ins.BLOCK_END)
    for needle in ("buzz-fleet task delegate", "buzz-fleet task ack", "buzz-fleet task report", "git worktree", "--commit"):
        assert needle in once
    assert ins.apply_coordination_block(once) == once


def test_apply_keeps_operator_text_and_replaces_old_version() -> None:
    old = "# Rules\n\n<!-- buzz-fleet:coordination v0 -->\nold text\n<!-- /buzz-fleet:coordination -->\n"
    out = ins.apply_coordination_block(old)
    assert out.startswith("# Rules") and "old text" not in out and out.count(ins.BLOCK_START) == 1


def test_has_current_block() -> None:
    assert ins.has_current_block(ins.apply_coordination_block("x")) is True
    assert ins.has_current_block("x") is False and ins.has_current_block(None) is False
```

`tests/test_systemd.py`:

```python
def test_write_agent_files_injects_coordination_block(tmp_path: Path, monkeypatch) -> None:
    from buzz_fleet.orchestration.instructions import BLOCK_START

    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.WORK_DIR", tmp_path / "work")
    monkeypatch.setattr("buzz_fleet.systemd.resolve_adapter_command", lambda harness: "/usr/bin/x")

    write_agent_files(_agent().model_copy(update={"team_instructions": None}), _community(), None, None)

    assert BLOCK_START in agent_env_path("laravel-backend-dev").read_text()
```

`tests/test_manager.py`:

```python
def test_ensure_runtime_ready_refreshes_stale_block(tmp_path: Path, monkeypatch) -> None:
    runner = FakeRunner()
    manager = _fresh_manager(tmp_path, monkeypatch, runner)
    agent = manager.create_agent(display_name="Blocky", harness="claude",
                                 system_prompt_source=SystemPromptSource(kind="inline", text="hi"))
    env_path = agent_env_path(agent.id)
    env_path.write_text(env_path.read_text().replace("coordination v1", "coordination v0"))
    runner.calls.clear()

    manager.ensure_runtime_ready()

    assert "coordination v1" in env_path.read_text()
    assert any(a[:3] == ["systemctl", "--user", "restart"] and agent.id in a[3] for a in runner.calls)
```

- [ ] **Step 2: Run to verify failure**

`uv run pytest tests/test_instructions.py tests/test_systemd.py tests/test_manager.py -q`

- [ ] **Step 3: Implement**

`instructions.py`:

```python
"""The fleet coordination block appended to every agent's team instructions (spec 5.0)."""

from __future__ import annotations

import re

COORDINATION_VERSION = "v1"
BLOCK_START = f"<!-- buzz-fleet:coordination {COORDINATION_VERSION} -->"
BLOCK_END = "<!-- /buzz-fleet:coordination -->"
_ANY_BLOCK = re.compile(r"<!-- buzz-fleet:coordination [^>]*-->.*?<!-- /buzz-fleet:coordination -->\n?", re.DOTALL)

COORDINATION_TEXT = """## Fleet coordination

You are one of several agents run by the same owner on different machines. Work moves between
agents with these commands (all on your PATH). Every command prints JSON; if one fails, say so in
your reply instead of pretending it worked.

**Receiving work.** A message containing `▶ task <id>` is a delegation to you.
1. Run `buzz-fleet task ack --task <id>` first, before any work.
2. If it names a repository and commit, work on exactly that commit: in your working directory,
   clone the repository once (`git clone <repo> repos/<name>` if missing), then
   `git -C repos/<name> fetch && git -C repos/<name> worktree add ../../runs/<run or task id> <commit>`
   and work inside that worktree. Never work in a shared checkout.
3. When you finish, push your commit if you made one, then run
   `buzz-fleet task report --task <id> --status done|blocked|failed --summary - --input-commit <commit you received> [--output-commit <commit you pushed>] <<'EOF' ... EOF`
   The summary must stand alone: the reader is in another session and shares none of your context.
   Use `failed` for defects in the work you were given, `blocked` when you need input.
   Add `--next default` (the default) to let the pipeline continue, `--next <task id>` if you
   delegated onward yourself, or `--next none` to pause the run for the owner.

**Handing work to another agent.** Run
`buzz-fleet task delegate --to "<Agent Name>" --repo <url> --commit <sha> --brief - --wait 45m --thread <root event id from the Thread root: line of your prompt> <<'EOF' ... EOF`
Push first; the command refuses a dirty or unpushed checkout. Prefer this over a bare @-mention:
it records the task, the exact revision, and the deadline, and brings the answer back into this
thread. Add `--run <run id>` when the message that woke you names one.

A delegation may state a pipeline default such as "when done, delegate to @Builder". Follow it
unless you have a concrete reason not to, and say why in your report if you deviate.

A message saying a task was cancelled means stop that work immediately and report nothing for it.
`buzz-fleet task show <id>` prints a task's history if you need it. Keep chat replies short; the
report carries the details.
"""


def _block() -> str:
    return f"{BLOCK_START}\n{COORDINATION_TEXT.rstrip()}\n{BLOCK_END}\n"


def apply_coordination_block(text: str | None) -> str:
    base = _ANY_BLOCK.sub("", text or "").rstrip()
    return f"{base}\n\n{_block()}" if base else _block()


def has_current_block(text: str | None) -> bool:
    return bool(text) and BLOCK_START in (text or "")
```

`systemd.py`: `lines.append(env_line("BUZZ_ACP_TEAM_INSTRUCTIONS", apply_coordination_block(agent.team_instructions)))` unconditionally, importing `apply_coordination_block`.

`manager.py` `ensure_runtime_ready` per-agent: `needs_block = not instructions.has_current_block(systemd.agent_env_path(agent.id).read_text() if systemd.agent_env_path(agent.id).exists() else "")` and `and not needs_block` in the skip condition.

- [ ] **Step 4: Run tests, verify live, commit**

```bash
uv run pytest -q && uv run ruff check . && uv run mypy
uv run buzz-fleet agent list --community <id>
```

Then in Desktop, in the fleet channel: `@<agent> which fleet commands do you know?` Expected: the agent replies (Task 1) naming `delegate`, `ack`, `report`.

```bash
git add src/buzz_fleet tests && git commit -m "Inject the fleet coordination block into every agent's team instructions"
```

---

### Task 11: Wire protocol: build and parse fleet messages

**Files:**
- Create: `src/buzz_fleet/orchestration/protocol.py`
- Test: `tests/test_protocol.py`

**Interfaces:**
- `TAG_FLEET = "fleet"`, `PAYLOAD_VERSION = 1`, `MAX_PAYLOAD_BYTES = 8192`, `task_tag(id)`, `run_tag(id)`.
- `Artifact(repo: str, commit: str, branch: str | None = None, base: str | None = None)` (Pydantic).
- `OutgoingMessage(content, mentions: list[str], tags: list[tuple[str, str]], root: str | None, parent: str | None)` (frozen dataclass).
- `build_delegate(*, task_id, attempt_id, from_pubkey, to_pubkey, to_name, retrieval_key, brief, deadline: int, acceptance: list[str], artifact: Artifact | None, run_id: str | None, step: int | None, parent_task: str | None, required: bool, rework_target: str | None, default_next: str | None, thread_root: str | None, thread_parent: str | None) -> OutgoingMessage`.
- `build_ack(*, task_id, attempt_id, from_pubkey, retrieval_key, root, parent) -> OutgoingMessage`.
- `build_report(*, task_id, attempt_id, status, summary, from_pubkey, recipient_pubkey, recipient_name, retrieval_key, next_task: str, input_commit, output_commit, evidence: list[str], run_id, root, parent) -> OutgoingMessage` (`next_task` is `"default"`, `"none"`, or a task id).
- `build_cancel_task(*, task_id, reason, from_pubkey, assignee_pubkey, retrieval_key, root, parent) -> OutgoingMessage`.
- `FleetEvent(id, pubkey, created_at, channel_id, content, mentions, root, parent, payload: dict | None, task_id, run_id)` with property `type`; `parse_event(raw: dict) -> FleetEvent` (payload None when the `fleet` tag is missing, malformed, too large, wrong version, or `from` differs from the author).

- [ ] **Step 1: Write the failing tests**

```python
import json

from buzz_fleet.orchestration import protocol as p

RK = "f" * 64
A, B = "a" * 64, "b" * 64
T, AT = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"


def test_build_delegate_content_tags_and_payload() -> None:
    msg = p.build_delegate(
        task_id=T, attempt_id=AT, from_pubkey=A, to_pubkey=B, to_name="Reviewer", retrieval_key=RK,
        brief="Review the CSV export.", deadline=1_800_000_000, acceptance=["tests pass", "no new deps"],
        artifact=p.Artifact(repo="git@github.com:o/r.git", commit="c" * 40, branch="feat"),
        run_id="33333333-3333-4333-8333-333333333333", step=2, parent_task=None, required=True,
        rework_target=A, default_next="Builder", thread_root="d" * 64, thread_parent="e" * 64,
    )
    assert msg.content.startswith("@Reviewer ▶ task 11111111 (run 33333333, step 2)")
    assert "Review the CSV export." in msg.content
    assert "git@github.com:o/r.git @ " + "c" * 40 in msg.content
    assert "- tests pass" in msg.content and "- no new deps" in msg.content
    assert f"buzz-fleet task ack --task {T}" in msg.content
    assert f"buzz-fleet task report --task {T}" in msg.content
    assert "Default when done: delegate to @Builder" in msg.content
    assert msg.mentions == [B, RK]
    assert ("t", "fleet") in msg.tags and ("t", f"fleet:task:{T}") in msg.tags
    assert ("t", "fleet:run:33333333-3333-4333-8333-333333333333") in msg.tags
    payload = json.loads(dict(msg.tags)["fleet"])
    assert payload["v"] == 1 and payload["type"] == "delegate" and payload["task"] == T and payload["attempt"] == AT
    assert payload["from"] == A and payload["to"] == B and payload["deadline"] == 1_800_000_000
    assert payload["artifact"] == {"repo": "git@github.com:o/r.git", "commit": "c" * 40, "branch": "feat", "base": None}
    assert payload["rework_target"] == A and payload["required"] is True and payload["step"] == 2
    assert msg.root == "d" * 64 and msg.parent == "e" * 64


def test_build_delegate_minimal() -> None:
    msg = p.build_delegate(task_id=T, attempt_id=AT, from_pubkey=A, to_pubkey=B, to_name="Reviewer", retrieval_key=RK,
                           brief="x", deadline=1, acceptance=[], artifact=None, run_id=None, step=None, parent_task=None,
                           required=True, rework_target=None, default_next=None, thread_root=None, thread_parent=None)
    assert msg.content.startswith("@Reviewer ▶ task 11111111\n") and "Default when done" not in msg.content
    assert msg.root is None and not any(v.startswith("fleet:run:") for _, v in msg.tags)


def test_build_ack_and_report_and_cancel() -> None:
    ack = p.build_ack(task_id=T, attempt_id=AT, from_pubkey=B, retrieval_key=RK, root="d" * 64, parent="d" * 64)
    assert json.loads(dict(ack.tags)["fleet"]) == {"v": 1, "type": "ack", "task": T, "attempt": AT, "from": B}
    assert ack.mentions == [RK]

    rep = p.build_report(task_id=T, attempt_id=AT, status="failed", summary="Quoting bug.", from_pubkey=B,
                         recipient_pubkey=A, recipient_name="Implementer", retrieval_key=RK, next_task="default",
                         input_commit="c" * 40, output_commit=None, evidence=["pytest: 3 failed"], run_id=None,
                         root="d" * 64, parent="e" * 64)
    assert rep.content.startswith("@Implementer ❌ task 11111111 failed: Quoting bug.")
    assert "pytest: 3 failed" in rep.content
    assert rep.mentions == [A, RK]
    payload = json.loads(dict(rep.tags)["fleet"])
    assert payload["status"] == "failed" and payload["next"] == "default" and payload["input_commit"] == "c" * 40

    can = p.build_cancel_task(task_id=T, reason="superseded", from_pubkey=A, assignee_pubkey=B, retrieval_key=RK,
                              root="d" * 64, parent="d" * 64)
    assert json.loads(dict(can.tags)["fleet"])["type"] == "cancel-task" and can.mentions == [B, RK]


def test_parse_event_full() -> None:
    raw = {"id": "1" * 64, "pubkey": B, "created_at": 1700, "kind": 9, "content": "hi",
           "tags": [["h", "6f1c0000-0000-4000-8000-000000000000"], ["t", "fleet"], ["t", f"fleet:task:{T}"],
                    ["fleet", json.dumps({"v": 1, "type": "ack", "task": T, "attempt": AT, "from": B})],
                    ["p", A], ["p", RK], ["e", "d" * 64, "", "root"], ["e", "e" * 64, "", "reply"]]}
    ev = p.parse_event(raw)
    assert ev.type == "ack" and ev.task_id == T and ev.channel_id == "6f1c0000-0000-4000-8000-000000000000"
    assert ev.mentions == [A, RK] and ev.root == "d" * 64 and ev.parent == "e" * 64


def test_parse_event_rejects_spoofed_from_and_bad_payloads() -> None:
    base = {"id": "2" * 64, "pubkey": B, "created_at": 1, "kind": 9, "content": ""}
    spoof = {**base, "tags": [["fleet", json.dumps({"v": 1, "type": "ack", "task": T, "attempt": AT, "from": A})]]}
    assert p.parse_event(spoof).payload is None
    assert p.parse_event({**base, "tags": [["fleet", "{not json"]]}).payload is None
    assert p.parse_event({**base, "tags": [["fleet", json.dumps({"v": 2, "type": "ack", "from": B})]]}).payload is None
    huge = {**base, "tags": [["fleet", json.dumps({"v": 1, "type": "ack", "from": B, "pad": "x" * 9000})]]}
    assert p.parse_event(huge).payload is None
    plain = {**base, "tags": [["e", "d" * 64, "", "reply"], ["p", A]]}
    ev = p.parse_event(plain)
    assert ev.payload is None and ev.root == "d" * 64 and ev.parent == "d" * 64
```

- [ ] **Step 2: Run to verify failure**

`uv run pytest tests/test_protocol.py -q`

- [ ] **Step 3: Implement**

```python
"""Fleet wire protocol: kind 9 channel messages carrying fleet tags (spec 5.1)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel

from buzz_fleet.orchestration.ids import short

TAG_FLEET = "fleet"
PAYLOAD_VERSION = 1
MAX_PAYLOAD_BYTES = 8192
ReportStatus = Literal["done", "blocked", "failed"]
_ICON = {"done": "✅", "blocked": "⛔", "failed": "❌"}


def task_tag(task_id: str) -> str:
    return f"fleet:task:{task_id}"


def run_tag(run_id: str) -> str:
    return f"fleet:run:{run_id}"


class Artifact(BaseModel):
    repo: str
    commit: str
    branch: str | None = None
    base: str | None = None


@dataclass(frozen=True)
class OutgoingMessage:
    content: str
    mentions: list[str]
    tags: list[tuple[str, str]]
    root: str | None
    parent: str | None


def _fmt_deadline(deadline: int) -> str:
    return datetime.fromtimestamp(deadline, tz=UTC).strftime("%Y-%m-%d %H:%M UTC")


def _tags(task_id: str, run_id: str | None, payload: dict) -> list[tuple[str, str]]:
    payload = {"v": PAYLOAD_VERSION, **payload}
    encoded = json.dumps(payload, separators=(",", ":"))
    if len(encoded.encode()) > MAX_PAYLOAD_BYTES:
        raise ValueError(f"fleet payload exceeds {MAX_PAYLOAD_BYTES} bytes")
    tags = [("t", TAG_FLEET), ("t", task_tag(task_id))]
    if run_id:
        tags.append(("t", run_tag(run_id)))
    tags.append((TAG_FLEET, encoded))
    return tags


def build_delegate(*, task_id: str, attempt_id: str, from_pubkey: str, to_pubkey: str, to_name: str,
                   retrieval_key: str, brief: str, deadline: int, acceptance: list[str], artifact: Artifact | None,
                   run_id: str | None, step: int | None, parent_task: str | None, required: bool,
                   rework_target: str | None, default_next: str | None, thread_root: str | None,
                   thread_parent: str | None) -> OutgoingMessage:
    header = f"@{to_name} ▶ task {short(task_id)}"
    if run_id:
        header += f" (run {short(run_id)}" + (f", step {step})" if step is not None else ")")
    lines = [header, brief.strip(), ""]
    if artifact:
        lines.append(f"Artifact: {artifact.repo} @ {artifact.commit}" + (f" (branch {artifact.branch})" if artifact.branch else ""))
    if acceptance:
        lines.append("Acceptance:")
        lines.extend(f"- {a}" for a in acceptance)
    lines += [
        f"Deadline: {_fmt_deadline(deadline)}",
        f"First: buzz-fleet task ack --task {task_id}",
        f"When done: buzz-fleet task report --task {task_id} --status done|blocked|failed --summary -"
        + (f" --input-commit {artifact.commit}" if artifact else ""),
    ]
    if default_next:
        lines.append(f"Default when done: delegate to @{default_next} (buzz-fleet task delegate --run {run_id} ...)")
    payload = {"type": "delegate", "task": task_id, "attempt": attempt_id, "run": run_id, "step": step,
               "parent_task": parent_task, "required": required, "from": from_pubkey, "to": to_pubkey,
               "deadline": deadline, "rework_target": rework_target,
               "artifact": artifact.model_dump() if artifact else None, "acceptance": acceptance}
    return OutgoingMessage("\n".join(lines), [to_pubkey, retrieval_key], _tags(task_id, run_id, payload),
                           thread_root, thread_parent or thread_root)


def build_ack(*, task_id: str, attempt_id: str, from_pubkey: str, retrieval_key: str, root: str | None,
              parent: str | None) -> OutgoingMessage:
    payload = {"type": "ack", "task": task_id, "attempt": attempt_id, "from": from_pubkey}
    return OutgoingMessage(f"▶ task {short(task_id)} received, starting.", [retrieval_key],
                           _tags(task_id, None, payload), root, parent)


def build_report(*, task_id: str, attempt_id: str, status: ReportStatus, summary: str, from_pubkey: str,
                 recipient_pubkey: str, recipient_name: str | None, retrieval_key: str, next_task: str,
                 input_commit: str | None, output_commit: str | None, evidence: list[str], run_id: str | None,
                 root: str | None, parent: str | None) -> OutgoingMessage:
    who = f"@{recipient_name}" if recipient_name else "@requester"
    lines = [f"{who} {_ICON[status]} task {short(task_id)} {status}: {summary.strip()}"]
    if output_commit:
        lines.append(f"Output commit: {output_commit}")
    lines.extend(f"- {e}" for e in evidence)
    if next_task not in ("default", "none"):
        lines.append(f"Handed onward as task {short(next_task)}.")
    payload = {"type": "report", "task": task_id, "attempt": attempt_id, "from": from_pubkey, "status": status,
               "next": next_task, "input_commit": input_commit,
               "output": {"commit": output_commit} if output_commit else None, "evidence": evidence, "run": run_id}
    return OutgoingMessage("\n".join(lines), [recipient_pubkey, retrieval_key], _tags(task_id, run_id, payload),
                           root, parent)


def build_cancel_task(*, task_id: str, reason: str, from_pubkey: str, assignee_pubkey: str, retrieval_key: str,
                      root: str | None, parent: str | None) -> OutgoingMessage:
    payload = {"type": "cancel-task", "task": task_id, "from": from_pubkey, "reason": reason}
    return OutgoingMessage(f"⏹ task {short(task_id)} cancelled: {reason}", [assignee_pubkey, retrieval_key],
                           _tags(task_id, None, payload), root, parent)


@dataclass(frozen=True)
class FleetEvent:
    id: str
    pubkey: str
    created_at: int
    channel_id: str | None
    content: str
    mentions: list[str] = field(default_factory=list)
    root: str | None = None
    parent: str | None = None
    payload: dict | None = None
    task_id: str | None = None
    run_id: str | None = None

    @property
    def type(self) -> str | None:
        return self.payload.get("type") if self.payload else None


def _decode_payload(value: str, author: str) -> dict | None:
    if len(value.encode()) > MAX_PAYLOAD_BYTES:
        return None
    try:
        obj = json.loads(value)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict) or obj.get("v") != PAYLOAD_VERSION or "type" not in obj:
        return None
    if obj.get("from") != author:
        return None  # payload identity must match the verified event author (spec 5.1)
    return obj


def parse_event(raw: dict) -> FleetEvent:
    channel = root = parent = None
    mentions: list[str] = []
    payload: dict | None = None
    task_id = run_id = None
    for tag in raw.get("tags") or []:
        if not tag:
            continue
        name, value = tag[0], (tag[1] if len(tag) > 1 else "")
        if name == "h":
            channel = value
        elif name == "p":
            mentions.append(value)
        elif name == "e":
            marker = tag[3] if len(tag) > 3 else ""
            if marker == "root":
                root = value
            elif marker == "reply":
                parent = value
        elif name == "t" and value.startswith("fleet:task:"):
            task_id = value.removeprefix("fleet:task:")
        elif name == "t" and value.startswith("fleet:run:"):
            run_id = value.removeprefix("fleet:run:")
        elif name == TAG_FLEET:
            payload = _decode_payload(value, raw["pubkey"])
    if parent and not root:
        root = parent
    return FleetEvent(id=raw["id"], pubkey=raw["pubkey"], created_at=int(raw["created_at"]), channel_id=channel,
                      content=raw.get("content", ""), mentions=mentions, root=root, parent=parent,
                      payload=payload, task_id=task_id, run_id=run_id)
```

- [ ] **Step 4: Run tests and commit**

```bash
uv run pytest -q && uv run ruff check . && uv run mypy
git add src/buzz_fleet/orchestration/protocol.py tests/test_protocol.py
git commit -m "Add the fleet wire protocol: delegate, ack, report, cancel builders and parser"
```

---

### Task 12: Reducer: events in, task state out

**Files:**
- Create: `src/buzz_fleet/orchestration/reducer.py`
- Test: `tests/test_reducer.py`

**Interfaces:**
- `TaskStatus = Literal["open", "acked", "done", "blocked", "failed", "cancelled", "superseded"]`.
- `Attempt(attempt_id, assignee, created_at, acked_at: int | None, status: TaskStatus, report: dict | None, reported_at: int | None)`.
- `Task(task_id, requester, run_id, step, parent_task, required, rework_target, artifact: dict | None, acceptance: list[str], deadline, created_at, channel_id, root_event_id, delegate_event_id, brief, attempts: list[Attempt], notes: list[str])` with properties `current: Attempt`, `assignee`, `status`, `is_live` (open/acked), `late(now) -> bool`, `unacked -> bool`, and `nudged`/`escalated` flags (`nudged_at`, `escalated_at`, `redelivered: int`).
- `State(tasks: dict[str, Task], seen_cmds: set[str])` with `open_tasks()`, `stuck_tasks(now)`, `unacked_tasks()`, `open_adhoc_by_requester(pubkey) -> int`, `chain_depth(task_id) -> int`.
- `reduce(events: Iterable[FleetEvent], record: FleetRecord | None, *, owner_pubkey: str | None = None, deleted_ids: set[str] = frozenset()) -> State`. Sorts by `(created_at, id)`; drops `deleted_ids`; idempotent.

Rules (spec 5.3/5.4, the state half): `delegate` creates a task (first attempt) or, for an existing task with a new `attempt` id from the conductor or the task's requester, adds an attempt and marks the previous one `superseded`; a duplicate delegate (same task and attempt) is ignored. `ack`/`report` are accepted only from the assignee of the named attempt while that attempt is live; a report on a superseded or terminal attempt becomes a note. `cancel-task` from the requester or owner cancels the live attempt. Conductor-only types (`redeliver`, `nudge`, `escalate`, `cancel-notice`) are accepted only from a pubkey in `record.conductors` (or, when `record` is None, from nobody) and set `redelivered`/`nudged_at`/`escalated_at`. Every payload's `cmd`, when present, goes into `seen_cmds`. Plain chat (payload None) never changes state.

- [ ] **Step 1: Write the failing tests**

```python
import json

import pytest

from buzz_fleet.orchestration.protocol import parse_event
from buzz_fleet.orchestration.record import ConductorEntry, FleetRecord
from buzz_fleet.orchestration.reducer import reduce

A, B, OWNER, COND, RK = "a" * 64, "b" * 64, "0" * 64, "c" * 64, "f" * 64
T1, AT1, AT2 = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222", "33333333-3333-4333-8333-333333333333"
CH = "6f1c0000-0000-4000-8000-000000000000"
REC = FleetRecord(retrieval_key=RK, conductors={"primary": ConductorEntry(pubkey=COND, host="vps")}, created_at=1)


def _ev(id_: str, pubkey: str, created_at: int, payload: dict | None, *, mentions=(), root=None, parent=None, content=""):
    tags = [["h", CH], ["p", RK]]
    if payload is not None:
        payload = {"v": 1, "from": pubkey, **payload}
        tags += [["t", "fleet"], ["t", f"fleet:task:{payload.get('task', T1)}"], ["fleet", json.dumps(payload)]]
    tags += [["p", m] for m in mentions]
    if root and parent and root != parent:
        tags += [["e", root, "", "root"], ["e", parent, "", "reply"]]
    elif root:
        tags += [["e", root, "", "reply"]]
    return parse_event({"id": id_ * 64, "pubkey": pubkey, "created_at": created_at, "kind": 9, "content": content, "tags": tags})


def _delegate(id_="1", created_at=100, deadline=1000, attempt=AT1, frm=A, to=B, **extra):
    return _ev(id_, frm, created_at, {"type": "delegate", "task": T1, "attempt": attempt, "to": to, "deadline": deadline,
                                       "required": True, "acceptance": [], **extra}, mentions=[to], content="brief")


def test_delegate_opens_task_with_first_attempt() -> None:
    t = reduce([_delegate()], REC).tasks[T1]
    assert t.status == "open" and t.assignee == B and t.requester == A and t.deadline == 1000
    assert t.root_event_id == "1" * 64 and t.current.attempt_id == AT1 and t.unacked


def test_ack_and_report_by_assignee() -> None:
    ack = _ev("2", B, 150, {"type": "ack", "task": T1, "attempt": AT1}, root="1" * 64)
    rep = _ev("3", B, 200, {"type": "report", "task": T1, "attempt": AT1, "status": "done", "next": "default",
                            "input_commit": None, "output": None, "evidence": []}, mentions=[A], root="1" * 64, content="done!")
    t = reduce([_delegate(), ack, rep], REC).tasks[T1]
    assert t.current.acked_at == 150 and t.status == "done" and t.current.report["next"] == "default"


def test_report_by_stranger_or_wrong_attempt_is_noted() -> None:
    stranger = _ev("2", "e" * 64, 200, {"type": "report", "task": T1, "attempt": AT1, "status": "done", "next": "default"})
    wrong = _ev("3", B, 201, {"type": "report", "task": T1, "attempt": AT2, "status": "done", "next": "default"})
    t = reduce([_delegate(), stranger, wrong], REC).tasks[T1]
    assert t.status == "open" and len(t.notes) == 2


def test_new_attempt_supersedes_and_late_report_is_noted() -> None:
    fallback = _delegate("2", created_at=300, attempt=AT2, frm=COND, to="d" * 64)
    late = _ev("3", B, 400, {"type": "report", "task": T1, "attempt": AT1, "status": "done", "next": "default"})
    t = reduce([_delegate(), fallback, late], REC).tasks[T1]
    assert t.attempts[0].status == "superseded" and t.current.assignee == "d" * 64 and t.status == "open"
    assert any("superseded" in n for n in t.notes)


def test_duplicate_and_out_of_order_events() -> None:
    rep = _ev("2", B, 200, {"type": "report", "task": T1, "attempt": AT1, "status": "failed", "next": "default"})
    t = reduce([rep, _delegate(), rep, _delegate()], REC).tasks[T1]
    assert t.status == "failed" and len(t.attempts) == 1


def test_cancel_authorization() -> None:
    by_owner = _ev("2", OWNER, 300, {"type": "cancel-task", "task": T1, "reason": "x"})
    by_stranger = _ev("3", "e" * 64, 300, {"type": "cancel-task", "task": T1, "reason": "x"})
    assert reduce([_delegate(), by_stranger], REC, owner_pubkey=OWNER).tasks[T1].status == "open"
    assert reduce([_delegate(), by_owner], REC, owner_pubkey=OWNER).tasks[T1].status == "cancelled"


def test_conductor_only_types_need_conductor_key() -> None:
    nudge_ok = _ev("2", COND, 1100, {"type": "nudge", "task": T1, "attempt": AT1, "cmd": f"nudge:{T1}:{AT1}"})
    nudge_bad = _ev("3", "e" * 64, 1100, {"type": "nudge", "task": T1, "attempt": AT1})
    state = reduce([_delegate(), nudge_ok, nudge_bad], REC)
    assert state.tasks[T1].nudged_at == 1100 and f"nudge:{T1}:{AT1}" in state.seen_cmds
    assert len(state.tasks[T1].notes) == 1


def test_deleted_events_are_dropped_and_plain_chat_ignored() -> None:
    chat = _ev("2", B, 150, None, mentions=[A], root="1" * 64, content="on it")
    assert reduce([_delegate(), chat], REC).tasks[T1].status == "open"
    assert reduce([_delegate()], REC, deleted_ids={"1" * 64}).tasks == {}


def test_limits_helpers() -> None:
    child = _ev("2", B, 120, {"type": "delegate", "task": "44444444-4444-4444-8444-444444444444", "attempt": AT2,
                             "to": A, "deadline": 900, "required": True, "acceptance": [], "parent_task": T1}, mentions=[A])
    state = reduce([_delegate(), child], REC)
    assert state.open_adhoc_by_requester(A) == 1 and state.open_adhoc_by_requester(B) == 1
    assert state.chain_depth("44444444-4444-4444-8444-444444444444") == 2
    assert [t.task_id for t in state.stuck_tasks(now=950)] == ["44444444-4444-4444-8444-444444444444"]
```

- [ ] **Step 2: Run to verify failure**

`uv run pytest tests/test_reducer.py -q`

- [ ] **Step 3: Implement**

```python
"""Pure reducer: fleet events -> task state (spec 5.3). Shared by CLI views, TUI, and the conductor."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Literal

from buzz_fleet.orchestration.protocol import FleetEvent
from buzz_fleet.orchestration.record import FleetRecord

TaskStatus = Literal["open", "acked", "done", "blocked", "failed", "cancelled", "superseded"]
_TERMINAL = frozenset({"done", "blocked", "failed", "cancelled", "superseded"})
_CONDUCTOR_TYPES = frozenset({"redeliver", "nudge", "escalate", "cancel-notice", "advance", "fallback",
                              "run-paused", "run-done", "run-failed", "budget-paused", "heartbeat", "takeover", "yield"})


@dataclass
class Attempt:
    attempt_id: str
    assignee: str
    created_at: int
    acked_at: int | None = None
    status: TaskStatus = "open"
    report: dict | None = None
    reported_at: int | None = None


@dataclass
class Task:
    task_id: str
    requester: str
    run_id: str | None
    step: int | None
    parent_task: str | None
    required: bool
    rework_target: str | None
    artifact: dict | None
    acceptance: list[str]
    deadline: int
    created_at: int
    channel_id: str | None
    root_event_id: str
    delegate_event_id: str
    brief: str
    attempts: list[Attempt] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    nudged_at: int | None = None
    escalated_at: int | None = None
    redelivered: int = 0

    @property
    def current(self) -> Attempt:
        return self.attempts[-1]

    @property
    def assignee(self) -> str:
        return self.current.assignee

    @property
    def status(self) -> TaskStatus:
        return self.current.status

    @property
    def is_live(self) -> bool:
        return self.status in ("open", "acked")

    @property
    def unacked(self) -> bool:
        return self.status == "open"

    def late(self, now: int) -> bool:
        return self.is_live and self.deadline < now


@dataclass
class State:
    tasks: dict[str, Task] = field(default_factory=dict)
    seen_cmds: set[str] = field(default_factory=set)

    def open_tasks(self) -> list[Task]:
        return [t for t in self.tasks.values() if t.is_live]

    def stuck_tasks(self, now: int) -> list[Task]:
        return [t for t in self.tasks.values() if t.late(now)]

    def unacked_tasks(self) -> list[Task]:
        return [t for t in self.tasks.values() if t.unacked]

    def open_adhoc_by_requester(self, pubkey: str) -> int:
        return sum(1 for t in self.open_tasks() if t.run_id is None and t.requester == pubkey)

    def chain_depth(self, task_id: str) -> int:
        depth, cur, seen = 0, self.tasks.get(task_id), set()
        while cur is not None and cur.task_id not in seen:
            seen.add(cur.task_id)
            depth += 1
            cur = self.tasks.get(cur.parent_task) if cur.parent_task else None
        return depth


def reduce(events: Iterable[FleetEvent], record: FleetRecord | None, *, owner_pubkey: str | None = None,
           deleted_ids: set[str] | frozenset[str] = frozenset()) -> State:
    state = State()
    conductors = {c.pubkey for c in record.conductors.values()} if record else set()
    seen: set[str] = set()
    for ev in sorted(events, key=lambda e: (e.created_at, e.id)):
        if ev.id in seen or ev.id in deleted_ids or ev.payload is None:
            continue
        seen.add(ev.id)
        p = ev.payload
        if cmd := p.get("cmd"):
            state.seen_cmds.add(cmd)
        kind = p.get("type")
        task_id = p.get("task") or ev.task_id
        if kind == "delegate" and task_id:
            _apply_delegate(state, ev, task_id, conductors)
            continue
        task = state.tasks.get(task_id or "")
        if task is None:
            continue
        if kind in _CONDUCTOR_TYPES:
            if ev.pubkey not in conductors:
                task.notes.append(f"ignored {kind} {ev.id[:8]} from non-conductor {ev.pubkey[:8]}")
                continue
            if kind == "redeliver":
                task.redelivered += 1
            elif kind == "nudge":
                task.nudged_at = ev.created_at
            elif kind == "escalate":
                task.escalated_at = ev.created_at
            continue
        if kind in ("ack", "report"):
            _apply_assignee_event(task, ev, kind)
        elif kind == "cancel-task":
            if ev.pubkey in {task.requester, owner_pubkey} and task.is_live:
                task.current.status = "cancelled"
            else:
                task.notes.append(f"ignored cancel {ev.id[:8]} from {ev.pubkey[:8]}")
    return state


def _apply_delegate(state: State, ev: FleetEvent, task_id: str, conductors: set[str]) -> None:
    p = ev.payload or {}
    attempt_id = p.get("attempt") or ev.id
    to = p.get("to") or (ev.mentions[0] if ev.mentions else "")
    existing = state.tasks.get(task_id)
    if existing is None:
        state.tasks[task_id] = Task(
            task_id=task_id, requester=ev.pubkey, run_id=p.get("run") or ev.run_id, step=p.get("step"),
            parent_task=p.get("parent_task"), required=bool(p.get("required", True)), rework_target=p.get("rework_target"),
            artifact=p.get("artifact"), acceptance=list(p.get("acceptance") or []), deadline=int(p.get("deadline") or 0),
            created_at=ev.created_at, channel_id=ev.channel_id, root_event_id=ev.root or ev.id, delegate_event_id=ev.id,
            brief=ev.content, attempts=[Attempt(attempt_id, to, ev.created_at)],
        )
        return
    if any(a.attempt_id == attempt_id for a in existing.attempts):
        existing.notes.append(f"duplicate delegate {ev.id[:8]} ignored")
        return
    if ev.pubkey not in conductors and ev.pubkey != existing.requester:
        existing.notes.append(f"ignored new attempt {ev.id[:8]} from {ev.pubkey[:8]}")
        return
    if existing.is_live:
        existing.current.status = "superseded"
    existing.attempts.append(Attempt(attempt_id, to, ev.created_at))
    existing.deadline = int(p.get("deadline") or existing.deadline)
    existing.nudged_at = existing.escalated_at = None
    existing.redelivered = 0


def _apply_assignee_event(task: Task, ev: FleetEvent, kind: str) -> None:
    p = ev.payload or {}
    attempt = next((a for a in task.attempts if a.attempt_id == p.get("attempt")), None)
    if attempt is None or ev.pubkey != attempt.assignee:
        task.notes.append(f"ignored {kind} {ev.id[:8]} from {ev.pubkey[:8]} (not the assignee of that attempt)")
        return
    if attempt.status in _TERMINAL:
        task.notes.append(f"{kind} {ev.id[:8]} on {attempt.status} attempt ignored")
        return
    if kind == "ack":
        attempt.acked_at = attempt.acked_at or ev.created_at
        attempt.status = "acked"
        return
    status = p.get("status")
    if status not in ("done", "blocked", "failed"):
        task.notes.append(f"report {ev.id[:8]} with unknown status {status!r} ignored")
        return
    attempt.status = status
    attempt.report = {**p, "content": ev.content}
    attempt.reported_at = ev.created_at
```

- [ ] **Step 4: Run tests and commit**

```bash
uv run pytest -q && uv run ruff check . && uv run mypy
git add src/buzz_fleet/orchestration/reducer.py tests/test_reducer.py
git commit -m "Add the fleet reducer: attempts, authorization, terminal precedence, limits"
```

---

### Task 13: Identity, complete relay reads, member resolution, git artifact detection

**Files:**
- Create: `src/buzz_fleet/orchestration/identity.py`, `relay.py`, `git_artifact.py`
- Test: `tests/test_identity.py`, `tests/test_orch_relay.py`, `tests/test_git_artifact.py`

**Interfaces:**
- `identity.Identity(nsec, pubkey, relay_url, auth_tag, fleet_channel, retrieval_key, is_owner, owner_pubkey: str | None, record: FleetRecord | None)`; `resolve_identity(env, runner, community_id) -> Identity`. Agent path: env `BUZZ_PRIVATE_KEY`, `BUZZ_RELAY_URL`, `BUZZ_AUTH_TAG`, `BUZZ_FLEET_CHANNEL`, `BUZZ_FLEET_RETRIEVAL_KEY`, plus `BUZZ_ACP_AGENT_OWNER` as `owner_pubkey`; `record` None (agents do not need conductor keys to delegate). Owner path: local community (`community_id` or the single one), `record` from the community file.
- `relay.fleet_filter(channel_id, retrieval_key, *, until=None, since=None, limit=1000) -> dict`; `relay.fetch_all(runner, ident, filter) -> list[dict]` (paging by `until` until a page adds no new ids); `relay.fetch_fleet_events(runner, ident, *, channel_id, since=None) -> list[FleetEvent]`; `relay.fetch_thread(runner, ident, *, channel_id, root) -> list[FleetEvent]`; `relay.fetch_deleted_ids(runner, ident, *, channel_id) -> set[str]` (kinds 9005 and 5 by `authors=[owner_pubkey]`, collecting every `e` tag); `relay.load_state(runner, ident, *, channel_id) -> State`; `relay.resolve_member(runner, ident, channel_id, name_or_pubkey) -> tuple[str, str | None]`; `relay.post(runner, ident, channel_id, msg) -> str`.
- `git_artifact.detect(cwd: Path, run: Callable[[list[str]], subprocess.CompletedProcess[str]]) -> Artifact` raising `ValueError` for not-a-repo, dirty tree, no remote, or HEAD not on any remote branch (`git branch -r --contains HEAD` empty).

- [ ] **Step 1: Write the failing tests**

`tests/test_identity.py`:

```python
import json
import subprocess

import pytest

from buzz_fleet import state
from buzz_fleet.models import Community
from buzz_fleet.orchestration.identity import resolve_identity
from buzz_fleet.orchestration.record import FleetRecord

CH, RK = "6f1c0000-0000-4000-8000-000000000000", "r" * 64


class FakeRunner:
    def run(self, args):
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps({"ok": True, "public_key": "b" * 64}), stderr="")


def test_agent_identity_from_env() -> None:
    env = {"BUZZ_PRIVATE_KEY": "nsec1agent", "BUZZ_RELAY_URL": "wss://r", "BUZZ_AUTH_TAG": '["auth"]',
           "BUZZ_FLEET_CHANNEL": CH, "BUZZ_FLEET_RETRIEVAL_KEY": RK, "BUZZ_ACP_AGENT_OWNER": "0" * 64}
    ident = resolve_identity(env, FakeRunner(), community_id=None)
    assert (ident.nsec, ident.pubkey, ident.relay_url, ident.auth_tag) == ("nsec1agent", "b" * 64, "wss://r", '["auth"]')
    assert (ident.fleet_channel, ident.retrieval_key, ident.owner_pubkey, ident.is_owner) == (CH, RK, "0" * 64, False)


def test_owner_identity_from_local_state(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(state, "CONFIG_DIR", tmp_path)
    state.save_community(Community(id="e", relay_url="wss://r", relay_admin_nsec="nsec1admin", owner_pubkey="0" * 64,
                                   fleet_channel_id=CH, fleet_record=FleetRecord(retrieval_key=RK, created_at=1)))
    ident = resolve_identity({}, FakeRunner(), community_id=None)
    assert ident.is_owner and ident.pubkey == "0" * 64 and ident.retrieval_key == RK and ident.record is not None


def test_owner_identity_errors(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(state, "CONFIG_DIR", tmp_path)
    with pytest.raises(RuntimeError, match="connect"):
        resolve_identity({}, FakeRunner(), community_id=None)
    for cid in ("a", "b"):
        state.save_community(Community(id=cid, relay_url="wss://r", relay_admin_nsec="nsec1x", owner_pubkey="0" * 64))
    with pytest.raises(RuntimeError, match="--community"):
        resolve_identity({}, FakeRunner(), community_id=None)
```

`tests/test_orch_relay.py`:

```python
import json
import subprocess

import pytest

from buzz_fleet.orchestration import relay
from buzz_fleet.orchestration.identity import Identity
from buzz_fleet.orchestration.protocol import OutgoingMessage

CH, RK, OWNER = "6f1c0000-0000-4000-8000-000000000000", "r" * 64, "0" * 64
IDENT = Identity(nsec="nsec1a", pubkey="a" * 64, relay_url="wss://r", auth_tag=None, fleet_channel=CH,
                 retrieval_key=RK, is_owner=False, owner_pubkey=OWNER, record=None)


def _event(i: int, created_at: int) -> dict:
    return {"id": f"{i:064x}", "pubkey": "a" * 64, "created_at": created_at, "kind": 9, "content": "", "tags": [["p", RK]]}


class PagingRunner:
    """Serves a 2,500-event history 1,000 at a time, honouring `until` and `limit` like the relay."""

    def __init__(self, events: list[dict], responses: dict[str, str] | None = None) -> None:
        self.events = sorted(events, key=lambda e: -e["created_at"])
        self.responses = responses or {}
        self.calls: list[list[str]] = []

    def run(self, args):
        self.calls.append(args)
        if args[1] != "query":
            return subprocess.CompletedProcess(args, 0, stdout=self.responses[args[1]], stderr="")
        f = json.loads(args[args.index("--filter") + 1])
        page = [e for e in self.events if e["created_at"] <= f.get("until", 10**12) and e["created_at"] >= f.get("since", 0)]
        if "authors" in f:
            page = [e for e in page if e["pubkey"] in f["authors"]]
        page = page[: f.get("limit", 1000)]
        return subprocess.CompletedProcess(args, 0, stdout="".join(json.dumps(e) + "\n" for e in page), stderr="")


def test_fleet_filter_shape() -> None:
    assert relay.fleet_filter(CH, RK) == {"kinds": [9], "#h": [CH], "#p": [RK], "limit": 1000}
    assert relay.fleet_filter(CH, RK, until=7, since=2)["until"] == 7


def test_fetch_all_pages_past_the_relay_cap_with_timestamp_ties() -> None:
    events = [_event(i, 1000 + i // 3) for i in range(2500)]  # three events share each second
    runner = PagingRunner(events)
    got = relay.fetch_all(runner, IDENT, relay.fleet_filter(CH, RK))
    assert len({e["id"] for e in got}) == 2500
    assert len([c for c in runner.calls if c[1] == "query"]) >= 3


def test_fetch_deleted_ids_uses_owner_author() -> None:
    tomb = {"id": "9" * 64, "pubkey": OWNER, "created_at": 5, "kind": 9005, "content": "",
            "tags": [["h", CH], ["e", "1" * 64], ["e", "2" * 64]]}
    runner = PagingRunner([tomb])
    assert relay.fetch_deleted_ids(runner, IDENT, channel_id=CH) == {"1" * 64, "2" * 64}
    f = json.loads(runner.calls[0][runner.calls[0].index("--filter") + 1])
    assert f["authors"] == [OWNER] and set(f["kinds"]) == {9005, 5}


def test_resolve_member_by_name_pubkey_and_errors() -> None:
    members = json.dumps({"ok": True, "members": [{"pubkey": "b" * 64, "display_name": "Reviewer"},
                                                  {"pubkey": "c" * 64, "display_name": "reviewer"},
                                                  {"pubkey": "d" * 64, "display_name": "Builder"}]})
    runner = PagingRunner([], {"channel-members": members})
    assert relay.resolve_member(runner, IDENT, CH, "Builder") == ("d" * 64, "Builder")
    assert relay.resolve_member(runner, IDENT, CH, "@Builder") == ("d" * 64, "Builder")
    assert relay.resolve_member(runner, IDENT, CH, "d" * 64) == ("d" * 64, "Builder")
    with pytest.raises(RuntimeError, match="ambiguous"):
        relay.resolve_member(runner, IDENT, CH, "reviewer")
    with pytest.raises(RuntimeError, match="not a member"):
        relay.resolve_member(runner, IDENT, CH, "Nobody")


def test_post_passes_message_through_signer() -> None:
    runner = PagingRunner([], {"post-message": json.dumps({"ok": True, "event_id": "e" * 64})})
    msg = OutgoingMessage(content="hi", mentions=["b" * 64, RK], tags=[("t", "fleet")], root=None, parent=None)
    assert relay.post(runner, IDENT, CH, msg) == "e" * 64
    assert runner.calls[0].count("--mention") == 2
```

`tests/test_git_artifact.py`:

```python
import subprocess
from pathlib import Path

import pytest

from buzz_fleet.orchestration.git_artifact import detect


def _git(responses: dict[str, tuple[int, str]]):
    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        key = " ".join(args[1:])
        code, out = responses.get(key, (1, ""))
        return subprocess.CompletedProcess(args, code, stdout=out, stderr="")
    return run


CLEAN = {
    "rev-parse --is-inside-work-tree": (0, "true\n"),
    "status --porcelain": (0, ""),
    "rev-parse HEAD": (0, "c" * 40 + "\n"),
    "rev-parse --abbrev-ref HEAD": (0, "feat\n"),
    "remote get-url origin": (0, "git@github.com:o/r.git\n"),
    "branch -r --contains HEAD": (0, "  origin/feat\n"),
}


def test_detect_clean_pushed_checkout() -> None:
    art = detect(Path("/x"), _git(CLEAN))
    assert (art.repo, art.commit, art.branch) == ("git@github.com:o/r.git", "c" * 40, "feat")


@pytest.mark.parametrize("key,value,match", [
    ("rev-parse --is-inside-work-tree", (1, ""), "not a git"),
    ("status --porcelain", (0, " M file.py\n"), "dirty"),
    ("branch -r --contains HEAD", (0, ""), "not pushed"),
    ("remote get-url origin", (1, ""), "no remote"),
])
def test_detect_refusals(key, value, match) -> None:
    with pytest.raises(ValueError, match=match):
        detect(Path("/x"), _git({**CLEAN, key: value}))
```

- [ ] **Step 2: Run to verify failure**

`uv run pytest tests/test_identity.py tests/test_orch_relay.py tests/test_git_artifact.py -q`

- [ ] **Step 3: Implement**

`identity.py`:

```python
"""Who is running this command: a fleet agent (env from its unit) or the owner (local state)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from buzz_fleet import signer_client, state
from buzz_fleet.orchestration.record import FleetRecord
from buzz_fleet.proc import CommandRunner


@dataclass(frozen=True)
class Identity:
    nsec: str
    pubkey: str
    relay_url: str
    auth_tag: str | None
    fleet_channel: str | None
    retrieval_key: str | None
    is_owner: bool
    owner_pubkey: str | None
    record: FleetRecord | None


def resolve_identity(env: Mapping[str, str], runner: CommandRunner, community_id: str | None) -> Identity:
    nsec, relay_url = env.get("BUZZ_PRIVATE_KEY"), env.get("BUZZ_RELAY_URL")
    if nsec and relay_url:
        return Identity(nsec=nsec, pubkey=signer_client.pubkey_from_nsec(runner, nsec), relay_url=relay_url,
                        auth_tag=env.get("BUZZ_AUTH_TAG") or None, fleet_channel=env.get("BUZZ_FLEET_CHANNEL") or None,
                        retrieval_key=env.get("BUZZ_FLEET_RETRIEVAL_KEY") or None, is_owner=False,
                        owner_pubkey=env.get("BUZZ_ACP_AGENT_OWNER") or None, record=None)
    ids = state.list_community_ids()
    if community_id is None:
        if not ids:
            raise RuntimeError("no BUZZ_PRIVATE_KEY in the environment and no local community; run `buzz-fleet connect` first")
        if len(ids) > 1:
            raise RuntimeError(f"several local communities ({', '.join(ids)}); pass --community")
        community_id = ids[0]
    community = state.load_community(community_id)
    if community is None:
        raise RuntimeError(f"no community '{community_id}'; run `buzz-fleet connect` first")
    nsec = community.relay_admin_nsec.get_secret_value()
    pubkey = community.owner_pubkey or signer_client.pubkey_from_nsec(runner, nsec)
    return Identity(nsec=nsec, pubkey=pubkey, relay_url=community.relay_url, auth_tag=None,
                    fleet_channel=community.fleet_channel_id,
                    retrieval_key=community.fleet_record.retrieval_key if community.fleet_record else None,
                    is_owner=True, owner_pubkey=pubkey, record=community.fleet_record)
```

`relay.py`:

```python
"""Complete relay reads (paged, pushed-down filters only), member resolution, and posting. Spec 5.2."""

from __future__ import annotations

from buzz_fleet import signer_client
from buzz_fleet.orchestration.identity import Identity
from buzz_fleet.orchestration.protocol import FleetEvent, OutgoingMessage, parse_event
from buzz_fleet.orchestration.reducer import State, reduce
from buzz_fleet.proc import CommandRunner

PAGE = 1000


def fleet_filter(channel_id: str, retrieval_key: str, *, until: int | None = None, since: int | None = None,
                 limit: int = PAGE) -> dict:
    f: dict = {"kinds": [9], "#h": [channel_id], "#p": [retrieval_key], "limit": limit}
    if until is not None:
        f["until"] = until
    if since is not None:
        f["since"] = since
    return f


def fetch_all(runner: CommandRunner, ident: Identity, filter: dict) -> list[dict]:
    """Page by `until` until a page adds no new ids. `until` is inclusive, so the
    boundary second is re-fetched and de-duplicated rather than lost."""
    seen: dict[str, dict] = {}
    f = dict(filter)
    while True:
        page = signer_client.query(runner, ident.relay_url, ident.nsec, f, auth_tag=ident.auth_tag)
        new = [e for e in page if e["id"] not in seen]
        for e in new:
            seen[e["id"]] = e
        if not new or len(page) < f.get("limit", PAGE):
            break
        f["until"] = min(int(e["created_at"]) for e in page)
    return list(seen.values())


def _require(ident: Identity) -> tuple[str, str]:
    if not ident.fleet_channel or not ident.retrieval_key:
        raise RuntimeError("no fleet record known here; run `buzz-fleet fleet init` once, then any buzz-fleet command")
    return ident.fleet_channel, ident.retrieval_key


def fetch_fleet_events(runner: CommandRunner, ident: Identity, *, channel_id: str | None,
                       since: int | None = None) -> list[FleetEvent]:
    default_channel, rk = _require(ident)
    raw = fetch_all(runner, ident, fleet_filter(channel_id or default_channel, rk, since=since))
    return [parse_event(e) for e in raw]


def fetch_thread(runner: CommandRunner, ident: Identity, *, channel_id: str, root: str) -> list[FleetEvent]:
    replies = fetch_all(runner, ident, {"kinds": [9], "#h": [channel_id], "#e": [root], "limit": PAGE})
    root_ev = signer_client.query(runner, ident.relay_url, ident.nsec, {"kinds": [9], "#h": [channel_id], "ids": [root]},
                                  auth_tag=ident.auth_tag)
    return [parse_event(e) for e in [*root_ev, *replies]]


def fetch_deleted_ids(runner: CommandRunner, ident: Identity, *, channel_id: str) -> set[str]:
    if not ident.owner_pubkey:
        return set()
    tombs = fetch_all(runner, ident, {"kinds": [9005, 5], "#h": [channel_id], "authors": [ident.owner_pubkey], "limit": PAGE})
    return {t[1] for e in tombs for t in e.get("tags", []) if t and t[0] == "e" and len(t) > 1}


def load_state(runner: CommandRunner, ident: Identity, *, channel_id: str | None) -> State:
    default_channel, _ = _require(ident)
    channel = channel_id or default_channel
    events = fetch_fleet_events(runner, ident, channel_id=channel)
    deleted = fetch_deleted_ids(runner, ident, channel_id=channel)
    return reduce(events, ident.record, owner_pubkey=ident.owner_pubkey, deleted_ids=deleted)


def resolve_member(runner: CommandRunner, ident: Identity, channel_id: str, name_or_pubkey: str) -> tuple[str, str | None]:
    members = signer_client.channel_members(runner, ident.relay_url, ident.nsec, channel_id, auth_tag=ident.auth_tag)
    key = name_or_pubkey.strip().lstrip("@")
    if len(key) == 64 and all(c in "0123456789abcdefABCDEF" for c in key):
        for pubkey, name in members:
            if pubkey == key.lower():
                return pubkey, name
        raise RuntimeError(f"{key} is not a member of channel {channel_id}")
    matches = [(pk, n) for pk, n in members if n and n.strip().lower() == key.lower()]
    if len(matches) > 1:
        raise RuntimeError(f"name {key!r} is ambiguous in channel {channel_id}: "
                           + ", ".join(f"{n} ({pk[:12]}…)" for pk, n in matches) + "; pass a pubkey")
    if not matches:
        known = ", ".join(sorted(n for _, n in members if n)) or "no named members"
        raise RuntimeError(f"{key!r} is not a member of channel {channel_id} (members: {known})")
    return matches[0]


def post(runner: CommandRunner, ident: Identity, channel_id: str, msg: OutgoingMessage) -> str:
    return signer_client.post_message(runner, ident.relay_url, ident.nsec, channel_id, msg.content, mentions=msg.mentions,
                                      root=msg.root, parent=msg.parent, tags=msg.tags, auth_tag=ident.auth_tag)
```

`git_artifact.py`:

```python
"""Detect the exact revision a checkout is at; refuse anything a peer could not fetch."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

from buzz_fleet.orchestration.protocol import Artifact

Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]


def _git(run: Runner, cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return run(["git", "-C", str(cwd), *args])


def detect(cwd: Path, run: Runner) -> Artifact:
    if _git(run, cwd, "rev-parse", "--is-inside-work-tree").returncode != 0:
        raise ValueError(f"{cwd} is not a git checkout; pass --repo and --commit explicitly")
    if _git(run, cwd, "status", "--porcelain").stdout.strip():
        raise ValueError("checkout is dirty; commit or stash before delegating so the peer sees the same code")
    head = _git(run, cwd, "rev-parse", "HEAD").stdout.strip()
    branch = _git(run, cwd, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or None
    remote = _git(run, cwd, "remote", "get-url", "origin")
    if remote.returncode != 0 or not remote.stdout.strip():
        raise ValueError("no remote named origin; pass --repo explicitly")
    if not _git(run, cwd, "branch", "-r", "--contains", "HEAD").stdout.strip():
        raise ValueError(f"HEAD {head[:12]} is not pushed to any remote branch; push first")
    return Artifact(repo=remote.stdout.strip(), commit=head, branch=None if branch == "HEAD" else branch)
```

- [ ] **Step 4: Run tests and commit**

```bash
uv run pytest -q && uv run ruff check . && uv run mypy
git add src/buzz_fleet/orchestration tests && git commit -m "Add identity, complete paged relay reads, member resolution, and git artifact detection"
```

---

### Task 14: `buzz-fleet task delegate`, `ack`, `report`, `cancel`

**Files:**
- Modify: `src/buzz_fleet/cli/fleet_commands.py`, `src/buzz_fleet/cli/app.py`
- Test: `tests/test_task_cli.py`

**Interfaces:**
- Python (tested directly; the Typer commands are thin): `delegate_task(runner, ident, *, to, brief, wait_seconds, acceptance, artifact, run_id, thread_root, parent_task, required, channel, cwd, git_run, now) -> dict`, `ack_task(runner, ident, *, task_ref, channel) -> dict`, `report_task(runner, ident, *, task_ref, status, summary, next_task, input_commit, output_commit, evidence, channel) -> dict`, `cancel_task(runner, ident, *, task_ref, reason, channel) -> dict`.
- Output JSON: delegate `{"task", "attempt", "event_id", "deadline", "channel"}`; ack/report/cancel `{"task", "attempt", "event_id"}`. Errors: `{"error": "..."}` on stderr, exit 1.
- Limits: refuse when `state.open_adhoc_by_requester(ident.pubkey) >= limits.open_adhoc_per_requester` (no `--run`) or `state.chain_depth(parent_task) + 1 > limits.chain_depth`; limits from `ident.record` or the defaults.
- Ambiguous publish: `relay.post` raising `RuntimeError` whose message contains `timeout` → re-read the task by `fetch_thread` on the intended root (or the fleet events when there is no root) and, if an event with the same task and attempt exists, return success; else re-post the same message once.

- [ ] **Step 1: Write the failing tests**

```python
import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from buzz_fleet.cli import fleet_commands as fc
from buzz_fleet.cli.app import app
from buzz_fleet.orchestration.identity import Identity
from buzz_fleet.orchestration.protocol import Artifact

CH, RK, OWNER = "6f1c0000-0000-4000-8000-000000000000", "r" * 64, "0" * 64
A, B = "a" * 64, "b" * 64
AGENT = Identity(nsec="nsec1a", pubkey=A, relay_url="wss://r", auth_tag=None, fleet_channel=CH, retrieval_key=RK,
                 is_owner=False, owner_pubkey=OWNER, record=None)
REVIEWER = Identity(**{**AGENT.__dict__, "nsec": "nsec1b", "pubkey": B})
cli = CliRunner()
T1, AT1 = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"


class FakeRunner:
    def __init__(self, events: list[dict] | None = None, *, post_error: str | None = None) -> None:
        self.events, self.post_error, self.calls = events or [], post_error, []

    def run(self, args):
        self.calls.append(args)
        sub = args[1]
        if sub == "channel-members":
            out = json.dumps({"ok": True, "members": [{"pubkey": B, "display_name": "Reviewer"},
                                                       {"pubkey": A, "display_name": "Implementer"}]})
        elif sub == "post-message":
            if self.post_error and len([c for c in self.calls if c[1] == "post-message"]) == 1:
                return subprocess.CompletedProcess(args, 1, stdout=json.dumps({"ok": False, "error": self.post_error}), stderr="")
            out = json.dumps({"ok": True, "event_id": "e" * 64})
        elif sub == "query":
            f = json.loads(args[args.index("--filter") + 1])
            evs = self.events if "authors" not in f else []
            out = "".join(json.dumps(e) + "\n" for e in evs)
        elif sub == "pubkey-from-nsec":
            out = json.dumps({"ok": True, "public_key": A})
        else:
            raise AssertionError(sub)
        return subprocess.CompletedProcess(args, 0, stdout=out, stderr="")


def _delegate_event(task=T1, attempt=AT1, requester=A, assignee=B, run=None, parent=None) -> dict:
    payload = {"v": 1, "type": "delegate", "task": task, "attempt": attempt, "run": run, "step": None, "parent_task": parent,
               "required": True, "from": requester, "to": assignee, "deadline": 1000, "rework_target": None,
               "artifact": {"repo": "git@x:o/r.git", "commit": "c" * 40, "branch": None, "base": None}, "acceptance": []}
    return {"id": task[:8] * 8, "pubkey": requester, "created_at": 100, "kind": 9, "content": "brief",
            "tags": [["h", CH], ["p", RK], ["t", "fleet"], ["t", f"fleet:task:{task}"], ["p", assignee], ["fleet", json.dumps(payload)]]}


def _post(runner: FakeRunner) -> list[str]:
    return next(c for c in runner.calls if c[1] == "post-message")


def test_delegate_posts_and_returns_ids(monkeypatch) -> None:
    monkeypatch.setattr(fc.ids, "new_id", lambda: T1)
    runner = FakeRunner()
    out = fc.delegate_task(runner, AGENT, to="Reviewer", brief="Review it", wait_seconds=1800, acceptance=["tests pass"],
                           artifact=Artifact(repo="git@x:o/r.git", commit="c" * 40), run_id=None, thread_root="d" * 64,
                           parent_task=None, required=True, channel=None, cwd=None, git_run=None, now=1000)
    assert out["task"] == T1 and out["deadline"] == 2800 and out["channel"] == CH
    post = _post(runner)
    assert post[post.index("--mention") + 1] == B and RK in post and post[post.index("--root") + 1] == "d" * 64
    assert "c" * 40 in post[post.index("--content") + 1]


def test_delegate_detects_artifact_from_checkout(monkeypatch) -> None:
    monkeypatch.setattr(fc.git_artifact, "detect", lambda cwd, run: Artifact(repo="git@x:o/r.git", commit="d" * 40))
    runner = FakeRunner()
    fc.delegate_task(runner, AGENT, to="Reviewer", brief="x", wait_seconds=60, acceptance=[], artifact=None, run_id=None,
                     thread_root=None, parent_task=None, required=True, channel=None, cwd=Path("/x"), git_run=lambda a: None, now=1)
    assert "d" * 40 in _post(runner)[_post(runner).index("--content") + 1]


def test_delegate_enforces_adhoc_limits() -> None:
    open_tasks = [_delegate_event(task=f"{i:08d}-0000-4000-8000-000000000000", attempt=f"{i:08d}-1111-4111-8111-111111111111") for i in range(5)]
    runner = FakeRunner(events=open_tasks)
    with pytest.raises(RuntimeError, match="5 open"):
        fc.delegate_task(runner, AGENT, to="Reviewer", brief="x", wait_seconds=60, acceptance=[], artifact=None, run_id=None,
                         thread_root=None, parent_task=None, required=True, channel=None, cwd=None, git_run=None, now=1)


def test_delegate_requires_channel() -> None:
    ident = Identity(**{**AGENT.__dict__, "fleet_channel": None, "retrieval_key": None})
    with pytest.raises(RuntimeError, match="fleet init"):
        fc.delegate_task(FakeRunner(), ident, to="Reviewer", brief="x", wait_seconds=60, acceptance=[], artifact=None,
                         run_id=None, thread_root=None, parent_task=None, required=True, channel=None, cwd=None, git_run=None, now=1)


def test_delegate_recovers_from_ambiguous_publish(monkeypatch) -> None:
    monkeypatch.setattr(fc.ids, "new_id", lambda: T1)
    runner = FakeRunner(events=[_delegate_event()], post_error="publish timeout waiting for OK")
    out = fc.delegate_task(runner, AGENT, to="Reviewer", brief="x", wait_seconds=60, acceptance=[], artifact=None, run_id=None,
                           thread_root=None, parent_task=None, required=True, channel=None, cwd=None, git_run=None, now=1)
    assert out["task"] == T1 and len([c for c in runner.calls if c[1] == "post-message"]) == 1


def test_ack_and_report_thread_and_mention_requester() -> None:
    runner = FakeRunner(events=[_delegate_event()])
    fc.ack_task(runner, REVIEWER, task_ref=T1[:8], channel=None)
    post = _post(runner)
    assert post[post.index("--root") + 1] == T1[:8] * 8 and post[post.index("--mention") + 1] == RK
    runner = FakeRunner(events=[_delegate_event()])
    out = fc.report_task(runner, REVIEWER, task_ref=T1, status="done", summary="Looks good", next_task="default",
                         input_commit="c" * 40, output_commit=None, evidence=[], channel=None)
    assert out["task"] == T1
    post = _post(runner)
    assert post[post.index("--mention") + 1] == A and post[post.index("--content") + 1].startswith("@Implementer ✅")


def test_report_refusals() -> None:
    runner = FakeRunner(events=[_delegate_event()])
    with pytest.raises(RuntimeError, match="assigned to"):
        fc.report_task(runner, AGENT, task_ref=T1, status="done", summary="x", next_task="default", input_commit=None,
                       output_commit=None, evidence=[], channel=None)
    with pytest.raises(RuntimeError, match="input-commit"):
        fc.report_task(runner, REVIEWER, task_ref=T1, status="done", summary="x", next_task="default", input_commit="9" * 40,
                       output_commit=None, evidence=[], channel=None)
    with pytest.raises(RuntimeError, match="unknown id"):
        fc.report_task(runner, REVIEWER, task_ref="zzzzzzzz", status="done", summary="x", next_task="default",
                       input_commit=None, output_commit=None, evidence=[], channel=None)


def test_cancel_by_requester_only() -> None:
    runner = FakeRunner(events=[_delegate_event()])
    assert fc.cancel_task(runner, AGENT, task_ref=T1, reason="obsolete", channel=None)["task"] == T1
    with pytest.raises(RuntimeError, match="requester or the owner"):
        fc.cancel_task(FakeRunner(events=[_delegate_event()]), REVIEWER, task_ref=T1, reason="x", channel=None)


def test_cli_delegate_reads_stdin_and_prints_json(monkeypatch) -> None:
    monkeypatch.setattr(fc, "RealCommandRunner", lambda: FakeRunner())
    monkeypatch.setattr(fc, "resolve_identity", lambda env, runner, community_id: AGENT)
    result = cli.invoke(app, ["task", "delegate", "--to", "Reviewer", "--brief", "-", "--wait", "5m",
                              "--repo", "git@x:o/r.git", "--commit", "c" * 40], input="Review please\n")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["channel"] == CH


def test_cli_report_rejects_bad_status_and_next() -> None:
    assert cli.invoke(app, ["task", "report", "--task", T1, "--status", "maybe", "--summary", "x"]).exit_code == 1
    assert cli.invoke(app, ["task", "report", "--task", T1, "--status", "done", "--summary", "x", "--next", "bogus"]).exit_code == 1
```

- [ ] **Step 2: Run to verify failure**

`uv run pytest tests/test_task_cli.py -q`

- [ ] **Step 3: Implement**

Append to `fleet_commands.py`:

```python
import json
import os
import sys
import time
from pathlib import Path
from typing import Literal

from buzz_fleet.orchestration import git_artifact, ids, protocol, relay
from buzz_fleet.orchestration.durations import parse_duration
from buzz_fleet.orchestration.identity import Identity, resolve_identity
from buzz_fleet.orchestration.protocol import Artifact
from buzz_fleet.orchestration.record import Limits
from buzz_fleet.orchestration.reducer import State, Task
from buzz_fleet.proc import CommandRunner

task_app = typer.Typer(help="Delegate work to fleet agents, ack it, report it, cancel it")


def _read_text_arg(value: str) -> str:
    return sys.stdin.read() if value == "-" else value


def _channel(ident: Identity, explicit: str | None) -> str:
    channel = explicit or ident.fleet_channel
    if not channel or not ident.retrieval_key:
        raise RuntimeError("no fleet record known here; run `buzz-fleet fleet init` once, then any buzz-fleet command")
    return channel


def _limits(ident: Identity) -> Limits:
    return ident.record.limits if ident.record else Limits()


def _find_task(state: State, task_ref: str) -> Task:
    return state.tasks[ids.match_prefix(task_ref, state.tasks)]


def _post_idempotent(runner: CommandRunner, ident: Identity, channel: str, msg: protocol.OutgoingMessage,
                     task_id: str, attempt_id: str) -> str:
    """Publish once; on an ambiguous result, look for the same task+attempt before retrying the same message."""
    try:
        return relay.post(runner, ident, channel, msg)
    except RuntimeError as e:
        if "timeout" not in str(e).lower():
            raise
        for ev in relay.fetch_fleet_events(runner, ident, channel_id=channel):
            if ev.payload and ev.payload.get("task") == task_id and ev.payload.get("attempt") == attempt_id \
                    and ev.type == msg_type(msg):
                return ev.id
        return relay.post(runner, ident, channel, msg)


def msg_type(msg: protocol.OutgoingMessage) -> str:
    return json.loads(dict(msg.tags)["fleet"])["type"]


def delegate_task(runner: CommandRunner, ident: Identity, *, to: str, brief: str, wait_seconds: int, acceptance: list[str],
                  artifact: Artifact | None, run_id: str | None, thread_root: str | None, parent_task: str | None,
                  required: bool, channel: str | None, cwd: Path | None, git_run, now: int) -> dict:
    channel_id = _channel(ident, channel)
    assert ident.retrieval_key
    state = relay.load_state(runner, ident, channel_id=channel_id)
    limits = _limits(ident)
    if run_id is None and state.open_adhoc_by_requester(ident.pubkey) >= limits.open_adhoc_per_requester:
        raise RuntimeError(f"you already have {limits.open_adhoc_per_requester} open ad-hoc tasks; report or cancel one first")
    if parent_task:
        parent_task = ids.match_prefix(parent_task, state.tasks)
        if state.chain_depth(parent_task) + 1 > limits.chain_depth:
            raise RuntimeError(f"delegation chain would exceed depth {limits.chain_depth}")
    if artifact is None and cwd is not None and git_run is not None:
        artifact = git_artifact.detect(cwd, git_run)
    to_pubkey, to_name = relay.resolve_member(runner, ident, channel_id, to)
    root = thread_root
    if run_id and not root:
        run_events = [e for e in relay.fetch_fleet_events(runner, ident, channel_id=channel_id) if e.run_id == run_id]
        if run_events:
            first = min(run_events, key=lambda e: (e.created_at, e.id))
            root = first.root or first.id
    task_id, attempt_id = ids.new_id(), ids.new_id()
    deadline = now + wait_seconds
    msg = protocol.build_delegate(task_id=task_id, attempt_id=attempt_id, from_pubkey=ident.pubkey, to_pubkey=to_pubkey,
                                  to_name=to_name or to, retrieval_key=ident.retrieval_key, brief=brief, deadline=deadline,
                                  acceptance=acceptance, artifact=artifact, run_id=run_id, step=None, parent_task=parent_task,
                                  required=required, rework_target=None, default_next=None, thread_root=root, thread_parent=root)
    event_id = _post_idempotent(runner, ident, channel_id, msg, task_id, attempt_id)
    return {"task": task_id, "attempt": attempt_id, "event_id": event_id, "deadline": deadline, "channel": channel_id}


def _load_task(runner: CommandRunner, ident: Identity, task_ref: str, channel: str | None) -> tuple[Task, str]:
    channel_id = _channel(ident, channel)
    task = _find_task(relay.load_state(runner, ident, channel_id=channel_id), task_ref)
    return task, task.channel_id or channel_id


def ack_task(runner: CommandRunner, ident: Identity, *, task_ref: str, channel: str | None) -> dict:
    task, channel_id = _load_task(runner, ident, task_ref, channel)
    if ident.pubkey != task.assignee:
        raise RuntimeError(f"task {ids.short(task.task_id)} is assigned to {task.assignee[:12]}…, not to you")
    assert ident.retrieval_key
    msg = protocol.build_ack(task_id=task.task_id, attempt_id=task.current.attempt_id, from_pubkey=ident.pubkey,
                             retrieval_key=ident.retrieval_key, root=task.root_event_id, parent=task.delegate_event_id)
    return {"task": task.task_id, "attempt": task.current.attempt_id,
            "event_id": _post_idempotent(runner, ident, channel_id, msg, task.task_id, task.current.attempt_id)}


def report_task(runner: CommandRunner, ident: Identity, *, task_ref: str, status: Literal["done", "blocked", "failed"],
                summary: str, next_task: str, input_commit: str | None, output_commit: str | None, evidence: list[str],
                channel: str | None) -> dict:
    task, channel_id = _load_task(runner, ident, task_ref, channel)
    if ident.pubkey != task.assignee:
        raise RuntimeError(f"task {ids.short(task.task_id)} is assigned to {task.assignee[:12]}…, not to you ({ident.pubkey[:12]}…)")
    if not task.is_live:
        raise RuntimeError(f"task {ids.short(task.task_id)} is {task.status}; nothing to report")
    expected = (task.artifact or {}).get("commit")
    if expected and input_commit != expected:
        raise RuntimeError(f"--input-commit must be {expected} (the commit you were given); got {input_commit!r}")
    assert ident.retrieval_key
    recipient = task.rework_target if status == "failed" and task.rework_target else task.requester
    _, recipient_name = relay.resolve_member(runner, ident, channel_id, recipient)
    msg = protocol.build_report(task_id=task.task_id, attempt_id=task.current.attempt_id, status=status, summary=summary,
                                from_pubkey=ident.pubkey, recipient_pubkey=recipient, recipient_name=recipient_name,
                                retrieval_key=ident.retrieval_key, next_task=next_task, input_commit=input_commit,
                                output_commit=output_commit, evidence=evidence, run_id=task.run_id,
                                root=task.root_event_id, parent=task.delegate_event_id)
    return {"task": task.task_id, "attempt": task.current.attempt_id,
            "event_id": _post_idempotent(runner, ident, channel_id, msg, task.task_id, task.current.attempt_id)}


def cancel_task(runner: CommandRunner, ident: Identity, *, task_ref: str, reason: str, channel: str | None) -> dict:
    task, channel_id = _load_task(runner, ident, task_ref, channel)
    if ident.pubkey not in (task.requester, ident.owner_pubkey if ident.is_owner else None):
        raise RuntimeError("only the requester or the owner may cancel a task")
    assert ident.retrieval_key
    msg = protocol.build_cancel_task(task_id=task.task_id, reason=reason, from_pubkey=ident.pubkey,
                                     assignee_pubkey=task.assignee, retrieval_key=ident.retrieval_key,
                                     root=task.root_event_id, parent=task.delegate_event_id)
    return {"task": task.task_id, "attempt": task.current.attempt_id,
            "event_id": _post_idempotent(runner, ident, channel_id, msg, task.task_id, task.current.attempt_id)}


def _fail(e: Exception) -> None:
    typer.echo(json.dumps({"error": str(e)}), err=True)
    raise typer.Exit(code=1)


_ERRORS = (RuntimeError, ValueError, json.JSONDecodeError, KeyError)


@task_app.command("delegate")
def task_delegate(
    to: Annotated[str, typer.Option(help="Agent display name or hex pubkey")],
    brief: Annotated[str, typer.Option(help="Task text; '-' reads stdin")],
    wait: Annotated[str, typer.Option(help="Deadline from now, e.g. 45m, 2h")] = "60m",
    repo: Annotated[str | None, typer.Option()] = None,
    commit: Annotated[str | None, typer.Option()] = None,
    branch: Annotated[str | None, typer.Option()] = None,
    base: Annotated[str | None, typer.Option()] = None,
    accept: Annotated[list[str] | None, typer.Option(help="Acceptance criterion (repeatable)")] = None,
    run: Annotated[str | None, typer.Option()] = None,
    thread: Annotated[str | None, typer.Option(help="Root event id of the thread to reply in")] = None,
    parent: Annotated[str | None, typer.Option(help="Parent task id or prefix")] = None,
    optional: Annotated[bool, typer.Option("--optional")] = False,
    channel: Annotated[str | None, typer.Option()] = None,
    community: Annotated[str | None, typer.Option()] = None,
) -> None:
    if (repo is None) != (commit is None):
        _fail(RuntimeError("--repo and --commit go together"))
    runner = RealCommandRunner()
    try:
        ident = resolve_identity(os.environ, runner, community)
        artifact = Artifact(repo=repo, commit=commit, branch=branch, base=base) if repo and commit else None
        out = delegate_task(runner, ident, to=to, brief=_read_text_arg(brief), wait_seconds=parse_duration(wait),
                            acceptance=accept or [], artifact=artifact, run_id=run, thread_root=thread, parent_task=parent,
                            required=not optional, channel=channel,
                            cwd=None if artifact else Path.cwd(), git_run=None if artifact else runner.run, now=int(time.time()))
    except _ERRORS as e:
        _fail(e)
        return
    typer.echo(json.dumps(out))


@task_app.command("ack")
def task_ack(task: Annotated[str, typer.Option()], channel: Annotated[str | None, typer.Option()] = None,
             community: Annotated[str | None, typer.Option()] = None) -> None:
    runner = RealCommandRunner()
    try:
        out = ack_task(runner, resolve_identity(os.environ, runner, community), task_ref=task, channel=channel)
    except _ERRORS as e:
        _fail(e)
        return
    typer.echo(json.dumps(out))


@task_app.command("report")
def task_report(
    task: Annotated[str, typer.Option()],
    status: Annotated[str, typer.Option(help="done, blocked, or failed")],
    summary: Annotated[str, typer.Option(help="Outcome text; '-' reads stdin")],
    next: Annotated[str, typer.Option(help="default, none, or a task id you delegated onward")] = "default",
    input_commit: Annotated[str | None, typer.Option()] = None,
    output_commit: Annotated[str | None, typer.Option()] = None,
    evidence: Annotated[list[str] | None, typer.Option(help="Evidence line (repeatable)")] = None,
    channel: Annotated[str | None, typer.Option()] = None,
    community: Annotated[str | None, typer.Option()] = None,
) -> None:
    if status not in ("done", "blocked", "failed"):
        _fail(RuntimeError("--status must be done, blocked, or failed"))
    if next not in ("default", "none") and len(next) < 8:
        _fail(RuntimeError("--next must be default, none, or a task id"))
    runner = RealCommandRunner()
    try:
        out = report_task(runner, resolve_identity(os.environ, runner, community), task_ref=task, status=status,  # type: ignore[arg-type]
                          summary=_read_text_arg(summary), next_task=next, input_commit=input_commit,
                          output_commit=output_commit, evidence=evidence or [], channel=channel)
    except _ERRORS as e:
        _fail(e)
        return
    typer.echo(json.dumps(out))


@task_app.command("cancel")
def task_cancel(task_id: Annotated[str, typer.Argument()], reason: Annotated[str, typer.Option()],
                channel: Annotated[str | None, typer.Option()] = None,
                community: Annotated[str | None, typer.Option()] = None) -> None:
    runner = RealCommandRunner()
    try:
        out = cancel_task(runner, resolve_identity(os.environ, runner, community), task_ref=task_id, reason=reason, channel=channel)
    except _ERRORS as e:
        _fail(e)
        return
    typer.echo(json.dumps(out))
```

Register in `cli/app.py`: `from buzz_fleet.cli.fleet_commands import fleet_app, task_app` and `app.add_typer(task_app, name="task")`.

- [ ] **Step 4: Run tests, then verify live with a real agent**

```bash
uv run pytest -q && uv run ruff check . && uv run mypy
uv run buzz-fleet task delegate --to "<agent display name>" --wait 10m \
  --brief "Ack this task, then report done with the summary 'pong'. Use --input-commit $(git rev-parse HEAD)."
uv run buzz-fleet tasks   # after Task 15; until then, watch the fleet channel in Desktop
```

Expected within a couple of minutes: the agent's ack and report appear in the thread, mentioning you. If the agent replies in chat but never runs the commands, read `journalctl --user -u buzz-agent@<id> -n 200` for the failing invocation before continuing. Run this once against the Codex agent (`my-dotnet-cdx`) as well: that is spec live check 2.

- [ ] **Step 5: Commit**

```bash
git add src/buzz_fleet/cli tests/test_task_cli.py && git commit -m "Add buzz-fleet task delegate, ack, report, and cancel"
```

---

### Task 15: Views: `task show` and `tasks`

**Files:**
- Modify: `src/buzz_fleet/cli/fleet_commands.py`, `src/buzz_fleet/cli/app.py`
- Test: `tests/test_task_cli.py`

**Interfaces:**
- `buzz-fleet tasks [--open] [--stuck] [--unacked] [--mine] [--channel] [--community] [--json]`; `buzz-fleet task show <id-or-prefix> [--channel] [--community] [--json]`.
- `task_rows(state, *, only_open, only_stuck, only_unacked, mine: str | None, now) -> list[Task]`; `render_tasks(tasks, now) -> rich.table.Table`; `task_to_json(task) -> dict`.

- [ ] **Step 1: Write the failing tests**

```python
from buzz_fleet.orchestration.protocol import parse_event  # noqa: E402
from buzz_fleet.orchestration.reducer import reduce  # noqa: E402

T2, AT2 = "44444444-4444-4444-8444-444444444444", "55555555-5555-4555-8555-555555555555"


def _state():
    return reduce([parse_event(_delegate_event()), parse_event(_delegate_event(task=T2, attempt=AT2, requester=B, assignee=A))], None)


def test_task_rows_filters() -> None:
    s = _state()
    assert {t.task_id for t in fc.task_rows(s, only_open=True, only_stuck=False, only_unacked=False, mine=None, now=500)} == {T1, T2}
    assert {t.task_id for t in fc.task_rows(s, only_open=False, only_stuck=True, only_unacked=False, mine=None, now=5000)} == {T1, T2}
    assert fc.task_rows(s, only_open=False, only_stuck=True, only_unacked=False, mine=None, now=500) == []
    assert [t.task_id for t in fc.task_rows(s, only_open=False, only_stuck=False, only_unacked=False, mine=A, now=500)] == [T2]


def test_cli_tasks_json_and_show(monkeypatch) -> None:
    monkeypatch.setattr(fc, "RealCommandRunner", lambda: FakeRunner(events=[_delegate_event()]))
    monkeypatch.setattr(fc, "resolve_identity", lambda env, runner, community_id: AGENT)
    result = cli.invoke(app, ["tasks", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)[0]["task_id"] == T1
    result = cli.invoke(app, ["task", "show", T1[:8]])
    assert result.exit_code == 0 and "11111111" in result.output and "open" in result.output
```

- [ ] **Step 2: Run to verify failure**

`uv run pytest tests/test_task_cli.py -q`

- [ ] **Step 3: Implement**

```python
from dataclasses import asdict

from rich.console import Console
from rich.table import Table


def task_rows(state: State, *, only_open: bool, only_stuck: bool, only_unacked: bool, mine: str | None, now: int) -> list[Task]:
    rows = list(state.tasks.values())
    if only_open:
        rows = [t for t in rows if t.is_live]
    if only_stuck:
        rows = [t for t in rows if t.late(now)]
    if only_unacked:
        rows = [t for t in rows if t.unacked]
    if mine:
        rows = [t for t in rows if t.assignee == mine]
    return sorted(rows, key=lambda t: t.created_at, reverse=True)


def _age(seconds: int) -> str:
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"
    return f"{seconds // 86400}d"


def render_tasks(tasks: list[Task], now: int) -> Table:
    table = Table(title="Fleet tasks")
    for col in ("Task", "Run", "Status", "Assignee", "Requester", "Age", "Deadline", "Attempt", "Summary"):
        table.add_column(col)
    for t in tasks:
        remaining = t.deadline - now
        deadline = (f"in {_age(remaining)}" if remaining > 0 else f"{_age(-remaining)} overdue") if t.is_live else "-"
        summary = (t.current.report or {}).get("content") or t.brief
        table.add_row(ids.short(t.task_id), ids.short(t.run_id) if t.run_id else "-", t.status, t.assignee[:8],
                      t.requester[:8], _age(now - t.created_at), deadline, str(len(t.attempts)), summary.splitlines()[0][:60])
    return table


def task_to_json(task: Task) -> dict:
    return asdict(task)


def tasks_command(
    open_only: Annotated[bool, typer.Option("--open")] = False,
    stuck: Annotated[bool, typer.Option("--stuck")] = False,
    unacked: Annotated[bool, typer.Option("--unacked")] = False,
    mine: Annotated[bool, typer.Option("--mine")] = False,
    channel: Annotated[str | None, typer.Option()] = None,
    community: Annotated[str | None, typer.Option()] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    runner, now = RealCommandRunner(), int(time.time())
    try:
        ident = resolve_identity(os.environ, runner, community)
        state = relay.load_state(runner, ident, channel_id=channel)
    except _ERRORS as e:
        _fail(e)
        return
    rows = task_rows(state, only_open=open_only, only_stuck=stuck, only_unacked=unacked, mine=ident.pubkey if mine else None, now=now)
    if as_json:
        typer.echo(json.dumps([task_to_json(t) for t in rows]))
        return
    Console().print(render_tasks(rows, now))


@task_app.command("show")
def task_show(task_ref: Annotated[str, typer.Argument()], channel: Annotated[str | None, typer.Option()] = None,
              community: Annotated[str | None, typer.Option()] = None,
              as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    runner, now = RealCommandRunner(), int(time.time())
    try:
        ident = resolve_identity(os.environ, runner, community)
        task, _ = _load_task(runner, ident, task_ref, channel)
    except _ERRORS as e:
        _fail(e)
        return
    if as_json:
        typer.echo(json.dumps(task_to_json(task)))
        return
    console = Console()
    console.print(render_tasks([task], now))
    console.print(f"[bold]Brief[/bold]\n{task.brief}")
    for i, a in enumerate(task.attempts, 1):
        console.print(f"[bold]Attempt {i}[/bold] {a.assignee[:8]} {a.status}"
                      + (f" acked {_age(now - a.acked_at)} ago" if a.acked_at else " (not acked)"))
        if a.report:
            console.print(a.report.get("content", ""))
    for note in task.notes:
        console.print(f"[dim]note: {note}[/dim]")
```

Register `app.command("tasks")(tasks_command)` in `cli/app.py`.

- [ ] **Step 4: Run tests, verify live, commit**

```bash
uv run pytest -q && uv run ruff check . && uv run mypy
uv run buzz-fleet tasks && uv run buzz-fleet task show <prefix from Task 14>
git add src/buzz_fleet/cli tests/test_task_cli.py && git commit -m "Add buzz-fleet tasks and task show views"
```

---

### Task 16: Documentation and release

**Files:**
- Modify: `README.md`, `CLAUDE.md`, version files per the release workflow

- [ ] **Step 1: README**

Add `## Orchestration` after the agents section: one-time `fleet init` on the VPS and automatic discovery elsewhere; what agents get (PATH, working directory, coordination block, `BUZZ_FLEET_*`, session policy, turn cap, heartbeat) and how to override per agent; `task delegate/ack/report/cancel/show` and `tasks --stuck/--unacked` with one example each; the artifact rule (push first, exact commit, `--input-commit` on reports); ad-hoc limits; unique display names and `--force`; the per-machine SSH prerequisite; "Conductor and pipelines: plans 2 and 3, see the spec".

- [ ] **Step 2: CLAUDE.md**

Under "Self-healing runtime", add incidents 8 (`buzz` not on PATH) and 9 (multi-line env truncation) in the existing style. Under "Documentation debt", add: "Orchestration layers 2 and 3 not yet built: conductor, notifier, failover, metrics, recycling, pipelines, purge, TUI screen, self-update. Spec section 4."

- [ ] **Step 3: Full check and commit**

```bash
uv run pytest -q && uv run ruff check . && uv run mypy && (cd signer && cargo test)
git add README.md CLAUDE.md && git commit -m "Document the fleet channel and task commands"
```

- [ ] **Step 4: Release 0.8.0**

Follow the project's release workflow exactly (memory `feedback_buzz-fleet-release-workflow`): bump `pyproject.toml`, `src/buzz_fleet/__init__.py`, `signer/Cargo.toml` to `0.8.0`; `cargo build --release` in `signer/`; commit `chore: bump version to 0.8.0` including `signer/Cargo.lock` and `uv.lock`; tag `v0.8.0`; push commit and tag; confirm `gh run list --workflow=release.yml --limit 3`. Then on each other machine: reinstall via `get.sh`, run `buzz-fleet agent list --community <id>` once (discovers the record, joins agents, rewrites env), and delegate one task to an agent on that machine from this one.

---


---

### Task 17: Agent directory fields in the model and the managed-agent record

**Files:**
- Modify: `src/buzz_fleet/models.py`, `src/buzz_fleet/visibility.py`, `src/buzz_fleet/personas.py`, `src/buzz_fleet/manager.py`, `src/buzz_fleet/cli/app.py`, `src/buzz_fleet/tui/screens/agent_form.py`
- Test: `tests/test_models.py`, `tests/test_visibility.py`, `tests/test_personas.py`, `tests/test_cli.py`

**Interfaces:**
- `Agent.role: str | None`, `Agent.capabilities: list[str] | None`, `Agent.description: str | None`.
- `visibility.managed_agent_content(agent)` adds `"role"`, `"capabilities"`, `"description"` (null/empty when unset) next to `"host"`.
- Persona import (`personas.py`) maps the persona's `description` to `Agent.description`; `agent create/update` gain `--role`, `--capability` (repeatable), `--description`; the TUI form gains the three inputs.
- A change to any of the three republishes the managed-agent record (`update_agent` already republishes on content-field changes; add these to `content_fields`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_models.py
def test_directory_fields_round_trip() -> None:
    agent = Agent(**_base_kwargs(), role="reviewer", capabilities=["laravel", "security-review"], description="Reviews PHP.")
    again = Agent.model_validate_json(agent.model_dump_json())
    assert (again.role, again.capabilities, again.description) == ("reviewer", ["laravel", "security-review"], "Reviews PHP.")


# tests/test_visibility.py
def test_managed_agent_content_includes_directory_fields() -> None:
    agent = _agent().model_copy(update={"role": "reviewer", "capabilities": ["laravel"], "description": "Reviews."})
    content = managed_agent_content(agent)
    assert content["role"] == "reviewer" and content["capabilities"] == ["laravel"] and content["description"] == "Reviews."
    empty = managed_agent_content(_agent())
    assert empty["role"] is None and empty["capabilities"] == [] and empty["description"] is None


# tests/test_personas.py  (model on the existing template-import test in that file)
def test_persona_description_is_imported() -> None:
    template = load_persona_template(PERSONA_WITH_DESCRIPTION_PATH)
    assert template.description == "Laravel/PHP backend specialist."


# tests/test_cli.py
def test_agent_create_passes_directory_flags(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeManager:
        def create_agent(self, **kwargs):
            captured.update(kwargs)
            return _agent()

    monkeypatch.setattr("buzz_fleet.cli.app._load_manager", lambda community: FakeManager())
    result = runner_cli.invoke(app, ["agent", "create", "--community", "e", "--display-name", "X", "--harness", "claude",
                                     "--prompt-file", "/dev/null", "--role", "reviewer", "--capability", "laravel",
                                     "--capability", "docker-build", "--description", "Reviews."])
    assert result.exit_code == 0, result.output
    assert (captured["role"], captured["capabilities"], captured["description"]) == ("reviewer", ["laravel", "docker-build"], "Reviews.")
```

Read `tests/test_personas.py` first for the template fixture name and the loader function's real name, and use those.

- [ ] **Step 2: Run to verify failure**

`uv run pytest tests/test_models.py tests/test_visibility.py tests/test_personas.py tests/test_cli.py -q`

- [ ] **Step 3: Implement**

`models.py` `Agent` (after `heartbeat_interval_seconds`):

```python
    # Agent directory (spec 5.10): published in the managed-agent record so
    # every machine and every agent can choose agents by role and capability.
    role: str | None = None
    capabilities: list[str] | None = None
    description: str | None = None
```

`visibility.py` `managed_agent_content`: add `"role": agent.role, "capabilities": list(agent.capabilities or []), "description": agent.description`. `personas.py`: carry the persona `description` field through the template model and into `create_agent(description=...)` where templates are applied. `manager.py` `create_agent`: add `role`, `capabilities`, `description` params → `Agent(...)`; in `update_agent` add the three names to `content_fields`. `cli/app.py`: `--role`, `--capability` (`list[str] | None`), `--description` on create and update. TUI: three inputs in the Identity section (`#role-input`, `#capabilities-input` comma-separated, `#description-input`), parsed in the save handler.

- [ ] **Step 4: Run tests and commit**

```bash
uv run pytest -q && uv run ruff check . && uv run mypy
git add src/buzz_fleet tests && git commit -m "Add role, capabilities, and description to agents and the managed-agent record"
```

---

### Task 18: `buzz-fleet fleet agents`

**Files:**
- Modify: `signer/src/fleet.rs`, `signer/src/main.rs` (`read-managed-agents`, `read-presence`)
- Modify: `src/buzz_fleet/signer_client.py`, `src/buzz_fleet/orchestration/relay.py`, `src/buzz_fleet/cli/fleet_commands.py`
- Test: `tests/test_signer_client.py`, `tests/test_orch_relay.py`, `tests/test_task_cli.py`

**Interfaces:**
- Signer `read-managed-agents --relay --nsec [--auth-tag] --owner <hex>` → `{"ok":true,"agents":[{"pubkey","content":{...}}]}` from kind 30177 events authored by the owner (`authors` pushed, `#d` = agent pubkey inside content per `visibility.py`); `read-presence --relay --nsec [--auth-tag] --pubkey <hex>...` → `{"ok":true,"presence":[{"pubkey","status","updated_at"}]}` from kind 40902 snapshots (read `crates/buzz-core/src/kind.rs` around `KIND_PRESENCE_SNAPSHOT` for the exact shape before writing the parser).
- Python: `signer_client.read_managed_agents(runner, relay_url, nsec, *, owner, auth_tag) -> list[dict]`, `signer_client.read_presence(runner, relay_url, nsec, *, pubkeys, auth_tag) -> list[dict]`; `relay.directory(runner, ident, *, channel_id) -> list[DirectoryEntry]` with `DirectoryEntry(pubkey, display_name, role, capabilities, description, harness, host, online: bool | None, last_seen: int | None, live_tasks: int, version: str | None)` joining channel members, managed-agent records, presence, and `load_state`; CLI `buzz-fleet fleet agents [--json] [--community]` rendering a table sorted by display name.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_orch_relay.py
def test_directory_joins_members_records_presence_and_load() -> None:
    members = json.dumps({"ok": True, "members": [{"pubkey": B, "display_name": "Reviewer"}]})
    records = json.dumps({"ok": True, "agents": [{"pubkey": B, "content": {"role": "reviewer", "capabilities": ["laravel"],
                                                                            "description": "Reviews.", "harness": "claude", "host": "vps", "version": "0.8.0"}}]})
    presence = json.dumps({"ok": True, "presence": [{"pubkey": B, "status": "online", "updated_at": 1700}]})
    runner = PagingRunner([_event(1, 1000) | {"tags": [["p", RK], ["fleet", json.dumps({"v": 1, "type": "delegate", "task": "t", "attempt": "a", "from": "a" * 64, "to": B, "deadline": 9, "required": True, "acceptance": []})], ["t", "fleet:task:t"]]}],
                          {"channel-members": members, "read-managed-agents": records, "read-presence": presence})
    ident = Identity(**{**IDENT.__dict__, "owner_pubkey": OWNER})
    [entry] = relay.directory(runner, ident, channel_id=CH)
    assert (entry.display_name, entry.role, entry.capabilities, entry.host, entry.online, entry.live_tasks) == ("Reviewer", "reviewer", ["laravel"], "vps", True, 1)


# tests/test_task_cli.py
def test_cli_fleet_agents_json(monkeypatch) -> None:
    from buzz_fleet.orchestration.relay import DirectoryEntry

    monkeypatch.setattr(fc, "_load_manager", lambda community: type("M", (), {"ensure_fleet_record": lambda self: None, "_community": None})())
    monkeypatch.setattr(fc, "RealCommandRunner", lambda: FakeRunner())
    monkeypatch.setattr(fc, "resolve_identity", lambda env, runner, community_id: AGENT)
    monkeypatch.setattr(fc.relay, "directory", lambda runner, ident, channel_id: [DirectoryEntry(
        pubkey=B, display_name="Reviewer", role="reviewer", capabilities=["laravel"], description=None, harness="claude",
        host="vps", online=True, last_seen=1, live_tasks=0, version="0.8.0")])
    result = cli.invoke(app, ["fleet", "agents", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)[0]["role"] == "reviewer"
```

Add `read_managed_agents`/`read_presence` argv tests to `tests/test_signer_client.py` in the style of Task 7.

- [ ] **Step 2: Run to verify failure**

`uv run pytest tests/test_orch_relay.py tests/test_task_cli.py tests/test_signer_client.py -q`

- [ ] **Step 3: Implement**

Signer: two subcommands following Task 6's pattern, using `collect_events` with `Filter::new().kind(Kind::Custom(30177)).authors([owner])` and `Filter::new().kind(Kind::Custom(40902)).authors(pubkeys)`; print content parsed as JSON. Python wrappers in the Task 7 style. In `relay.py`:

```python
@dataclass(frozen=True)
class DirectoryEntry:
    pubkey: str
    display_name: str | None
    role: str | None
    capabilities: list[str]
    description: str | None
    harness: str | None
    host: str | None
    online: bool | None
    last_seen: int | None
    live_tasks: int
    version: str | None


def directory(runner: CommandRunner, ident: Identity, *, channel_id: str | None) -> list[DirectoryEntry]:
    default_channel, _ = _require(ident)
    channel = channel_id or default_channel
    members = signer_client.channel_members(runner, ident.relay_url, ident.nsec, channel, auth_tag=ident.auth_tag)
    records = {r["pubkey"]: r["content"] for r in signer_client.read_managed_agents(
        runner, ident.relay_url, ident.nsec, owner=ident.owner_pubkey or "", auth_tag=ident.auth_tag)} if ident.owner_pubkey else {}
    presence = {p["pubkey"]: p for p in signer_client.read_presence(
        runner, ident.relay_url, ident.nsec, pubkeys=[pk for pk, _ in members], auth_tag=ident.auth_tag)}
    state = load_state(runner, ident, channel_id=channel)
    load = {t.assignee: 0 for t in state.open_tasks()}
    for t in state.open_tasks():
        load[t.assignee] += 1
    out = []
    for pubkey, name in members:
        rec, pres = records.get(pubkey, {}), presence.get(pubkey)
        out.append(DirectoryEntry(pubkey=pubkey, display_name=name, role=rec.get("role"), capabilities=list(rec.get("capabilities") or []),
                                  description=rec.get("description"), harness=rec.get("harness"), host=rec.get("host"),
                                  online=(pres["status"] == "online") if pres else None, last_seen=pres["updated_at"] if pres else None,
                                  live_tasks=load.get(pubkey, 0), version=rec.get("version")))
    return sorted(out, key=lambda e: (e.display_name or "").lower())
```

Exclude the retrieval key and conductor keys from the listing when `ident.record` is present. CLI `fleet agents`: resolve identity, call `relay.directory`, print JSON (`asdict`) or a Rich table with columns Name, Role, Capabilities, Harness, Host, Online, Live tasks, Version.

- [ ] **Step 4: Run tests, verify live, commit**

```bash
uv run pytest -q && uv run ruff check . && uv run mypy && (cd signer && cargo test)
uv run buzz-fleet fleet agents --community <id>
git add signer/src src/buzz_fleet tests && git commit -m "Add buzz-fleet fleet agents: the agent directory"
```



---

### Task 19: Per-agent environment secrets and one MCP server

**Files:**
- Modify: `src/buzz_fleet/models.py`, `src/buzz_fleet/systemd.py`, `src/buzz_fleet/personas.py`, `src/buzz_fleet/manager.py`, `src/buzz_fleet/state.py`, `src/buzz_fleet/cli/app.py`, `src/buzz_fleet/tui/screens/agent_form.py`
- Test: `tests/test_models.py`, `tests/test_systemd.py`, `tests/test_personas.py`, `tests/test_state.py`, `tests/test_cli.py`

**Interfaces:**
- `Agent.env: dict[str, SecretStr] | None`; `Agent.mcp_server: McpServer | None` with `McpServer(name: str, command: str, args: list[str] = [], env: dict[str, SecretStr] = {})`.
- `state._serialize_with_secrets` must reveal `SecretStr` values nested in `env` and `mcp_server.env` (today it only patches top-level fields and `system_prompt_source`).
- `systemd.write_agent_files` writes every `env` entry as `env_line(K, V)` after the API keys, and, when `mcp_server` is set, writes `WORK_DIR/<agent>/mcp-<name>.sh` (0700, `#!/bin/sh`, `export K=V` lines, `exec "<command>" <args...>`) and `BUZZ_ACP_MCP_COMMAND=<that path>`.
- `personas.py`: import the persona's `env` block; import the first `mcp_servers` entry; raise `ValueError("persona declares N MCP servers; buzz-acp supports one")` for more than one.
- CLI: `--env KEY=VALUE` (repeatable), `--env-file <path>` (KEY=VALUE lines), `--mcp-command`, `--mcp-arg` (repeatable), `--mcp-name`, `--mcp-env KEY=VALUE` (repeatable) on create and update.
- Pi (spec fact 17, 5.13): for `harness == "pi"`, `write_agent_files` also creates `WORK_DIR/<agent>/.pi-agent/` with `settings.json` = `{"defaultProjectTrust": "always", "packages": ["npm:pi-mcp-adapter@<PINNED>"]}`, `mcp.json` = `{"mcpServers": {<name>: {"command", "args", "env"}}}` when `mcp_server` is set (else no `mcp.json`), and appends `env_line("PI_CODING_AGENT_DIR", <that dir>)`. `harnesses.install_adapter("pi")` additionally runs `pi install npm:pi-mcp-adapter@<PINNED>` with `PI_CODING_AGENT_DIR` pointed at a shared template dir `~/.local/share/buzz-fleet/pi-agent-template/` and `write_agent_files` copies its `npm/` into each new Pi agent dir so the first turn needs no network. `PINNED` lives in `harnesses.py` as `PI_MCP_ADAPTER_VERSION` (set it to the current npm version at implementation time and record it in the fleet record's `versions`).
- TUI: a multi-line `TextArea` for env (KEY=VALUE per line) and three inputs for the MCP server (name, command, args); secrets are masked in the edit form by showing `KEY=********` for existing values and only replacing a value when the user types a new one.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_models.py
def test_env_and_mcp_round_trip_with_secrets(tmp_path, monkeypatch) -> None:
    from buzz_fleet import state
    from buzz_fleet.models import McpServer

    monkeypatch.setattr(state, "CONFIG_DIR", tmp_path)
    agent = Agent(**_base_kwargs(), env={"DATABASE_URL": "postgres://x"},
                  mcp_server=McpServer(name="boost", command="php", args=["artisan", "boost:mcp"], env={"TOKEN": "t"}))
    state.save_agent(agent)
    again = state.load_agents("eltahir")[0]
    assert again.env["DATABASE_URL"].get_secret_value() == "postgres://x"
    assert again.mcp_server.env["TOKEN"].get_secret_value() == "t" and again.mcp_server.args == ["artisan", "boost:mcp"]


# tests/test_systemd.py
def test_write_agent_files_env_and_mcp_wrapper(tmp_path: Path, monkeypatch) -> None:
    from buzz_fleet.models import McpServer

    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.WORK_DIR", tmp_path / "work")
    monkeypatch.setattr("buzz_fleet.systemd.resolve_adapter_command", lambda harness: "/usr/bin/x")
    agent = _agent().model_copy(update={"env": {"DATABASE_URL": "postgres://x"},
                                        "mcp_server": McpServer(name="boost", command="php", args=["artisan", "boost:mcp"], env={"TOKEN": "t"})})

    write_agent_files(agent, _community(), None, None)

    env = agent_env_path(agent.id).read_text()
    wrapper = tmp_path / "work" / agent.id / "mcp-boost.sh"
    assert "DATABASE_URL=postgres://x\n" in env and f"BUZZ_ACP_MCP_COMMAND={wrapper}\n" in env
    assert wrapper.stat().st_mode & 0o777 == 0o700
    body = wrapper.read_text()
    assert "export TOKEN='t'" in body and "exec 'php' 'artisan' 'boost:mcp'" in body


# tests/test_personas.py
def test_persona_imports_env_and_single_mcp_server() -> None:
    template = load_persona_template(LARAVEL_PERSONA_PATH)   # declares one server: boost
    assert template.mcp_server.name == "boost" and template.mcp_server.command == "php"


def test_persona_with_two_mcp_servers_is_refused(tmp_path) -> None:
    path = tmp_path / "two.persona.md"
    path.write_text("---\nname: two\ndisplay_name: Two\nruntime: claude\nmcp_servers:\n  - {name: a, command: a}\n  - {name: b, command: b}\n---\nbody\n")
    with pytest.raises(ValueError, match="supports one"):
        load_persona_template(path)


# tests/test_systemd.py
def test_write_agent_files_pi_gets_private_agent_dir_and_mcp_json(tmp_path: Path, monkeypatch) -> None:
    import json as _json
    from buzz_fleet.models import McpServer

    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.WORK_DIR", tmp_path / "work")
    monkeypatch.setattr("buzz_fleet.systemd.resolve_adapter_command", lambda harness: "/usr/bin/pi-acp")
    agent = _agent().model_copy(update={"harness": "pi", "mcp_server": McpServer(name="boost", command="php", args=["artisan", "boost:mcp"])})

    write_agent_files(agent, _community(), None, None)

    pi_dir = tmp_path / "work" / agent.id / ".pi-agent"
    assert f"PI_CODING_AGENT_DIR={pi_dir}\n" in agent_env_path(agent.id).read_text()
    settings = _json.loads((pi_dir / "settings.json").read_text())
    assert settings["defaultProjectTrust"] == "always" and settings["packages"][0].startswith("npm:pi-mcp-adapter@")
    assert _json.loads((pi_dir / "mcp.json").read_text())["mcpServers"]["boost"]["args"] == ["artisan", "boost:mcp"]


# tests/test_cli.py
def test_agent_create_env_and_mcp_flags(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeManager:
        def create_agent(self, **kwargs):
            captured.update(kwargs)
            return _agent()

    monkeypatch.setattr("buzz_fleet.cli.app._load_manager", lambda community: FakeManager())
    result = runner_cli.invoke(app, ["agent", "create", "--community", "e", "--display-name", "X", "--harness", "claude",
                                     "--prompt-file", "/dev/null", "--env", "A=1", "--env", "B=2",
                                     "--mcp-name", "boost", "--mcp-command", "php", "--mcp-arg", "artisan", "--mcp-arg", "boost:mcp"])
    assert result.exit_code == 0, result.output
    assert captured["env"] == {"A": "1", "B": "2"}
    assert captured["mcp_server"].command == "php" and captured["mcp_server"].args == ["artisan", "boost:mcp"]
```

Use `shlex.quote` for every value in the wrapper so the assertions above hold for values with spaces.

- [ ] **Step 2: Run to verify failure**

`uv run pytest tests/test_models.py tests/test_systemd.py tests/test_personas.py tests/test_cli.py -q`

- [ ] **Step 3: Implement**

`models.py`:

```python
class McpServer(BaseModel):
    name: str
    command: str
    args: list[str] = Field(default_factory=list)
    env: dict[str, SecretStr] = Field(default_factory=dict)
```

and on `Agent`: `env: dict[str, SecretStr] | None = None`, `mcp_server: McpServer | None = None`. `state.py`: extend `patch_secrets` to recurse into dict values (`env`, and `mcp_server.env`) by walking the original model: for a `dict[str, SecretStr]` field, replace each masked value with `original[key].get_secret_value()`; for a nested `BaseModel`, recurse. `systemd.py`:

```python
def _mcp_wrapper_path(agent_id: str, name: str) -> Path:
    return WORK_DIR / agent_id / f"mcp-{name}.sh"


def write_mcp_wrapper(agent: Agent) -> Path | None:
    if agent.mcp_server is None:
        return None
    m = agent.mcp_server
    lines = ["#!/bin/sh"] + [f"export {k}={shlex.quote(v.get_secret_value())}" for k, v in m.env.items()]
    lines.append("exec " + " ".join(shlex.quote(x) for x in [m.command, *m.args]))
    path = _mcp_wrapper_path(agent.id, m.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o700)
    try:
        os.write(fd, ("\n".join(lines) + "\n").encode())
    finally:
        os.close(fd)
    return path
```

called from `write_agent_files`, which then appends `env_line("BUZZ_ACP_MCP_COMMAND", str(path))` and one `env_line(k, v.get_secret_value())` per `agent.env` entry. `personas.py`: parse `env` (dict of strings) and `mcp_servers` (list); build `McpServer` from the first; raise on more than one; carry both on `PersonaTemplate` and into `create_agent`. `manager.create_agent`/`update_agent`: accept `env` and `mcp_server`; `update_agent` restarts on change (it already rewrites files and restarts). CLI and TUI per the interfaces above; parse `KEY=VALUE` with `split("=", 1)` and reject lines without `=`.

- [ ] **Step 4: Run tests, verify live, commit**

```bash
uv run pytest -q && uv run ruff check . && uv run mypy
uv run buzz-fleet agent update --community <id> laravel-backend-developer-claude --mcp-name boost --mcp-command php --mcp-arg artisan --mcp-arg boost:mcp
# Pi: create one Pi agent with the same server, then mention it in the fleet channel and ask it to list its mcp tool (spec live check 14)
pid=$(systemctl --user show -p MainPID --value buzz-agent@laravel-backend-developer-claude); tr '\0' '\n' < /proc/$pid/environ | grep BUZZ_ACP_MCP_COMMAND
git add src/buzz_fleet tests && git commit -m "Add per-agent environment secrets and a single MCP server with a generated wrapper"
```


## Self-review

**Spec coverage, layers 0–1.**
- 5.0: PATH and working directory (T1), quoted env (T2), settings and host (T3), instructions (T10), unique names (T9), versions recorded in the record (T9). `self-update` → plan 3 (deployment), stated in the header.
- 5.1: tags, retrieval-key mention, payload version and size, identity check, ids (T11); deletions consumed (T13 `fetch_deleted_ids`); owner commands and conductor types are parsed (T11/T12) and *emitted* in plan 2.
- 5.2: paged reads by `#p`, `#e`, `authors` (T13), verified live in T14/T16 (spec live check 1 happens the first time the fleet channel exceeds 1,000 messages; T13's fake relay covers the algorithm).
- 5.3: `delegate/ack/report/cancel/show/tasks` (T14–T15), limits (T14), artifact refusal (T13), idempotent publish (T14). `runs`, `run *`, `conductor *`, `!fleet` → plans 2–3.
- 5.9: channel, record in `about`, discovery, auto-join, env vars (T9). Conductor keys in the record → plan 2.
- 7: refusals and notes (T12, T14); 8 unit tests present per task; acceptance gates 1–3 partly exercisable after this plan (gate 2's lost-ack case is T14's test plus the live smoke).

**Type consistency.** `Identity` fields, `OutgoingMessage`, `FleetEvent`, `Task`/`Attempt`/`State`, `signer_client` keyword-only wrappers, `fleet_filter`, and `FleetRecord` are used with the same names and shapes across T7–T15.

**Placeholders.** None: every step carries its code or the exact command.

**Addendum coverage.** Spec 5.10 (directory) → Tasks 17–18. Spec 5.13 (env secrets, one MCP server) → Task 19; `allowed_actions` → plan 3 with pipelines, and the coordination block paragraph for it is added when plan 3 lands. Spec 5.11 (planner, proposals, `auto_start`) and 5.12 (workspace files) → plan 3; they need the conductor (approval handling) and the persona pack changes, and nothing in this plan blocks them.
