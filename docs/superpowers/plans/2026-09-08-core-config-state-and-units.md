# Core config, state and units — Implementation Plan (Phase A)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give buzz-fleet an XDG-correct file layout, secrets split out of state files, durable atomic writes, community-qualified systemd unit names, a real `config.toml`, and one resumable `migrate` command that moves an existing machine onto all of it.

**Architecture:** Four new stdlib-only modules (`paths`, `atomic`, `units`, `config`) that existing modules delegate to, replacing six hardcoded `Path.home() / ".config"` sites and two hand-rolled `_write_secure` implementations. Agent files and systemd instances become keyed on `<community>:<agent>` instead of a bare agent id, which fixes a live cross-community collision. A `migrate` command moves an existing tree, per-agent and idempotently.

**Tech Stack:** Python ≥3.12 (stdlib `tomllib`, `fcntl`, `tempfile`), Pydantic v2, Typer, pytest.

**Spec:** `docs/superpowers/specs/2026-09-08-opentui-frontend-design.md` (sections 4.1, 4.2, 4.3, 4.4, 4.5, 5.2, 5.3; facts 6, 7, 8, 12). Decision record: `docs/adr/0003-frontends-talk-to-a-long-lived-core-over-json-rpc.md`.

## Global Constraints

- **Python ≥ 3.12.** `tomllib` is stdlib; do not add a TOML library.
- **No new runtime dependencies.** Current set is exactly: `typer`, `textual`, `pydantic`, `rich`, `pyyaml`.
- **ruff line-length 100.** `src = ["src", "tests"]`.
- **pytest `asyncio_mode = "auto"`.**
- **Every file write goes through `atomic.write_secure`.** No bare `open(...,"w")`, no `os.O_TRUNC` in-place writes, anywhere in `src/`.
- **Secret files are `0600`, their directories `0700`.** No secret value is ever written into a state file.
- **Unit instance names are community-first:** `<community>:<agent>`. Never `<community>-<agent>`.
- **`config.toml` is read-only to the application.** Nothing in `src/` writes it.
- **`migrate` is idempotent and per-agent.** Re-running it after an interruption must be safe.
- Existing behaviour that must not regress: `AgentManager.ensure_runtime_ready()` heals nine documented incidents (see `CLAUDE.md`); do not change what it heals in this phase.

## File Structure

| File | Responsibility |
|---|---|
| `src/buzz_fleet/paths.py` *(new)* | The only module that knows where anything lives. XDG resolution. |
| `src/buzz_fleet/atomic.py` *(new)* | Durable write + advisory lock. The only writer primitive. |
| `src/buzz_fleet/units.py` *(new)* | Instance keys and `systemd.unit(5)` escaping. |
| `src/buzz_fleet/config.py` *(new)* | Read-only `config.toml` with defaults and `env:` indirection. |
| `src/buzz_fleet/migrate.py` *(new)* | The one-shot, resumable move from the legacy tree. |
| `src/buzz_fleet/state.py` | Loses `CONFIG_DIR`; splits secrets; delegates writes to `atomic`. |
| `src/buzz_fleet/systemd.py` | Loses its three hardcoded paths and its own `_write_secure`; takes instance keys. |
| `src/buzz_fleet/systemctl_client.py` | Takes instance keys instead of bare agent ids. |
| `src/buzz_fleet/manager.py` | Builds instance keys at every call site. |
| `src/buzz_fleet/orchestration/identity.py` | Community resolution gains the active-community and config steps. |
| `src/buzz_fleet/cli/app.py` | Gains `migrate` and `config show`. |

---

### Task 1: `paths` — one module that knows where things live

Six modules currently build their own `Path.home() / ".config" / "buzz-fleet"` (spec fact 7). This collapses them into one.

**Files:**
- Create: `src/buzz_fleet/paths.py`
- Test: `tests/test_paths.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `config_dir() -> Path`, `state_dir() -> Path`, `data_dir() -> Path`, `runtime_dir() -> Path`, `secrets_dir() -> Path`, `legacy_dir() -> Path`. All are functions, never module constants, so tests can drive them with `monkeypatch.setenv` instead of patching attributes.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_paths.py
"""XDG resolution. Every location buzz-fleet writes comes from here."""

from pathlib import Path

from buzz_fleet import paths


def test_config_dir_honours_xdg_config_home(monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", "/xdg/cfg")
    assert paths.config_dir() == Path("/xdg/cfg/buzz-fleet")


def test_config_dir_falls_back_to_dot_config(monkeypatch) -> None:
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/home/u")))
    assert paths.config_dir() == Path("/home/u/.config/buzz-fleet")


def test_state_dir_honours_xdg_state_home(monkeypatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", "/xdg/state")
    assert paths.state_dir() == Path("/xdg/state/buzz-fleet")


def test_state_dir_falls_back_to_local_state(monkeypatch) -> None:
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/home/u")))
    assert paths.state_dir() == Path("/home/u/.local/state/buzz-fleet")


def test_data_dir_falls_back_to_local_share(monkeypatch) -> None:
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/home/u")))
    assert paths.data_dir() == Path("/home/u/.local/share/buzz-fleet")


def test_secrets_dir_lives_under_state(monkeypatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", "/xdg/state")
    assert paths.secrets_dir() == Path("/xdg/state/buzz-fleet/secrets")


def test_runtime_dir_falls_back_under_state_when_unset(monkeypatch) -> None:
    """XDG_RUNTIME_DIR is genuinely absent in some non-login contexts (cron,
    a bare `ssh host cmd`), so it needs a fallback the others don't."""
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", "/xdg/state")
    assert paths.runtime_dir() == Path("/xdg/state/buzz-fleet/run")


def test_legacy_dir_is_the_pre_migration_tree(monkeypatch) -> None:
    """Fixed, and deliberately ignores XDG: it names where files actually are
    on a machine installed before this change, not where they should be."""
    monkeypatch.setenv("XDG_CONFIG_HOME", "/xdg/cfg")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/home/u")))
    assert paths.legacy_dir() == Path("/home/u/.config/buzz-fleet")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_paths.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'buzz_fleet.paths'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/buzz_fleet/paths.py
"""Every location buzz-fleet owns, resolved once.

Before this module, six others each built their own
`Path.home() / ".config" / "buzz-fleet"` and none of them honoured XDG. These
are functions rather than module constants so a test can drive them with
`monkeypatch.setenv` and get a different answer on the next call — patching a
constant only works if every caller re-reads it, which they didn't.
"""

from __future__ import annotations

import os
from pathlib import Path

APP = "buzz-fleet"


def _xdg(var: str, fallback: Path) -> Path:
    raw = os.environ.get(var)
    return (Path(raw) if raw else fallback) / APP


def config_dir() -> Path:
    """User-editable configuration. `config.toml` and `personas/`."""
    return _xdg("XDG_CONFIG_HOME", Path.home() / ".config")


def state_dir() -> Path:
    """Machine-owned state. Communities, agents, the active-community pointer."""
    return _xdg("XDG_STATE_HOME", Path.home() / ".local" / "state")


def data_dir() -> Path:
    """Installed artefacts and working directories: bin/, work/, templates."""
    return _xdg("XDG_DATA_HOME", Path.home() / ".local" / "share")


def runtime_dir() -> Path:
    """Sockets and pidfiles. Unlike the others, XDG_RUNTIME_DIR is genuinely
    absent in non-login contexts, so this falls back inside state_dir()."""
    raw = os.environ.get("XDG_RUNTIME_DIR")
    return Path(raw) / APP if raw else state_dir() / "run"


def secrets_dir() -> Path:
    """Key material only, mirroring the state tree's shape. Mode 0700."""
    return state_dir() / "secrets"


def legacy_dir() -> Path:
    """The pre-migration tree. Only `migrate` may read this; it names where
    files are on an already-installed machine, so it ignores XDG on purpose."""
    return Path.home() / ".config" / APP
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_paths.py -v`
Expected: 8 passed

- [ ] **Step 5: Port the personas directory onto it**

Spec §4.1 puts personas in config, as user-authored content. `personas.py:33`
hardcodes `Path.home() / ".config" / "buzz-fleet" / "personas"`, ignoring XDG:

```python
# src/buzz_fleet/personas.py — replace the DEFAULT_PERSONAS_DIR constant
from buzz_fleet import paths


def default_personas_dir() -> Path:
    # User-authored content, so config rather than state.
    return paths.config_dir() / "personas"
```

Update every reference to `DEFAULT_PERSONAS_DIR` to call `default_personas_dir()`.
Leave `scripts/get.sh`'s `gs_seed_personas` alone in this task: its literal
`~/.config/buzz-fleet/personas` is still correct whenever `XDG_CONFIG_HOME` is
unset, which is the default, and `migrate` (Task 8) is what moves an existing
machine.

- [ ] **Step 6: Run the affected tests**

Run: `uv run pytest tests/test_paths.py tests/test_personas.py -v`
Expected: all pass

- [ ] **Step 7: Lint and commit**

```bash
uv run ruff check src/buzz_fleet/paths.py src/buzz_fleet/personas.py tests/test_paths.py
git add src/buzz_fleet/paths.py src/buzz_fleet/personas.py tests/test_paths.py
git commit -m "Add XDG-aware paths module"
```

---

### Task 2: `atomic` — durable writes and advisory locking

`state._write_secure` and `systemd._write_secure` both `os.open(..., O_TRUNC)` and write in place: no temp file, no fsync, no rename, and no lock anywhere in the codebase (spec 4.5). A crash mid-write leaves truncated JSON containing an agent's private key.

**Files:**
- Create: `src/buzz_fleet/atomic.py`
- Test: `tests/test_atomic.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `write_secure(path: Path, content: str, *, mode: int = 0o600) -> None` and the context manager `locked(lock_path: Path) -> Iterator[None]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_atomic.py
"""Durable writes. The fourth step — fsyncing the directory — is the one most
implementations omit, and without it the rename itself can be lost."""

import fcntl
import os
from pathlib import Path

import pytest

from buzz_fleet import atomic


def test_writes_content(tmp_path: Path) -> None:
    target = tmp_path / "a.json"
    atomic.write_secure(target, '{"x":1}')
    assert target.read_text() == '{"x":1}'


def test_creates_parent_directories(tmp_path: Path) -> None:
    target = tmp_path / "deep" / "nested" / "a.json"
    atomic.write_secure(target, "hi")
    assert target.read_text() == "hi"


