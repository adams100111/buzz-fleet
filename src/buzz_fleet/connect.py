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

# Generous — real ids are short slugs like "eltahir" — but a cap all the
# same: an unbounded id otherwise passes validation and only fails later
# with a raw OSError (ENAMETOOLONG) from the filesystem, once it's already
# part of a path.
_MAX_LENGTH = 64


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
    if len(community_id) > _MAX_LENGTH:
        raise ValueError(
            f"community id {community_id!r} is {len(community_id)} characters; "
            f"the limit is {_MAX_LENGTH}"
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

    Reconnecting an id that already exists preserves `display_name`,
    `fleet_channel_id` and `fleet_record` rather than wiping them: the
    connect screen is the de-facto way to switch/re-auth a community, and
    only `relay_url`, the admin nsec and `owner_pubkey` are what the caller
    is actually re-supplying here. The latter two self-heal on next use via
    `AgentManager.ensure_fleet_record`, but `display_name` has no such
    backfill — dropping it would just be gone.
    """
    validate_community_id(community_id)
    if not signer_client.check_connection(runner, relay_url, admin_nsec):
        return False
    owner_pubkey = signer_client.pubkey_from_nsec(runner, admin_nsec)
    existing = state.load_community(community_id)
    state.save_community(
        Community(
            id=community_id,
            relay_url=relay_url,
            relay_admin_nsec=admin_nsec,
            owner_pubkey=owner_pubkey,
            display_name=existing.display_name if existing else None,
            fleet_channel_id=existing.fleet_channel_id if existing else None,
            fleet_record=existing.fleet_record if existing else None,
        )
    )
    return True
