"""Complete relay reads (paged, pushed-down filters only), member resolution, and posting. Spec 5.2."""

from __future__ import annotations

from dataclasses import dataclass

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
    boundary second is re-fetched and de-duplicated rather than lost.

    A *full* page (exactly `limit` rows) that adds nothing new means more events
    share that boundary `created_at` than fit in one page: an inclusive `until`
    cannot advance past it, so there is no way to page further without silently
    returning partial history. That is refused with a RuntimeError rather than
    treated as "done" -- a caller reconstructing state from a truncated read would
    have no way to tell it apart from a genuinely complete one.
    """
    seen: dict[str, dict] = {}
    f = dict(filter)
    while True:
        page = signer_client.query(runner, ident.relay_url, ident.nsec, f, auth_tag=ident.auth_tag)
        limit = f.get("limit", PAGE)
        new = [e for e in page if e["id"] not in seen]
        if not new:
            if len(page) >= limit:
                raise RuntimeError(
                    f"the relay returned a full page of {len(page)} events all sharing "
                    f"created_at {f.get('until')}; history cannot be paged past it"
                )
            break
        for e in new:
            seen[e["id"]] = e
        if len(page) < limit:
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
    """The fleet directory: every channel member joined with its owner-signed
    managed-agent record (role/capabilities/description/harness/host/
    version), its relay presence (online/last_seen), and its live task count
    from the reducer. Each source is independently best-effort -- a member
    with no managed-agent record, no presence entry, or no open tasks still
    gets a row, with the corresponding fields None/empty/0 rather than the
    row vanishing or the join raising.

    The retrieval key and any conductor pubkeys are real channel members
    (they hold keypairs the fleet posts to/reads as) but are not agents, so
    both are excluded from the listing whenever a fleet record is known.
    `ident.record` is `None` for an identity -- typically an agent's own --
    that has never seen a fleet record, in which case nothing is excluded
    (there is nothing to exclude it *with*).
    """
    default_channel, _ = _require(ident)
    channel = channel_id or default_channel
    members = signer_client.channel_members(runner, ident.relay_url, ident.nsec, channel, auth_tag=ident.auth_tag)
    if ident.record:
        excluded = {ident.record.retrieval_key, *(c.pubkey for c in ident.record.conductors.values())}
        members = [(pk, name) for pk, name in members if pk not in excluded]
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


def post(runner: CommandRunner, ident: Identity, channel_id: str, msg: OutgoingMessage) -> str:
    return signer_client.post_message(runner, ident.relay_url, ident.nsec, channel_id, msg.content, mentions=msg.mentions,
                                      root=msg.root, parent=msg.parent, tags=msg.tags, auth_tag=ident.auth_tag)
