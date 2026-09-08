"""Who is running this command: a fleet agent (env from its unit) or the owner (local state)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from buzz_fleet import config, signer_client, state
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
    # Checked before config.load(): with exactly one local community, a
    # config default can only ever resolve to that same id (or fail its
    # `in ids` check and fall through to this same line anyway) — so it's a
    # no-op here, and checking it first would mean a config.toml typo (a bad
    # bool/enum/refresh_interval_ms, or invalid TOML — config.load() raises
    # ValueError for all of those) breaks resolution even for a user who has
    # nothing ambiguous to resolve at all.
    if len(ids) == 1:
        return ids[0]
    default = config.load().default_community
    if default and default in ids:
        return default
    if not ids:
        raise RuntimeError("no local community; run `buzz-fleet connect` first")
    raise RuntimeError(f"several local communities ({', '.join(ids)}); pass --community")


def resolve_identity(env: Mapping[str, str], runner: CommandRunner, community_id: str | None) -> Identity:
    nsec, relay_url = env.get("BUZZ_PRIVATE_KEY"), env.get("BUZZ_RELAY_URL")
    if nsec and relay_url:
        return Identity(nsec=nsec, pubkey=signer_client.pubkey_from_nsec(runner, nsec), relay_url=relay_url,
                        auth_tag=env.get("BUZZ_AUTH_TAG") or None, fleet_channel=env.get("BUZZ_FLEET_CHANNEL") or None,
                        retrieval_key=env.get("BUZZ_FLEET_RETRIEVAL_KEY") or None, is_owner=False,
                        owner_pubkey=env.get("BUZZ_ACP_AGENT_OWNER") or None, record=None)
    try:
        community_id = resolve_community_id(env, community_id)
    except RuntimeError as e:
        if str(e).startswith("no local community"):
            # resolve_community_id's own message is generic — it's also
            # called directly by the TUI, which has no BUZZ_PRIVATE_KEY
            # concept at all. Here, reaching this point already means
            # neither BUZZ_PRIVATE_KEY nor BUZZ_RELAY_URL was set, which is
            # the more actionable half of "why did this fail" for an agent
            # running under an incomplete unit environment.
            raise RuntimeError(
                "no BUZZ_PRIVATE_KEY in the environment and no local community; "
                "run `buzz-fleet connect` first"
            ) from e
        raise
    community = state.load_community(community_id)
    if community is None:
        raise RuntimeError(f"no community '{community_id}'; run `buzz-fleet connect` first")
    nsec = community.relay_admin_nsec.get_secret_value()
    pubkey = community.owner_pubkey or signer_client.pubkey_from_nsec(runner, nsec)
    return Identity(nsec=nsec, pubkey=pubkey, relay_url=community.relay_url, auth_tag=None,
                    fleet_channel=community.fleet_channel_id,
                    retrieval_key=community.fleet_record.retrieval_key if community.fleet_record else None,
                    is_owner=True, owner_pubkey=pubkey, record=community.fleet_record)
