# Project factory and durable planning

Status: future planner handoff; documentation only. Do not create this epic yet.
Related plans: shared-directory-coordination.md, multi-provider-job-board.md,
centralized-storage.md, web-dashboard.md.

## User need

The owner wants to state a business/product need, resolve ambiguity with a
planning agent, agree requirements and acceptance, and send work to a factory.
Execution continues externally until human input is required. Feedback may be
visual review, integration testing, a business decision, or final acceptance.
Repeated development/review loops must preserve intent and decisions over long
periods and across replacement agents. A project is not accepted merely because
its current tickets are closed.

Arbite stores where this process is, what it means, and what unblocks it. It does
not run agents or promise autonomous completion. No workflow engine or general
purpose automation language should be presumed by a later planner.

## Proposed durable records

Project: stable opaque ID created at intake, optional codename, display name,
statement of need, intended beneficiaries, desired outcomes, human owner,
constraints/exclusions, lifecycle, current iteration, artifact/requirement links,
created/updated/revision. Naming after requirements changes display metadata,
not identity. Distinguish project from checkout and from an arbitrary epic label.

Epic: stable ID within project, legacy label/alias, intended outcome, scope,
strategy, acceptance criteria, dependencies, risk/assumptions, accountable owner,
state and planning revision. Its documents can be Markdown backed by either sink.
An epic is a bounded outcome, not a special execution ticket. Preserve existing
ticket epic labels with an explicit adoption/mapping migration; do not guess that
every legacy string is an approved plan.

Requirement: durable ID, description, rationale/source, priority, verifiable
acceptance, current revision and state. Link to epics/tickets and evidence.
Decision: question, alternatives, decision, actor, date, rationale, superseded
decision link. Assumption/risk: claim, impact, validation plan, resolution.
These can initially be structured sections in versioned documents where separate
records would overcomplicate authoring, but references must remain stable.

Iteration: project ID, objective, baseline planning revision, selected epics,
completion evidence, submitted/accepted/rework outcome and review links. Changing
scope creates a new plan revision and makes affected work discoverable; it must
not retroactively rewrite what a completed attempt was asked to achieve.

Review gate: project/epic/iteration scope, requested decision, human or automated
reviewer role, evidence bundle, clear response options, open/answered/superseded
state, answer/rationale, created/answered timestamps, affected work. A gate may
block one branch instead of freezing the entire project. Store the dependency
explicitly. Do not overload ticket blocked_by prose as the only gate linkage.

## Minimal lifecycle proposal

1. Intake: record need, source and open questions; allow unnamed projects.
2. Requirements: gather goals, reconcile conflicts, define boundaries and name.
3. Plan review: record strategy, decomposition, acceptance and human agreement.
4. Execution: publish/assign work through the job board, track attempts/evidence.
5. Review: submit an evidence bundle to the appropriate gate(s).
6. Accepted, or rework: acceptance closes the agreed outcome; feedback creates a
   revised plan/iteration and returns to execution or requirements as appropriate.

Paused/cancelled are explicit outcomes with reasons. Transitions require their
recorded evidence/decisions. Keep phase independent from worker activity: a project
may remain in execution with no workers running. Gates can be used within phases.
An external factory controller may drive this lifecycle via CLI/events; arbite
does not implement that controller as part of this plan.

## Human feedback and continuity

A pending gate must be understandable without the prior conversation: what is
being decided, concise context, exact version being reviewed, evidence links,
options and consequences, and who can answer. Record answers durably. Repeated
delivery of the same answer must be idempotent. An answer to an obsolete revision
cannot approve a newer implementation silently.

Support artifacts such as screenshots, test reports, demos and deployment links
with digests/revisions and provenance where available. Evidence existence is not
verification of quality. Keep agent self-report, automated validation and human
acceptance distinct. Do not automatically send messages to reviewers; a future
notification adapter consumes events.

A resuming planner needs a compact project-context query: current need, accepted
requirements/plan version, unresolved decisions, open gates, current iteration,
completed evidence, blocked/ready work, and latest handoff. Full history remains
available separately. This is the long-horizon memory surface; scratchpads alone
are insufficient and model-generated summaries cannot supersede recorded decisions.

## Interfaces and storage

Plan CLI commands for project/epic show/create/update, plan revision, review request/
answer, iteration start/submit, and context queries. Exact command vocabulary is
future work. Use the common application operations, optimistic revisions, events,
and JSON errors from the first epics. File sink remains legible Markdown plus
structured metadata; SQLite uses equivalent logical records. Artifacts may be
local or externally referenced; a future centralized sink needs an explicit
artifact transport policy, not host-local paths disguised as shared artifacts.

## Future implementation breakdown

1. Confirm minimal lifecycle and gate policy with owner using one real project.
2. First-class project/epic records and legacy label adoption.
3. Versioned planning content, requirement/decision references and traceability.
4. Iterations and explicit gates/answers, with revision checks and events.
5. Readiness integration so gates prevent dependent work acquisition.
6. Context/evidence views and human-readable exports.
7. Both-sink migration, conformance and a complete two-iteration example.

Acceptance: intake an unnamed idea, name it after requirements, approve a plan,
execute tickets, stop at a visual review gate, record rejection with actionable
feedback, create a revision/iteration, execute rework, accept the revised outcome.
Restart every participant between steps and recover the exact current state.
Verify unrelated work can continue past a branch-specific gate; closed tickets
cannot bypass required acceptance; superseded gate answers cannot approve new
work; all links remain traceable after migration/export.

## Decisions for the future planner

Which phases/gates are required versus project-specific? Who may approve and how
is their identity authenticated remotely? What constitutes final acceptance for
an evolving business? What is the minimum structured requirement model? How are
budget/resource limits expressed and checked by an external controller? Which
artifacts should be embedded versus referenced? How does scope change invalidate
existing acceptance? Resolve these before ticketing this leg; do not invent a
generic business automation platform in advance.
