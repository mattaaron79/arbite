---
id: tic-cf9f
title: Create work attempts and guard every ticket acquisition path
status: closed
type: feature
tier: high
domain: io
epic: shared-directory-coordination
priority: null
tags:
- attempts
- lifecycle
- readiness
assignee: deepseek.code.004
depends_on:
- tic-1a75
references:
- planning/shared-directory-coordination.md
- planning/interaction-examples.md
blocked_by: null
created: '2026-09-21T01:16:32'
updated: '2026-09-21T10:08:38'
closed: '2026-09-21T10:08:38'
---

## Description
Planning key: C03
Handoff: .arbite/planning/shared-directory-coordination.md
Examples: .arbite/planning/interaction-examples.md (CL1, CL2, CL3, CL4, CL5, CL6, LC5, RC1)

## Outcome and scope
Create and end durable attempts on ticket acquisition and transitions, and enforce readiness atomically with acquisition. Route direct claim, list next --claim, batch claim, set, set-status, force, release, block, shelve and reopen through the application layer. Add explicit adoption for legacy in-progress tickets, and generation revocation for explicit administrative takeover.

## Acceptance criteria
- Direct claim cannot bypass unmet dependencies, placeholder classification, an invalid status, or another active attempt.
- Claim racing a dependency edit or reopen has a documented serial outcome; reopening a prerequisite emits an invalidation without silently undoing running work.
- Generic setters route through the lifecycle or refuse, so no backdoor exists through force.
- Activity timestamps are stored with no expiry logic and no liveness inference.
- Timeout-prone callers get a fast, structured refusal rather than a wait.
- Scenarios CL1, CL2, CL3, CL4, CL5, CL6, LC5, RC1 pass exactly as written in the examples doc.

## Validation
Concurrent claims, stale-generation calls, dependency changes racing a claim, legacy adoption, every lifecycle entry point and batch claims on both sinks.

## Boundaries and handoff
Implement this slice only: no daemon, agent launcher, automatic stale recovery, worktree workflow, central database, factory or dashboard. Preserve both-sink behaviour. Sibling tickets may touch the same files; coordinate edits or work serially until the proxy exists. Record implementation decisions, validation results, observable behaviour and remaining limitations in ticket notes before close.


## Notes
- 2026-09-21T09:38:35 deepseek.code.004: Plan (C03). Read tic-cf9f, the C03 slice, planning README/shared-directory-coordination, and the frozen CL1-CL6/LC5/RC1 blocks. Design: new src/arbite/coordination/lifecycle.py = the operations that touch BOTH stores (ticket sink + coordination store): claim (readiness+state+attempt guards, CAS ticket write as the acquisition serialisation point, then the attempt in one coordination transaction that re-checks readiness and the one-active-attempt rule), adopt (legacy in_progress, no invented activity), release/block/shelve/reopen/unblock (end/fresh attempt + events), guards for set/set-status (LC5) and for the generation a later file command presents. CLI routes claim/list-next-claim/batch/promote --agent through it; attempt adopt is a new command. Tests: extend tests/examples.py (refusal output goes to stderr) and add a CL/LC/RC suite plus multiprocess claim races. Deliberate changes to document: list next --claim loses its per-ticket stderr receipt lines (CL5), claim's lost-race message becomes the CL3 text, --force requires --reason, set status closed refuses with the LC5 text.

- 2026-09-21T10:08:33 deepseek.code.004: C03 final note (tic-cf9f): implementation, evidence, scenarios, limits.

