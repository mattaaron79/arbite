"""Ticket acquisition and lifecycle policy (planning key C03).

`arbite.application` is the generic guarded-operation plumbing: records, guards
and one-shot transactions that know nothing about tickets. This module is the
layer above it that turns that plumbing into *ticket policy*: who may claim a
ticket, when a claim is allowed, how a takeover supersedes an old work attempt,
and how every command that moves a ticket also ends or touches its attempt. The
planning rules put it plainly -- "hard rules belong in operations, not just
discovery filters or prompts" -- so readiness is enforced here, inside the
acquisition path, rather than in a `list next` filter that a direct `claim` can
bypass.

The three acquisition origins
-----------------------------

- ``claimed`` -- fresh acquisition of an open, ready, non-placeholder ticket.
- ``adopted`` -- explicit adoption of a pre-existing ``in_progress`` ticket that
  has **no attempt record** (a legacy ticket, or a ticket a pre-C03 arbite
  started). Adoption never invents history: the attempt starts *now* and the
  pre-existing declared assignee is recorded in the attempt's ``handoff``.
- ``takeover`` -- explicit administrative revocation (``--force --reason``) of a
  live attempt. The old attempt is *interrupted* (never mutated back to active,
  never reactivated) and a new attempt at a higher generation begins.

One active attempt per ticket
-----------------------------

The invariant is enforced three times, deliberately:

1. **Readiness** (`require_claimable`) refuses a ticket that is not `open`, not
   classified, has unmet dependencies, or already has an active attempt. It names
   the blocking reason in the message so an agent can act on it without guessing.
2. **The ticket compare-and-swap** (`TicketSink.update(..., expect=Expect(status=,
   assignee=, revision=))`) is what makes exactly one of two concurrent claims of
   one ticket win, and -- through the revision -- what stops a claim from
   overwriting a concurrent edit to any other field. The loser's `Conflict`
   abandons its journal entry before any attempt is written, so no attempt row
   survives for it.
3. **The coordination transaction** re-checks "no active attempt for this ticket"
   *inside* the transaction that stores the attempt, which is what makes adoption
   (where the ticket state does not change, so a CAS cannot detect a race) safe.

`--force` is an *ownership* override, never a readiness override. A forced claim
of an open ticket still refuses an unmet dependency, a placeholder classification
or another live attempt, and no claim may be forced past a non-open status -- the
plan's "no backdoor through force" rule. The only things `--force --reason` may
supersede are ownership (a legacy `in_progress` ticket with no attempt record,
which is recovered rather than silently reassigned) and a live attempt (explicit
administrative takeover, which interrupts it and starts a new generation).

The lifecycle journal: two stores, one recoverable transition
-------------------------------------------------------------

A transition changes two stores that cannot commit together. Nesting the ticket
write inside the coordination transaction is impossible for the shipped SQLite
sink (the coordination transaction holds ``BEGIN IMMEDIATE`` on the database the
ticket store also writes), and branching on sink kind would break the "both sinks
behave equivalently" rule. So every transition -- acquisition, release, block,
unblock, close, reopen, shelve, unshelve -- goes through `commit_transition`,
which makes the gap recoverable instead of pretending it is not there:

1. **Validate under the operation lock.** Every transition holds the coarse
   operation lock (as does every file mutation and every dependency edit), reads
   the ticket, its dependencies and its attempts fresh, and refuses anything not
   allowed *before* writing.
2. **Journal.** A `LifecycleIntent` naming the whole transition -- the ticket
   revision it expects, the status/assignee it will write, the attempt it ends or
   starts, the claims it releases and the events it records -- is committed to the
   coordination store.
3. **Ticket write (the commit point).** A compare-and-swap on the ticket's
   status, assignee *and revision*, so a concurrent edit to any field -- a new
   dependency, a note -- refuses the transition instead of being overwritten. A
   refusal abandons the intent; nothing else has changed.
4. **Coordination cascade.** One transaction applies everything the intent names
   and marks it `completed`. If it fails, the ticket write is reverted (again by
   revision) and the intent `abandoned`: a failed close leaves an in_progress
   ticket with its attempt and claims intact, never a half-closed one.

A crash leaves the intent `pending`. The next lifecycle operation -- or
``arbite doctor --fix`` -- settles it: when the ticket shows the target state the
cascade is rolled forward, otherwise the transition is abandoned. Until then the
attempt cannot claim or mutate (`application.require_current_attempt` refuses
it), so no operation can run in the window where the ticket and its attempt
disagree. Nothing runs on a timer: settling is part of the next operation.

Dependency-edit and reopen races (documented serial outcome)
-----------------------------------------------------------

Readiness is checked against the ticket set as it is read, under the operation
lock that dependency edits (`depend`, `set`, `reopen`) also take, and the claim's
ticket write is a revision compare-and-swap. A dependency change and a claim
therefore serialize in one of two honest ways: *a claim that commits
before the dependency change keeps its attempt* (work is not silently undone),
while *a claim that starts after it sees the new state and is refused by
readiness*. Reopening a completed ticket that other live tickets depend on
likewise never rewrites the dependents: it emits ``dependency_invalidated``
events naming them and leaves their status and assignee untouched.

No timers, no expiration
------------------------

Timestamps are recorded (``started``/``last_activity``/``ended``), never
interpreted. Nothing here decides that a stopped worker is dead, sleeps, polls or
reclaims work automatically. An attempt ends only because a command ends it, or
because an explicit ``--force --reason`` takeover supersedes it.

The cleanup cascade (C09)
-------------------------

Every transition that ends an attempt now also cleans up after it *in the same
guarded transaction that makes the attempt terminal*: the attempt's active file
claims are released (each with a ``claim_released`` event naming the reason), and
any incomplete file operation for the workspace is reconciled first. Ending the
attempt and releasing its claims together is what makes the plan's rule hold --
"no observer may successfully mutate under an old token after close succeeds" --
because a mutation takes the same coarse operation lock, so it either runs
entirely before the transition or is refused entirely after it.

Reconciliation runs before the transition and is not a blanket refusal: an
*unambiguous* interrupted operation (observed bytes match the recorded before or
after version) is finalized exactly as the next mutation would have, while an
*ambiguous* one (bytes match neither) raises ``DriftDetected`` so a close can
never report itself clean over bytes that may already have changed.

The C03 "refuse to end an attempt that still holds file claims" stub is gone.
``release_file_claims_hook`` is now the real cleanup: it releases the claims into
the transaction that ends the attempt, retaining each released claim as history.
"""

from __future__ import annotations

import contextlib
import copy
import functools
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

from . import coordination, eligibility, graph, schema, workers
from .application import Actor, CoordinationService, require_active_attempt
from .coordination import (
    EVENT_PAYLOAD_VERSION,
    Event,
    FileClaim,
    LifecycleIntent,
    WorkAttempt,
    new_record_id,
    utc_now,
)
from .errors import (
    Conflict,
    CoordinationConflict,
    CoordinationNotFound,
    DriftDetected,
    InvalidRecord,
    RecoveryRequired,
    TicketError,
    TicketNotFound,
    UnsupportedCoordination,
)
from .fileclaims import FileClaimService
from .mutation import MutationEngine
from .query import TicketQuery
from .schema import Ticket
from .sinks.base import Expect

