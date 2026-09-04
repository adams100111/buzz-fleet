# buzz-fleet

A TUI for managing headless Buzz agents (Claude Code / Codex / Pi / goose)
on a Linux/systemd box. See `docs/superpowers/plans/2026-09-04-buzz-fleet-v1.md`
for the implementation plan and the linked design spec for the "why".

## Development

    cd signer && cargo build
    uv sync
    uv run buzz-fleet --help
