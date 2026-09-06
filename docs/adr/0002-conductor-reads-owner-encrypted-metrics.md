---
status: accepted
---
# The conductor decrypts turn metrics with the locally stored owner key, read-only

buzz-acp publishes per-turn token and cost metrics encrypted to the owner and readable only by the owner's key. Every machine that runs buzz-fleet already stores the community's admin key in its community file (it is how agents are created and attested), so the conductor and the recycle timer use that key to *read* metrics and to publish purge deletions, and for nothing else: they never sign delegations, reports, or conductor actions as the owner, which keep their own keys. We accepted the wider blast radius of the conductor host holding a key that can decrypt usage data because there is no other way to get cost into run reports and budgets without changing the public Buzz repo, and the host already held that key before this design.

## Consequences

The VPS and the dedicated server (standby) must be treated as owner-key holders in backups and access control. Metrics reads and purge are the only owner-key code paths in the conductor, and they are isolated in one module so that boundary can be audited.