def test_file_is_0600_by_default(tmp_path: Path) -> None:
    target = tmp_path / "secret.json"
    atomic.write_secure(target, "s")
    assert oct(target.stat().st_mode & 0o777) == "0o600"


def test_mode_is_overridable(tmp_path: Path) -> None:
    target = tmp_path / "public.toml"
    atomic.write_secure(target, "s", mode=0o644)
    assert oct(target.stat().st_mode & 0o777) == "0o644"


def test_replaces_existing_content_entirely(tmp_path: Path) -> None:
    """os.replace, not truncate-and-write: a shorter payload must not leave
    a tail of the previous one behind."""
    target = tmp_path / "a.json"
    atomic.write_secure(target, "a-very-long-previous-value")
    atomic.write_secure(target, "short")
    assert target.read_text() == "short"


def test_leaves_no_temporary_file_behind(tmp_path: Path) -> None:
    target = tmp_path / "a.json"
    atomic.write_secure(target, "x")
    assert [p.name for p in tmp_path.iterdir()] == ["a.json"]


def test_failed_write_leaves_original_intact_and_no_temp(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "a.json"
    atomic.write_secure(target, "original")

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(atomic.os, "replace", boom)
    with pytest.raises(OSError):
        atomic.write_secure(target, "replacement")

    assert target.read_text() == "original"
    assert [p.name for p in tmp_path.iterdir()] == ["a.json"]


def test_locked_is_exclusive(tmp_path: Path) -> None:
    """flock is per open-file-description, so a second open() in this same
    process contends exactly as another process would."""
    lock = tmp_path / "community.lock"
    with atomic.locked(lock):
        fd = os.open(lock, os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)


def test_lock_is_released_on_exit(tmp_path: Path) -> None:
    lock = tmp_path / "community.lock"
    with atomic.locked(lock):
        pass
    fd = os.open(lock, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # must not raise
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def test_lock_is_released_when_body_raises(tmp_path: Path) -> None:
    lock = tmp_path / "community.lock"
    with pytest.raises(ValueError):
        with atomic.locked(lock):
            raise ValueError("boom")
    fd = os.open(lock, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # must not raise
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_atomic.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'buzz_fleet.atomic'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/buzz_fleet/atomic.py
"""The only way buzz-fleet writes a file, and the only lock it takes.

`os.replace` alone is not durable: the bytes can be synced while the rename
itself is lost on power failure. Syncing the *containing directory* after the
rename is the step that closes that, and it is the one usually left out.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path


def write_secure(path: Path, content: str, *, mode: int = 0o600) -> None:
    """Write `content` to `path` atomically and durably.

    The temporary file is created in the target's own directory so the rename
    never crosses a filesystem — `os.replace` raises `OSError` if it would.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        try:
            os.write(fd, content.encode())
            os.fchmod(fd, mode)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise

    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


@contextlib.contextmanager
def locked(lock_path: Path) -> Iterator[None]:
    """Hold an exclusive advisory lock for a read-modify-write sequence.

    Atomic writes stop a *torn* file; they do not stop a lost update when two
    processes read, modify and write the same state. The CLI runs standalone
    inside agents' own systemd units, so there is always a second writer.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_atomic.py -v`
Expected: 10 passed

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/buzz_fleet/atomic.py tests/test_atomic.py
git add src/buzz_fleet/atomic.py tests/test_atomic.py
git commit -m "Add atomic durable writes and advisory locking"
```

---

### Task 3: `units` — instance keys and systemd escaping

`manager.py:454` scopes agent ids per community; `systemctl_client.py:19` builds `buzz-agent@{agent_id}` globally (spec fact 6). Two communities with a `reviewer` claim one unit, and the wrong Nostr key is loaded. This task adds the key type; Task 4 threads it through.

`systemd.unit(5)`'s escaping is implemented here rather than shelling to `systemd-escape` on every call: it is a documented, total function, and a test asserts parity with the real binary.

**Files:**
- Create: `src/buzz_fleet/units.py`
- Test: `tests/test_units.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `instance_key(community_id: str, agent_id: str) -> str`, `split_key(key: str) -> tuple[str, str]`, `escape_instance(value: str) -> str`, `unit_name(key: str) -> str`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_units.py
"""Community-qualified systemd instance names.

Fact 12 of the spec: systemd.unit(5)'s escaping leaves ASCII alphanumerics,
':', '_' and '.' alone, which is why ':' is the right separator — and why it
cannot collide with the slug charset ([a-z0-9-]) the way '-' would.
"""

import shutil
import subprocess

import pytest

from buzz_fleet import units


def test_instance_key_is_community_first() -> None:
    assert units.instance_key("eltahir", "reviewer") == "eltahir:reviewer"


def test_split_key_round_trips() -> None:
    assert units.split_key(units.instance_key("eltahir", "reviewer")) == ("eltahir", "reviewer")


def test_split_key_rejects_an_unqualified_id() -> None:
    """A bare agent id reaching a unit call is the pre-migration bug; refuse
    loudly rather than silently addressing the wrong unit."""
    with pytest.raises(ValueError, match="not a community-qualified"):
        units.split_key("reviewer")


def test_two_communities_do_not_collide() -> None:
    a = units.instance_key("eltahir", "reviewer")
    b = units.instance_key("acme", "reviewer")
    assert a != b


def test_unit_name_wraps_the_template() -> None:
    assert units.unit_name("eltahir:reviewer") == "buzz-agent@eltahir:reviewer.service"


def test_escape_leaves_safe_characters_alone() -> None:
    assert units.escape_instance("eltahir:reviewer-2") == "eltahir:reviewer-2"


def test_escape_replaces_slash_with_dash() -> None:
    assert units.escape_instance("a/b") == "a-b"


def test_escape_escapes_a_leading_dot() -> None:
    assert units.escape_instance(".hidden") == r"\x2ehidden"


def test_escape_does_not_escape_a_non_leading_dot() -> None:
    assert units.escape_instance("a.b") == "a.b"


def test_escape_escapes_a_space() -> None:
    assert units.escape_instance("a b") == r"a\x20b"


@pytest.mark.skipif(shutil.which("systemd-escape") is None, reason="systemd-escape not installed")
@pytest.mark.parametrize(
    "value", ["eltahir:reviewer", "a/b", ".hidden", "a.b", "a b", "acme:my-lara-cdx", "a-b_c.d:e"]
)
def test_escape_matches_systemd_escape(value: str) -> None:
    """The implementation is ours; the authority is the binary."""
    expected = subprocess.run(
        ["systemd-escape", "--", value], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert units.escape_instance(value) == expected
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_units.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'buzz_fleet.units'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/buzz_fleet/units.py
"""Community-qualified systemd instance names.

Agent ids are unique within a community (`manager.create_agent` derives them
from that community's agents alone), but the unit template is global. Before
this module two communities with a `reviewer` shared one
`buzz-agent@reviewer.service`, so whichever env file was written last decided
which Nostr key the unit loaded — silently, and with the wrong identity.

The separator is ':' because systemd.unit(5) leaves it unescaped and the slug
charset ([a-z0-9-]) excludes it, so `<community>:<agent>` is unambiguous where
`<community>-<agent>` would not be when either half contains a dash.
"""

from __future__ import annotations

import string

TEMPLATE = "buzz-agent@.service"
SEPARATOR = ":"

# systemd.unit(5): everything outside this set becomes a C-style \xNN escape.
_SAFE = frozenset(string.ascii_letters + string.digits + ":_.")


def instance_key(community_id: str, agent_id: str) -> str:
    """The identity of one agent's unit and its files: `<community>:<agent>`."""
    return f"{community_id}{SEPARATOR}{agent_id}"


def split_key(key: str) -> tuple[str, str]:
    """Inverse of `instance_key`. Raises on a bare agent id.

    An unqualified id reaching a unit call is exactly the pre-migration bug, so
    it fails loudly rather than addressing some other community's unit.
    """
    community, separator, agent = key.partition(SEPARATOR)
    if not separator or not community or not agent:
        raise ValueError(f"{key!r} is not a community-qualified instance key")
    return community, agent


def escape_instance(value: str) -> str:
    """Escape a string for use as a systemd instance name.

    Implements systemd.unit(5)'s algorithm directly rather than shelling out to
    `systemd-escape` on every call: it is total, documented, and hot enough that
    a subprocess per unit name would be absurd. `tests/test_units.py` asserts
    parity against the real binary wherever it is installed.
    """
    out: list[str] = []
    for index, char in enumerate(value):
        if char == "/":
            out.append("-")
        elif char in _SAFE and not (index == 0 and char == "."):
            out.append(char)
        else:
            out.extend(f"\\x{byte:02x}" for byte in char.encode())
    return "".join(out)


def unit_name(key: str) -> str:
    """The full unit name for an instance key, escaped."""
    prefix, _, suffix = TEMPLATE.partition("@")
    return f"{prefix}@{escape_instance(key)}{suffix}"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_units.py -v`
Expected: 17 passed (10 plus 7 parametrised parity cases)

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/buzz_fleet/units.py tests/test_units.py
git add src/buzz_fleet/units.py tests/test_units.py
git commit -m "Add community-qualified systemd instance keys"
```

---

### Task 4: Thread instance keys through systemd and systemctl

`systemctl_client` and `systemd`'s path helpers take a bare `agent_id` at 11 call sites. They now take an instance key, and the template unit moves to the new data directory.

**Files:**
- Modify: `src/buzz_fleet/systemctl_client.py:19` (`_unit`), and the five public functions that call it
- Modify: `src/buzz_fleet/systemd.py:30-32` (paths), `:53` (`TEMPLATE_UNIT`), `:114-128` (`agent_env_path`, `agent_prompt_path`, `_write_secure`), `:306-313` (`write_agent_files`)
- Modify: `src/buzz_fleet/manager.py:35,44,56,71,322,333,341,504,517,621,628,635,685,686,696,698`
- Modify: `src/buzz_fleet/tui/screens/dashboard.py:57`, `src/buzz_fleet/tui/screens/logs.py:52`
- Test: `tests/test_systemctl_client.py`, `tests/test_systemd.py`

**Interfaces:**
- Consumes: `units.instance_key`, `units.unit_name` (Task 3); `atomic.write_secure` (Task 2); `paths.data_dir`, `paths.config_dir` (Task 1).
- Produces: `systemctl_client.{enable_now,disable_now,restart,stop,status,tail_logs}(runner, key: str)`; `systemd.agent_env_path(key: str) -> Path`; `systemd.agent_prompt_path(key: str) -> Path`; `systemd.work_dir(key: str) -> Path`; `systemd.write_agent_files(agent, community, anthropic_api_key, openai_api_key, auth_tag=None)` unchanged in signature — it derives the key from `agent.community_id` and `agent.id` internally.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_systemctl_client.py
from buzz_fleet import systemctl_client, units


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def run(self, args):
        import subprocess

        self.calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="active", stderr="")


def test_unit_name_is_community_qualified() -> None:
    runner = RecordingRunner()
    systemctl_client.restart(runner, units.instance_key("eltahir", "reviewer"))
    assert runner.calls == [
        ["systemctl", "--user", "restart", "buzz-agent@eltahir:reviewer.service"]
    ]


def test_two_communities_address_different_units() -> None:
    runner = RecordingRunner()
    systemctl_client.stop(runner, units.instance_key("eltahir", "reviewer"))
    systemctl_client.stop(runner, units.instance_key("acme", "reviewer"))
    assert runner.calls[0] != runner.calls[1]


def test_status_reads_the_qualified_unit() -> None:
    runner = RecordingRunner()
    assert systemctl_client.status(runner, "eltahir:reviewer") is systemctl_client.AgentStatus.RUNNING
    assert runner.calls[0][-1] == "buzz-agent@eltahir:reviewer.service"


def test_tail_logs_reads_the_qualified_unit() -> None:
    runner = RecordingRunner()
    systemctl_client.tail_logs(runner, "eltahir:reviewer", lines=10)
    assert "buzz-agent@eltahir:reviewer.service" in runner.calls[0]
```

```python
# append to tests/test_systemd.py
from pathlib import Path

from buzz_fleet import paths, systemd, units


def test_agent_env_path_is_under_secrets_and_qualified(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    key = units.instance_key("eltahir", "reviewer")
    assert systemd.agent_env_path(key) == paths.secrets_dir() / "units" / "eltahir:reviewer.env"


def test_agent_prompt_path_is_under_state_and_qualified(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    key = units.instance_key("eltahir", "reviewer")
    assert systemd.agent_prompt_path(key) == paths.state_dir() / "units" / "eltahir:reviewer.prompt.md"


def test_work_dir_is_under_data_and_qualified(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert systemd.work_dir("eltahir:reviewer") == paths.data_dir() / "work" / "eltahir:reviewer"


def test_template_unit_points_at_the_new_locations(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    rendered = systemd.render_template_unit()
    assert f"EnvironmentFile={tmp_path / 'state'}/buzz-fleet/secrets/units/%i.env" in rendered
    assert f"WorkingDirectory={tmp_path / 'data'}/buzz-fleet/work/%i" in rendered


def test_env_file_is_written_0600(monkeypatch, tmp_path: Path) -> None:
    """write_agent_files now goes through atomic.write_secure; the mode must
    survive the temp-file-and-rename it does internally."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    from buzz_fleet import atomic

    target = paths.secrets_dir() / "units" / "eltahir:reviewer.env"
    atomic.write_secure(target, "BUZZ_PRIVATE_KEY=nsec1x")
    assert oct(target.stat().st_mode & 0o777) == "0o600"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_systemctl_client.py tests/test_systemd.py -v`
Expected: FAIL — `buzz-agent@reviewer.service` where `buzz-agent@eltahir:reviewer.service` is asserted, and `AttributeError: module 'buzz_fleet.systemd' has no attribute 'work_dir'`

- [ ] **Step 3: Rewrite `systemctl_client`'s unit resolution**

```python
# src/buzz_fleet/systemctl_client.py — replace `_unit`
from buzz_fleet import units


def _unit(key: str) -> str:
    """`key` is a community-qualified instance key (see units.instance_key).

    Before this took a bare agent id, and two communities with the same agent
    name addressed one another's units.
    """
    units.split_key(key)  # refuse an unqualified id loudly
    return units.unit_name(key)
```

Then rename the `agent_id` parameter to `key` in `enable_now`, `disable_now`, `restart`, `stop`, `status` and `tail_logs`. Their bodies are otherwise unchanged.

- [ ] **Step 4: Rewrite `systemd`'s paths, template and writer**

```python
# src/buzz_fleet/systemd.py — replace the three module constants at :30-32
from buzz_fleet import atomic, paths


def units_state_dir() -> Path:
    """Prompt files: state, not secret."""
    return paths.state_dir() / "units"


def units_secrets_dir() -> Path:
    """Env files: they carry BUZZ_PRIVATE_KEY."""
    return paths.secrets_dir() / "units"


def work_dir(key: str) -> Path:
    return paths.data_dir() / "work" / key


def template_unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / "buzz-agent@.service"


def agent_env_path(key: str) -> Path:
    return units_secrets_dir() / f"{key}.env"


def agent_prompt_path(key: str) -> Path:
    return units_state_dir() / f"{key}.prompt.md"
```

```python
# src/buzz_fleet/systemd.py — TEMPLATE_UNIT becomes a function, because its
# paths are now resolved at call time rather than at import time.
def render_template_unit() -> str:
    return f"""[Unit]
Description=Buzz headless agent (%i)
After=network-online.target

[Service]
EnvironmentFile={units_secrets_dir()}/%i.env
Environment=PATH={BUZZ_ACP_DIR}:/usr/local/bin:/usr/bin:/bin
WorkingDirectory={paths.data_dir()}/work/%i
ExecStart={BUZZ_ACP_PATH}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""
```

Delete `systemd._write_secure` entirely and replace its two call sites with `atomic.write_secure`. In `write_agent_files`, derive the key once at the top and use it for all three paths:

```python
def write_agent_files(
    agent: Agent,
    community: Community,
    anthropic_api_key: str | None,
    openai_api_key: str | None,
    auth_tag: str | None = None,
) -> None:
    key = units.instance_key(agent.community_id, agent.id)
    work_dir(key).mkdir(parents=True, exist_ok=True)

    prompt_path = agent_prompt_path(key)
    atomic.write_secure(prompt_path, resolve_prompt_text(agent), mode=0o600)
    # ... the existing `lines` construction is unchanged ...
    atomic.write_secure(agent_env_path(key), "\n".join(lines) + "\n")
```

- [ ] **Step 5: Update every caller**

In `manager.py`, replace each bare `agent.id` / `agent_id` passed to `systemctl_client.*`, `systemd.agent_env_path`, `systemd.agent_prompt_path` or `WORK_DIR` with a key. The manager always knows its community, so add one helper near the top of `AgentManager`:

```python
    def _key(self, agent_id: str) -> str:
        return units.instance_key(self._community.id, agent_id)
```

and use `self._key(agent.id)` / `self._key(agent_id)` at lines 35, 44, 56, 71, 322, 341, 517, 628, 635, 685, 686, 696, 698. (Lines 333, 504 and 621 call `write_agent_files`, whose signature is unchanged.) Replace `systemd.WORK_DIR / agent_id` with `systemd.work_dir(self._key(agent_id))` at 696 and 698.

In `tui/screens/dashboard.py:57`, `agent_status` gains the community:

```python
def agent_status(community_id: str, agent_id: str) -> AgentStatus:
    return systemctl_status(RealCommandRunner(), units.instance_key(community_id, agent_id))
```

and its caller in `refresh_agents` passes `agent.community_id`. In `tui/screens/logs.py`, `LogsScreen.__init__` takes the key instead of an agent id, and `dashboard.action_view_logs` builds it.

- [ ] **Step 6: Port the remaining data-directory consumers**

Two more modules hardcode `~/.local/share/buzz-fleet` (spec fact 7), and one of
them is interpolated into the template unit's `PATH`, so it must move in this
same change or the rendered unit disagrees with where the binary actually is:

```python
# src/buzz_fleet/buzz_acp.py — replace the BUZZ_ACP_DIR constant
def buzz_acp_dir() -> Path:
    return paths.data_dir() / "bin"


# src/buzz_fleet/harnesses.py — replace the PI_AGENT_TEMPLATE_DIR constant
def pi_agent_template_dir() -> Path:
    return paths.data_dir() / "pi-agent-template"
```

Update every reference, including `BUZZ_ACP_PATH` and the `Environment=PATH=`
line inside `render_template_unit`.

- [ ] **Step 7: Run the full suite**

Run: `uv run pytest -q`
Expected: all pass. Existing tests that assert `buzz-agent@<id>` need their expectations updated to `buzz-agent@<community>:<id>` — that is the point of the change, not a regression.

- [ ] **Step 8: Lint and commit**

```bash
uv run ruff check src tests && uv run mypy src
git add -A src tests
git commit -m "Key agent units and files on <community>:<agent>"
```

---

### Task 5: Split secrets out of state files

`state._serialize_with_secrets` patches real `SecretStr` values back into the dumped JSON, so key material and ordinary state share one file (spec fact 8). This splits them into two trees of the same shape, so a state dump is safe to read and paste without redaction machinery.

**Files:**
- Modify: `src/buzz_fleet/state.py` (whole module)
- Test: `tests/test_state.py`

**Interfaces:**
- Consumes: `paths.state_dir`, `paths.secrets_dir` (Task 1); `atomic.write_secure`, `atomic.locked` (Task 2).
- Produces: `save_community`, `load_community`, `list_community_ids`, `save_agent`, `load_agents`, `delete_agent` — all with unchanged signatures. `CONFIG_DIR` is **removed**; tests drive location with `monkeypatch.setenv("XDG_STATE_HOME", ...)`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_state.py
import json
import stat

from buzz_fleet import paths, state
from buzz_fleet.models import Community
from pydantic import SecretStr


def _community() -> Community:
    return Community(
        id="eltahir",
        relay_url="wss://relay.example",
        relay_admin_nsec=SecretStr("nsec1secret"),
    )


def test_state_file_contains_no_secret(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    state.save_community(_community())
    raw = (paths.state_dir() / "communities" / "eltahir.json").read_text()
    assert "nsec1secret" not in raw
    assert json.loads(raw)["relay_url"] == "wss://relay.example"


def test_secret_file_contains_the_secret(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    state.save_community(_community())
    secret = paths.secrets_dir() / "communities" / "eltahir.json"
    assert json.loads(secret.read_text())["relay_admin_nsec"] == "nsec1secret"


def test_round_trip_restores_the_secret(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    state.save_community(_community())
    loaded = state.load_community("eltahir")
    assert loaded is not None
    assert loaded.relay_admin_nsec.get_secret_value() == "nsec1secret"


def test_secrets_directory_is_0700(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    state.save_community(_community())
    mode = stat.S_IMODE((paths.secrets_dir() / "communities").stat().st_mode)
    assert oct(mode) == "0o700"


def test_nested_and_dict_secrets_round_trip(monkeypatch, tmp_path) -> None:
    """Agent.env is dict[str, SecretStr] and mcp_server.env is nested one level
    deeper — both must survive the split, and neither may leak into state."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    from buzz_fleet.models import Agent, McpServer, SystemPromptSource

    agent = Agent(
        id="reviewer",
        community_id="eltahir",
        display_name="Reviewer",
        harness="claude",
        private_key=SecretStr("nsec1agent"),
        public_key="a" * 64,
        system_prompt_source=SystemPromptSource(kind="inline", text="hi"),
        env={"ANTHROPIC_API_KEY": SecretStr("sk-top")},
        mcp_server=McpServer(name="m", command="c", args=[], env={"TOKEN": SecretStr("tok")}),
    )
    state.save_agent(agent)
    raw = (paths.state_dir() / "communities" / "eltahir" / "agents" / "reviewer.json").read_text()
    for leaked in ("nsec1agent", "sk-top", "tok"):
        assert leaked not in raw

    loaded = state.load_agents("eltahir")[0]
    assert loaded.private_key.get_secret_value() == "nsec1agent"
    assert loaded.env["ANTHROPIC_API_KEY"].get_secret_value() == "sk-top"
    assert loaded.mcp_server.env["TOKEN"].get_secret_value() == "tok"


def test_delete_agent_removes_both_files(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    from buzz_fleet.models import Agent, SystemPromptSource

    state.save_agent(
        Agent(
            id="reviewer",
            community_id="eltahir",
            display_name="R",
            harness="claude",
            private_key=SecretStr("nsec1agent"),
            public_key="a" * 64,
            system_prompt_source=SystemPromptSource(kind="inline", text="hi"),
        )
    )
    state.delete_agent("eltahir", "reviewer")
    assert state.load_agents("eltahir") == []
    assert not (paths.secrets_dir() / "communities" / "eltahir" / "agents" / "reviewer.json").exists()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_state.py -v`
Expected: FAIL — files land under `CONFIG_DIR` with secrets inline; `paths.secrets_dir()` tree does not exist.

- [ ] **Step 3: Rewrite `state.py`**

```python
"""Local state for buzz-fleet: one file per community plus per-agent files,
with every secret held in a parallel tree under `paths.secrets_dir()`.

Splitting them means a state dump is safe to read, diff and paste into a bug
report without any redaction step. The secrets tree mirrors the state tree's
shape exactly — same relative paths, same filenames — so the two are trivially
correlated by eye, and a merge is a recursive dict update with no key escaping
to get wrong.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, SecretStr

from buzz_fleet import atomic, paths
from buzz_fleet.models import Agent, Community


def _communities_dir() -> Path:
    return paths.state_dir() / "communities"


def _secret_communities_dir() -> Path:
    return paths.secrets_dir() / "communities"


def _agents_dir(community_id: str) -> Path:
    return _communities_dir() / community_id / "agents"


def _secret_agents_dir(community_id: str) -> Path:
    return _secret_communities_dir() / community_id / "agents"


def _extract_secrets(original: BaseModel) -> dict:
    """A dict mirroring the model's shape, holding only its SecretStr leaves.

    Mirrors the three shapes secrets actually take in these models: a plain
    field (`relay_admin_nsec`, `private_key`), a nested model (`mcp_server`),
    and a dict of secrets (`Agent.env`, `McpServer.env`).
    """
    out: dict = {}
    for name in type(original).model_fields:
        value = getattr(original, name, None)
        if isinstance(value, SecretStr):
            out[name] = value.get_secret_value()
        elif isinstance(value, BaseModel):
            nested = _extract_secrets(value)
            if nested:
                out[name] = nested
        elif isinstance(value, dict):
            inner = {k: v.get_secret_value() for k, v in value.items() if isinstance(v, SecretStr)}
            if inner:
                out[name] = inner
    return out


def _merge_secrets(data: dict, secrets: dict) -> dict:
    for name, value in secrets.items():
        if isinstance(value, dict) and isinstance(data.get(name), dict):
            _merge_secrets(data[name], value)
        else:
            data[name] = value
    return data


def _write_split(state_path: Path, secret_path: Path, obj: Community | Agent) -> None:
    """`model_dump(mode="json")` already masks every SecretStr as
    "**********", so the public dump needs no scrubbing of its own — the mask
    is what lands in state, and the real values go to the secrets tree."""
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    secret_path.parent.chmod(0o700)
    atomic.write_secure(state_path, json.dumps(obj.model_dump(mode="json"), indent=2), mode=0o600)
    atomic.write_secure(secret_path, json.dumps(_extract_secrets(obj), indent=2), mode=0o600)


def _read_merged(state_path: Path, secret_path: Path) -> dict:
    data = json.loads(state_path.read_text())
    if secret_path.exists():
        _merge_secrets(data, json.loads(secret_path.read_text()))
    return data


def save_community(community: Community) -> None:
    with atomic.locked(paths.state_dir() / f".{community.id}.lock"):
        _write_split(
            _communities_dir() / f"{community.id}.json",
            _secret_communities_dir() / f"{community.id}.json",
            community,
        )


def load_community(community_id: str) -> Community | None:
    path = _communities_dir() / f"{community_id}.json"
    if not path.exists():
        return None
    secret = _secret_communities_dir() / f"{community_id}.json"
    return Community.model_validate(_read_merged(path, secret))


def list_community_ids() -> list[str]:
    directory = _communities_dir()
    return sorted(p.stem for p in directory.glob("*.json")) if directory.exists() else []


def save_agent(agent: Agent) -> None:
    with atomic.locked(paths.state_dir() / f".{agent.community_id}.lock"):
        _write_split(
            _agents_dir(agent.community_id) / f"{agent.id}.json",
            _secret_agents_dir(agent.community_id) / f"{agent.id}.json",
            agent,
        )


def load_agents(community_id: str) -> list[Agent]:
    directory = _agents_dir(community_id)
    if not directory.exists():
        return []
    secrets = _secret_agents_dir(community_id)
    return [
        Agent.model_validate(_read_merged(p, secrets / p.name))
        for p in sorted(directory.glob("*.json"))
    ]


def delete_agent(community_id: str, agent_id: str) -> None:
    with atomic.locked(paths.state_dir() / f".{community_id}.lock"):
        (_agents_dir(community_id) / f"{agent_id}.json").unlink(missing_ok=True)
        (_secret_agents_dir(community_id) / f"{agent_id}.json").unlink(missing_ok=True)
```

- [ ] **Step 4: Run the full suite**

Run: `uv run pytest -q`
Expected: all pass. Every existing `monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)` in `tests/` must become `monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))` — `CONFIG_DIR` no longer exists, so those tests fail with `AttributeError` until updated.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src tests && uv run mypy src
git add -A src tests
git commit -m "Split secrets out of state files into a parallel tree"
```

---

### Task 6: `config` — read-only `config.toml`

buzz-fleet has no config file at all today (spec fact 7). This adds one, read-only to the application so comments and formatting survive and no TOML *writer* dependency is needed.

**Files:**
- Create: `src/buzz_fleet/config.py`
- Modify: `src/buzz_fleet/cli/app.py` (add `config show`)
- Modify: `README.md` (document `config.toml`)
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: `paths.config_dir` (Task 1).
- Produces: `load() -> Config`; the frozen dataclass `Config` with fields `default_community: str | None`, `refresh_interval_ms: int`, `default_view: str`, `theme: str`, `confirm_destructive: bool`, `default_harness: str`, `ntfy_url: str | None`, `ntfy_token: str | None`, `herdr_report_agents: bool`; and `resolve_secret(raw: str | None) -> str | None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config.py
"""config.toml is read-only to the app: nothing in src/ writes it, so comments
and formatting survive and no TOML writer dependency is needed."""

import pytest

from buzz_fleet import config, paths


def _write(tmp_path, body: str, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    target = paths.config_dir() / "config.toml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body)


def test_defaults_apply_when_no_file_exists(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    cfg = config.load()
    assert cfg.default_community is None
    assert cfg.refresh_interval_ms == 2000
    assert cfg.default_view == "agents"
    assert cfg.confirm_destructive is True
    assert cfg.default_harness == "claude"
    assert cfg.herdr_report_agents is False


def test_values_are_read(monkeypatch, tmp_path) -> None:
    _write(
        tmp_path,
        """
[general]
default_community = "eltahir"

[ui]
refresh_interval_ms = 500
default_view = "runs"
confirm_destructive = false

[defaults]
harness = "codex"
""",
        monkeypatch,
    )
    cfg = config.load()
    assert cfg.default_community == "eltahir"
    assert cfg.refresh_interval_ms == 500
    assert cfg.default_view == "runs"
    assert cfg.confirm_destructive is False
    assert cfg.default_harness == "codex"


def test_partial_file_keeps_other_defaults(monkeypatch, tmp_path) -> None:
    _write(tmp_path, '[ui]\ntheme = "mono"\n', monkeypatch)
    cfg = config.load()
    assert cfg.theme == "mono"
    assert cfg.refresh_interval_ms == 2000


def test_env_indirection_resolves(monkeypatch, tmp_path) -> None:
    _write(tmp_path, '[notifier]\nntfy_token = "env:MY_TOKEN"\n', monkeypatch)
    monkeypatch.setenv("MY_TOKEN", "t0ken")
    assert config.load().ntfy_token == "t0ken"


def test_env_indirection_to_an_unset_variable_is_none(monkeypatch, tmp_path) -> None:
    _write(tmp_path, '[notifier]\nntfy_token = "env:MISSING"\n', monkeypatch)
    monkeypatch.delenv("MISSING", raising=False)
    assert config.load().ntfy_token is None


def test_literal_secret_is_refused(monkeypatch, tmp_path) -> None:
    """A secret-typed field must indirect through the environment. Accepting a
    literal would put a token in a file the user is invited to edit and share."""
    _write(tmp_path, '[notifier]\nntfy_token = "tk_literal"\n', monkeypatch)
    with pytest.raises(ValueError, match="must use env:"):
        config.load()


def test_malformed_toml_names_the_file(monkeypatch, tmp_path) -> None:
    _write(tmp_path, "[ui\n", monkeypatch)
    with pytest.raises(ValueError, match="config.toml"):
        config.load()


def test_unknown_default_view_is_refused(monkeypatch, tmp_path) -> None:
    _write(tmp_path, '[ui]\ndefault_view = "nonsense"\n', monkeypatch)
    with pytest.raises(ValueError, match="default_view"):
        config.load()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'buzz_fleet.config'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/buzz_fleet/config.py
"""Read-only view of `config.toml`.

Nothing in `src/` writes this file. That is deliberate: the user is invited to
edit and comment it, and a round-trip through a TOML writer would destroy both
— and would need a dependency, since `tomllib` reads only. Anything the *app*
sets (the active community, for one) is machine state, not config, and lives
under `paths.state_dir()` instead.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass

from buzz_fleet import paths

_VIEWS = ("agents", "runs", "tasks")
_ENV_PREFIX = "env:"


@dataclass(frozen=True)
class Config:
    default_community: str | None = None
    refresh_interval_ms: int = 2000
    default_view: str = "agents"
    theme: str = "buzz-fleet"
    confirm_destructive: bool = True
    default_harness: str = "claude"
    ntfy_url: str | None = None
    ntfy_token: str | None = None
    herdr_report_agents: bool = False


def resolve_secret(raw: str | None) -> str | None:
    """Secret-typed fields must indirect through the environment.

    A literal is refused rather than accepted-with-a-warning: config.toml is a
    file the user is encouraged to edit and hand around, and a token pasted
    into it would be the one plaintext secret outside the secrets tree.
    """
    if raw is None:
        return None
    if not raw.startswith(_ENV_PREFIX):
        raise ValueError(f"secret config values must use env:NAME, got {raw!r}")
    return os.environ.get(raw[len(_ENV_PREFIX) :]) or None


def load() -> Config:
    path = paths.config_dir() / "config.toml"
    if not path.exists():
        return Config()
    try:
        raw = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"{path} is not valid TOML: {e}") from e

    general = raw.get("general", {})
    ui = raw.get("ui", {})
    defaults = raw.get("defaults", {})
    notifier = raw.get("notifier", {})
    herdr = raw.get("herdr", {})

    view = ui.get("default_view", "agents")
    if view not in _VIEWS:
        raise ValueError(f"[ui] default_view must be one of {', '.join(_VIEWS)}, got {view!r}")

    return Config(
        default_community=general.get("default_community"),
        refresh_interval_ms=int(ui.get("refresh_interval_ms", 2000)),
        default_view=view,
        theme=ui.get("theme", "buzz-fleet"),
        confirm_destructive=bool(ui.get("confirm_destructive", True)),
        default_harness=defaults.get("harness", "claude"),
        ntfy_url=notifier.get("ntfy_url"),
        ntfy_token=resolve_secret(notifier.get("ntfy_token")),
        herdr_report_agents=bool(herdr.get("report_agents", False)),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_config.py -v`
Expected: 8 passed

- [ ] **Step 5: Add `config show` to the CLI**

```python
# src/buzz_fleet/cli/app.py
config_app = typer.Typer(help="Inspect configuration")
app.add_typer(config_app, name="config")


@config_app.command("show")
def config_show() -> None:
    """Print the effective configuration and where it was read from."""
    from dataclasses import asdict

    from buzz_fleet import config as config_module

    path = paths.config_dir() / "config.toml"
    typer.echo(f"# {path}{'' if path.exists() else '  (not present; showing defaults)'}")
    values = asdict(config_module.load())
    values["ntfy_token"] = "<set>" if values["ntfy_token"] else None
    for key, value in values.items():
        typer.echo(f"{key} = {value!r}")
```

- [ ] **Step 6: Document it in README.md**

Add a `### Configuration` section under the install instructions containing the annotated `config.toml` example from spec §4.2 verbatim, the statement that the file is never rewritten by buzz-fleet, and the note that secret-typed values must use `env:NAME`.

- [ ] **Step 7: Run the full suite, lint and commit**

```bash
uv run pytest -q && uv run ruff check src tests && uv run mypy src
git add -A src tests README.md
git commit -m "Add read-only config.toml with a config show command"
```

---

### Task 7: Active community and resolution order

`tui/screens/dashboard.py:25` hardcodes `CURRENT_COMMUNITY_ID = "eltahir"` (spec fact 5), and `resolve_identity` refuses to guess whenever more than one community exists. This adds the persisted pointer a toggle needs, without weakening the agent-identity rule.

**Files:**
- Modify: `src/buzz_fleet/state.py` (add two functions)
- Modify: `src/buzz_fleet/orchestration/identity.py:33-40`
- Modify: `src/buzz_fleet/tui/screens/dashboard.py:25` (delete the constant), `tui/app.py:9,17`, `tui/screens/connect.py:12,50`
- Test: `tests/test_state.py`, `tests/test_identity.py`

**Interfaces:**
- Consumes: `paths.state_dir` (Task 1), `atomic.write_secure` (Task 2), `config.load` (Task 6).
- Produces: `state.save_active_community(community_id: str) -> None`, `state.load_active_community() -> str | None`, and `identity.resolve_community_id(env: Mapping[str, str], explicit: str | None) -> str`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_identity.py
"""Resolution order (spec 5.2). Steps 1, 2, 5 and 6 are today's behaviour and
must not change; 3 and 4 are what make a toggle possible."""

import pytest

from buzz_fleet import paths, state
from buzz_fleet.orchestration import identity


def _community(monkeypatch, tmp_path, cid: str) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    from pydantic import SecretStr

    from buzz_fleet.models import Community

    state.save_community(
        Community(id=cid, relay_url="wss://r", relay_admin_nsec=SecretStr("nsec1x"))
    )


def test_explicit_argument_wins(monkeypatch, tmp_path) -> None:
    _community(monkeypatch, tmp_path, "eltahir")
    _community(monkeypatch, tmp_path, "acme")
    state.save_active_community("acme")
    assert identity.resolve_community_id({"BUZZ_FLEET_COMMUNITY": "eltahir"}, "acme") == "acme"


def test_env_var_beats_active_and_config(monkeypatch, tmp_path) -> None:
    _community(monkeypatch, tmp_path, "eltahir")
    _community(monkeypatch, tmp_path, "acme")
    state.save_active_community("acme")
    assert identity.resolve_community_id({"BUZZ_FLEET_COMMUNITY": "eltahir"}, None) == "eltahir"


def test_active_community_beats_config_default(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    cfg = paths.config_dir() / "config.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text('[general]\ndefault_community = "eltahir"\n')
    _community(monkeypatch, tmp_path, "eltahir")
    _community(monkeypatch, tmp_path, "acme")
    state.save_active_community("acme")
    assert identity.resolve_community_id({}, None) == "acme"


def test_config_default_used_when_nothing_active(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    cfg = paths.config_dir() / "config.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text('[general]\ndefault_community = "eltahir"\n')
    _community(monkeypatch, tmp_path, "eltahir")
    _community(monkeypatch, tmp_path, "acme")
    assert identity.resolve_community_id({}, None) == "eltahir"


def test_single_community_needs_no_pointer(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    _community(monkeypatch, tmp_path, "eltahir")
    assert identity.resolve_community_id({}, None) == "eltahir"


def test_several_communities_and_no_pointer_is_an_error(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    _community(monkeypatch, tmp_path, "eltahir")
    _community(monkeypatch, tmp_path, "acme")
    with pytest.raises(RuntimeError, match="acme, eltahir"):
        identity.resolve_community_id({}, None)


def test_no_community_at_all_is_an_error(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    with pytest.raises(RuntimeError, match="buzz-fleet connect"):
        identity.resolve_community_id({}, None)


def test_a_stale_pointer_is_ignored(monkeypatch, tmp_path) -> None:
    """A community deleted while it was active must not wedge every command."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    _community(monkeypatch, tmp_path, "eltahir")
    state.save_active_community("deleted-one")
    assert identity.resolve_community_id({}, None) == "eltahir"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_identity.py -v`
Expected: FAIL with `AttributeError: module 'buzz_fleet.state' has no attribute 'save_active_community'`

- [ ] **Step 3: Add the pointer to `state.py`**

```python
def _active_path() -> Path:
    return paths.state_dir() / "active-community"


def save_active_community(community_id: str) -> None:
    """What the toggle writes. Deliberately state, not config: config.toml
    holds the *default*, this holds what was last selected."""
    atomic.write_secure(_active_path(), community_id + "\n", mode=0o600)


def load_active_community() -> str | None:
    path = _active_path()
    return path.read_text().strip() or None if path.exists() else None
```

- [ ] **Step 4: Add the resolver to `identity.py`**

```python
def resolve_community_id(env: Mapping[str, str], explicit: str | None) -> str:
    """Spec 5.2. First match wins.

    A pointer naming a community that no longer exists is ignored rather than
    raising: deleting the active community must not wedge every later command.
    """
    ids = state.list_community_ids()
    if explicit:
        return explicit
    from_env = env.get("BUZZ_FLEET_COMMUNITY")
    if from_env:
        return from_env
    active = state.load_active_community()
    if active and active in ids:
        return active
    default = config.load().default_community
    if default and default in ids:
        return default
    if len(ids) == 1:
        return ids[0]
    if not ids:
        raise RuntimeError("no local community; run `buzz-fleet connect` first")
    raise RuntimeError(f"several local communities ({', '.join(ids)}); pass --community")
```

Then in `resolve_identity`, replace the inline block at lines 33-40 with `community_id = resolve_community_id(env, community_id)`. The `BUZZ_PRIVATE_KEY` + `BUZZ_RELAY_URL` branch above it is untouched: an agent running under its own unit still wins over all local state.

- [ ] **Step 5: Delete the hardcoded community from the TUI**

Delete `CURRENT_COMMUNITY_ID` from `tui/screens/dashboard.py:25` and its two
imports (`tui/app.py:9`, `tui/screens/connect.py:12`). `BuzzFleetApp` resolves
the community once and hands it down:

```python
# src/buzz_fleet/tui/app.py
class BuzzFleetApp(App):
    def on_mount(self) -> None:
        self.register_theme(BUZZ_FLEET_THEME)
        self.theme = "buzz-fleet"
        try:
            community_id = resolve_community_id(os.environ, None)
        except RuntimeError:
            # No community yet, or several with nothing selected. Connecting
            # is the answer to the first; the picker is the answer to the
            # second, and it is the same screen.
            self.push_screen(ConnectScreen())
            return
        self.push_screen(DashboardScreen(community_id))
```

```python
# src/buzz_fleet/tui/screens/dashboard.py
class DashboardScreen(Screen):
    def __init__(self, community_id: str) -> None:
        super().__init__()
        self._community_id = community_id
```

Replace every `state.load_community(CURRENT_COMMUNITY_ID)` in that file with
`state.load_community(self._community_id)`, and `agent_status(agent.id)` with
`agent_status(agent.community_id, agent.id)` per Task 4.

`ConnectScreen` gains an id input so connecting **adds** a community instead of
overwriting one — the current screen always writes `eltahir`:

```python
# src/buzz_fleet/tui/screens/connect.py — in compose(), above the relay input
yield Input(placeholder="Local id for this community, e.g. eltahir", id="id-input")
```

```python
# src/buzz_fleet/tui/screens/connect.py — in on_button_pressed
community_id = self.query_one("#id-input", Input).value.strip()
if not community_id:
    self.notify("A community id is required.", severity="error")
    return
if connect_and_save(runner, community_id, relay_url, admin_nsec):
    state.save_active_community(community_id)
    self.app.switch_screen(DashboardScreen(community_id))
```

- [ ] **Step 6: Run the full suite**

Run: `uv run pytest -q`
Expected: all pass. `tests/tui/test_connect.py` and `tests/tui/conftest.py` import `CURRENT_COMMUNITY_ID` and must be updated to pass an explicit id.

- [ ] **Step 7: Lint and commit**

```bash
uv run ruff check src tests && uv run mypy src
git add -A src tests
git commit -m "Add active-community pointer and full resolution order"
```

---

### Task 8: `migrate` — one resumable move onto the new layout

Everything above changes where files live and what units are called. This moves an existing machine, per-agent and idempotently, so an interruption is recoverable (spec 4.4, §13.4).

**Files:**
- Create: `src/buzz_fleet/migrate.py`
- Modify: `src/buzz_fleet/cli/app.py` (add `migrate`)
- Modify: `README.md` (upgrade instructions)
- Modify: `CLAUDE.md` (record the layout change under "Documentation debt")
- Test: `tests/test_migrate.py`

**Interfaces:**
- Consumes: everything from Tasks 1–7.
- Produces: `plan(runner: CommandRunner) -> list[Step]`; the frozen dataclass `Step` with fields `kind: str`, `description: str`, `source: Path | None`, `destination: Path | None`, `old_unit: str | None`, `new_unit: str | None`; `run(runner: CommandRunner, *, dry_run: bool = False) -> list[Step]` returning the steps actually applied; and the constant `LAYOUT_VERSION: int`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_migrate.py
"""The migration is per-agent and idempotent, so resuming after an interruption
is just running it again. It refuses to start while any unit is active, because
moving an agent's env file out from under a running unit is how you get an agent
holding a key it can no longer re-read."""

import subprocess
from pathlib import Path

import pytest

from buzz_fleet import migrate, paths


class FakeRunner:
    """`is-active` answers from `self.active`; everything else succeeds."""

    def __init__(self, active: set[str] | None = None) -> None:
        self.active = active or set()
        self.calls: list[list[str]] = []

    def run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        if "is-active" in args:
            unit = args[-1]
            return subprocess.CompletedProcess(
                args, 0, stdout="active" if unit in self.active else "inactive", stderr=""
            )
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")


def _legacy_tree(tmp_path: Path, monkeypatch) -> Path:
    """A pre-migration machine: one community, one agent, env and prompt files."""
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))

    legacy = home / ".config" / "buzz-fleet"
    (legacy / "communities" / "eltahir" / "agents").mkdir(parents=True)
    (legacy / "communities" / "eltahir.json").write_text(
        '{"id":"eltahir","relay_url":"wss://r","relay_admin_nsec":"nsec1owner"}'
    )
    (legacy / "communities" / "eltahir" / "agents" / "reviewer.json").write_text(
        '{"id":"reviewer","community_id":"eltahir","display_name":"R","harness":"claude",'
        '"private_key":"nsec1agent","public_key":"' + "a" * 64 + '",'
        '"system_prompt_source":{"kind":"inline","text":"hi"}}'
    )
    (legacy / "agents").mkdir(parents=True)
    (legacy / "agents" / "reviewer.env").write_text("BUZZ_PRIVATE_KEY=nsec1agent\n")
    (legacy / "agents" / "reviewer.prompt.md").write_text("hi")
    return legacy


def test_dry_run_changes_nothing(tmp_path, monkeypatch) -> None:
    legacy = _legacy_tree(tmp_path, monkeypatch)
    steps = migrate.run(FakeRunner(), dry_run=True)
    assert steps
    assert (legacy / "agents" / "reviewer.env").exists()
    assert not (paths.state_dir() / "communities" / "eltahir.json").exists()


def test_refuses_while_a_unit_is_active(tmp_path, monkeypatch) -> None:
    _legacy_tree(tmp_path, monkeypatch)
    runner = FakeRunner(active={"buzz-agent@reviewer.service"})
    with pytest.raises(RuntimeError, match="still active"):
        migrate.run(runner)


def test_moves_state_and_secrets_apart(tmp_path, monkeypatch) -> None:
    _legacy_tree(tmp_path, monkeypatch)
    migrate.run(FakeRunner())
    community = (paths.state_dir() / "communities" / "eltahir.json").read_text()
    assert "nsec1owner" not in community
    secret = (paths.secrets_dir() / "communities" / "eltahir.json").read_text()
    assert "nsec1owner" in secret


def test_env_file_moves_to_the_qualified_secret_path(tmp_path, monkeypatch) -> None:
    _legacy_tree(tmp_path, monkeypatch)
    migrate.run(FakeRunner())
    moved = paths.secrets_dir() / "units" / "eltahir:reviewer.env"
    assert moved.read_text() == "BUZZ_PRIVATE_KEY=nsec1agent\n"
    assert oct(moved.stat().st_mode & 0o777) == "0o600"


def test_prompt_file_moves_to_the_qualified_state_path(tmp_path, monkeypatch) -> None:
    _legacy_tree(tmp_path, monkeypatch)
    migrate.run(FakeRunner())
    assert (paths.state_dir() / "units" / "eltahir:reviewer.prompt.md").read_text() == "hi"


def test_old_unit_is_disabled_and_new_one_enabled(tmp_path, monkeypatch) -> None:
    _legacy_tree(tmp_path, monkeypatch)
    runner = FakeRunner()
    migrate.run(runner)
    flat = [" ".join(c) for c in runner.calls]
    assert any("disable --now buzz-agent@reviewer.service" in c for c in flat)
    assert any("enable --now buzz-agent@eltahir:reviewer.service" in c for c in flat)


def test_backup_is_taken_before_anything_moves(tmp_path, monkeypatch) -> None:
    _legacy_tree(tmp_path, monkeypatch)
    migrate.run(FakeRunner())
    backups = list((Path.home()).glob(".config/buzz-fleet.bak-*"))
    assert len(backups) == 1
    assert (backups[0] / "agents" / "reviewer.env").exists()


def test_running_twice_is_a_no_op(tmp_path, monkeypatch) -> None:
    _legacy_tree(tmp_path, monkeypatch)
    migrate.run(FakeRunner())
    second = migrate.run(FakeRunner())
    assert second == []


def test_resumes_a_half_migrated_tree(tmp_path, monkeypatch) -> None:
    """The interrupted case: the env file already moved, the prompt did not.
    Only the unfinished half is redone."""
    legacy = _legacy_tree(tmp_path, monkeypatch)
    already = paths.secrets_dir() / "units" / "eltahir:reviewer.env"
    already.parent.mkdir(parents=True, exist_ok=True)
    already.write_text("BUZZ_PRIVATE_KEY=nsec1agent\n")
    (legacy / "agents" / "reviewer.env").unlink()

    migrate.run(FakeRunner())
    assert (paths.state_dir() / "units" / "eltahir:reviewer.prompt.md").read_text() == "hi"
    assert already.read_text() == "BUZZ_PRIVATE_KEY=nsec1agent\n"


def test_writes_a_layout_version_marker(tmp_path, monkeypatch) -> None:
    _legacy_tree(tmp_path, monkeypatch)
    migrate.run(FakeRunner())
    assert (paths.state_dir() / "layout-version").read_text().strip() == str(migrate.LAYOUT_VERSION)


def test_no_legacy_tree_is_a_no_op(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert migrate.run(FakeRunner()) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_migrate.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'buzz_fleet.migrate'`

- [ ] **Step 3: Write the implementation**

```python
# src/buzz_fleet/migrate.py
"""Move a machine from the pre-XDG layout onto the current one, once.

Every step checks its own postcondition, so resuming after an interruption is
just running it again — which matters, because an interrupted run leaves some
agents on old unit names and some on new. Migration is per-agent for the same
reason: an agent already moved is skipped rather than re-moved.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from buzz_fleet import atomic, paths, state, units
from buzz_fleet.models import Agent, Community
from buzz_fleet.proc import CommandRunner

LAYOUT_VERSION = 2


@dataclass(frozen=True)
class Step:
    # Unit names are carried as fields rather than re-parsed out of
    # `description`: the description is for humans and will get reworded, and a
    # migration that renames the wrong unit is not a bug you notice quickly.
    kind: str
    description: str
    source: Path | None = None
    destination: Path | None = None
    old_unit: str | None = None
    new_unit: str | None = None


def _version_path() -> Path:
    return paths.state_dir() / "layout-version"


def _legacy_agent_ids(legacy: Path, community_id: str) -> list[str]:
    directory = legacy / "communities" / community_id / "agents"
    return sorted(p.stem for p in directory.glob("*.json")) if directory.exists() else []


def _legacy_community_ids(legacy: Path) -> list[str]:
    directory = legacy / "communities"
    return sorted(p.stem for p in directory.glob("*.json")) if directory.exists() else []


def _active_legacy_units(runner: CommandRunner, legacy: Path) -> list[str]:
    active = []
    for community_id in _legacy_community_ids(legacy):
        for agent_id in _legacy_agent_ids(legacy, community_id):
            unit = f"buzz-agent@{agent_id}.service"
            if runner.run(["systemctl", "--user", "is-active", unit]).stdout.strip() == "active":
                active.append(unit)
    return active


def plan(runner: CommandRunner) -> list[Step]:
    """Steps still outstanding. An already-migrated machine plans nothing."""
    legacy = paths.legacy_dir()
    if not (legacy / "communities").exists():
        return []

    steps: list[Step] = []
    for community_id in _legacy_community_ids(legacy):
        target = paths.state_dir() / "communities" / f"{community_id}.json"
        if not target.exists():
            steps.append(
                Step("community", f"split {community_id} into state and secrets",
                     legacy / "communities" / f"{community_id}.json", target)
            )
        for agent_id in _legacy_agent_ids(legacy, community_id):
            key = units.instance_key(community_id, agent_id)
            agent_target = paths.state_dir() / "communities" / community_id / "agents" / f"{agent_id}.json"
            if not agent_target.exists():
                steps.append(
                    Step("agent", f"split {key} into state and secrets",
                         legacy / "communities" / community_id / "agents" / f"{agent_id}.json",
                         agent_target)
                )
            env_source = legacy / "agents" / f"{agent_id}.env"
            env_target = paths.secrets_dir() / "units" / f"{key}.env"
            if env_source.exists() and not env_target.exists():
                steps.append(Step("env", f"move env file for {key}", env_source, env_target))
            prompt_source = legacy / "agents" / f"{agent_id}.prompt.md"
            prompt_target = paths.state_dir() / "units" / f"{key}.prompt.md"
            if prompt_source.exists() and not prompt_target.exists():
                steps.append(Step("prompt", f"move prompt for {key}", prompt_source, prompt_target))
            work_source = Path.home() / ".local" / "share" / "buzz-fleet" / "work" / agent_id
            work_target = paths.data_dir() / "work" / key
            if work_source.exists() and not work_target.exists():
                steps.append(Step("work", f"move workdir for {key}", work_source, work_target))
            # The legacy unit name is deliberately unqualified: that is what it
            # is actually called on a pre-migration machine.
            steps.append(
                Step(
                    "unit",
                    f"rename buzz-agent@{agent_id} to buzz-agent@{key}",
                    old_unit=f"buzz-agent@{agent_id}.service",
                    new_unit=units.unit_name(key),
                )
            )
    return steps


def run(runner: CommandRunner, *, dry_run: bool = False) -> list[Step]:
    """Apply the outstanding steps. Returns what was applied — empty when the
    machine is already on the current layout."""
    legacy = paths.legacy_dir()
    steps = plan(runner)
    if not steps or dry_run:
        return steps

    still_active = _active_legacy_units(runner, legacy)
    if still_active:
        raise RuntimeError(
            f"these units are still active: {', '.join(still_active)}. "
            "Stop them before migrating — moving an env file out from under a "
            "running unit leaves it holding a key it cannot re-read."
        )

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = legacy.parent / f"{legacy.name}.bak-{stamp}"
    shutil.copytree(legacy, backup)

    for step in steps:
        if step.kind == "community":
            # Re-saving through state.save_community is what performs the
            # secrets split: the legacy file has them inline.
            state.save_community(Community.model_validate(json.loads(step.source.read_text())))
        elif step.kind == "agent":
            state.save_agent(Agent.model_validate(json.loads(step.source.read_text())))
        elif step.kind in ("env", "prompt"):
            atomic.write_secure(step.destination, step.source.read_text(), mode=0o600)
        elif step.kind == "work":
            step.destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(step.source, step.destination)
        elif step.kind == "unit":
            runner.run(["systemctl", "--user", "disable", "--now", step.old_unit])
            runner.run(["systemctl", "--user", "enable", "--now", step.new_unit])

    atomic.write_secure(_version_path(), f"{LAYOUT_VERSION}\n", mode=0o600)
    return steps
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_migrate.py -v`
Expected: 11 passed

- [ ] **Step 5: Add the CLI command**

```python
# src/buzz_fleet/cli/app.py
@app.command()
def migrate(
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Print the plan and change nothing.")] = False,
) -> None:
    """Move this machine onto the current file layout and unit names.

    Never runs automatically: it stops units and moves key material. Safe to
    re-run — every step checks its own postcondition, so an interrupted
    migration resumes where it stopped.
    """
    from buzz_fleet import migrate as migrate_module

    try:
        steps = migrate_module.run(RealCommandRunner(), dry_run=dry_run)
    except RuntimeError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1) from e

    if not steps:
        typer.echo("Already on the current layout; nothing to do.")
        return
    for step in steps:
        typer.echo(f"{'would ' if dry_run else ''}{step.description}")
    typer.echo(f"\n{len(steps)} step(s){' planned' if dry_run else ' applied'}.")
```

- [ ] **Step 6: Run the full suite**

Run: `uv run pytest -q && uv run ruff check src tests && uv run mypy src`
Expected: all pass

- [ ] **Step 7: Document and commit**

Add an `### Upgrading from 0.8.x` section to `README.md`: stop agents (`systemctl --user stop 'buzz-agent@*'`), run `buzz-fleet migrate --dry-run`, review, run `buzz-fleet migrate`, confirm with `buzz-fleet agent list`. State that the backup is kept at `~/.config/buzz-fleet.bak-<timestamp>` and may be deleted once the fleet is healthy.

Add to `CLAUDE.md` under "Documentation debt": the file layout is now XDG-based with secrets in a parallel tree, and unit instance names are `<community>:<agent>` — anything referring to `~/.config/buzz-fleet` or `buzz-agent@<id>` is describing the pre-0.9 layout.

```bash
git add -A src tests README.md CLAUDE.md
git commit -m "Add resumable migrate command for the new layout"
```

---

---

### Task 9: `community` commands — list, use, show

Task 7 stores and reads the active Community but nothing sets it. Without this you can only change Community by connecting a new one or by exporting `BUZZ_FLEET_COMMUNITY`. These commands survive Phase C untouched — the CLI is not replaced by the frontend.

**Files:**
- Modify: `src/buzz_fleet/cli/app.py`
- Test: `tests/test_community_cli.py`

**Interfaces:**
- Consumes: `state.list_community_ids`, `state.load_community`, `state.load_agents`, `state.save_active_community` (Tasks 5, 7); `identity.resolve_community_id` (Task 7).
- Produces: the Typer group `community` with commands `list`, `use`, `show`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_community_cli.py
"""`community use` is the toggle for the command line. The picker screen in
Task 10 calls the same state function."""

import json

from pydantic import SecretStr
from typer.testing import CliRunner

from buzz_fleet import state
from buzz_fleet.cli.app import app
from buzz_fleet.models import Community

runner_cli = CliRunner()


def _connect(monkeypatch, tmp_path, cid: str) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.delenv("BUZZ_FLEET_COMMUNITY", raising=False)
    state.save_community(
        Community(id=cid, relay_url=f"wss://{cid}.example", relay_admin_nsec=SecretStr("nsec1x"))
    )


def test_list_shows_every_community(monkeypatch, tmp_path) -> None:
    _connect(monkeypatch, tmp_path, "eltahir")
    _connect(monkeypatch, tmp_path, "acme")
    result = runner_cli.invoke(app, ["community", "list"])
    assert result.exit_code == 0
    assert "eltahir" in result.stdout
    assert "acme" in result.stdout


def test_list_marks_the_active_community(monkeypatch, tmp_path) -> None:
    _connect(monkeypatch, tmp_path, "eltahir")
    _connect(monkeypatch, tmp_path, "acme")
    state.save_active_community("acme")
    lines = runner_cli.invoke(app, ["community", "list"]).stdout.splitlines()
    marked = [line for line in lines if line.startswith("*")]
    assert marked == ["* acme"]


def test_list_json_reports_active(monkeypatch, tmp_path) -> None:
    _connect(monkeypatch, tmp_path, "eltahir")
    _connect(monkeypatch, tmp_path, "acme")
    state.save_active_community("acme")
    payload = json.loads(runner_cli.invoke(app, ["community", "list", "--json"]).stdout)
    assert {"id": "acme", "active": True} in payload
    assert {"id": "eltahir", "active": False} in payload


def test_list_with_no_communities_points_at_connect(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    result = runner_cli.invoke(app, ["community", "list"])
    assert result.exit_code == 0
    assert "buzz-fleet connect" in result.stdout


def test_use_sets_the_active_community(monkeypatch, tmp_path) -> None:
    _connect(monkeypatch, tmp_path, "eltahir")
    _connect(monkeypatch, tmp_path, "acme")
    result = runner_cli.invoke(app, ["community", "use", "acme"])
    assert result.exit_code == 0
    assert state.load_active_community() == "acme"


def test_use_rejects_an_unknown_community(monkeypatch, tmp_path) -> None:
    _connect(monkeypatch, tmp_path, "eltahir")
    result = runner_cli.invoke(app, ["community", "use", "nope"])
    assert result.exit_code == 1
    assert "eltahir" in result.stdout


def test_use_does_not_change_the_pointer_on_rejection(monkeypatch, tmp_path) -> None:
    _connect(monkeypatch, tmp_path, "eltahir")
    state.save_active_community("eltahir")
    runner_cli.invoke(app, ["community", "use", "nope"])
    assert state.load_active_community() == "eltahir"


def test_show_reports_the_active_community(monkeypatch, tmp_path) -> None:
    _connect(monkeypatch, tmp_path, "eltahir")
    state.save_active_community("eltahir")
    out = runner_cli.invoke(app, ["community", "show"]).stdout
    assert "eltahir" in out
    assert "wss://eltahir.example" in out


def test_show_without_an_active_community_fails(monkeypatch, tmp_path) -> None:
    _connect(monkeypatch, tmp_path, "eltahir")
    _connect(monkeypatch, tmp_path, "acme")
    result = runner_cli.invoke(app, ["community", "show"])
    assert result.exit_code == 1
    assert "community use" in result.stdout
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_community_cli.py -v`
Expected: FAIL — `No such command 'community'`

- [ ] **Step 3: Add the command group**

```python
# src/buzz_fleet/cli/app.py
community_app = typer.Typer(help="List the connected communities and choose the active one")
app.add_typer(community_app, name="community")


def _active_or_none() -> str | None:
    # resolve_community_id raises when several communities exist and none is
    # selected. For `list` that is a state to display, not an error.
    from buzz_fleet.orchestration.identity import resolve_community_id

    try:
        return resolve_community_id(os.environ, None)
    except RuntimeError:
        return None


@community_app.command("list")
def community_list(
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List every connected community and mark the active one."""
    ids = state.list_community_ids()
    active = _active_or_none()
    if as_json:
        typer.echo(json.dumps([{"id": i, "active": i == active} for i in ids]))
        return
    if not ids:
        typer.echo("No communities yet. Run `buzz-fleet connect` first.")
        return
    for community_id in ids:
        typer.echo(f"{'*' if community_id == active else ' '} {community_id}")
    if active is None:
        typer.echo("\nNo active community. Run `buzz-fleet community use <id>`.")


@community_app.command("use")
def community_use(
    community_id: Annotated[str, typer.Argument(help="The community to make active")],
) -> None:
    """Set the active community for this machine."""
    ids = state.list_community_ids()
    if community_id not in ids:
        typer.echo(
            f"No community {community_id!r}. Known: {', '.join(ids) or 'none'}", err=True
        )
        raise typer.Exit(code=1)
    state.save_active_community(community_id)
    typer.echo(f"Active community: {community_id}")


@community_app.command("show")
def community_show() -> None:
    """Show the active community, its relay, and how many agents it has."""
    active = _active_or_none()
    if active is None:
        typer.echo(
            "No active community. Run `buzz-fleet community use <id>`.", err=True
        )
        raise typer.Exit(code=1)
    community = state.load_community(active)
    if community is None:
        typer.echo(f"Community {active!r} is selected but its file is missing.", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"id:            {community.id}")
    typer.echo(f"relay:         {community.relay_url}")
    typer.echo(f"agents:        {len(state.load_agents(active))}")
    typer.echo(f"fleet channel: {community.fleet_channel_id or '(none)'}")
```

Add `import json` and `import os` to the module's imports if they are not already present.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_community_cli.py -v`
Expected: 9 passed

- [ ] **Step 5: Document and commit**

Add a `### Communities` section to `README.md` showing `buzz-fleet community list`, `use` and `show`, and stating that `--community` and `BUZZ_FLEET_COMMUNITY` both override the active community for one command.

```bash
uv run pytest -q && uv run ruff check src tests && uv run mypy src
git add -A src tests README.md
git commit -m "Add community list, use and show commands"
```

---

### Task 10: Community picker screen (optional — Phase C retires it)

**Read this before starting.** This screen lives in the Textual TUI, which Phase C replaces at cutover. It is roughly 60 lines you will build once more in OpenTUI. It is worth it only if you want the toggle in the TUI you use daily, months before Phase C lands. It is the last task deliberately: drop it and Phase A is still complete.

**Files:**
- Create: `src/buzz_fleet/tui/screens/community_picker.py`
- Modify: `src/buzz_fleet/tui/screens/dashboard.py` (one binding, one action)
- Test: `tests/tui/test_community_picker.py`

**Interfaces:**
- Consumes: `state.list_community_ids`, `state.load_community`, `state.load_agents`, `state.save_active_community` (Tasks 5, 7); `DashboardScreen(community_id: str)` (Task 7).
- Produces: `CommunityPickerScreen(active: str)`, a `ModalScreen[str | None]` that dismisses with the chosen community id, or `None` on cancel.

- [ ] **Step 1: Write the failing test**

```python
# tests/tui/test_community_picker.py
"""The picker writes the same active-community pointer that
`buzz-fleet community use` writes. One source of truth, two front doors."""

from pydantic import SecretStr

from buzz_fleet import state
from buzz_fleet.models import Community
from buzz_fleet.tui.app import BuzzFleetApp
from buzz_fleet.tui.screens.community_picker import CommunityPickerScreen


def _connect(monkeypatch, tmp_path, cid: str) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    state.save_community(
        Community(id=cid, relay_url=f"wss://{cid}.example", relay_admin_nsec=SecretStr("nsec1x"))
    )


async def test_lists_every_community(monkeypatch, tmp_path) -> None:
    _connect(monkeypatch, tmp_path, "eltahir")
    _connect(monkeypatch, tmp_path, "acme")
    app = BuzzFleetApp()
    async with app.run_test() as pilot:
        await app.push_screen(CommunityPickerScreen("eltahir"))
        await pilot.pause()
        table = app.screen.query_one("#community-table")
        shown = {str(table.get_row_at(row)[1]) for row in range(table.row_count)}
        assert shown == {"eltahir", "acme"}


async def test_marks_the_active_community(monkeypatch, tmp_path) -> None:
    _connect(monkeypatch, tmp_path, "eltahir")
    _connect(monkeypatch, tmp_path, "acme")
    app = BuzzFleetApp()
    async with app.run_test() as pilot:
        await app.push_screen(CommunityPickerScreen("acme"))
        await pilot.pause()
        table = app.screen.query_one("#community-table")
        rows = {str(table.get_row_at(r)[1]): str(table.get_row_at(r)[0]) for r in range(table.row_count)}
        assert rows["acme"].strip() == "*"
        assert rows["eltahir"].strip() == ""


async def test_choosing_saves_the_active_community(monkeypatch, tmp_path) -> None:
    _connect(monkeypatch, tmp_path, "eltahir")
    _connect(monkeypatch, tmp_path, "acme")
    state.save_active_community("eltahir")
    app = BuzzFleetApp()
    async with app.run_test() as pilot:
        await app.push_screen(CommunityPickerScreen("eltahir"))
        await pilot.pause()
        table = app.screen.query_one("#community-table")
        target = next(r for r in range(table.row_count) if str(table.get_row_at(r)[1]) == "acme")
        table.move_cursor(row=target)
        await pilot.press("enter")
        await pilot.pause()
    assert state.load_active_community() == "acme"


async def test_escape_cancels_without_changing_anything(monkeypatch, tmp_path) -> None:
    _connect(monkeypatch, tmp_path, "eltahir")
    _connect(monkeypatch, tmp_path, "acme")
    state.save_active_community("eltahir")
    app = BuzzFleetApp()
    async with app.run_test() as pilot:
        await app.push_screen(CommunityPickerScreen("eltahir"))
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
    assert state.load_active_community() == "eltahir"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/tui/test_community_picker.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'buzz_fleet.tui.screens.community_picker'`

- [ ] **Step 3: Write the screen**

```python
# src/buzz_fleet/tui/screens/community_picker.py
"""Choose the active community.

This writes the same pointer as `buzz-fleet community use`, through the same
`state.save_active_community` function, so the CLI and the TUI can never
disagree about which community is active.
"""

from __future__ import annotations

from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Label

from buzz_fleet import state
from buzz_fleet.tui.theme import PANEL_BORDER


class CommunityPickerScreen(ModalScreen[str | None]):
    """List the connected communities and dismiss with the chosen id."""

    DEFAULT_CSS = f"""
    CommunityPickerScreen {{
        align: center middle;

        #picker-dialog {{
            width: auto;
            max-width: 80;
            height: auto;
            border: round {PANEL_BORDER};
            background: $surface;
            padding: 1 2;
        }}

        #picker-hint {{
            margin-top: 1;
        }}
    }}
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, active: str) -> None:
        super().__init__()
        self._active = active

    def compose(self) -> ComposeResult:
        dialog = Vertical(id="picker-dialog")
        dialog.border_title = "Switch community"
        with dialog:
            yield DataTable(id="community-table", cursor_type="row")
            yield Label("Enter to switch, Escape to cancel.", id="picker-hint")

    def on_mount(self) -> None:
        table = self.query_one("#community-table", DataTable)
        table.add_columns("", "community", "relay", "agents")
        for community_id in state.list_community_ids():
            community = state.load_community(community_id)
            if community is None:
                continue
            marker = Text("*", style="bold") if community_id == self._active else Text("")
            table.add_row(
                marker,
                community_id,
                community.relay_url,
                str(len(state.load_agents(community_id))),
            )
        table.focus()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        table = self.query_one("#community-table", DataTable)
        if table.row_count == 0:
            return
        chosen = str(table.get_row_at(event.cursor_row)[1])
        state.save_active_community(chosen)
        self.dismiss(chosen)

    def action_cancel(self) -> None:
        self.dismiss(None)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/tui/test_community_picker.py -v`
Expected: 4 passed

- [ ] **Step 5: Reach it from the dashboard**

```python
# src/buzz_fleet/tui/screens/dashboard.py — add to BINDINGS
        Binding("s", "switch_community", "Switch community"),
```

```python
# src/buzz_fleet/tui/screens/dashboard.py — add the action
    def action_switch_community(self) -> None:
        def on_chosen(chosen: str | None) -> None:
            # Rebuilding the screen is deliberate: every widget on it is bound
            # to one community's agents, so switching in place would mean
            # resetting each of them by hand.
            if chosen is not None and chosen != self._community_id:
                self.app.switch_screen(DashboardScreen(chosen))

        self.app.push_screen(CommunityPickerScreen(self._community_id), on_chosen)
```

Import `CommunityPickerScreen` at the top of `dashboard.py`.

- [ ] **Step 6: Run the full suite, lint and commit**

Run: `uv run pytest -q && uv run ruff check src tests && uv run mypy src`
Expected: all pass

Add the `s` key to `README.md`'s "Manage agents (TUI)" key list, beside the existing `c`, `u`, `x` and `l`.

```bash
git add -A src tests README.md
git commit -m "Add a community picker screen to the TUI"
```

## Verification

After Task 8, on a throwaway machine or container with a real systemd user instance:

- [ ] `uv run pytest -q` — full suite green
- [ ] `uv run ruff check src tests` and `uv run mypy src` — clean
- [ ] Create two communities with an identically-named agent in each; confirm two distinct units exist and each loads its own key (`systemctl --user show buzz-agent@<c>:<a> -p Id` and the env file's `BUZZ_PRIVATE_KEY`). **This is the bug Phase A exists to fix; it must be demonstrated, not assumed.**
- [ ] `grep -rn "CONFIG_DIR\|Path.home() / \".config\"" src/` returns only `paths.py` and `systemd.template_unit_path`
- [ ] `grep -rn "O_TRUNC" src/` returns nothing
- [ ] Interrupt `migrate` with SIGKILL partway, re-run it, confirm the fleet comes up healthy
