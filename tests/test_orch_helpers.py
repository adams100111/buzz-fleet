import pytest

from buzz_fleet.orchestration import ids, record
from buzz_fleet.orchestration.durations import parse_duration


def test_ids_new_short_and_prefix() -> None:
    a = ids.new_id()
    assert len(a) == 36 and ids.short(a) == a[:8]
    assert ids.match_prefix(a[:8], [a, ids.new_id()]) == a
    with pytest.raises(ValueError, match="unknown"):
        ids.match_prefix("zzzzzzzz", [a])
    twin = a[:8] + "-0000-4000-8000-000000000000"
    with pytest.raises(ValueError, match="ambiguous"):
        ids.match_prefix(a[:8], [a, twin])


@pytest.mark.parametrize("text,expected", [("90", 90), ("90s", 90), ("45m", 2700), ("2h", 7200), ("1h30m", 5400), (" 12h ", 43200)])
def test_parse_duration(text: str, expected: int) -> None:
    assert parse_duration(text) == expected


@pytest.mark.parametrize("text", ["", "abc", "5x", "-5m", "1m1h", "0m"])
def test_parse_duration_rejects(text: str) -> None:
    with pytest.raises(ValueError):
        parse_duration(text)


def test_record_round_trips_through_about() -> None:
    rec = record.FleetRecord(
        retrieval_key="r" * 64,
        conductors={"primary": record.ConductorEntry(pubkey="p" * 64, host="vps")},
        versions={"buzz-fleet": "0.8.0"}, created_at=1_800_000_000,
    )
    about = record.encode_about(rec)
    assert about.startswith(record.ABOUT_HEADER + "\n")
    again = record.decode_about(about)
    assert again == rec
    assert again.limits.open_adhoc_per_requester == 5 and again.retention == "keep"


def test_record_decode_ignores_foreign_about() -> None:
    assert record.decode_about(None) is None
    assert record.decode_about("") is None
    assert record.decode_about("just a channel description") is None
    assert record.decode_about(record.ABOUT_HEADER + "\n{not json") is None
    assert record.decode_about(record.ABOUT_HEADER) is None
    assert record.decode_about(record.ABOUT_HEADER + "\n" + '{"foo": "bar"}') is None