IMPLEMENTATION DECISIONS
1. New module src/arbite/coordination/lifecycle.py: the operations that touch BOTH stores (a ticket in the sink, an attempt in the coordination store). store.py records attempts, app.py owns the coordination-only operations (workspace, events), and everything that is a decision about a ticket AND its attempt lives here: claim, adopt, release, block, shelve, reopen, unblock-resume, promote --agent, plus the guards the generic setters call. The CLI only resolves ids, parses flags and prints, so no acquisition path can carry its own copy of a rule.
2. Acquisition order, stated because two storage domains cannot be committed together (the durability section of the plan says so): guards -> ticket compare-and-swap -> attempt + its events in ONE coordination transaction -> post-commit verification. The ticket exchange (Expect(status, assignee), enforced inside the sink's own atomic write) is the acquisition's serialisation point, so exactly one of two racing claims wins; a loser writes NOTHING anywhere (no attempt, no event, no ticket change) and is told the winner's attempt (CL3). The attempt+events unit commits together or not at all (proved by killing a process at both commit boundaries).
3. Readiness is asked twice and belongs to the operation, not to the queue: once as a guard, and again inside the attempt-creation transaction, read from the ticket store at that moment. `arbite list next` filters on it, the claim re-checks it, so naming a ticket directly cannot bypass it (CL2). Classification is a guard too (a `TODO:` title/tier/domain is refused with `arbite promote`/`set` named), which closes the hole where `set status open` on a raw capture put half-classified work in the workable queue.
4. One active attempt per ticket, decided in the coordination store's own commit: `claim` refuses (outcome 4, reason attempt_held) when an attempt already owns the ticket, `adopt` refuses to adopt twice, and the takeover revokes the attempt it replaces inside the SAME transaction that creates the new one.
5. Takeover = `claim --force --reason`. The reason is now required (plan rule: an administrative override keeps its reason); the old attempt ends as `interrupted` with outcome `taken_over` and the reason kept as its handoff, an `attempt.revoked` event carries its generation, and the new worker gets a fresh attempt (generation 1, new id -- CL7's shape). Output matches CL7 lines 1, 2 and 4; line 3 (the file claims it releases) is tic-e9ed's cascade.
6. `arbite attempt adopt <id> --agent <a>` is a new command (CL6): only for a ticket that is already in_progress, refusing a ticket that is assigned to a different worker (that is the takeover, and `claim --force --reason` owns it) or that already has an attempt (busy). It records an attempt starting now and says no prior activity is implied; it writes no ticket field, because adoption claims nothing.
7. Generation revocation is a real guard, not a convention: `TicketLifecycle.require_attempt(ticket, attempt_id, generation=None)` is the public check later file operations present (tic-9b57/C04, tic-60c7/C07) and raises StaleGeneration (outcome 5, reason stale_generation) for an attempt that does not exist, belongs to another ticket, has moved generation, or is no longer active. Nothing infers a stopped worker from a clock: attempts keep started/last_activity/ended and there is no expiry, heartbeat or automatic reassignment anywhere.
8. Lifecycle commands end the attempt they stop: release and shelve end it `released`, block and reopen `interrupted`, each keeping the reason as the attempt's handoff and appending `attempt.ended`; the receipt adds "partial work is left on disk and visible; the next worker must re-read it" (LC2's wording, extended by tic-e9ed with the file-claim detail). `unblock` resumes in_progress work with a FRESH attempt for the worker it names. Reopen also emits `ticket.reopened` and never resurrects an old claim or attempt.
9. The claim-versus-reopen race has a defined order, and it is enforced, not just documented. Neither store can be locked with the other, so each side verifies AFTER it commits: the claim re-reads its dependencies and flags its own attempt (`attempt.invalidated`, plus a receipt line) if a prerequisite is no longer closed, and `reopen` scans its dependents' active attempts twice -- before and after its own state change -- skipping attempts that already carry an invalidation for that dependency so one race cannot produce two events. Whichever of the two commits landed second therefore always records the other's effect. Both orders are asserted deterministically, and the concurrent case asserts the invariant that holds for every interleaving.
10. The generic setters route or refuse, with the command named: `set <id> status closed` / `set-status <id> closed` refuse on a ticket with an active attempt with LC5's exact words, `set <id> status open|blocked|shelved` point at release/block/shelve, and `set <id> assignee <other>` points at `claim --force --reason` (clearing the assignee points at `release`). Everything else is still a plain edit, and a ticket nobody holds is untouched by these guards. `review` stays reachable through `set`/`set-status` for now: submit/accept do not end attempts until tic-e9ed, and a refusal must not name a route that does not do the job yet (recorded as tic-e9ed's to close).
11. Both-sink equality: every rule above is expressed once, in Python, on top of the sink's `update(expect=...)` and the coordination transaction, so the file and SQLite backends behave identically (parametrized tests over both where the CLI runs, over both backends for the store-level flows).
12. CLI surface: `arbite claim` gains `--reason`, `--json` and the attempt receipt; `arbite attempt adopt` is new; `list next --claim` claims through the same operation (so a batch creates one attempt per row, and a ticket with an active attempt is never offered); `promote --agent`, `release`, `block`, `shelve`, `reopen`, `unblock`, `set` and `set-status` now go through the application layer. result.OperationResult gained `text_hint` for the one case where the printed `next:` sentence wraps the command JSON publishes (CL1's claim line) -- the same "one fact, two renderings" rule the events view already used.
13. Test harness (tests/examples.py) extended rather than duplicated: a transcript whose body starts with an outcome label is asserted on stderr (that is where refusals have always gone) with stdout required empty, `# exit N, note on stderr` moves the block's trailing `note:` lines to stderr (CL5), and `assert_scenario(..., stream=...)` is the documented override for the one refusal printed on stdout (EV7's `--follow`, whose test now says so).

