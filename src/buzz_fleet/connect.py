"""Shared "check relay connection then save community" logic.

Used by both `cli/app.py`'s `connect` command and the TUI's `ConnectScreen` so
the two surfaces don't duplicate the check-then-save flow.
"""

from __future__ import annotations

from buzz_fleet import signer_client, state
from buzz_fleet.models import Community
from buzz_fleet.proc import CommandRunner

# A community id becomes a path segment (state.py's communities/<id>.json and
# secrets/communities/<id>.json), an advisory lock filename (state.py's
# ".<id>.lock"), and half of a systemd instance name (units.instance_key,
# joined with ':'). The allowed charset is the intersection of what all three
# tolerate: slug.py's agent-id charset ([a-z0-9-]) widened to also allow
# uppercase, digits, '_' and '.' the way units.py's instance-name charset
# does, but with ':' excluded — a colon would be indistinguishable from
# units.SEPARATOR to units.split_key.
_VALID_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_."
)


def validate_community_id(community_id: str) -> None:
    """Reject a community id that is not a safe single path segment.

    Raises ValueError naming exactly what's wrong. This is the real fix for
    the free-form `Community.id` a bad `connect --id` would otherwise let
    through; `units.validate_instance_key` downstream is only a backstop.
    """
    if not community_id:
        raise ValueError("community id must not be empty")
    if community_id.startswith("."):
        raise ValueError(
            f"community id {community_id!r} must not start with '.' (would hide the file "
            "and, for '..', escape the state directory)"
        )
    for char in community_id:
        if char not in _VALID_CHARS:
            raise ValueError(
                f"community id {community_id!r} contains invalid character {char!r}; "
                "only letters, digits, '-', '_' and '.' are allowed (no '/', no ':', no spaces)"
            )


def connect_and_save(runner: CommandRunner, community_id: str, relay_url: str, admin_nsec: str) -> bool:
    """Check that `admin_nsec` authenticates against `relay_url`, and if so save the community.

    Returns True on success (community saved), False on failure (nothing saved).
    Raises ValueError (before any I/O or network call) if `community_id` is
    not a safe single path segment — see `validate_community_id`.
    """
    validate_community_id(community_id)
    if not signer_client.check_connection(runner, relay_url, admin_nsec):
        return False
    owner_pubkey = signer_client.pubkey_from_nsec(runner, admin_nsec)
    state.save_community(
        Community(
            id=community_id,
            relay_url=relay_url,
            relay_admin_nsec=admin_nsec,
            owner_pubkey=owner_pubkey,
        )
    )
    return True
