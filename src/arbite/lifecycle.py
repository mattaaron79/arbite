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

The invariant is enforced twice, deliberately:

1. **Readiness** (`require_claimable`) refuses a ticket that is not `open`, not
   classified, has unmet dependencies, or already has an active attempt. It names
   the blocking reason in the message so an agent can act on it without guessing.
2. **The ticket compare-and-swap** (`TicketSink.update(..., expect=Expect(status=,
   assignee=))`) is what makes exactly one of two concurrent claims of one ticket
   win. The loser's `Conflict` is raised before any coordination write, so no
   attempt row survives for it.
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

Why the ticket CAS and the coordination write are two steps, not one
--------------------------------------------------------------------

The obvious implementation nests the ticket CAS inside the coordination
transaction. That is impossible for the shipped SQLite sink: the coordination
transaction holds ``BEGIN IMMEDIATE`` on the same database file the ticket store
uses, so a nested ticket write on a second connection fails with "database is
locked". Rather than branch on sink kind -- which would break the "both sinks
behave equivalently" rule -- acquisition runs the ticket CAS first and the
coordination write second, and compensates by reverting the ticket if the
coordination write fails. This is documented, not hidden:

- a **lost** claim (the CAS refused) leaves no attempt record at all;
- a **coordination-storage failure** after a successful CAS reverts the ticket and
  propagates the error;
- the two stores are **not** updated atomically, so a crash between them can leave
  a ticket `in_progress` with no attempt -- exactly the legacy state ``--adopt``
  exists to resolve. (C04's explicit binding work is where true cross-store
  atomicity would be designed; this ticket does not pretend to have it.)

Dependency-edit and reopen races (documented serial outcome)
-----------------------------------------------------------

Readiness is checked against the ticket set as it is read. A dependency change
and a claim therefore serialize in one of two honest ways: *a claim that commits
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

import functools
from dataclasses import dataclass
from typing import Dict, List, Optional