#: Fresh acquisition of an open, ready ticket.
ORIGIN_CLAIMED = "claimed"
#: Explicit adoption of a pre-existing `in_progress` ticket with no attempt record.
ORIGIN_ADOPTED = "adopted"
#: Explicit administrative takeover (`--force` plus a non-empty `--reason`).
ORIGIN_TAKEOVER = "takeover"
#: A fresh attempt on an already-owned ticket whose previous attempt was ended
#: (used by `unblock` when a blocked ticket resumes). Never a revocation.
ORIGIN_RESUMED = "resumed"

#: Crash-simulation boundaries for `commit_transition` (tests only). A fault at
#: one of these raises `LifecycleFault`, which -- unlike an ordinary failure -- is
#: never compensated, so it leaves exactly the state a process death would.
#: The intent is durable; the ticket has not been written.
FAULT_AFTER_INTENT = "after_intent"
#: The ticket write landed; the coordination cascade has not run.
FAULT_AFTER_TICKET_WRITE = "after_ticket_write"
FAULT_PHASES = (FAULT_AFTER_INTENT, FAULT_AFTER_TICKET_WRITE)


class LifecycleFault(Exception):
    """A simulated crash at one lifecycle boundary (see `FAULT_PHASES`)."""

    def __init__(self, phase: str):
        super().__init__(f"lifecycle fault injected at {phase}")
        self.phase = phase


#: Attempt state a transition ends an attempt in -> the event kind it emits.
_END_EVENT_KINDS = {
    "released": "attempt_released",
    "finished": "attempt_finished",
    "interrupted": "attempt_interrupted",
}

#: Where a caller should go instead of `set status=`, per destination status.
_LIFECYCLE_COMMAND_FOR_STATUS = {
    "in_progress": "claim",
    "open": "release",
    "blocked": "block",
    "shelved": "shelve",
    "closed": "close",
}


def _event(
    kind: str,
    *,
    ticket_id: str,
    attempt_id: Optional[str],
    payload: Dict,
    category: str = "lifecycle",
    timestamp: Optional[str] = None,
) -> Event:
    """A validated lifecycle `Event` for a ticket/attempt transition.

    Events carry no `operation_id`: the guarded operation that wraps the
    transition writes its own `operation_recorded` event, and giving these the
    same operation id would make the dedup logic collapse them into one.
    """
    subjects = [ticket_id] + ([attempt_id] if attempt_id else [])
    return Event(
        id=new_record_id("event"),
        cursor=None,  # the store assigns the per-store cursor on append
        kind_=kind,
        category=category,
        timestamp=timestamp or utc_now(),
        subject_ids=subjects,
        operation_id=None,
        payload=dict(payload),
        payload_version=EVENT_PAYLOAD_VERSION,
    )


@dataclass(frozen=True)
class AcquisitionResult:
    """The outcome of one successful `TicketLifecycle.acquire`.

    `ticket` is the ticket as stored after the acquisition, `attempt` the new
    attempt, `created_attempt` is always True today (kept explicit so a future
    "reuse an existing attempt" path is visible), and `took_over` says whether an
    administrative takeover superseded a previous active attempt.
    """

    ticket: Ticket
    attempt: WorkAttempt
    created_attempt: bool
    took_over: bool


def require_claimable(
    ticket: Ticket,
    by_id: dict,
    active_attempt: Optional[WorkAttempt],
) -> None:
    """Refuse a ticket that is not ready to be freshly claimed.

    Pure: it reads the ticket, the whole ticket set and the current attempt and
    raises an `ArbiteError` naming the *blocking reason*, or returns None. The
    checks are the planning rules in order of what a caller can act on:

    - status must be ``open`` (a legacy ``in_progress`` ticket is adopted, not
      claimed; a closed/blocked/shelved ticket is not work);
    - the classification must be real: ``type``/``tier``/``domain`` must not be a
      ``TODO:`` placeholder and the status must not be ``raw``;
    - every dependency that resolves to a known ticket must be closed;
    - no other active attempt may exist for this ticket.

    The compare-and-swap that follows this guard is what makes a *concurrent*
    claim lose; this function is what makes an *unready* ticket refuse with a
    reason rather than a generic conflict.
    """
    if ticket.status != "open":
        hint = ""
        if ticket.status == "in_progress":
            hint = (
                f" -- use 'arbite claim {ticket.id} --agent <id> --adopt' for a legacy "
                "ticket with no attempt record, or '--force --reason <why>' to take over"
            )
        raise TicketError(
            f"ticket {ticket.id} is '{ticket.status}', not 'open'; only an open ticket "
            f"can be claimed{hint}"
        )

    for field_name in ("type", "tier", "domain"):
        value = getattr(ticket, field_name, None)
        if schema.is_placeholder(value):
            raise TicketError(
                f"ticket {ticket.id} is not classified yet ({field_name} is {value!r}); "
                f"finish triage (arbite fetch / arbite set) before claiming it"
            )

    unmet = graph.unmet_dependencies(ticket, by_id)
    if unmet:
        raise TicketError(
            f"ticket {ticket.id} has unmet dependencies: {', '.join(sorted(unmet))}; "
            "close them before claiming it (readiness is enforced on the operation, "
            "not just in 'list next')"
        )

    if active_attempt is not None:
        raise CoordinationConflict(
            f"ticket {ticket.id} already has an active attempt {active_attempt.id} "
            f"(worker {active_attempt.worker_id}, generation {active_attempt.generation}); "
            "only one attempt may be active per ticket -- release it first, or pass "
            "--force --reason <why> for an explicit administrative takeover",
            details={
                "ticket_id": ticket.id,
                "attempt_id": active_attempt.id,
                "worker_id": active_attempt.worker_id,
                "generation": active_attempt.generation,
            },
        )


@dataclass(frozen=True)
class _FileClaimReleaseRequest:
    """Everything the file-claim cleanup hook needs to release an attempt's claims.

    Deliberately a record rather than loose arguments: the hook writes into the
    *same* coordination transaction that ends the attempt, so the terminal attempt
    state and the released claims commit together and no observer can mutate under
    an old token once the transition is durable.
    """

    attempt: Optional[WorkAttempt]
    claims: List[FileClaim]
    transaction: object = None
    reason: str = "attempt ended"
    released_at: Optional[str] = None


def _release_file_claims_hook(request: _FileClaimReleaseRequest) -> List[FileClaim]:
    """Release every active claim in `request` into `request.transaction`.

    This replaces the C03 refusal stub with the real cleanup C09 requires. Each
    claim is marked released and a ``claim_released`` event naming the reason is
    appended, so the ownership change is attributable and the release history is
    retained (the claim record is never deleted). A released token can never be
    reactivated: reacquiring a path mints a new generation.

    The transaction is mandatory -- releasing claims *outside* the transaction
    that ends the attempt would leave a window in which the attempt is still
    active, which is exactly the state this cascade exists to remove.
    """
    transaction = request.transaction
    if transaction is None:  # pragma: no cover - internal misuse
        raise UnsupportedCoordination(
            "releasing an attempt's file claims requires the coordination "
            "transaction that ends the attempt, so the claim release and the "
            "attempt's terminal state commit together",
            details={"attempt_id": getattr(request.attempt, "id", None)},
        )
    moment = request.released_at or utc_now()
    released: List[FileClaim] = []
    for claim in request.claims:
        if not claim.is_active:
            continue
        claim.release(moment)
        transaction.put(claim)
        transaction.append_event(_claim_released_event(claim, request.reason))
        released.append(claim)
    return released


