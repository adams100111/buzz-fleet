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
