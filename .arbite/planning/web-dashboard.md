# Web dashboard and control surface

Status: medium-term planner handoff; documentation only. Separately deployable
component within the arbite project. No web implementation tickets in this batch.

## User need and scope

A human can connect the dashboard to a project-local arbite deployment or a
multi-project database deployment, inspect progress and evidence, and perform
appropriate work/plan/review actions. The same surface eventually serves human
owners and meta-orchestrators. It should explain what needs attention without
requiring the human to reconstruct agent transcripts.

The first useful release should focus on project selection, tickets, job-board
state, file ownership and change receipts. Factory/epic planning and human gates
can appear when their underlying records exist. Do not invent duplicate UI-only
states or make a web deployment necessary for CLI usage.

## Proposed views

- Connections: configured endpoints, deployment identity, capabilities and access
  status; selection of projects/workspaces without conflating name with identity.
- Ticket board/detail: ready, active, blocked and completed work; dependencies,
  descriptions, notes, attempts, acceptance evidence and explicit reasons work
  cannot be claimed. Forms for ordinary ticket authoring and lifecycle actions.
- Coordination: current file holders, owning ticket/attempt, observed activity,
  pending operations and drift. Never label an old activity timestamp as proof a
  worker is dead. Recovery actions show the exact affected files and attempts.
- Job board: reservations, offers, required capabilities, configured worker
  profiles, declared availability/cost and ordered packages. No implied live fleet.
- Evidence: ordered changes and net diff, before/after versions, binary metadata,
  validation artifacts and distinction between automatic evidence and agent prose.
- Planning/factory, later: need, plan revisions, epic outcomes, decisions, current
  iteration, review gates and human feedback with the version being accepted.

## Deployment and API boundary

A standalone frontend cannot directly operate arbitrary local files or SQLite
stores. It needs an optional API adapter running where that store/workspace is
accessible, or a future central service. This is explicitly a later relaxation of
the current no-running-process scope. Do not implement it in the two current epics.

All mutations call the same application operations as the CLI: optimistic record
revisions, claims, readiness, cascade cleanup and event publication are enforced
there. Never have forms perform raw SQL/status updates. API responses include
stable IDs, revisions, machine-readable errors and capability information. Remote
workspace mutations need the explicit host access model from centralized-storage.md.

Configuration can list multiple endpoints; do not assume cross-endpoint atomic
operations or globally ordered events. Polling durable events with cursors is a
simple initial update mechanism. WebSockets/SSE can be adapters later. A lost
connection shows last-known state and prompts refresh before mutation.

## Interaction rules

Ticket edits and human gate answers submit the revision the user viewed. On a
conflict, explain the changed state and preserve unsaved input. Long evidence
lists use pagination and lazy loading. Write actions use idempotency keys to
avoid duplicate transitions on retries. Closing a ticket shows file cleanup and
receipt status from the operation response; it cannot falsely report success if
the underlying operation is unresolved.

Distinguish ordinary changes from explicit administrative overrides. Takeover,
deletion and recovery must present the concrete effects and record a reason.
Display content safely: ticket Markdown, filenames, diffs and agent prose are
untrusted rendered content. Binary/oversized artifacts need safe bounded views.
Do not embed agent/provider secrets or database credentials in the frontend.

## Future breakdown and acceptance

1. Confirm first-release views, supported deployments and access model.
2. Optional API adapter over the existing application contract.
3. Connection/project navigation and ticket read/write views.
4. Coordination, job-board and evidence views.
5. Planning/gate views when the factory model exists.
6. Packaging, accessibility, authentication and deployment documentation.

Acceptance: configure two deployments, view an active ticket and its exact file
changes, observe a competing update, submit an edit with a stale revision and get
a useful conflict, close through the same lifecycle cascade as CLI, reconnect
without losing the event position, and render untrusted Markdown safely. A file
sink and SQLite adapter produce equivalent outcomes. A later gate review must
record exactly which plan/evidence revision was accepted.

Future decisions: framework/design, API transport, local adapter installation,
authentication/roles, remote hosting, artifact access, read-only versus editable
connections and deployment packaging. These do not need to block today's core.
