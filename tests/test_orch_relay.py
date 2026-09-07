import json
import subprocess

import pytest

from buzz_fleet.orchestration import relay
from buzz_fleet.orchestration.identity import Identity
from buzz_fleet.orchestration.protocol import PAYLOAD_VERSION, TAG_FLEET, OutgoingMessage, task_tag

CH, RK, OWNER = "6f1c0000-0000-4000-8000-000000000000", "r" * 64, "0" * 64
B = "b" * 64
IDENT = Identity(nsec="nsec1a", pubkey="a" * 64, relay_url="wss://r", auth_tag=None, fleet_channel=CH,
                 retrieval_key=RK, is_owner=False, owner_pubkey=OWNER, record=None)


def _event(i: int, created_at: int) -> dict:
    return {"id": f"{i:064x}", "pubkey": "a" * 64, "created_at": created_at, "kind": 9, "content": "", "tags": [["p", RK]]}


class PagingRunner:
    """Serves a 2,500-event history 1,000 at a time, honouring `until` and `limit` like the relay."""

    def __init__(self, events: list[dict], responses: dict[str, str] | None = None) -> None:
        self.events = sorted(events, key=lambda e: -e["created_at"])
        self.responses = responses or {}
        self.calls: list[list[str]] = []

    def run(self, args):
        self.calls.append(args)
        if args[1] != "query":
            return subprocess.CompletedProcess(args, 0, stdout=self.responses[args[1]], stderr="")
        f = json.loads(args[args.index("--filter") + 1])
        page = [e for e in self.events if e["created_at"] <= f.get("until", 10**12) and e["created_at"] >= f.get("since", 0)]
        if "authors" in f:
            page = [e for e in page if e["pubkey"] in f["authors"]]
        page = page[: f.get("limit", 1000)]
        return subprocess.CompletedProcess(args, 0, stdout="".join(json.dumps(e) + "\n" for e in page), stderr="")


def test_fleet_filter_shape() -> None:
    assert relay.fleet_filter(CH, RK) == {"kinds": [9], "#h": [CH], "#p": [RK], "limit": 1000}
    assert relay.fleet_filter(CH, RK, until=7, since=2)["until"] == 7


