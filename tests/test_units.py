"""Community-qualified systemd instance names.

Fact 12 of the spec: systemd.unit(5)'s escaping leaves ASCII alphanumerics,
':', '_' and '.' alone, which is why ':' is the right separator — and why it
cannot collide with the slug charset ([a-z0-9-]) the way '-' would.
"""

import shutil
import subprocess

import pytest

from buzz_fleet import units


def test_instance_key_is_community_first() -> None:
    assert units.instance_key("eltahir", "reviewer") == "eltahir:reviewer"


def test_split_key_round_trips() -> None:
    assert units.split_key(units.instance_key("eltahir", "reviewer")) == (
        "eltahir",
        "reviewer",
    )


def test_split_key_rejects_an_unqualified_id() -> None:
    """A bare agent id reaching a unit call is the pre-migration bug; refuse
    loudly rather than silently addressing the wrong unit."""
    with pytest.raises(ValueError, match="not a community-qualified"):
        units.split_key("reviewer")


def test_two_communities_do_not_collide() -> None:
    a = units.instance_key("eltahir", "reviewer")
    b = units.instance_key("acme", "reviewer")
    assert a != b


def test_unit_name_wraps_the_template() -> None:
    assert units.unit_name("eltahir:reviewer") == "buzz-agent@eltahir:reviewer.service"


def test_escape_leaves_safe_characters_alone() -> None:
    assert units.escape_instance("eltahir:reviewer") == "eltahir:reviewer"


def test_escape_replaces_slash_with_dash() -> None:
    assert units.escape_instance("a/b") == "a-b"


def test_escape_escapes_a_leading_dot() -> None:
    assert units.escape_instance(".hidden") == r"\x2ehidden"


def test_escape_does_not_escape_a_non_leading_dot() -> None:
    assert units.escape_instance("a.b") == "a.b"


def test_escape_escapes_a_space() -> None:
    assert units.escape_instance("a b") == r"a\x20b"


@pytest.mark.skipif(shutil.which("systemd-escape") is None, reason="systemd-escape not installed")
@pytest.mark.parametrize(
    "value",
    [
        "eltahir:reviewer",
        "a/b",
        ".hidden",
        "a.b",
        "a b",
        "acme:my-lara-cdx",
        "a-b_c.d:e",
    ],
)
def test_escape_matches_systemd_escape(value: str) -> None:
    """The implementation is ours; the authority is the binary."""
    expected = subprocess.run(
        ["systemd-escape", "--", value], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert units.escape_instance(value) == expected
