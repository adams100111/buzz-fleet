"""Community-qualified systemd instance names.

Agent ids are unique within a community (`manager.create_agent` derives them
from that community's agents alone), but the unit template is global. Before
this module two communities with a `reviewer` shared one
`buzz-agent@reviewer.service`, so whichever env file was written last decided
which Nostr key the unit loaded — silently, and with the wrong identity.

The separator is ':' because systemd.unit(5) leaves it unescaped and the slug
charset ([a-z0-9-]) excludes it, so `<community>:<agent>` is unambiguous where
`<community>-<agent>` would not be when either half contains a dash.

Keys are validated and used verbatim — no escaping. A literal '-' is not
escaped by systemd because '-' is systemd's escape-sequence for '/', and a
literal dash is valid in a systemd unit name (every real agent uses dashes:
'my-lara-cdx', 'my-dotnet-cdx', 'laravel-backend-developer-claude'). Escaping
them to '\x2d' would corrupt the names on disk. The unit template must use %i
(literal instance specifier) and never %I (unescaped specifier), because they
differ for dashed names — %I unescapes '-' back to '/'.
"""

from __future__ import annotations

TEMPLATE = "buzz-agent@.service"
SEPARATOR = ":"

# Valid characters in a systemd instance name: alphanumerics, dashes, underscores,
# dots (not leading), and colons. Matches the slug charset [a-z0-9-] plus the
# separator `:` and the non-slug safe characters `_` and `.`.
_VALID = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-_.:")


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


def validate_instance_key(key: str) -> None:
    """Validate an instance key for use in a systemd unit name.

    Raises ValueError if the key contains invalid characters or starts with '.'.
    A leading dot is invalid because systemd would escape it as \x2e.
    """
    if key.startswith("."):
        raise ValueError(
            f"instance key {key!r} starts with invalid character '.'"
        )
    for char in key:
        if char not in _VALID:
            raise ValueError(
                f"instance key {key!r} contains invalid character {char!r}"
            )


def unit_name(key: str) -> str:
    """The full unit name for an instance key, used verbatim.

    Validates the key and returns the unit name. The template uses %i (literal
    instance specifier), not %I, to preserve dashes in the instance name.
    """
    validate_instance_key(key)
    prefix, _, suffix = TEMPLATE.partition("@")
    return f"{prefix}@{key}{suffix}"