VALIDATION (exact commands and observed results)
- `python3 -m pytest -q` -> 605 passed, 3 skipped, 0 failed (baseline 558/3/0; +47 new tests: 10 in tests/test_lifecycle_examples.py, 37 in tests/test_coordination_lifecycle.py). No test is skipped differently from the baseline.
- `python3 -m pytest -q tests/test_lifecycle_examples.py` -> 10 passed: CL1 (transcript + the documented JSON payload), CL2, CL3, CL4, CL5 (table on stdout, note on stderr), CL6, LC5, read straight out of .arbite/planning/interaction-examples.md with ids, times and paths normalised; plus the harness's own two tests for the refusal-stream and note-on-stderr rules.
- `python3 -m pytest -q tests/test_coordination_lifecycle.py` -> 37 passed (~35s), the evidence for the criteria a transcript cannot show:
  * one claim records one attempt + exactly the two events on consecutive cursors (both sinks);
  * a refused claim (unmet dependency, unclassified work, another active attempt) writes nothing at all: no attempt, no event, no ticket change;
  * a claim that dies at `commit_staged` (real process, os._exit(9), file backend) leaves no attempt and no event, `doctor` reports `pending_commit`, and the next write to the store replays the unit so the attempt and BOTH events appear together; a claim that dies at `commit_applied` replays without duplicating them;
  * a takeover revokes the old attempt (interrupted, outcome taken_over, reason kept as handoff, ended set) and starts a new one, with one `attempt.revoked` event carrying the old generation;
  * a revoked generation, a generation that moved, another ticket's attempt and a non-existent id are all refused by `require_attempt` with reason `stale_generation`;
  * adoption records one attempt and refuses a second (exit 4) and a different worker (exit 1);
  * release/block/shelve/reopen end the attempt with the right state and leave `doctor` clean on both sinks; unblock resumes with a fresh attempt;
  * every setter refusal above, plus the same-status no-op, plus a non-status edit that still works while an attempt is active;
  * `promote --agent` records the attempt;
  * a claim arriving while the store's commit lock is held refuses in 0.3s (LOCK_TIMEOUT monkeypatched) with reason `store_locked`, nothing written to the store, and the message saying so -- a structured refusal, not a wait;
  * a batch claim creates one attempt per row (both sinks) and its JSON carries each attempt;
  * both orders of the claim-versus-reopen race (deterministic), and one unorchestrated concurrent run asserting the interleaving-independent invariant.
