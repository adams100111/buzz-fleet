"""Shared "check relay connection then save community" logic.

Used by both `cli/app.py`'s `connect` command and the TUI's `ConnectScreen` so
the two surfaces don't duplicate the check-then-save flow.
"""

from __future__ import annotations

from buzz_fleet import signer_client, state
from buzz_fleet.models import Community
from buzz_fleet.proc import CommandRunner


def connect_and_save(runner: CommandRunner, community_id: str, relay_url: str, admin_nsec: str) -> bool:
    """Check that `admin_nsec` authenticates against `relay_url`, and if so save the community.

    Returns True on success (community saved), False on failure (nothing saved).
    """
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
