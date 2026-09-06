---
status: accepted
---
# The Buzz relay is the orchestration ledger; the tool surface runs locally under each agent

Orchestration state (delegations, acks, reports, conductor actions) is stored only as ordinary kind 9 channel messages on the shared Buzz relay, every one of which mentions a retrieval keypair that no process holds, so a single indexed relay filter (`#p`) plus `until` paging reconstructs the complete history on any machine. The commands agents call (`buzz-fleet task ...`) run locally under each agent and inherit that agent's own key from its unit environment; only the conductor is central. We chose this over a central state service or a hosted MCP endpoint because every machine already reaches the relay from behind NAT, agent keys never leave their machines, buzz-acp only supports a local stdio MCP server anyway, and the owner's existing Desktop and mobile apps become the live view for free.

## Considered options

- A central HTTP service or MCP server on the VPS holding task state: adds a second availability dependency for every handoff and would have to hold or proxy every agent's signing key.
- Post-filtered `#t` tags as the query key: silently incomplete, because the relay clamps historical queries to 1,000 rows before applying non-indexed tag filters.
- A durable workflow engine (Temporal) as the ledger: kept as the upgrade path; the reducer/actions boundary in the conductor is what it would replace.

## Consequences

Every fleet message shows a mention of the retrieval identity in Desktop. Readers must consume the owner's delete events as well, or a purge makes them disagree with the conductor. Retrieval completeness must be verified against the live relay before the reads are trusted.
