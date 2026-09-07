---
name: fleet-planning
description: Turn a requirement into a fleet run proposal. Use whenever the owner asks the Fleet PM to plan, scope, estimate, or "get X done", and whenever a proposal needs revising.
---

# Fleet planning

## Procedure

1. **Intake.** Restate the requirement in two sentences. List what is unclear. Ask at most
   three questions whose answers change the plan; give defaults for everything else under
   "Assumptions".
2. **Get the revision.** If the owner named a repository and commit, use them. If they named a
   branch, resolve it:
   ```bash
   git ls-remote <repo> refs/heads/<branch>
   ```
   If they named nothing, ask which repository. Clone or fetch into `repos/<name>` under your
   working directory and check out the commit in a worktree named after the request:
   ```bash
   git -C repos/<name> fetch --all && git -C repos/<name> worktree add ../../runs/<slug> <commit>
   ```
3. **Survey.** In that worktree, find: the module the change lands in, the conventions it
   follows, the tests that cover it, how it is built and deployed, and anything the change could
   break. Use `rg`, `tree -L 3`, and the project's own docs. Write down file paths; the briefs
   will cite them.
4. **Directory.** Run:
   ```bash
   buzz-fleet fleet agents --json
   ```
   Pick agents by `role` and `capabilities`. Note `online` and `live_tasks`. Never pick an agent
   that is offline; name a fallback for any step whose agent has live tasks.
5. **Draft the steps.** Use the schema below. One concern per step. Implementation, review, and
   build are separate steps with different agents. Add a human step only where a decision needs
   the owner.
6. **Check before proposing.**
   - Every step names an agent that exists in the directory, with a capability that matches.
   - Every brief stands alone: repository, commit, files, what to change, what not to touch,
     how to verify, and the acceptance criteria repeated.
   - Every acceptance criterion is a command or a measurable statement.
   - Timeouts have a stated estimate; the run deadline leaves room for one rework cycle.
   - Budget is set. Retention is set or defaults to keep.
7. **Propose.**
   ```bash
   buzz-fleet run propose --brief - --steps - --rationale - <<'EOF'
   ...
   EOF
   ```
   Pass the requirement as the brief, the YAML below as the steps, and the rationale with its
   "Assumptions" and "Risks" lists. The command prints the proposal id and thread.
8. **Revise.** When the owner replies in the proposal thread, change only what they asked,
   re-run step 6, and propose again with `--revises <proposal id>`. Stop after the owner types
   `!fleet approve` or `!fleet reject`.

## Steps schema

```yaml
steps:
  - role: implement
    agent: Laravel Backend Developer        # display name from the directory
    timeout: 90m
    brief: |
      Repository git@github.com:o/app.git at commit <sha>.
      Add CSV export to reports: app/Http/Controllers/ReportController.php (new export action),
      app/Exports/ReportCsv.php (new), routes/web.php (route under the reports group).
      Follow the existing ExcelExport pattern in app/Exports/. Do not touch the PDF path.
      Verify: php artisan test --filter=ReportExport.
    acceptance:
      - "php artisan test --filter=ReportExport passes"
      - "export of the demo dataset (10,342 rows) completes in under 5 s"
  - parallel:
      - role: review
        agent: Reviewer
        timeout: 45m
        fallback: Reviewer-2
        brief: |
          Review the output commit of the implement step against the acceptance criteria above
          and the conventions in app/Exports/. Report failed with findings, or done.
      - role: security-review
        agent: SecReviewer
        timeout: 30m
        required: false
        brief: Check the export for injection through report filters and for unbounded memory use.
  - role: owner-gate
    agent: owner
    timeout: 12h
    brief: Approve the reviewed commit for build.
  - role: build
    agent: Builder
    timeout: 60m
    brief: Build and deploy the approved commit to staging with the project's usual pipeline; report the deployed URL as evidence.
limits: {max_rework: 2, max_tasks: 12, run_deadline: 6h}
budget: {usd: 12}
retention: keep
notify_on_done: [owner]
```

## Rationale template

```
Why this cast: <one line per agent>
Estimate: <per step, with the reason>
Assumptions:
- ...
Risks:
- ...
Out of scope: <what this run deliberately does not do>
```

## Commands you use

| Purpose | Command |
|---|---|
| Who is in the fleet | `buzz-fleet fleet agents --json` |
| Propose a run | `buzz-fleet run propose --brief - --steps - --rationale -` |
| Revise a proposal | same, plus `--revises <id>` |
| See a run or task | `buzz-fleet runs`, `buzz-fleet task show <id>` |
| Answer a blocked task that mentions you | reply in its thread; if the decision is the owner's, say so and mention the owner |

You never run `buzz-fleet run <pipeline>`, `task delegate`, or `run approve`; those are for
the owner and for agents doing work. If the owner has put you on `auto_start`, `run propose`
starts the run by itself and prints that it did.
