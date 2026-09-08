"""Community-qualified systemd instance names.

Fact 12 of the spec: systemd.unit(5)'s escaping leaves ASCII alphanumerics,
':', '_' and '.' alone, which is why ':' is the right separator — and why it
cannot collide with the slug charset ([a-z0-9-]) the way '-' would.

A literal '-' in a key is valid in a systemd unit name and is what every real
agent uses (e.g. 'my-lara-cdx', 'my-dotnet-cdx', 'laravel-backend-developer-claude').
Keys are validated and used verbatim — no escaping applied.
"""

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


def test_unit_name_with_simple_ids() -> None:
    assert units.unit_name("eltahir:reviewer") == "buzz-agent@eltahir:reviewer.service"


def test_unit_name_with_dashes() -> None:
    """Real agent ids contain dashes; they must be preserved verbatim."""
    assert (
        units.unit_name("eltahir:my-lara-cdx")
        == "buzz-agent@eltahir:my-lara-cdx.service"
    )


def test_unit_name_accepts_underscore_and_dot() -> None:
    """All characters in the key are valid in a systemd unit name."""
    assert (
        units.unit_name("eltahir:my_agent.v2")
        == "buzz-agent@eltahir:my_agent.v2.service"
    )


def test_validate_instance_key_accepts_valid_keys() -> None:
    """Valid keys are silently accepted."""
    units.validate_instance_key("eltahir:reviewer")
    units.validate_instance_key("eltahir:my-lara-cdx")
    units.validate_instance_key("acme:my_agent.v2")


def test_validate_instance_key_rejects_space() -> None:
    with pytest.raises(ValueError, match="contains invalid character ' '"):
        units.validate_instance_key("eltahir:my agent")


def test_validate_instance_key_rejects_slash() -> None:
    with pytest.raises(ValueError, match="contains invalid character '/'"):
        units.validate_instance_key("eltahir/reviewer")


def test_validate_instance_key_rejects_leading_dot() -> None:
    with pytest.raises(ValueError, match="starts with invalid character '.'"):
        units.validate_instance_key(".hidden:reviewer")


def test_unit_name_raises_for_invalid_key() -> None:
    """unit_name validates the key and raises rather than emitting a mangled name."""
    with pytest.raises(ValueError, match="contains invalid character"):
        units.unit_name("eltahir:my agent")


def test_unit_name_with_uppercase() -> None:
    """Uppercase is valid in systemd unit names and must be preserved."""
    assert (
        units.unit_name("Eltahir:reviewer")
        == "buzz-agent@Eltahir:reviewer.service"
    )


def test_validate_instance_key_accepts_mixed_case() -> None:
    """Mixed-case keys are valid and accepted without raising."""
    units.validate_instance_key("Eltahir:Reviewer")


def test_validate_instance_key_rejects_multiple_colons() -> None:
    """Keys with more than one colon are rejected; split_key would misparse them."""
    with pytest.raises(ValueError, match="has 2 colons"):
        units.validate_instance_key("a:b:c")


def test_validate_instance_key_rejects_no_separator() -> None:
    """Keys without a colon separator are rejected."""
    with pytest.raises(ValueError, match="has no separator"):
        units.validate_instance_key("noseparator")


def test_unit_name_raises_for_multiple_colons() -> None:
    """unit_name rejects keys with multiple colons rather than emitting a name."""
    with pytest.raises(ValueError, match="has 2 colons"):
        units.unit_name("a:b:c")
