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
