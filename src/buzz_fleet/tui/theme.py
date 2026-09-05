"""The buzz-fleet Textual theme — shared by app.py and every screen.

A fleet of agents, run from a "hive" — grounded in the project's own name
(Buzz) rather than a generic dark-mode default. Status colors (success/
warning/error/STATUS_INACTIVE) are reserved strictly for agent/adapter
health state; `accent`/`primary` (honey-gold) is the only color used for
interactive elements (buttons, focus, selection) — the two vocabularies
never overlap, so color always tells the truth.

STATUS_INACTIVE and PANEL_BORDER are plain hex constants, not Theme
`variables` — a Theme's custom `variables` aren't resolvable from a
Screen's class-level DEFAULT_CSS at the point it's parsed, and Rich's
`Text(style=...)` (used for DataTable cell coloring) has no concept of
Textual's `$variable` syntax at all. Literal hex strings work in both
places without relying on timing or a system that doesn't apply there.
"""

from __future__ import annotations

from textual.containers import Vertical
from textual.theme import Theme

STATUS_INACTIVE = "#6B7280"
PANEL_BORDER = "#4A4433"

BUZZ_FLEET_THEME = Theme(
    name="buzz-fleet",
    primary="#D9A73B",
    secondary="#C98A2C",
    warning="#C98A2C",
    error="#C1553A",
    success="#7FB069",
    accent="#D9A73B",
    foreground="#E8E1D0",
    background="#16130D",
    surface="#1E1A11",
    panel="#1E1A11",
    dark=True,
)

# CSS shared by any screen using `section()` below — field groups framed by
# one bordered panel (titled by what the fields mean), with plain-background
# fields inside it rather than a second, competing border per field.
SECTION_CSS = f"""
.form-section {{
    border: round {PANEL_BORDER};
    margin: 0 2 1 2;
    padding: 0 1;

    & > Input, & > Select > SelectCurrent {{
        border: none;
        background: $surface;
    }}
}}
"""


def section(title: str) -> Vertical:
    """A bordered field group, titled by what the fields mean — not by field
    type or the order they happen to live in code. See the design spec:
    "Group fields by meaning, not code order."
    """
    container = Vertical(classes="form-section")
    container.border_title = title
    return container
