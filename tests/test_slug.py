from buzz_fleet.slug import agent_slug


def test_slugifies_display_name() -> None:
    assert agent_slug("Laravel Backend Dev", existing_ids=set()) == "laravel-backend-dev"


def test_strips_invalid_systemd_instance_characters() -> None:
    assert agent_slug("Codex @ VPS!", existing_ids=set()) == "codex-vps"


def test_dedupes_collisions_with_numeric_suffix() -> None:
    existing = {"react-dev"}
    assert agent_slug("React Dev", existing_ids=existing) == "react-dev-2"
    existing.add("react-dev-2")
    assert agent_slug("React Dev", existing_ids=existing) == "react-dev-3"