- RC1 with real processes: four `arbite claim` processes wait on a shared starting gun and claim one ticket -> exactly one exit 0 and three exit 1; every loser is told the holder's attempt id, worker and generation on stderr and writes nothing to stdout; the store ends with exactly one active attempt, one `attempt.started` event, the winner as assignee, and `doctor --json` clean. `list next --claim` for a second worker never offers the claimed ticket.
- `arbite doctor` (this repo, pipx 0.2.0 binary) -> exit 0, no problems; the epic's other tickets all still listed open (tic-9b57, tic-e9ed, tic-b03b, tic-1c4f, tic-7c42, tic-008f, tic-60c7, tic-95c0, tic-6015, tic-42d2, tic-74e2, tic-faae) and no sibling ticket was modified.

EXISTING TESTS DELIBERATELY CHANGED (each with its safety justification)
- tests/test_cli.py::test_claim_refuses_another_agents_ticket_unless_forced -- now asserts CL3's refusal (the compare-and-swap text plus "attempt held by: att-...") and that `--force` without `--reason` changes nothing. Safety: CL3 is frozen and mandates the new message; requiring the reason is the plan's "administrative overrides require a reason" rule, and the new assertion checks that the refused override left the ticket with its original assignee.
- tests/test_cli.py::test_claim_sets_in_progress_and_files_the_ticket_under_in_progress -- the lost-race assertion is now the CL3 text (the old "already assigned to" wording is gone) and the takeover passes `--reason`. Safety: a lost claim still leaves the winner's ticket, status and location untouched, which the test still checks; the takeover is still a note-recording, status-preserving takeover.
- tests/test_events_examples.py::test_EV7_no_follow -- passes `stream="stdout"`. Safety: EV7 is the one refusal the CLI deliberately prints on stdout; the scenario still asserts the exact text, the exit code and an empty stderr, it just no longer has the harness infer the stream from the label.
- No other existing test was touched; the suite went from 558 to 605 passing with nothing skipped differently.

SCENARIO STATUS
- CL1 pass exactly as written (text and the documented JSON block). CL2 pass. CL3 pass. CL4 pass. CL5 pass, including the "note on stderr" annotation and each row's own attempt. CL6 pass. LC5 pass.
- RC1: the block is two interleaved columns of elided output and depends on `arbite file claim` (tic-9b57/C04), so it cannot be run as a transcript. Its target is asserted directly with real processes (one winner, the loser told why in one line, no partial state) and by `list next --claim` never offering an attempted ticket. Deviation spelled out: the loser may also read the file sink's transient "no ticket found" when its read lands inside the winner's folder relocation -- a pre-existing non-isolation the file sink documents ("reads are not isolated"); the winner is still unique and the loser wrote nothing. That tolerance is explicit in the test rather than hidden.
- CL1's `next:` line and CL6's name `arbite file claim`/`file list`, which tic-9b57 and tic-1c4f ship. The frozen transcripts are the acceptance targets and are printed as written; no help text, README or docstring claims that file ownership exists today (README says plainly that it does not yet).

