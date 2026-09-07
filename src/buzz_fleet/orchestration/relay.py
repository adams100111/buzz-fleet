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