def _claim_released_event(claim: FileClaim, reason: str) -> Event:
    """A ``claim_released`` event for a claim the lifecycle cascade revoked.

    Deliberately no ``operation_id``: the lifecycle's guarded transition writes
    its own ``operation_recorded`` event, and sharing an id would collapse the two
    distinct records into one.
    """
    return Event(
        id=new_record_id("event"),
        cursor=None,
        kind_="claim_released",
        category="claim",
        timestamp=claim.released,
        subject_ids=[claim.id, claim.attempt_id, claim.ticket_id],
        operation_id=None,
        payload={
            "workspace_id": claim.workspace_id,
            "path": claim.path,
            "ticket_id": claim.ticket_id,
            "attempt_id": claim.attempt_id,
            "generation": claim.generation,
            "reason": reason,
            "cascade": "lifecycle",
        },
        payload_version=EVENT_PAYLOAD_VERSION,
    )


def _serialized(method):
    """Run a lifecycle method while holding the coarse operation lock (C05).

    This is how a lifecycle transition serializes against an in-flight filesystem
    operation: a mutation holds the same lock across intent -> apply -> receipt, so
    a close/release/takeover either happens entirely before it or entirely after
    it -- never in the middle of a half-applied change. The lock is one-shot,
    process-death-safe (the OS releases a `flock`), and re-entrant within this
    process, so a caller that already holds it is not deadlocked.
    """

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self.coordination.store.operation_lock():
            return method(self, *args, **kwargs)

    return wrapper