WHAT A REVIEWER CAN OBSERVE (integration testing)
- In a scratch project with a workable ticket: `claim tic-X --agent me` prints the claimed line, `attempt: att-XXXX (generation 1, ticket tic-X, workspace ws-XXXX)` and a `next:` line naming the file command; `claim tic-X --json` carries an `attempt` object with id/generation/state/workspace/started; a second agent's claim exits 1 with the holder's attempt; `arbite events` shows `attempt.started` + `ticket.claimed` with consecutive cursors.
- `list next --claim <agent> --count 3` prints the table (rows already in_progress, assigned to that agent) and one `note:` line on stderr -- and no per-ticket receipt lines (a deliberate change: the table carries those facts and CL5 is frozen).
- Claiming a dependent whose prerequisite is open exits 1 with `depends_on <dep> is <status> (not closed)` and hints at `list next --tier ... --epic ...` / `deps`; `list next` with nothing ready prints the two-line CL4 answer and exits 2.
- `claim --force --reason "<why>"` prints the takeover, `revoked: attempt att-XXXX generation N (reason recorded)` and `new attempt: att-XXXX`; `arbite events` shows `attempt.revoked` with the generation and reason; the old attempt reads as `interrupted` with outcome `taken_over`.
- `arbite attempt adopt <legacy-in_progress-ticket> --agent <its assignee>` creates the attempt and says no prior activity is implied; running it twice exits 4 naming the attempt it already has.
- `arbite set <id> status closed` on claimed work exits 1 with LC5's two lines and `next: 'arbite close <id>'`; `arbite release/block/shelve/reopen` each print `ended attempt att-XXXX (<state>)` and the partial-work caution; `unblock` on an assigned ticket starts a fresh attempt.
- Kill a claim's process at the commit boundary (tests/coordination_worker.py claim-crash) and `doctor` reports `pending_commit` until the next write to the store replays the attempt and its events together.
- Delete/hold coordination state by hand (e.g. `rm -rf .arbite/coordination` stays invisible to reads) -- nothing in the new code creates coordination state on a read path.

DEFERRED TO NAMED TICKETS
- tic-9b57 (C04): canonical paths, exclusive FILE claims, the acquisition loop's ordering/rollback, `file claims` inspection. It consumes `require_attempt`, `active_attempt` and the `attempt.invalidated` events this slice emits.
- tic-1c4f (C06): discovery and reads (`file list`/`file read`) -- the commands CL1/CL6's hints name.
- tic-60c7 (C07): version-checked writes and the `stale_read`/generation checks at write time (RC2).
- tic-b03b (C05): the file-operation intent journal and `store.recover()` (still a stub naming it); the `attempt.invalidated` and `pending_commit` findings are inputs, not substitutes.
- tic-e9ed (C10): the lifecycle CASCADE -- close/submit/accept ending attempts and releasing claims, unblock's fresh attempt semantics if it wants them differently, CL7's claim-release reporting, LC1-LC4's frozen output, `set status review` routing to submit, and `delete` refusing a ticket with an active attempt.
- tic-008f (C12) and tic-7c42 (C11): migration of attempts/events between sinks, receipts and change views.
- tic-1c4f/C15: the generated guide and README sweep (this slice updated the claim/promote/attempt paragraphs and the "coordination state is part built" bullet so nothing claims a capability that does not exist, but the full sweep is theirs).

LIMITATIONS / RESIDUAL RISK
- Two storage domains, one commit each: a claim that lands its ticket write and then fails its attempt write leaves a ticket that is in_progress with no attempt. That is exactly the legacy state `attempt adopt` exists for, the refusal names that command, and `doctor` does not report it (a coordination store cannot see tickets) -- reported here rather than hidden.
- The claim-versus-reopen race is closed by post-commit verification on both sides, which relies on reads seeing a commit that has returned. Both backends document that reads are not isolated (the file backend's `pending_commit` finding is how a reader is told), so the guarantee is "one of the two operations always records the conflict", not "no reader can observe a commit in flight".
- A file-sink read that races a claim's relocation can report "no ticket found" for a moment (pre-existing; the loser wrote nothing and the winner is unaffected).
- `close`, `submit` and `accept` do NOT end attempts yet (tic-e9ed), so a ticket closed through them can keep an active attempt; LC5's guard means `set status closed` cannot be used to work around that.
- The generation guard exists and is tested, but no CLI command presents a generation yet: the commands that do arrive with tic-9b57/tic-60c7.
- `arbite create --status in_progress --assignee X` still creates a ticket that looks claimed without an attempt; it is a creation path, not an acquisition path, so it is out of this slice and left for the cascade/guide slices to decide about. The migration path (`attempt adopt`) covers it today.

- 2026-09-21T10:08:38 system: Submitted; closed (review disabled).
