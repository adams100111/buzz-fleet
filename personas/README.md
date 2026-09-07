# Bundled persona templates

Starter `.persona.md` templates for `buzz-fleet`'s create-agent template
picker, seeded automatically into `~/.config/buzz-fleet/personas/` the
first time `scripts/get.sh` runs (never overwriting a directory that
already exists — see `scripts/get.sh`).

## Layout convention

Each subdirectory here is one "pack" — a group of personas sharing team-wide
discipline. A pack's `pack_instructions.md`, if present, must live in the
**same directory** as its `.persona.md` files (not one level up) — that's
the sibling-file convention `personas.py`'s `_sibling_pack_instructions`
looks for. This is a deliberate simplification of `buzz-deploy`'s own pack
layout (which nests personas one directory below `pack_instructions.md`) —
buzz-fleet's own bundled copies are flattened for that reason, not a typo.

`developers/` — six stack-specific developer personas (Laravel, Inertia +
React, .NET, React Native, native iOS, native Android) plus one shared
`pack_instructions.md` (test-first, strict typing, idempotent operations,
and the rest of the team-wide discipline every persona in the pack
inherits). Sourced from `buzz-deploy`'s `packs/developers/` pack.

`fleet/` — the planning pack: one persona, `Fleet PM` (`fleet-pm.persona.md`),
its `pack_instructions.md` (planning discipline), a `fleet-planning` skill
under `skills/`, and harness rule files under `workspace/` that buzz-fleet
copies into the agent's working directory as declared by the persona's
`workspace_files` (spec 5.12). The planner reads the agent directory,
surveys the codebase, and publishes run proposals for the owner to approve;
it never does the work itself.
