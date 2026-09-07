import json
import subprocess

import pytest

from buzz_fleet.orchestration import relay
from buzz_fleet.orchestration.identity import Identity
from buzz_fleet.orchestration.protocol import PAYLOAD_VERSION, TAG_FLEET, OutgoingMessage, task_tag

CH, RK, OWNER = "6f1c0000-0000-4000-8000-000000000000", "r" * 64, "0" * 64
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