class TicketLifecycle:
    """Ticket acquisition and transition policy, bound to one coordination service.

    Construct with the workspace's `CoordinationService` (which owns the
    authoritative store and its binding) and the ticket sink. Every method is
    one-shot: it opens at most a bounded number of transactions, holds no lock
    between calls, and never waits for or reclaims another worker's attempt.
    """

    def __init__(
        self,
        coordination_service: CoordinationService,
        tickets,
        *,
        clock=None,
        fault_injector=None,
    ) -> None:
        self.coordination = coordination_service
        self.tickets = tickets
        self._clock = clock or utc_now
        self._fault_injector = fault_injector
        #: Lazily-built `MutationEngine`, used only for reconciliation. Built on
        #: first use so a lifecycle object with no file operations never pays for
        #: (or requires) the artifact store.
        self._engine = None

    # -- time --------------------------------------------------------------

    def now(self) -> str:
        return self._clock()

    def _fault(self, phase: str) -> None:
        """Simulate a crash at `phase` when a test installed an injector."""
        if self._fault_injector is not None:
            self._fault_injector(phase)

    # -- serialization -----------------------------------------------------

    @contextlib.contextmanager
    def locked(self):
        """Hold the operation lock for a whole read-validate-write transition.

        A command wraps its entire body in this, so the ticket and attempt state it
        validates against cannot change before its own write: every transition,
        every file mutation and every dependency edit takes the same lock. On entry
        any transition a crash left unsettled is settled first, so the command
        never validates against a half-applied one. Re-entrant within a process.
        """
        with self.coordination.store.operation_lock():
            self.reconcile_lifecycle()
            yield self

    # -- attempt queries ---------------------------------------------------

    def attempts_for(self, ticket_id: str) -> List[WorkAttempt]:
        """Every attempt recorded for `ticket_id`, oldest generation first."""
        with self.coordination.store.transaction(write=False) as tx:
            found = list(tx.find("work_attempt", ticket_id=ticket_id))
        return sorted(found, key=lambda attempt: (attempt.generation, attempt.started))

    def active_attempt(self, ticket_id: str) -> Optional[WorkAttempt]:
        """The ticket's active attempt, or None.

        A terminal attempt is not returned: the whole point of generations is
        that a released/finished/interrupted attempt can never act again.
        """
        active = [attempt for attempt in self.attempts_for(ticket_id) if attempt.is_active]
        if not active:
            return None
        return max(active, key=lambda attempt: attempt.generation)

    def next_generation(self, ticket_id: str) -> int:
        """The next attempt generation for a ticket: max recorded + 1, else 1."""
        generations = [attempt.generation for attempt in self.attempts_for(ticket_id)]
        return max(generations) + 1 if generations else 1

    # -- guards ------------------------------------------------------------

    def require_ownership(
        self,
        attempt: WorkAttempt,
        agent: str,
        *,
        force: bool = False,
        reason: Optional[str] = None,
        action: str = "this command",
    ) -> WorkAttempt:
        """Refuse a transition by anyone but the attempt's worker.

        `--force` with a non-empty `--reason` converts the refusal into an
        explicit administrative revocation: the caller may proceed, and the
        transition ends the old attempt as *interrupted* with the reason recorded.
        There is no silent bypass and no last-write-wins here.
        """
        if agent == attempt.worker_id:
            return attempt
        if force:
            if not (reason and str(reason).strip()):
                raise TicketError(
                    f"ticket {attempt.ticket_id} is held by attempt {attempt.id} "
                    f"(worker {attempt.worker_id}); --force revokes another worker's "
                    "attempt and therefore requires a non-empty --reason"
                )
            return attempt
        raise TicketError(
            f"ticket {attempt.ticket_id} is held by attempt {attempt.id} "
            f"(worker {attempt.worker_id}); {action} by {agent!r} is refused -- pass "
            f"--agent {attempt.worker_id}, or --force --reason <why> for an explicit "
            "administrative revocation"
        )

    # -- file-claim cleanup (C09) -----------------------------------------

    def mutation_engine(self) -> "MutationEngine":
        """A `MutationEngine` for reconciling incomplete operations, built lazily.

        The lifecycle only needs the engine's *reconciliation* half, so the same
        engine (and the same coarse operation lock) a mutation uses is reused
        rather than reimplemented -- there is no second recovery policy.
        """
        if self._engine is None:
            self._engine = MutationEngine(
                self.coordination, FileClaimService(self.coordination)
            )
        return self._engine

    def reconcile_operations(self) -> List:
        """Reconcile every incomplete file operation for this workspace.

        Unambiguous intents (observed bytes match the recorded before or after
        version) are finalized exactly as the next mutation would; an ambiguous
        one (bytes match neither) raises `DriftDetected` with
        ``bytes_may_have_changed=True``. A lifecycle transition calls this before
        it ends an attempt, so a close can never report itself clean over bytes
        that may already have changed.
        """
        return self.mutation_engine().reconcile()

    def release_file_claims(
        self,
        *,
        attempt: Optional[WorkAttempt] = None,
        ticket_id: Optional[str] = None,
        reason: str,
        transaction,
    ) -> List[FileClaim]:
        """Release active claims for `attempt`/`ticket_id` inside `transaction`.

        Normally called with `attempt` from the transaction that ends it;
        `ticket_id` alone sweeps any orphan claim a pre-C09 store may hold for the
        ticket. The release is written in the *caller's* transaction, so it
        commits atomically with whatever else that transaction records -- which is
        what removes the window in which an old token could still mutate.
        """
        found = list(
            transaction.find("file_claim", workspace_id=self.coordination.workspace.id)
        )
        selected = [
            claim
            for claim in found
            if claim.is_active
            and (attempt is None or claim.attempt_id == attempt.id)
            and (ticket_id is None or claim.ticket_id == ticket_id)
        ]
        if not selected:
            return []
        return _release_file_claims_hook(
            _FileClaimReleaseRequest(
                attempt=attempt,
                claims=selected,
                transaction=transaction,
                reason=reason,
                released_at=self.now(),
            )
        )

    @_serialized
    def release_ticket_claims(self, ticket_id: str, *, reason: str) -> List[FileClaim]:
        """Release every active claim still recorded for `ticket_id`.

        A defensive sweep for `close`: in normal operation `end_attempt` has
        already released the attempt's claims, so this finds nothing. It exists so
        a claim a pre-C09 store left behind cannot keep exclusive ownership of a
        path after the ticket is closed. Does nothing (and opens no write
        transaction) when there is nothing to release.
        """
        with self.coordination.store.transaction(write=False) as tx:
            active = [
                claim
                for claim in tx.find(
                    "file_claim",
                    workspace_id=self.coordination.workspace.id,
                    ticket_id=ticket_id,
                )
                if claim.is_active
            ]
        if not active:
            return []
        with self.coordination.store.transaction() as tx:
            return self.release_file_claims(
                ticket_id=ticket_id, reason=reason, transaction=tx
            )

    def require_deletable(self, ticket: Ticket) -> None:
        """Refuse a destructive delete that would bypass cleanup or lose history.

        Two rules, both from the plan's "delete" row: a ticket with an active
        attempt (or an active file claim) is refused so lifecycle cleanup is not
        bypassed, and a ticket with *any* retained coordination history --
        attempts, claims or receipted operations -- is refused with an explicit
        explanation rather than silently cascading that change history away when
        the ticket row is removed (`arbite close` archives it instead). `--force`
        overrides neither rule; there is no backdoor through force.
        """
        active = self.active_attempt(ticket.id)
        if active is not None:
            raise TicketError(
                f"ticket {ticket.id} has an active attempt {active.id} (worker "
                f"{active.worker_id}); release or close it first so its file claims "
                "are released and its evidence is finalized, then delete it"
            )
        with self.coordination.store.transaction(write=False) as tx:
            claims = [
                claim
                for claim in tx.find(
                    "file_claim",
                    workspace_id=self.coordination.workspace.id,
                    ticket_id=ticket.id,
                )
                if claim.is_active
            ]
        if claims:
            raise TicketError(
                f"ticket {ticket.id} still holds {len(claims)} active file claim(s) "
                f"({', '.join(sorted(claim.path for claim in claims))}); release them "
                "before deleting it"
            )
        attempts = self.attempts_for(ticket.id)
        if not attempts:
            return
        raise TicketError(
            f"ticket {ticket.id} has {len(attempts)} recorded work attempt(s); "
            "deleting it would silently cascade away its change history and evidence "
            "-- use 'arbite close' to archive it instead"
        )

    def new_attempt(
        self, ticket_id: str, *, worker_id: str, handoff: Optional[str] = None
    ) -> WorkAttempt:
        """A fresh, not-yet-stored attempt at the ticket's next generation."""
        now = self.now()
        return WorkAttempt(
            id=new_record_id("work_attempt"),
            ticket_id=ticket_id,
            worker_id=worker_id,
            workspace_id=self.coordination.workspace.id,
            generation=self.next_generation(ticket_id),
            started=now,
            last_activity=now,
            handoff=handoff,
        )

    def require_no_pending_operations(self, attempt: WorkAttempt, *, transaction=None) -> None:
        """Refuse to end/supersede an attempt that has an unresolved file operation.

        A pending or drifted intent means bytes may be mid-change; ending the
        attempt now (and, in C09, releasing its claims) would be a clean-looking
        transition over an ambiguous filesystem. The refusal carries
        `bytes_may_have_changed=True` and the operation id to inspect. Runs under
        the operation lock (see `_serialized`), so it cannot interleave with the
        operation it is checking.
        """
        if transaction is not None:
            found = list(transaction.find("operation_intent", attempt_id=attempt.id))
        else:
            with self.coordination.store.transaction(write=False) as tx:
                found = list(tx.find("operation_intent", attempt_id=attempt.id))
        unresolved = [i for i in found if i.state in ("pending", "applied", "drifted")]
        if unresolved:
            first = sorted(unresolved, key=lambda i: i.created)[0]
            raise RecoveryRequired(
                f"attempt {attempt.id} has {len(unresolved)} unresolved file "
                f"operation(s); operation {first.operation_id} is {first.state} "
                f"({first.detail or 'incomplete'}). Inspect and reconcile it before "
                "ending the attempt -- bytes may already have changed",
                details={
                    "attempt_id": attempt.id,
                    "ticket_id": attempt.ticket_id,
                    "operation_id": first.operation_id,
                    "state": first.state,
                    "paths": list(first.paths),
                    "unresolved_operation_ids": [i.operation_id for i in unresolved],
                },
                bytes_may_have_changed=True,
            )

    # -- acquisition -------------------------------------------------------

    @_serialized
    def acquire(
        self,
        ticket: Ticket,
        *,
        worker_id: str,
        adopt: bool = False,
        takeover: bool = False,
        reason: Optional[str] = None,
        declared_tier: Optional[str] = None,
    ) -> AcquisitionResult:
        """Acquire `ticket` for `worker_id`, recording exactly one active attempt.

        See the module docstring for the journaled ordering (intent, then a
        revision compare-and-swap on the ticket, then the coordination cascade,
        with compensation) and for the three origins. Readiness is enforced
        *here*, under the operation lock, not by the caller's candidate filter, so
        no acquisition path can bypass it.

        Worker eligibility (B01) is enforced here too, for every origin: a
        registered worker is evaluated through its profile (configured tier
        authoritative, disabled refused, `declared_tier` may not exceed it); an
        ad-hoc worker keeps the legacy behaviour. `--force` does not bypass it.
        """
        # Settle any transition a crash left behind, then re-read from the sink:
        # the caller's copy may be stale, and the compare-and-swap below must
        # describe exactly the state (revision included) it is replacing.
        self.reconcile_lifecycle()
        current = self.tickets.get(ticket.id, unique=True)
        by_id = {t.id: t for t in self.tickets.query(TicketQuery(buckets=("*",)))}
        attempts = self.attempts_for(current.id)
        active = None
        for attempt in attempts:
            if attempt.is_active and (active is None or attempt.generation > active.generation):
                active = attempt

        if adopt and takeover:
            raise TicketError(
                "--adopt and --force takeover are different operations: --adopt takes a "
                "legacy ticket with no attempt record, --force --reason revokes a live attempt"
            )
        if adopt and active is not None:
            raise TicketError(
                f"ticket {current.id} already has an active attempt {active.id} "
                f"(worker {active.worker_id}); --adopt is only for a ticket with no "
                "attempt record -- use '--force --reason <why>' to take over"
            )
        if adopt and attempts:
            raise TicketError(
                f"ticket {current.id} has {len(attempts)} attempt record(s) already; "
                "--adopt is only for a ticket with no attempt record (claim it normally, "
                "or --force --reason to take over a live attempt)"
            )
        if adopt and current.status != "in_progress":
            raise TicketError(
                f"--adopt only applies to a legacy in_progress ticket; ticket {current.id} "
                f"is '{current.status}' (claim it normally with 'arbite claim')"
            )

        legacy_takeover = False
        if adopt:
            origin = ORIGIN_ADOPTED
            # Adopt against the state just read, so a concurrent change loses. The
            # adopting worker becomes the assignee (recorded as the previous
            # declared owner in the attempt's handoff); for the common case of the
            # same worker resuming, this is a no-op.
            expect = Expect(status="in_progress", assignee=current.assignee)
        elif active is not None:
            # A live attempt exists, so this is an ownership question even without
            # --force: refuse (or take over explicitly) before looking at anything
            # else. Readiness is deliberately *not* re-checked on a takeover: the
            # work is already underway, and refusing because a dependency moved
            # would strand it. A takeover is a revocation, not a claim.
            if not takeover:
                raise CoordinationConflict(
                    f"ticket {current.id} already has an active attempt {active.id} "
                    f"(worker {active.worker_id}, generation {active.generation}); "
                    "release it first, or pass --force --reason <why> for an explicit "
                    "administrative takeover",
                    details={
                        "ticket_id": current.id,
                        "attempt_id": active.id,
                        "worker_id": active.worker_id,
                        "generation": active.generation,
                    },
                )
            if not (reason and str(reason).strip()):
                raise TicketError(
                    f"ticket {current.id} is held by attempt {active.id} "
                    f"(worker {active.worker_id}); an administrative takeover requires a "
                    "non-empty --reason"
                )
            # Reconcile the old attempt's incomplete operations *before* the
            # takeover transaction opens (a coordination transaction cannot nest),
            # so an unambiguous interrupted change is finalized and an ambiguous
            # one refuses the takeover rather than racing past it.
            self.reconcile_operations()
            origin = ORIGIN_TAKEOVER
            expect = Expect(status=current.status, assignee=current.assignee)
        else:
            # No attempt record for this ticket: either a fresh claim, or an
            # explicit ownership override of a legacy (pre-C03) owned ticket.
            forced = bool(takeover)
            takeover = False
            origin = ORIGIN_CLAIMED
            owned_elsewhere = bool(current.assignee and current.assignee != worker_id)
            if current.status == "open":
                # Readiness is enforced identically with and without --force:
                # --force may override *ownership*, never readiness. There is no
                # backdoor through force (an unmet dependency, a placeholder
                # classification or a non-open status still refuses).
                require_claimable(current, by_id, active)
                if owned_elsewhere:
                    if not (forced and reason and str(reason).strip()):
                        raise TicketError(
                            f"ticket {current.id} is assigned to {current.assignee!r}; "
                            "take it over with '--force --reason <why>' (an explicit "
                            "administrative override), or leave it alone"
                        )
                    expect = Expect(status="open", assignee=current.assignee)
                else:
                    expect = Expect(status="open", assignee=None)
            elif current.status == "in_progress":
                # A legacy in_progress ticket with no attempt record: a pre-C03
                # arbite started it, or a crash landed between the ticket write and
                # the attempt record. Only an explicit --force --reason recovers it
                # (--adopt handles the no-attempt adoption form). Readiness is not
                # re-checked here: the ticket was already claimed, and refusing
                # would strand the work rather than protect it.
                if not (forced and reason and str(reason).strip()):
                    raise TicketError(
                        f"ticket {current.id} is 'in_progress' with no attempt record; "
                        f"use 'arbite claim {current.id} --agent {worker_id} --adopt' to "
                        "adopt it, or --force --reason <why> to take it over from its "
                        "declared assignee"
                    )
                legacy_takeover = True
                origin = ORIGIN_TAKEOVER
                expect = Expect(status="in_progress", assignee=current.assignee)
            else:
                # Status is not something a claim may override, forced or not.
                raise TicketError(
                    f"ticket {current.id} is '{current.status}', not 'open'; a claim "
                    "cannot be forced past ticket status -- unblock, unshelve or reopen "
                    "it first"
                )

        self.require_eligible(current, worker_id, declared_tier=declared_tier)

        now = self.now()
        attempt = WorkAttempt(
            id=new_record_id("work_attempt"),
            ticket_id=current.id,
            worker_id=worker_id,
            workspace_id=self.coordination.workspace.id,
            generation=(max([a.generation for a in attempts]) + 1) if attempts else 1,
            started=now,
            last_activity=now,
            handoff=self._handoff_for(
                current, adopt=adopt, takeover=(takeover or legacy_takeover), reason=reason
            ),
        )

        previous = copy.deepcopy(current)
        if adopt:
            current.assignee = worker_id
        else:
            current.status = "in_progress"
            current.assignee = worker_id
            current.blocked_by = None
        current.updated = schema.now()
        # Status/assignee name the state being replaced; the revision makes the
        # swap refuse *any* intervening edit -- a dependency added between the
        # readiness check and this write must never be silently overwritten.
        expect = Expect(status=expect.status, assignee=expect.assignee, revision=previous.revision)
        try:
            self.commit_transition(
                "claim",
                current,
                previous=previous,
                expect=expect,
                start=attempt,
                supersede=active,
                origin=origin,
                reason=reason,
            )
        except Conflict as e:
            raise CoordinationConflict(
                f"refused to acquire {current.id}: the ticket changed while being "
                f"acquired ({e})",
                details={"ticket_id": current.id, "reason": str(e)},
            ) from e

        stored = self.tickets.get(current.id, unique=True)
        return AcquisitionResult(
            ticket=stored,
            attempt=attempt,
            created_attempt=True,
            took_over=bool((takeover and active is not None) or legacy_takeover),
        )

    def worker_declaration(
        self, worker_id: str, *, declared_tier: Optional[str] = None
    ) -> "eligibility.WorkerDeclaration":
        """What is known about `worker_id` (its profile, else ad hoc)."""
        return workers.declaration_for(
            self.coordination.store, worker_id, declared_tier=declared_tier
        )

    def require_eligible(
        self, ticket: Ticket, worker_id: str, *, declared_tier: Optional[str] = None
    ) -> "eligibility.Eligibility":
        """Raise `WorkerIneligible` unless `worker_id` may acquire `ticket`."""
        declaration = self.worker_declaration(worker_id, declared_tier=declared_tier)
        result = eligibility.evaluate(eligibility.requirements_for_ticket(ticket), declaration)
        return result.require(subject=f"ticket {ticket.id}")

    def _handoff_for(
        self,
        ticket: Ticket,
        *,
        adopt: bool,
        takeover: bool,
        reason: Optional[str],
    ) -> Optional[str]:
        if adopt:
            declared = ticket.assignee or "unassigned"
            return f"adopted legacy in_progress ticket (declared assignee: {declared})"
        if takeover:
            return f"administrative takeover: {reason}"
        return None

    # -- ticket transition events -----------------------------------------

    def ticket_event(
        self,
        kind: str,
        ticket_id: str,
        *,
        attempt_id: Optional[str] = None,
        payload: Optional[Dict] = None,
        timestamp: Optional[str] = None,
    ) -> Event:
        """A validated ticket-transition event, not yet stored.

        Pass it to `commit_transition(events=[...])` so it commits with the
        transition's coordination cascade. A rejected kind raises rather than
        producing an unreadable row.
        """
        event = _event(
            kind,
            ticket_id=ticket_id,
            attempt_id=attempt_id,
            payload=payload or {"ticket_id": ticket_id},
            timestamp=timestamp,
        )
        problems = event.validate()
        if problems:
            raise UnsupportedCoordination(
                f"cannot record event {kind!r}: {'; '.join(problems)}",
                details={"kind": kind, "ticket_id": ticket_id, "problems": list(problems)},
            )
        return event

    def transition_event(
        self,
        name: str,
        ticket_id: str,
        *,
        attempt_id: Optional[str] = None,
        payload: Optional[Dict] = None,
        timestamp: Optional[str] = None,
    ) -> Event:
        """An event for a transition that has no dedicated event kind, not yet stored.

        The planning rule allows reusing `operation_recorded` (category
        `operation`) for a transition that would otherwise have no event kind, with
        a payload describing it -- e.g. `release` on a ticket with no active
        attempt -- so the event log still says what moved the ticket.
        """
        merged = {"ticket_id": ticket_id, "transition": name}
        merged.update(payload or {})
        return _event(
            "operation_recorded",
            ticket_id=ticket_id,
            attempt_id=attempt_id,
            payload=merged,
            category="operation",
            timestamp=timestamp,
        )

    def record_ticket_event(self, kind: str, ticket_id: str, **kwargs) -> Event:
        """Append one ticket-transition event in its own transaction."""
        event = self.ticket_event(kind, ticket_id, **kwargs)
        with self.coordination.store.transaction() as tx:
            tx.append_event(event)
        return event

    def record_transition(self, name: str, ticket_id: str, **kwargs) -> Event:
        """Append one `transition_event` in its own transaction."""
        event = self.transition_event(name, ticket_id, **kwargs)
        with self.coordination.store.transaction() as tx:
            tx.append_event(event)
        return event

    # -- the lifecycle journal ---------------------------------------------

    @_serialized
    def commit_transition(
        self,
        name: str,
        ticket: Ticket,
        *,
        previous: Ticket,
        expect: Optional[Expect] = None,
        end: Optional[WorkAttempt] = None,
        end_state: Optional[str] = None,
        touch: Optional[WorkAttempt] = None,
        start: Optional[WorkAttempt] = None,
        supersede: Optional[WorkAttempt] = None,
        origin: Optional[str] = None,
        reason: Optional[str] = None,
        handoff: Optional[str] = None,
        sweep_ticket_claims: bool = False,
        events: Sequence[Event] = (),
    ) -> LifecycleIntent:
        """Write `ticket` and apply its coordination cascade as one recoverable unit.

        `previous` is the ticket exactly as it was read (it supplies the revision
        the write must replace, and is what a compensation restores); `ticket` is
        the changed copy. The cascade is described by the keyword arguments: `end`
        ends an active attempt in `end_state` (releasing its claims), `supersede`
        interrupts a taken-over attempt, `start` stores a new attempt, `touch`
        records activity, `sweep_ticket_claims` releases any claim still held for
        the ticket, and `events` are appended with it.

        See the module docstring for the protocol. Raises the ticket store's
        `Conflict` when the ticket changed since `previous` was read (nothing is
        changed), and re-raises a cascade failure after reverting the ticket.
        """
        if end is not None:
            if end_state not in _END_EVENT_KINDS:
                raise TicketError(
                    f"unknown attempt end state {end_state!r} "
                    f"(valid: {', '.join(sorted(_END_EVENT_KINDS))})"
                )
            self._stored_active(end)
            # An ambiguous interrupted operation refuses the transition (a close
            # cannot look clean over bytes that may already have changed); an
            # unambiguous one is finalized first, exactly as a mutation would.
            self.reconcile_operations()
            self.require_no_pending_operations(end)
        if expect is None:
            expect = Expect(
                status=previous.status,
                assignee=previous.assignee,
                revision=previous.revision,
            )

        moment = self.now()
        intent = LifecycleIntent(
            id=new_record_id("lifecycle_intent"),
            workspace_id=self.coordination.workspace.id,
            ticket_id=ticket.id,
            transition=name,
            actor=self.coordination.actor.id,
            created=moment,
            updated=moment,
            expected_revision=previous.revision,
            target_status=ticket.status,
            target_assignee=ticket.assignee,
            end_attempt_id=end.id if end is not None else None,
            end_state=end_state if end is not None else None,
            touch_attempt_id=touch.id if touch is not None else None,
            supersedes_attempt_id=supersede.id if supersede is not None else None,
            start_attempt=start,
            origin=origin,
            reason=reason,
            handoff=handoff,
            sweep_ticket_claims=bool(sweep_ticket_claims),
            events=[event.to_dict() for event in events],
        )
        problems = intent.validate()
        if problems:
            raise InvalidRecord(
                f"invalid lifecycle intent: {'; '.join(problems)}",
                details={"problems": list(problems)},
            )
        with self.coordination.store.transaction() as tx:
            tx.put(intent)
        self._fault(FAULT_AFTER_INTENT)

        # -- the commit point: the ticket compare-and-swap ------------------
        try:
            self.tickets.update(ticket, expect=expect)
        except Conflict as error:
            self._settle_intent(intent, "abandoned", f"ticket write refused: {error}")
            raise
        except LifecycleFault:
            raise
        except Exception:
            # The ticket store failed; whether the write landed is decided the
            # way reconciliation decides it, so the journal never stays pending
            # merely because the error path ran.
            try:
                if self._settle(intent) == "completed":
                    return intent
            except Exception:  # pragma: no cover - leave it pending for later
                pass
            raise
        self._fault(FAULT_AFTER_TICKET_WRITE)

        # -- the coordination cascade -------------------------------------
        try:
            self._apply_intent(intent)
        except LifecycleFault:
            raise
        except Exception as error:
            if self._compensate(ticket, previous):
                self._settle_intent(
                    intent,
                    "abandoned",
                    f"coordination cascade failed and the ticket write was reverted: {error}",
                )
            raise
        return intent

    @_serialized
    def reconcile_lifecycle(self) -> List[LifecycleIntent]:
        """Settle every lifecycle transition a crash left pending; return them.

        For each pending intent the ticket is read: if it shows the recorded
        target status/assignee at a revision past the one the intent expected,
        the ticket write landed and the coordination cascade is rolled forward;
        otherwise the write never happened and the transition is abandoned. Runs
        at the start of every lifecycle transition (see `locked`) and from
        ``arbite doctor --fix`` -- never on a timer.
        """
        with self.coordination.store.transaction(write=False) as tx:
            pending = [
                intent
                for intent in tx.find(
                    "lifecycle_intent", workspace_id=self.coordination.workspace.id
                )
                if intent.is_pending
            ]
        settled: List[LifecycleIntent] = []
        for intent in sorted(pending, key=lambda i: (i.created, i.id)):
            self._settle(intent)
            settled.append(intent)
        return settled

    def _settle(self, intent: LifecycleIntent) -> str:
        """Roll one pending intent forward or abandon it; return the new state."""
        try:
            ticket = self.tickets.read(intent.ticket_id)
        except TicketNotFound:
            ticket = None
        landed = (
            ticket is not None
            and ticket.revision != intent.expected_revision
            and ticket.status == intent.target_status
            and ticket.assignee == intent.target_assignee
        )
        if landed:
            intent.detail = "completed by reconciliation after an interrupted transition"
            self._apply_intent(intent)
            return "completed"
        detail = (
            "the ticket no longer exists"
            if ticket is None
            else "the ticket write never landed (ticket is "
            f"{ticket.status}/{ticket.assignee or 'unassigned'} at revision {ticket.revision})"
        )
        self._settle_intent(intent, "abandoned", detail)
        return "abandoned"

    def _settle_intent(self, intent: LifecycleIntent, state: str, detail: str) -> None:
        with self.coordination.store.transaction() as tx:
            intent.state = state
            intent.detail = detail
            intent.updated = self.now()
            tx.put(intent)

    def _stored_active(self, attempt: WorkAttempt) -> WorkAttempt:
        """The stored version of `attempt`, which must still be active."""
        with self.coordination.store.transaction(write=False) as tx:
            stored = tx.get("work_attempt", attempt.id)
        if stored is None:
            raise CoordinationNotFound(
                f"no attempt {attempt.id!r} is recorded in this store",
                details={"attempt_id": attempt.id, "ticket_id": attempt.ticket_id},
            )
        return require_active_attempt(stored)

    def _compensate(self, ticket: Ticket, previous: Ticket) -> bool:
        """Revert a landed ticket write to `previous`; True when it was reverted.

        Guarded by the revision the write produced, so the revert can never
        overwrite a change made after it. A failure leaves the intent pending, and
        reconciliation then rolls the transition forward instead."""
        restored = copy.deepcopy(previous)
        try:
            self.tickets.update(
                restored,
                expect=Expect(
                    status=ticket.status, assignee=ticket.assignee, revision=ticket.revision
                ),
            )
        except Exception:
            return False
        return True

    def _apply_intent(self, intent: LifecycleIntent) -> None:
        """Apply the coordination half of `intent` in one transaction.

        Everything is derived from the intent and from records re-read inside the
        transaction, so the normal path and a roll-forward after a crash are the
        same code. The transaction commits the attempt changes, the released
        claims, the events and the intent's `completed` state together.
        """
        anchor = intent.start_attempt
        kind = "claim"
        if anchor is None:
            kind = "lifecycle"
            for attempt_id in (intent.end_attempt_id, intent.touch_attempt_id):
                if attempt_id is None:
                    continue
                with self.coordination.store.transaction(write=False) as tx:
                    found = tx.get("work_attempt", attempt_id)
                if found is not None and found.is_active:
                    anchor = found
                    break
        if anchor is not None:
            context = self.coordination.guarded(kind, attempt=anchor, ticket_id=intent.ticket_id)
        else:
            context = _PlainTransaction(self.coordination.store.transaction())
        with context as op:
            self._cascade(op.transaction, intent)
        return None

    def _cascade(self, tx, intent: LifecycleIntent) -> None:
        moment = self.now()
        reason = intent.reason
        if intent.start_attempt is not None:
            # The atomic "one active attempt per ticket" guard, checked before the
            # superseded attempt is interrupted in this same transaction.
            unexpected = [
                candidate
                for candidate in tx.find("work_attempt", ticket_id=intent.ticket_id)
                if candidate.is_active and candidate.id != intent.supersedes_attempt_id
            ]
            if unexpected:
                raise CoordinationConflict(
                    f"ticket {intent.ticket_id} gained an active attempt "
                    f"{unexpected[0].id} while being acquired; nothing was recorded for "
                    "this attempt",
                    details={
                        "ticket_id": intent.ticket_id,
                        "active_attempt_id": unexpected[0].id,
                        "caller_attempt_id": intent.start_attempt.id,
                    },
                )

        if intent.supersedes_attempt_id is not None:
            old = tx.get("work_attempt", intent.supersedes_attempt_id)
            if old is not None and old.is_active:
                why = reason or "superseded by an administrative takeover"
                # Release the superseded attempt's claims in this same
                # transaction, so the interrupt and the claim release commit
                # together and no stale token survives the takeover.
                self.release_file_claims(attempt=old, reason=why, transaction=tx)
                self.require_no_pending_operations(old, transaction=tx)
                old.interrupt(timestamp=moment, reason=why)
                tx.put(old)
                tx.append_event(
                    _event(
                        "attempt_interrupted",
                        ticket_id=intent.ticket_id,
                        attempt_id=old.id,
                        timestamp=old.ended,
                        payload={
                            "ticket_id": intent.ticket_id,
                            "worker_id": old.worker_id,
                            "generation": old.generation,
                            "reason": old.outcome,
                            "superseded_by": (
                                intent.start_attempt.id if intent.start_attempt else None
                            ),
                        },
                    )
                )

        if intent.end_attempt_id is not None:
            attempt = tx.get("work_attempt", intent.end_attempt_id)
            if attempt is not None and attempt.is_active:
                self._end_in(tx, attempt, intent.end_state, reason, intent.handoff, moment)

        if intent.touch_attempt_id is not None:
            attempt = tx.get("work_attempt", intent.touch_attempt_id)
            if attempt is not None and attempt.is_active:
                attempt.touch(timestamp=moment)
                tx.put(attempt)

        if intent.start_attempt is not None:
            attempt = intent.start_attempt
            tx.put(attempt)
            for event_kind in ("attempt_started", "ticket_claimed"):
                payload = {
                    "ticket_id": intent.ticket_id,
                    "worker_id": attempt.worker_id,
                    "origin": intent.origin,
                    "generation": attempt.generation,
                }
                if event_kind == "attempt_started":
                    payload["handoff"] = attempt.handoff
                tx.append_event(
                    _event(
                        event_kind,
                        ticket_id=intent.ticket_id,
                        attempt_id=attempt.id,
                        timestamp=attempt.started,
                        payload=payload,
                    )
                )

        if intent.sweep_ticket_claims:
            # Defensive: a claim a pre-C09 store left behind for this ticket must
            # not keep exclusive ownership of a path once the ticket is closed.
            self.release_file_claims(
                ticket_id=intent.ticket_id, reason=reason or intent.transition, transaction=tx
            )

        for data in intent.events:
            tx.append_event(Event.from_dict(data))

        intent.state = "completed"
        intent.updated = moment
        tx.put(intent)

    def _end_in(self, tx, attempt: WorkAttempt, state: str, reason, handoff, moment) -> None:
        """End `attempt` in `state` inside `tx`, releasing its claims first."""
        # Releasing the claims in the transaction that ends the attempt is what
        # makes "no observer can mutate under an old token once the attempt has
        # ended" hold.
        self.release_file_claims(attempt=attempt, reason=reason or state, transaction=tx)
        self.require_no_pending_operations(attempt, transaction=tx)
        if state == "released":
            attempt.release(timestamp=moment, handoff=handoff, outcome=reason or "released")
        elif state == "finished":
            attempt.finish(timestamp=moment, outcome=reason or "finished", handoff=handoff)
        else:
            attempt.interrupt(timestamp=moment, reason=reason or "interrupted")
        tx.put(attempt)
        tx.append_event(
            _event(
                _END_EVENT_KINDS[state],
                ticket_id=attempt.ticket_id,
                attempt_id=attempt.id,
                timestamp=attempt.ended,
                payload={
                    "ticket_id": attempt.ticket_id,
                    "worker_id": attempt.worker_id,
                    "generation": attempt.generation,
                    "outcome": attempt.outcome,
                    "reason": reason,
                },
            )
        )

    # -- transitions -------------------------------------------------------

    @_serialized
    def end_attempt(
        self,
        attempt: WorkAttempt,
        *,
        state: str = "released",
        reason: Optional[str] = None,
        handoff: Optional[str] = None,
    ) -> WorkAttempt:
        """End an active attempt in `state` and record the matching event.

        The coordination half of a transition on its own, with no ticket write --
        the commands use `commit_transition`, which journals both halves. Acts on
        the attempt *as stored*: an attempt another command already ended is
        refused however current the caller's object looks. On success the
        caller's object is updated to the terminal state as well.
        """
        require_active_attempt(attempt)
        if state not in _END_EVENT_KINDS:
            raise TicketError(
                f"unknown attempt end state {state!r} "
                f"(valid: {', '.join(sorted(_END_EVENT_KINDS))})"
            )
        # Reconcile first: an unambiguous interrupted operation is finalized, an
        # ambiguous one raises `DriftDetected`, so a close cannot look clean over
        # bytes that may already have changed.
        self.reconcile_operations()
        stored = self._stored_active(attempt)
        with self.coordination.guarded(
            "lifecycle", attempt=stored, ticket_id=stored.ticket_id
        ) as op:
            current = op.transaction.get("work_attempt", stored.id)
            require_active_attempt(current)
            self._end_in(op.transaction, current, state, reason, handoff, self.now())
        _copy_attempt(current, into=attempt)
        return attempt

    @_serialized
    def touch(self, attempt: WorkAttempt, when: Optional[str] = None) -> WorkAttempt:
        """Record activity on an active attempt without ending it.

        Used for non-terminal edits (a field change on a claimed ticket): the
        attempt stays active, and `last_activity` is stored for a *future*
        stale-work policy to read. Nothing here interprets it.
        """
        require_active_attempt(attempt)
        stored = self._stored_active(attempt)
        with self.coordination.guarded(
            "lifecycle", attempt=stored, ticket_id=stored.ticket_id
        ) as op:
            current = op.transaction.get("work_attempt", stored.id)
            require_active_attempt(current)
            current.touch(timestamp=when or self.now())
            op.transaction.put(current)
        _copy_attempt(current, into=attempt)
        return attempt

    # -- reopen / dependency invalidation ----------------------------------

    def invalidate_dependents(self, reopened: Ticket, every) -> List[str]:
        """Emit `dependency_invalidated` for live tickets depending on `reopened`.

        Reopening a completed ticket does not retroactively rewrite its dependents'
        status or assignee -- their work is not silently undone. What it does do is
        record, as events, that their readiness assumption changed, so an agent can
        see why a ticket that was workable a moment ago is not any more. Returns
        the affected ticket ids.

        Emitted through one read-only-safe coordination transaction; no attempt is
        created or ended and no ticket is modified.
        """
        dependents = sorted(
            t.id for t in every if reopened.id in t.depends_on and t.status != "closed"
        )
        if not dependents:
            return []
        with self.coordination.store.transaction() as tx:
            for dependent_id in dependents:
                tx.append_event(
                    _event(
                        "dependency_invalidated",
                        ticket_id=dependent_id,
                        attempt_id=None,
                        payload={
                            "ticket_id": dependent_id,
                            "reopened_ticket_id": reopened.id,
                            "affected_ticket_ids": dependents,
                        },
                    )
                )
        return dependents

    # -- set-status guidance ----------------------------------------------

    def refuse_status_edit(
        self,
        ticket: Ticket,
        *,
        new_status: Optional[str],
        new_assignee: Optional[str],
        attempt: WorkAttempt,
    ) -> None:
        """Refuse a `set status=`/`set assignee=` on a ticket with a live attempt.

        The refusal names the lifecycle command that *is* correct for the target
        status, because `arbite set status=...` cannot safely reproduce the side
        effects (ending/touching the attempt, filing, dating `closed`) that the
        dedicated command owns.
        """
        if new_status is not None and new_status != ticket.status:
            command = _LIFECYCLE_COMMAND_FOR_STATUS.get(new_status, "the matching lifecycle command")
            raise TicketError(
                f"ticket {ticket.id} has an active attempt {attempt.id} "
                f"(worker {attempt.worker_id}); refusing 'set status={new_status}' -- use "
                f"'arbite {command} {ticket.id}' so the attempt is ended/touched and the "
                "change is recorded, or --force --reason to revoke the attempt"
            )
        if new_assignee is not None and new_assignee != ticket.assignee:
            raise TicketError(
                f"ticket {ticket.id} has an active attempt {attempt.id} "
                f"(worker {attempt.worker_id}); refusing 'set assignee={new_assignee}' -- "
                f"use 'arbite release {ticket.id}' / 'arbite claim {ticket.id} --force "
                "--reason <why>' so ownership changes through the lifecycle, not a field edit"
            )


