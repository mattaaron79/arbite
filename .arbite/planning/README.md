# Arbite coordination and long-horizon roadmap

Planning date: 2026-09-18. Source: owner/planner design conversation.

## Mandate and scope

The owner wants arbite to coordinate agents working in one visible directory.
Agents must use arbite's file tools; enforcement initially comes from agent
instructions, with runtime restrictions possible later. A file proxy is part of
the first delivery, not an optional future experiment. No worktrees, merging
workflow, agent launcher, daemon, scheduler, or automatic stale-work takeover is
required. Agents are started manually and may use different providers/runtimes.

Implement now through the two ticket epics:

1. [Shared-directory coordination](shared-directory-coordination.md), including
   file proxy, claims, attempts, scratch payload transport, automatic change
   evidence, recovery, and agent command passthrough. Its agent-facing surface is
   frozen command by command in
   [interaction-examples.md](interaction-examples.md), which is normative: those
   transcripts are the acceptance targets. The ticket cut is in
   [ticket-index.md](ticket-index.md).
2. [Multi-provider job board](multi-provider-job-board.md), including passive
   worker profiles, reservations, offers, continuity packages, and event queries.

Document only; do not create implementation tickets yet:

- [Project factory and durable planning](project-factory.md).
- [Centralized storage and project/workspace identity](centralized-storage.md).
- [Web dashboard and control surface](web-dashboard.md).

Each document is a planner handoff: intent, proposed contracts, limits,
acceptance scenarios, implementation slices, and deferred decisions. Ticket IDs
and dependency mappings are in [ticket-manifest.json](ticket-manifest.json) and
[ticket-index.md](ticket-index.md). Tickets use existing epic labels; no new
first-class epic implementation is presumed.

## Shared design rules

- The configured sink remains authoritative. `.arbite/project.yaml` selects the
  `file` sink in this checkout, and `.arbite/` is tracked in git, so tickets are
  version-controlled documents. Existing closed tickets are untouched. The
  2026-09-18 attempt at this epic did not live on `main`: its SQLite store and its
  implementation travelled on branch `temp-checkin`, which was abandoned as
  overengineered, deleted on 2026-09-21, and is not a foundation to build on — see
  the "Rejected approach" section of [ticket-index.md](ticket-index.md).
- File and SQLite sinks must implement the same coordination semantics. A file
  sink may use process locks and a journal; it must not secretly require SQLite.
- Add a storage-neutral application layer for operations that span tickets,
  attempts, claims, reservations, and events. CLI and any later web API call it.
- Use schema versions, record revisions, opaque durable IDs, UTC timestamps for
  new records, and documented JSON errors. Keep legacy ticket IDs and timestamp
  parsing compatible. Do not claim distributed guarantees for local sinks.
- IDs and self-declared agent names are attribution, not authentication. The
  initial trust model is cooperating local processes using one authoritative
  coordination store for a workspace.
- Hard rules belong in operations, not just discovery filters or prompts.
  Direct claim, list-next claim, set, force, and future APIs must not bypass
  invariants. Administrative overrides require a reason and retain history.
- One-shot commands return promptly. Contention returns structured details;
  an external caller can choose other work or invoke again later. No sleeping
  model loop, background watcher, or worker process is introduced.
- Future stale detection gets creation/activity/end timestamps and attempt
  generations now. No expiration, heartbeat service, or automatic reassignment.
- Preserve readable planning documents and raw change evidence independently
  of agent-authored summaries. An operation log is not a model explanation.

## Delivery and compatibility

The first epic establishes correctness primitives used by the second. Some
job-board schema/profile work can follow the shared core; integration acceptance
waits for proxy lifecycle completion. Ticket dependencies specify safe order,
not a promise that sibling tickets touch disjoint source files. Until the proxy
exists, implementers must coordinate shared source edits manually or work serially.

The current README explicitly excludes several new capabilities. Update its
scope during implementation, together with docs.py and generated agent guidance;
do not pretend these capabilities already exist. Existing commands should remain
usable unless their behavior violated newly documented safety rules. Migration
tests and refusal messages must cover those deliberate restrictions.

Everything under `.arbite/` is tracked in git except runtime coordination state:
`.arbite/coordination/` and `.arbite/scratch/` are ignored. Tickets, plan
documents and agent scratchpads are committed and are the development record;
claims, receipts, events and staged payloads are local and are not. Evidence
therefore dies with the machine unless the owner keeps it, so generate any devlog
from the tickets plus a receipt summary taken before pruning. Back up the plans
and tickets together before moving this roadmap elsewhere.