from . import coordination, graph, schema
from .application import Actor, CoordinationService, require_active_attempt
from .coordination import (
    EVENT_PAYLOAD_VERSION,
    Event,
    FileClaim,
    WorkAttempt,
    new_record_id,
    utc_now,
)
from .errors import (
    Conflict,
    CoordinationConflict,
    DriftDetected,
    RecoveryRequired,
    TicketError,
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

    def __init__(self, coordination_service: CoordinationService, tickets, *, clock=None) -> None:
        self.coordination = coordination_service
        self.tickets = tickets
        self._clock = clock or utc_now
        #: Lazily-built `MutationEngine`, used only for reconciliation. Built on
        #: first use so a lifecycle object with no file operations never pays for
        #: (or requires) the artifact store.
        self._engine = None

    # -- time --------------------------------------------------------------

    def now(self) -> str:
        return self._clock()

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

    @_serialized
    def start_resumed_attempt(
        self, ticket: Ticket, *, worker_id: str, reason: Optional[str] = None
    ) -> AcquisitionResult:
        """Mint a fresh attempt for a ticket whose previous attempt was ended.

        Used by `unblock` when a blocked ticket returns to `in_progress` for its
        declared owner: `block` ended the previous attempt, and resuming work must
        be a *new* generation -- the old attempt and its tokens stay dead. This is
        deliberately not a `--force` takeover (nobody is being revoked); it simply
        starts the next generation on a ticket that is already owned.
        """
        active = self.active_attempt(ticket.id)
        if active is not None:
            raise CoordinationConflict(
                f"ticket {ticket.id} already has an active attempt {active.id} "
                f"(worker {active.worker_id}); resume is only for a ticket with no "
                "active attempt",
                details={"ticket_id": ticket.id, "attempt_id": active.id},
            )
        now = self.now()
        attempt = WorkAttempt(
            id=new_record_id("work_attempt"),
            ticket_id=ticket.id,
            worker_id=worker_id,
            workspace_id=self.coordination.workspace.id,
            generation=self.next_generation(ticket.id),
            started=now,
            last_activity=now,
            handoff=(
                f"resumed after block: {reason}" if reason else "resumed after block"
            ),
        )
        self._record_acquisition(
            ticket, attempt, old=None, origin=ORIGIN_RESUMED, reason=reason
        )
        return AcquisitionResult(
            ticket=ticket, attempt=attempt, created_attempt=True, took_over=False
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
    ) -> AcquisitionResult:
        """Acquire `ticket` for `worker_id`, recording exactly one active attempt.

        See the module docstring for the ordering rationale (ticket CAS first,
        coordination transaction second, with compensation) and for the three
        origins. Readiness is enforced *here*, not by the caller's candidate
        filter, so no acquisition path can bypass it.
        """
        # Re-read from the sink: the caller's copy may be stale, and the CAS below
        # must describe the state it is replacing.
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

        state_changes = True
        legacy_takeover = False
        if adopt:
            origin = ORIGIN_ADOPTED
            # Adopt against the state just read, so a concurrent change loses. The
            # adopting worker becomes the assignee (recorded as the previous
            # declared owner in the attempt's handoff); for the common case of the
            # same worker resuming, this is a no-op.
            expect = Expect(status="in_progress", assignee=current.assignee)
            state_changes = False
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

        # --- step 1: the authoritative ticket compare-and-swap ---------------
        previous = {
            "status": current.status,
            "assignee": current.assignee,
            "blocked_by": current.blocked_by,
            "closed": current.closed,
        }
        if adopt:
            current.assignee = worker_id
        else:
            current.status = "in_progress"
            current.assignee = worker_id
            current.blocked_by = None
        current.updated = schema.now()
        try:
            self.tickets.update(current, expect=expect)
        except Conflict as e:
            raise CoordinationConflict(
                f"refused to acquire {current.id}: the ticket changed while being "
                f"acquired ({e})",
                details={"ticket_id": current.id, "reason": str(e)},
            ) from e

        # --- step 2: the coordination transaction ----------------------------
        try:
            self._record_acquisition(
                current,
                attempt,
                old=active,
                origin=origin,
                reason=reason,
            )
        except BaseException:
            if state_changes:
                self._revert_ticket(current, previous, worker_id)
            raise

        stored = self.tickets.get(current.id, unique=True)
        return AcquisitionResult(
            ticket=stored,
            attempt=attempt,
            created_attempt=True,
            took_over=bool((takeover and active is not None) or legacy_takeover),
        )

    def _record_acquisition(
        self,
        ticket: Ticket,
        attempt: WorkAttempt,
        *,
        old: Optional[WorkAttempt],
        origin: str,
        reason: Optional[str],
    ) -> None:
        """Store the attempt and its events in one guarded transaction.

        The in-transaction re-check is the atomic "one active attempt per ticket"
        guard: it is what makes concurrent *adoption* (which cannot be caught by a
        ticket CAS, because adoption does not change ticket state) safe.
        """
        with self.coordination.guarded("claim", attempt=attempt, ticket_id=ticket.id) as op:
            tx = op.transaction
            active_now = [
                candidate
                for candidate in tx.find("work_attempt", ticket_id=ticket.id)
                if candidate.is_active
            ]
            unexpected = [
                candidate
                for candidate in active_now
                if old is None or candidate.id != old.id
            ]
            if unexpected:
                raise CoordinationConflict(
                    f"ticket {ticket.id} gained an active attempt {unexpected[0].id} "
                    "while being acquired; nothing was recorded for this attempt",
                    details={
                        "ticket_id": ticket.id,
                        "active_attempt_id": unexpected[0].id,
                        "caller_attempt_id": attempt.id,
                    },
                )

            if old is not None:
                # Release the superseded attempt's claims in this same
                # transaction, so the interrupt and the claim release commit
                # together and no stale token survives the takeover.
                self.release_file_claims(
                    attempt=old,
                    reason=reason or "superseded by an administrative takeover",
                    transaction=tx,
                )
                self.require_no_pending_operations(old, transaction=tx)
                old.interrupt(
                    timestamp=self.now(),
                    reason=reason or "superseded by an administrative takeover",
                )
                tx.put(old)
                tx.append_event(
                    _event(
                        "attempt_interrupted",
                        ticket_id=ticket.id,
                        attempt_id=old.id,
                        timestamp=old.ended,
                        payload={
                            "ticket_id": ticket.id,
                            "worker_id": old.worker_id,
                            "generation": old.generation,
                            "reason": old.outcome,
                            "superseded_by": attempt.id,
                        },
                    )
                )

            tx.put(attempt)
            tx.append_event(
                _event(
                    "attempt_started",
                    ticket_id=ticket.id,
                    attempt_id=attempt.id,
                    timestamp=attempt.started,
                    payload={
                        "ticket_id": ticket.id,
                        "worker_id": attempt.worker_id,
                        "origin": origin,
                        "generation": attempt.generation,
                        "handoff": attempt.handoff,
                    },
                )
            )
            tx.append_event(
                _event(
                    "ticket_claimed",
                    ticket_id=ticket.id,
                    attempt_id=attempt.id,
                    timestamp=attempt.started,
                    payload={
                        "ticket_id": ticket.id,
                        "worker_id": attempt.worker_id,
                        "origin": origin,
                        "generation": attempt.generation,
                    },
                )
            )

    def _revert_ticket(self, ticket: Ticket, previous: Dict, worker_id: str) -> None:
        """Best-effort compensation for a failed coordination write after the CAS.

        The ticket was moved but no attempt was recorded, so put it back to the
        state it was read in. A failure here is reported by the original exception
        (which is re-raised by the caller); the ticket is then a legacy state that
        `--adopt`/`--force` can still resolve, so nothing is silently lost.
        """
        try:
            restored = self.tickets.get(ticket.id, unique=True)
            restored.status = previous["status"]
            restored.assignee = previous["assignee"]
            restored.blocked_by = previous["blocked_by"]
            restored.closed = previous["closed"]
            restored.updated = schema.now()
            self.tickets.update(
                restored, expect=Expect(status="in_progress", assignee=worker_id)
            )
        except Exception:  # pragma: no cover - compensation is best-effort
            pass

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

    def record_ticket_event(
        self,
        kind: str,
        ticket_id: str,
        *,
        attempt_id: Optional[str] = None,
        payload: Optional[Dict] = None,
        timestamp: Optional[str] = None,
    ) -> Event:
        """Append one ticket-transition event in its own transaction.

        Used for transitions that happen with no active attempt (a legacy ticket),
        so the event log still records what moved the ticket. A rejected event kind
        raises rather than writing an unreadable row.
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
        with self.coordination.store.transaction() as tx:
            tx.append_event(event)
        return event

    def record_transition(
        self,
        name: str,
        ticket_id: str,
        *,
        attempt_id: Optional[str] = None,
        payload: Optional[Dict] = None,
        timestamp: Optional[str] = None,
    ) -> Event:
        """Record a ticket transition that has no dedicated event kind.

        The planning rule allows reusing `operation_recorded` (category
        `operation`) for a transition that would otherwise have no event kind, with
        a payload describing it. Used for legacy transitions -- e.g. `release` on
        a ticket with no active attempt -- so the event log still says what moved
        the ticket rather than silently omitting it.
        """
        merged = {"ticket_id": ticket_id, "transition": name}
        merged.update(payload or {})
        event = _event(
            "operation_recorded",
            ticket_id=ticket_id,
            attempt_id=attempt_id,
            payload=merged,
            category="operation",
            timestamp=timestamp,
        )
        with self.coordination.store.transaction() as tx:
            tx.append_event(event)
        return event

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

        `state` is one of `released`/`finished`/`interrupted`; the attempt is
        mutated to its terminal state inside a guarded transaction, so the record
        and the event commit together. A terminal attempt can never be re-ended
        (the model refuses), which is how an old token stays dead.

        Callers end the attempt **before** moving the ticket: if the ticket write
        then loses a race, the attempt is already terminal and the retry takes the
        legacy path, rather than the ticket moving while the attempt stays active.
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
        with self.coordination.guarded(
            "lifecycle", attempt=attempt, ticket_id=attempt.ticket_id
        ) as op:
            tx = op.transaction
            # Release the attempt's file claims in this same transaction, so the
            # attempt becoming terminal and the claims dying commit together: no
            # observer can mutate under an old token once the attempt has ended.
            self.release_file_claims(
                attempt=attempt,
                reason=reason or state,
                transaction=tx,
            )
            self.require_no_pending_operations(attempt, transaction=tx)
            moment = self.now()
            if state == "released":
                attempt.release(timestamp=moment, handoff=handoff, outcome=reason or "released")
            elif state == "finished":
                attempt.finish(
                    timestamp=moment, outcome=reason or "finished", handoff=handoff
                )
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
        return attempt

    @_serialized
    def touch(self, attempt: WorkAttempt, when: Optional[str] = None) -> WorkAttempt:
        """Record activity on an active attempt without ending it.

        Used for non-terminal transitions (a field edit, a status change that does
        not end the work): the attempt stays active, and `last_activity` is stored
        for a *future* stale-work policy to read. Nothing here interprets it.
        """
        require_active_attempt(attempt)
        with self.coordination.guarded(
            "lifecycle", attempt=attempt, ticket_id=attempt.ticket_id
        ) as op:
            attempt.touch(timestamp=when or self.now())
            op.transaction.put(attempt)
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


#: Documented name for the file-claim cleanup hook, now the real C09 cleanup
#: (it releases an attempt's claims into the transaction that ends the attempt).
release_file_claims_hook = _release_file_claims_hook

__all__ = [
    "AcquisitionResult",
    "ORIGIN_ADOPTED",
    "ORIGIN_CLAIMED",
    "ORIGIN_RESUMED",
    "ORIGIN_TAKEOVER",
    "TicketLifecycle",
    "release_file_claims_hook",
    "require_claimable",
]