class _PlainTransaction:
    """Adapts a bare store transaction to the `guarded()` context shape.

    Used by a transition with no active attempt to anchor an operation receipt to
    (a legacy ticket), so `_apply_intent` has one code path either way."""

    def __init__(self, transaction):
        self.transaction = transaction

    def __enter__(self):
        self.transaction.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        return self.transaction.__exit__(exc_type, exc, tb)


def _copy_attempt(source: WorkAttempt, *, into: WorkAttempt) -> None:
    """Make the caller's attempt object reflect the stored outcome."""
    if into is source:
        return
    for name in ("state", "ended", "outcome", "handoff", "last_activity"):
        setattr(into, name, getattr(source, name))


#: Documented name for the file-claim cleanup hook, now the real C09 cleanup
#: (it releases an attempt's claims into the transaction that ends the attempt).
release_file_claims_hook = _release_file_claims_hook

__all__ = [
    "AcquisitionResult",
    "FAULT_AFTER_INTENT",
    "FAULT_AFTER_TICKET_WRITE",
    "FAULT_PHASES",
    "LifecycleFault",
    "ORIGIN_ADOPTED",
    "ORIGIN_CLAIMED",
    "ORIGIN_RESUMED",
    "ORIGIN_TAKEOVER",
    "TicketLifecycle",
    "release_file_claims_hook",
    "require_claimable",
]