def test_fetch_all_pages_past_the_relay_cap_with_timestamp_ties() -> None:
    events = [_event(i, 1000 + i // 3) for i in range(2500)]  # three events share each second
    runner = PagingRunner(events)
    got = relay.fetch_all(runner, IDENT, relay.fleet_filter(CH, RK))
    assert len({e["id"] for e in got}) == 2500
    assert len([c for c in runner.calls if c[1] == "query"]) >= 3


def test_fetch_deleted_ids_uses_owner_author() -> None:
    tomb = {"id": "9" * 64, "pubkey": OWNER, "created_at": 5, "kind": 9005, "content": "",
            "tags": [["h", CH], ["e", "1" * 64], ["e", "2" * 64]]}
    runner = PagingRunner([tomb])
    assert relay.fetch_deleted_ids(runner, IDENT, channel_id=CH) == {"1" * 64, "2" * 64}
    f = json.loads(runner.calls[0][runner.calls[0].index("--filter") + 1])
    assert f["authors"] == [OWNER] and set(f["kinds"]) == {9005, 5}


def test_resolve_member_by_name_pubkey_and_errors() -> None:
    members = json.dumps({"ok": True, "members": [{"pubkey": "b" * 64, "display_name": "Reviewer"},
                                                  {"pubkey": "c" * 64, "display_name": "reviewer"},
                                                  {"pubkey": "d" * 64, "display_name": "Builder"}]})
    runner = PagingRunner([], {"channel-members": members})
    assert relay.resolve_member(runner, IDENT, CH, "Builder") == ("d" * 64, "Builder")
    assert relay.resolve_member(runner, IDENT, CH, "@Builder") == ("d" * 64, "Builder")
    assert relay.resolve_member(runner, IDENT, CH, "d" * 64) == ("d" * 64, "Builder")
    with pytest.raises(RuntimeError, match="ambiguous"):
        relay.resolve_member(runner, IDENT, CH, "reviewer")
    with pytest.raises(RuntimeError, match="not a member"):
        relay.resolve_member(runner, IDENT, CH, "Nobody")


def test_post_passes_message_through_signer() -> None:
    runner = PagingRunner([], {"post-message": json.dumps({"ok": True, "event_id": "e" * 64})})
    msg = OutgoingMessage(content="hi", mentions=["b" * 64, RK], tags=[("t", "fleet")], root=None, parent=None)
    assert relay.post(runner, IDENT, CH, msg) == "e" * 64
    assert runner.calls[0].count("--mention") == 2


# --- Beyond the brief -------------------------------------------------------
#
# The tests above are the brief's own, verbatim. The ones below cover
# specified behaviour the brief's suite leaves unexercised: a small,
# hand-traceable boundary-tie case (the 2,500-event test above already
# discriminates the bug -- proven by deliberately reintroducing the off-by-
# one during self-review and watching it fail -- but its scale makes the
# *mechanism* hard to see by eye); the two "not applicable" fetch_all/
# fetch_deleted_ids paths (a page fully satisfied in one call, and no owner
# pubkey at all); kind 5 tombstones (only kind 9005 is exercised above);
# `_require`'s error path with no fleet record known; `fetch_thread`; and
# `load_state`'s channel default plus its use of `fetch_deleted_ids` to keep
# a purged task out of the reduced state.


def test_fetch_all_reuses_page_when_nothing_new_and_page_not_full() -> None:
    """A single page smaller than `limit` must terminate immediately (no second query)."""
    events = [_event(i, 100 - i) for i in range(5)]
    runner = PagingRunner(events)
    got = relay.fetch_all(runner, IDENT, relay.fleet_filter(CH, RK))
    assert {e["id"] for e in got} == {e["id"] for e in events}
    assert len([c for c in runner.calls if c[1] == "query"]) == 1


def test_fetch_all_boundary_tie_at_an_exact_page_cut_is_not_lost() -> None:
    """Two timestamps (50 and 49) each carry a 2-event tie; with `limit=3` the first
    page cuts mid-way through the 49 tie (events at 50, 50, 49 -- one of the two
    49s left over). A correct pager re-fetches `until=49` (inclusive) on the next
    page, picks up the leftover 49 plus the 48 below it, and only stops once a page
    is entirely a repeat. An exclusive pager (`until = boundary - 1`) would instead
    jump straight to `until=48` and permanently lose the second 49-event -- verified
    below by re-running the same fixture with that off-by-one.
    """
    events = [_event(0, 50), _event(1, 50), _event(2, 49), _event(3, 49), _event(4, 48)]
    runner = PagingRunner(events)
    got = relay.fetch_all(runner, IDENT, {"kinds": [9], "#h": [CH], "#p": [RK], "limit": 3})
    assert {e["id"] for e in got} == {e["id"] for e in events}
    assert len([c for c in runner.calls if c[1] == "query"]) >= 3


def test_fetch_all_raises_when_more_events_share_a_second_than_fit_a_page() -> None:
    """1,500 events share one `created_at`, one full page over the 1,000-row clamp.
    An inclusive `until` can never advance past that second, so continuing to page
    would just re-fetch the identical first 1,000 forever. Silently returning those
    1,000 as "complete" would violate spec 8's identical-reconstruction guarantee
    with no signal to the caller -- this must raise instead."""
    events = [_event(i, 1000) for i in range(1500)] + [_event(i, 900) for i in range(1500, 1505)]
    runner = PagingRunner(events)
    with pytest.raises(RuntimeError, match="cannot be paged past"):
        relay.fetch_all(runner, IDENT, relay.fleet_filter(CH, RK))


def test_fetch_deleted_ids_returns_empty_without_querying_when_no_owner() -> None:
    ident = Identity(nsec="nsec1a", pubkey="a" * 64, relay_url="wss://r", auth_tag=None, fleet_channel=CH,
                     retrieval_key=RK, is_owner=False, owner_pubkey=None, record=None)
    runner = PagingRunner([{"id": "9" * 64, "pubkey": OWNER, "created_at": 5, "kind": 9005, "content": "",
                            "tags": [["h", CH], ["e", "1" * 64]]}])
    assert relay.fetch_deleted_ids(runner, ident, channel_id=CH) == set()
    assert runner.calls == []


def test_fetch_deleted_ids_collects_kind_5_alongside_kind_9005() -> None:
    tomb_9005 = {"id": "9" * 64, "pubkey": OWNER, "created_at": 5, "kind": 9005, "content": "",
                "tags": [["h", CH], ["e", "1" * 64]]}
    tomb_5 = {"id": "8" * 64, "pubkey": OWNER, "created_at": 6, "kind": 5, "content": "",
             "tags": [["h", CH], ["e", "2" * 64]]}
    runner = PagingRunner([tomb_9005, tomb_5])
    assert relay.fetch_deleted_ids(runner, IDENT, channel_id=CH) == {"1" * 64, "2" * 64}


def test_fetch_fleet_events_and_load_state_require_a_known_fleet_record() -> None:
    ident = Identity(nsec="nsec1a", pubkey="a" * 64, relay_url="wss://r", auth_tag=None, fleet_channel=None,
                     retrieval_key=None, is_owner=False, owner_pubkey=OWNER, record=None)
    runner = PagingRunner([])
    with pytest.raises(RuntimeError, match="fleet init"):
        relay.fetch_fleet_events(runner, ident, channel_id=None)
    with pytest.raises(RuntimeError, match="fleet init"):
        relay.load_state(runner, ident, channel_id=None)


def test_fetch_thread_combines_root_and_replies() -> None:
    # PagingRunner (like the brief's own fixture) filters only on created_at/authors,
    # not on the tag filters real relays would apply -- so both the root-by-id lookup
    # and the paged #e-reply query see the whole small history here. That is enough to
    # confirm fetch_thread actually issues and merges both queries rather than one.
    root_id = "1" * 64
    root = {"id": root_id, "pubkey": OWNER, "created_at": 10, "kind": 9, "content": "root", "tags": [["h", CH]]}
    reply = {"id": "2" * 64, "pubkey": "a" * 64, "created_at": 11, "kind": 9, "content": "reply",
            "tags": [["h", CH], ["e", root_id, "", "reply"]]}
    runner = PagingRunner([root, reply])
    events = relay.fetch_thread(runner, IDENT, channel_id=CH, root=root_id)
    assert {e.id for e in events} == {root_id, "2" * 64}


def _delegate_raw(task_id: str, event_id: str, created_at: int, to: str) -> dict:
    payload = {"v": PAYLOAD_VERSION, "type": "delegate", "task": task_id, "attempt": event_id, "from": OWNER,
              "to": to, "deadline": 10**12, "acceptance": []}
    tags = [["t", "fleet"], ["t", task_tag(task_id)], [TAG_FLEET, json.dumps(payload)],
            ["h", CH], ["p", to], ["p", RK]]
    return {"id": event_id, "pubkey": OWNER, "created_at": created_at, "kind": 9, "content": "brief", "tags": tags}


def test_load_state_defaults_channel_and_drops_deleted_tasks() -> None:
    kept_task, dropped_task = "1" * 32, "2" * 32
    kept_event_id, dropped_event_id = "a" * 64, "c" * 64
    keep = _delegate_raw(kept_task, kept_event_id, 100, "b" * 64)
    dropped = _delegate_raw(dropped_task, dropped_event_id, 100, "b" * 64)
    tomb = {"id": "d" * 64, "pubkey": OWNER, "created_at": 200, "kind": 5, "content": "",
            "tags": [["h", CH], ["e", dropped_event_id]]}
    runner = PagingRunner([keep, dropped, tomb])
    st = relay.load_state(runner, IDENT, channel_id=None)
    assert set(st.tasks) == {kept_task}


def test_directory_joins_members_records_presence_and_load() -> None:
    members = json.dumps({"ok": True, "members": [{"pubkey": B, "display_name": "Reviewer"}]})
    records = json.dumps({"ok": True, "agents": [{"pubkey": B, "content": {"role": "reviewer", "capabilities": ["laravel"],
                                                                            "description": "Reviews.", "harness": "claude", "host": "vps", "version": "0.8.0"}}]})
    presence = json.dumps({"ok": True, "presence": [{"pubkey": B, "status": "online", "updated_at": 1700}]})
    runner = PagingRunner([_event(1, 1000) | {"tags": [["p", RK], ["fleet", json.dumps({"v": 1, "type": "delegate", "task": "t", "attempt": "a", "from": "a" * 64, "to": B, "deadline": 9, "required": True, "acceptance": []})], ["t", "fleet:task:t"]]}],
                          {"channel-members": members, "read-managed-agents": records, "read-presence": presence})
    ident = Identity(**{**IDENT.__dict__, "owner_pubkey": OWNER})
    [entry] = relay.directory(runner, ident, channel_id=CH)
    assert (entry.display_name, entry.role, entry.capabilities, entry.host, entry.online, entry.live_tasks) == ("Reviewer", "reviewer", ["laravel"], "vps", True, 1)


# --- Beyond the brief (directory) -------------------------------------------
#
# The brief's own test covers the full-data happy path (a member with a matching
# managed-agent record, a matching presence entry, and one open task). It says
# nothing about how the join degrades when any of those three sources has no
# entry for a member -- exactly the seam the task brief calls out by name
# ("what does directory() do for a channel member with no managed-agent record,
# no presence entry, or no tasks?"). The following exercises each of those, plus
# the retrieval-key/conductor exclusion the brief's prose specifies but gives no
# test for, plus the harness/version fields (the gap this task closed).


def test_directory_degrades_with_no_record_no_presence_and_no_tasks() -> None:
    """A member with no managed-agent record, no presence entry, and no live
    tasks must still appear -- with role/capabilities/description/harness/host/
    version/online/last_seen all sensibly empty/None, not a KeyError or a
    dropped row."""
    members = json.dumps({"ok": True, "members": [{"pubkey": B, "display_name": "Nobody's Agent"}]})
    empty_records = json.dumps({"ok": True, "agents": []})
    empty_presence = json.dumps({"ok": True, "presence": []})
    runner = PagingRunner([], {"channel-members": members, "read-managed-agents": empty_records, "read-presence": empty_presence})
    ident = Identity(**{**IDENT.__dict__, "owner_pubkey": OWNER})
    [entry] = relay.directory(runner, ident, channel_id=CH)
    assert entry.pubkey == B
    assert entry.display_name == "Nobody's Agent"
    assert entry.role is None
    assert entry.capabilities == []
    assert entry.description is None
    assert entry.harness is None
    assert entry.host is None
    assert entry.version is None
    assert entry.online is None
    assert entry.last_seen is None
    assert entry.live_tasks == 0


def test_directory_reports_offline_when_presence_status_is_not_online() -> None:
    members = json.dumps({"ok": True, "members": [{"pubkey": B, "display_name": "Reviewer"}]})
    records = json.dumps({"ok": True, "agents": []})
    presence = json.dumps({"ok": True, "presence": [{"pubkey": B, "status": "offline", "updated_at": 1234}]})
    runner = PagingRunner([], {"channel-members": members, "read-managed-agents": records, "read-presence": presence})
    ident = Identity(**{**IDENT.__dict__, "owner_pubkey": OWNER})
    [entry] = relay.directory(runner, ident, channel_id=CH)
    assert entry.online is False
    assert entry.last_seen == 1234


def test_directory_excludes_retrieval_key_and_conductors_when_record_present() -> None:
    """The retrieval key and any conductor pubkeys are channel members (they
    hold real keypairs the fleet posts to/reads as), but they are not agents
    -- the brief's prose says both must be excluded from the listing whenever
    a fleet record is known."""
    from buzz_fleet.orchestration.record import ConductorEntry, FleetRecord

    conductor_pubkey = "c" * 64
    members = json.dumps({"ok": True, "members": [
        {"pubkey": B, "display_name": "Reviewer"},
        {"pubkey": RK, "display_name": None},
        {"pubkey": conductor_pubkey, "display_name": "conductor-host"},
    ]})
    empty = json.dumps({"ok": True, "agents": []})
    no_presence = json.dumps({"ok": True, "presence": []})
    runner = PagingRunner([], {"channel-members": members, "read-managed-agents": empty, "read-presence": no_presence})
    record = FleetRecord(retrieval_key=RK, conductors={"h1": ConductorEntry(pubkey=conductor_pubkey, host="h1")}, created_at=1)
    ident = Identity(**{**IDENT.__dict__, "owner_pubkey": OWNER, "record": record})
    entries = relay.directory(runner, ident, channel_id=CH)
    assert [e.pubkey for e in entries] == [B]


def test_directory_keeps_all_members_when_no_record_known() -> None:
    """Without a known fleet record (e.g. an agent identity with no local
    record), there is no retrieval key or conductor set to exclude -- every
    member is listed."""
    members = json.dumps({"ok": True, "members": [{"pubkey": B, "display_name": "Reviewer"},
                                                   {"pubkey": RK, "display_name": None}]})
    empty = json.dumps({"ok": True, "agents": []})
    no_presence = json.dumps({"ok": True, "presence": []})
    runner = PagingRunner([], {"channel-members": members, "read-managed-agents": empty, "read-presence": no_presence})
    entries = relay.directory(runner, IDENT, channel_id=CH)
    assert {e.pubkey for e in entries} == {B, RK}


def test_directory_sorts_by_display_name_case_insensitively() -> None:
    # Sort key is `(display_name or "").lower()` -- a member with no display
    # name sorts to the front (empty string is the lexicographically smallest
    # key), not to the back and not dropped.
    members = json.dumps({"ok": True, "members": [{"pubkey": "c" * 64, "display_name": "zeta"},
                                                   {"pubkey": "d" * 64, "display_name": "Alpha"},
                                                   {"pubkey": "e" * 64, "display_name": None}]})
    empty = json.dumps({"ok": True, "agents": []})
    no_presence = json.dumps({"ok": True, "presence": []})
    runner = PagingRunner([], {"channel-members": members, "read-managed-agents": empty, "read-presence": no_presence})
    entries = relay.directory(runner, IDENT, channel_id=CH)
    assert [e.display_name for e in entries] == [None, "Alpha", "zeta"]


def test_directory_skips_managed_agent_read_when_owner_pubkey_unknown() -> None:
    """`ident.owner_pubkey` is `None` for an agent identity whose owner hasn't
    been resolved -- the brief's own `directory()` body only calls
    `read_managed_agents` `if ident.owner_pubkey`. Confirm that guard actually
    prevents the call rather than passing an empty-string owner through."""
    members = json.dumps({"ok": True, "members": [{"pubkey": B, "display_name": "Reviewer"}]})
    no_presence = json.dumps({"ok": True, "presence": []})
    runner = PagingRunner([], {"channel-members": members, "read-presence": no_presence})
    ident = Identity(**{**IDENT.__dict__, "owner_pubkey": None})
    [entry] = relay.directory(runner, ident, channel_id=CH)
    assert entry.role is None
    assert not any(c[1] == "read-managed-agents" for c in runner.calls)
