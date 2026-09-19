"""The storage-neutral application layer for shared-directory coordination.

This is where coordination *policy* lives: the hard rules that must hold no
matter which command, script or future web API asked for the operation. The
planning rules say it plainly -- "hard rules belong in operations, not just
discovery filters or prompts" -- so the guards here are called by every entry
point (today none of the CLI's ticket commands, later the file proxy), and none
of them can be bypassed by calling a sink directly or by phrasing a filter
differently.

Two layers, deliberately separate:

- **`coordination`** (the domain module) defines the records and their
  invariants and knows nothing about storage. Use it for construction,
  serialization and validation.
- **`application`** (this module) sequences guarded multi-record operations
  through a `CoordinationStore`. A sink stores records; it does not decide
  whether a write is allowed, whether a read authorizes it, or whether an
  attempt is still current. Those questions are answered here, once, for both
  sinks.

Guards (`require_*`) are pure functions on records: they raise a typed
`CoordinationError` carrying an `ErrorCode`, so callers branch on the code.
They are written to run *inside* an operation holding the store's transaction,
which is what makes check-then-act safe rather than a race with extra steps.

One-shot, by construction: nothing here sleeps, retries automatically, polls or
backgrounds work. A caller that hits contention gets a structured refusal and
decides for itself whether to do something else or call again.

Attribution, not authentication: `Actor` is a self-declared label. Nothing in
this module verifies it. See `coordination.ATTRIBUTION_NOTICE`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from . import coordination, workspace as workspace_module
from .coordination import (
    ABSENT,
    EVENT_PAYLOAD_VERSION,
    Event,
    FileClaim,
    OperationReceipt,
    ReadObservation,
    RecoveryReport,
    StoreBinding,
    WorkAttempt,
    Workspace,
    canonical_relative_path,
    digest_of_bytes,
    is_digest_or_absent,
    new_record_id,
    utc_now,
)
from .errors import (
    AttemptInactive,
    ClaimConflict,
    CoordinationError,
    CoordinationNotFound,
    InvalidRecord,
    RecoveryRequired,
    StaleRead,
    UnsupportedCoordination,
)

#: Re-exported so a caller quoting the trust model has one canonical sentence.
ATTRIBUTION_NOTICE = coordination.ATTRIBUTION_NOTICE


@dataclass(frozen=True)
class Actor:
    """Who is asking, as *declared* by the caller.

    `id` is the durable handle used in records and receipts; `declared_name` is
    the friendly form for a human reading a log. Neither is authenticated:
    `attribution_only` is True by construction, and an operation's real guarantee
    comes from the claim generation and digests it presents, not from the name it
    claims. This distinction is what keeps "who says they did this" separate from
    "what the store can prove happened"."""

    id: str
    declared_name: Optional[str] = None

    @property
    def attribution_only(self) -> bool:
        return True

    def __str__(self) -> str:
        return self.declared_name or self.id


# ---------------------------------------------------------------------------
# Guards: the hard rules, as pure functions
# ---------------------------------------------------------------------------


def require_active_attempt(attempt: Optional[WorkAttempt]) -> WorkAttempt:
    """Raise unless `attempt` exists and is active.

    A finished/released/interrupted attempt may not claim or mutate. Resuming
    work mints a *new* attempt (or goes through an explicit administrative
    takeover); reactivating the old token is never allowed, which is why an old
    worker cannot write after release or takeover."""
    if attempt is None:
        raise CoordinationNotFound("no work attempt given for this operation")
    if not attempt.is_active:
        raise AttemptInactive(
            f"attempt {attempt.id} for ticket {attempt.ticket_id} is {attempt.state}; "
            "only an active attempt may claim or mutate (resume with a new attempt)",
            details={
                "attempt_id": attempt.id,
                "ticket_id": attempt.ticket_id,
                "state": attempt.state,
            },
        )
    return attempt


def pending_lifecycle_intents(transaction, ticket_id: str) -> list:
    """The ticket's lifecycle intents that have not settled yet, oldest first.

    A pending intent means a lifecycle transition's ticket write and its
    coordination cascade may disagree (a crash landed between them), so the
    attempt's state cannot be trusted until the next lifecycle operation -- or
    `arbite doctor --fix` -- settles it."""
    found = [
        intent
        for intent in transaction.find("lifecycle_intent", ticket_id=ticket_id)
        if intent.is_pending
    ]
    return sorted(found, key=lambda intent: (intent.created, intent.id))


def require_current_attempt(transaction, attempt: Optional[WorkAttempt]) -> WorkAttempt:
    """The *stored* version of `attempt`, which must still be active and settled.

    The caller's `attempt` object is only a handle: it was loaded before this
    operation began, and a close, release or takeover may have ended the attempt
    since. This reloads it inside the caller's transaction (or operation lock) and
    refuses a terminal attempt, and it refuses while the ticket has an unsettled
    lifecycle transition, because then the ticket may already be closed while the
    attempt still reads as active. Returns the stored attempt, which is what every
    later check in the operation must use."""
    if attempt is None:
        raise CoordinationNotFound("no work attempt given for this operation")
    stored = transaction.get("work_attempt", attempt.id)
    if stored is None:
        raise CoordinationNotFound(
            f"no attempt {attempt.id!r} is recorded in this store",
            details={"attempt_id": attempt.id, "ticket_id": attempt.ticket_id},
        )
    require_active_attempt(stored)
    pending = pending_lifecycle_intents(transaction, stored.ticket_id)
    if pending:
        first = pending[0]
        raise RecoveryRequired(
            f"ticket {stored.ticket_id} has an unfinished '{first.transition}' "
            f"transition (lifecycle intent {first.id}); run any lifecycle command on "
            "the ticket, or 'arbite doctor --fix', to settle it before using attempt "
            f"{stored.id}",
            details={
                "attempt_id": stored.id,
                "ticket_id": stored.ticket_id,
                "lifecycle_intent_id": first.id,
                "transition": first.transition,
            },
        )
    return stored


def require_claim_holder(
    claim: Optional[FileClaim],
    *,
    attempt: WorkAttempt,
    workspace_id: str,
    ticket_id: Optional[str] = None,
    path: Optional[str] = None,
) -> FileClaim:
    """Raise unless `claim` is active and held by this attempt/ticket/workspace.

    This is the rule that stops one attempt writing under another's token, and it
    is checked inside the operation lock -- a claim read before the lock proves
    nothing."""
    if claim is None:
        raise ClaimConflict(
            f"no active claim for path {path!r} (claim the path before mutating it)",
            details={"workspace_id": workspace_id, "path": path},
        )
    ticket_id = ticket_id if ticket_id is not None else attempt.ticket_id
    if not claim.is_active:
        raise ClaimConflict(
            f"claim {claim.id} for path {claim.path!r} is {claim.state}; "
            "reacquire the path to get a new claim",
            details={"claim_id": claim.id, "path": claim.path, "state": claim.state},
        )
    if (
        claim.attempt_id != attempt.id
        or claim.ticket_id != ticket_id
        or claim.workspace_id != workspace_id
    ):
        raise ClaimConflict(
            f"path {claim.path!r} is held by attempt {claim.attempt_id} "
            f"(ticket {claim.ticket_id}), not attempt {attempt.id}",
            details={
                "path": claim.path,
                "holder_attempt": claim.attempt_id,
                "holder_ticket": claim.ticket_id,
                "caller_attempt": attempt.id,
                "caller_ticket": ticket_id,
            },
        )
    return claim


def require_current_generation(expected: Optional[int], actual: int, *, what: str = "claim") -> int:
    """Raise `StaleRead` unless the caller's expected generation is current.

    Generations are how a token from before a release/reclaim is rejected: the
    numbers differ and nothing is written."""
    if expected is None or int(expected) != int(actual):
        raise StaleRead(
            f"{what} generation is {actual}, but the request expected {expected!r}; "
            "re-read and retry",
            details={"what": what, "expected": expected, "current": actual},
        )
    return int(actual)


def require_expected_digest(observed: str, expected: Optional[str], *, path: str) -> str:
    """Raise `StaleRead` unless the observed digest matches the expectation.

    `ABSENT` is a legitimate expectation (creating a file that must not already
    exist), so absence and mismatch are distinguished. Nothing is written on a
    mismatch -- `StaleRead` means bytes are unchanged."""
    if expected is None:
        raise StaleRead(
            f"no expected digest was supplied for {path!r}; a mutation must state "
            "the version it read",
            details={"path": path, "observed": observed},
        )
    if observed != expected:
        raise StaleRead(
            f"{path!r} is {observed!r}, but the request expected {expected!r}; "
            "re-read the file and retry",
            details={"path": path, "observed": observed, "expected": expected},
        )
    return observed


def require_write_authorization(
    observation: Optional[ReadObservation],
    *,
    claim: FileClaim,
    attempt: WorkAttempt,
    expected_digest: Optional[str] = None,
) -> ReadObservation:
    """Raise unless `observation` authorizes a write under `claim`.

    The plan's rule, enforced here: a writer must claim first and read again
    after claim acquisition, so a pre-claim observation (or one served to a
    different attempt, or against an older claim generation) never authorizes a
    mutation. On success the observation is marked `write_authorizing`, which is
    the explicit, durable record that arbite served those bytes to this attempt
    under this claim.
    """
    if observation is None:
        raise StaleRead(
            f"no read observation for {claim.path!r}; read the file through arbite "
            "after claiming it",
            details={"path": claim.path},
        )
    if observation.path != claim.path:
        raise StaleRead(
            f"observation is for {observation.path!r}, not the claimed {claim.path!r}",
            details={"observed_path": observation.path, "claimed_path": claim.path},
        )
    if observation.attempt_id != attempt.id:
        raise StaleRead(
            f"observation for {claim.path!r} was taken by attempt "
            f"{observation.attempt_id!r}, not {attempt.id}",
            details={
                "path": claim.path,
                "observation_attempt": observation.attempt_id,
                "attempt_id": attempt.id,
            },
        )
    if observation.claim_generation != claim.generation:
        raise StaleRead(
            f"observation for {claim.path!r} was taken under claim generation "
            f"{observation.claim_generation!r}, not the current {claim.generation}",
            details={
                "path": claim.path,
                "observation_generation": observation.claim_generation,
                "claim_generation": claim.generation,
            },
        )
    if observation.observed_at < claim.acquired:
        raise StaleRead(
            f"observation for {claim.path!r} predates the claim ({claim.acquired}); "
            "read again after acquiring the claim",
            details={"path": claim.path, "observed_at": observation.observed_at, "acquired": claim.acquired},
        )
    wanted = expected_digest if expected_digest is not None else claim.observed_version
    if observation.digest != wanted:
        raise StaleRead(
            f"observation for {claim.path!r} holds digest {observation.digest!r}, "
            f"but {wanted!r} is current; re-read the file",
            details={"path": claim.path, "observed": observation.digest, "expected": wanted},
        )
    # Single use, independent of content: every applied mutation advances the
    # claim's `mutation_seq`, so a token read before it is consumed even when the
    # bytes are identical or have returned to the version the token observed.
    # (An observation recorded before sequences existed counts as 0.)
    observed_seq = observation.claim_mutation_seq or 0
    current_seq = claim.mutation_seq or 0
    if observed_seq != current_seq:
        raise StaleRead(
            f"read token {observation.id} for {claim.path!r} was already consumed: "
            f"{current_seq - observed_seq} mutation(s) have been applied under this "
            "claim since it was read; re-read the file",
            details={
                "path": claim.path,
                "read_token": observation.id,
                "observation_mutation_seq": observed_seq,
                "claim_mutation_seq": current_seq,
                "reason": "token_consumed",
            },
        )
    observation.authorize()
    return observation


# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------


class GuardedOperation:
    """One guarded multi-record operation, open inside a store transaction.

    Created by `CoordinationService.guarded()`. The body does its record writes
    through `transaction` and records what it touched through `track_path` /
    `add_artifact`; on a clean exit this class writes the `OperationReceipt` and
    its `operation_recorded` event and commits, all together.

    On a failure the body's changes are rolled back and, for a typed
    `CoordinationError`, an *error* receipt plus its event are written in a
    separate transaction: evidence of a refused operation is preserved, but a
    half-applied one is not. The original exception always propagates.
    """

    def __init__(
        self,
        service: "CoordinationService",
        transaction,
        *,
        kind: str,
        attempt: WorkAttempt,
        actor: Actor,
        ticket_id: Optional[str] = None,
        paths: Optional[List[str]] = None,
        claim_generation: Optional[int] = None,
        operation_id: Optional[str] = None,
    ):
        if kind not in coordination.OPERATION_KINDS:
            raise InvalidRecord(
                f"unknown operation kind {kind!r} "
                f"(valid: {', '.join(coordination.OPERATION_KINDS)})"
            )
        canonical_paths = [canonical_relative_path(p) for p in (paths or [])]
        self.service = service
        self.transaction = transaction
        self.attempt = attempt
        self.actor = actor
        self.receipt = OperationReceipt(
            id=operation_id or service.new_operation_id(),
            attempt_id=attempt.id,
            ticket_id=ticket_id or attempt.ticket_id,
            actor=actor.id,
            kind_=kind,
            timestamp=service.now(),
            paths=canonical_paths,
            claim_generation=claim_generation,
        )

    @property
    def operation_id(self) -> str:
        return self.receipt.id

    @property
    def kind(self) -> str:
        return self.receipt.operation_kind

    def track_path(self, path: str, before: str, after: str) -> None:
        """Record one path's before/after version for the receipt. Versions are
        digests or `ABSENT`; anything else is a programming error, not data."""
        canonical = canonical_relative_path(path)
        for label, version in (("before", before), ("after", after)):
            if not is_digest_or_absent(version):
                raise InvalidRecord(
                    f"{label} version for {canonical!r} must be a digest or {ABSENT!r}, "
                    f"got {version!r}"
                )
        if canonical not in self.receipt.paths:
            self.receipt.paths.append(canonical)
        self.receipt.before[canonical] = before
        self.receipt.after[canonical] = after

    def add_artifact(self, artifact) -> None:
        """Store an artifact and reference it from this operation's receipt."""
        self.transaction.put(artifact)
        if artifact.id not in self.receipt.artifact_refs:
            self.receipt.artifact_refs.append(artifact.id)

    def _event(self, receipt: OperationReceipt) -> Event:
        return Event(
            id=self.service.new_record_id("event"),
            cursor=None,  # the store assigns the per-store cursor on append
            kind_="operation_recorded",
            category="operation",
            timestamp=receipt.timestamp,
            subject_ids=[self.attempt.id, receipt.ticket_id],
            operation_id=receipt.id,
            payload={
                "operation_kind": receipt.operation_kind,
                "result": receipt.result,
                "paths": list(receipt.paths),
            },
            payload_version=EVENT_PAYLOAD_VERSION,
        )

    def __enter__(self) -> "GuardedOperation":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.receipt.timestamp = self.service.now()
        if exc_type is None:
            self.receipt.result = "ok"
            # Deduplicated by operation id: `receipt.id` IS the operation id, so a
            # retry of `service.guarded(..., operation_id=X)` finds the original
            # receipt and event and writes neither a second receipt nor a second
            # event. The dedup happens inside this transaction, so it is the same
            # atomic unit as the receipt it protects.
            self.transaction.put_if_absent(self.receipt)
            self.transaction.append_event_once(self._event(self.receipt))
            self.transaction.commit()
            return False

        # A failure: undo whatever the body had staged, then preserve the
        # refusal as evidence in its own transaction. The receipt is written
        # even when the store cannot, in which case the original error wins.
        self.transaction.rollback()
        if isinstance(exc, CoordinationError):
            self.receipt.result = "error"
            self.receipt.error = {
                "code": exc.error_code,
                "message": str(exc),
                "details": dict(exc.details),
                "retryable": exc.retryable,
                "bytes_may_have_changed": exc.bytes_may_have_changed,
            }
            try:
                with self.service.store.transaction() as audit:
                    audit.put_if_absent(self.receipt)
                    audit.append_event_once(self._event(self.receipt))
            except Exception:  # pragma: no cover - storage already failing
                pass
        return False


class CoordinationService:
    """Guarded, multi-record operations over a workspace's authoritative store.

    Construct once per command with the workspace and its coordination backend,
    then call its operations. Each call is one-shot: it returns promptly, never
    sleeps and never schedules anything. Contention is reported as a typed
    `CoordinationError`, so the caller decides what to do next.
    """

    def __init__(
        self,
        workspace: Workspace,
        store,
        *,
        actor: Optional[Actor] = None,
        clock=None,
        id_factory=None,
    ):
        if workspace.store_binding is None:
            raise InvalidRecord(
                f"workspace {workspace.id} has no authoritative store binding; "
                "bind it before coordinating"
            )
        self.workspace = workspace
        self.store = store
        self.actor = actor or Actor("unknown")
        self._clock = clock or utc_now
        self._new_id = id_factory or new_record_id

    # -- plumbing ---------------------------------------------------------

    def now(self) -> str:
        return self._clock()

    def new_record_id(self, kind: str) -> str:
        return self._new_id(kind)

    def new_operation_id(self) -> str:
        return self._new_id("operation_receipt")

    def read_record(self, kind: str, record_id: str):
        """Read one record in a short read-only transaction."""
        with self.store.transaction(write=False) as tx:
            return tx.get(kind, record_id)

    # -- workspace/store binding -----------------------------------------

    def binding(self, sink_kind: str, location: str) -> StoreBinding:
        """Build and record this workspace's authoritative binding.

        Idempotent for the same sink/location; the store raises
        `StoreBindingConflict` for a different one. Building the record here (not
        in a command) keeps the "one authoritative store per workspace" rule in
        the application layer, where every caller is held to it."""
        binding = StoreBinding(
            id=self.new_record_id("store_binding"),
            workspace_id=self.workspace.id,
            sink_kind=sink_kind,
            location=location,
            bound_at=self.now(),
        )
        self.workspace.bind(binding, timestamp=self.now())
        stored = self.store.bind_store(self.workspace.store_binding)
        return stored if stored is not None else self.workspace.store_binding

    # -- observations -----------------------------------------------------

    def record_read(
        self,
        attempt: WorkAttempt,
        path: str,
        content: bytes,
        *,
        claim: Optional[FileClaim] = None,
        line_range=None,
        actor: Optional[Actor] = None,
        authorize: bool = True,
        observed_claim_generation: Optional[int] = None,
        version_only: bool = False,
    ) -> ReadObservation:
        """Record that bytes were served to `attempt`.

        The digest always covers the whole `content`, even when only
        `line_range` was returned. `write_authorizing` is True only when the
        caller passes the claim this attempt already holds *and* `authorize` is
        left True: a plain read is evidence, not permission.

        `authorize=False` with a claim still records the observed claim
        `generation` but leaves the observation non-writable. C06's read path uses
        it when the bytes it just served no longer match the claim's recorded
        `observed_version` (external drift): the read happened under this claim,
        but it must not be presented as permission to write, because the writer's
        own version check will refuse it.

        `observed_claim_generation` records the generation of a claim that is
        *not* the caller's (a foreign/busy holder read): the read observed that
        generation, so C06 records it even though passing `claim` itself would --
        correctly -- be refused by `require_claim_holder`.

        The claim's `mutation_seq` is recorded with the observation (from the
        claim object the caller loaded *before* reading the bytes), which is what
        lets the next mutation under the claim consume this token.
        `version_only` records a read that observed the whole-file version but
        served no content."""
        canonical = canonical_relative_path(path)
        actor = actor or self.actor
        authorizing = False
        generation = None
        mutation_seq = None
        if claim is not None:
            require_active_attempt(attempt)
            require_claim_holder(
                claim, attempt=attempt, workspace_id=self.workspace.id, path=canonical
            )
            authorizing = bool(authorize)
            generation = claim.generation
            mutation_seq = claim.mutation_seq or 0
        else:
            generation = observed_claim_generation
        observation = ReadObservation(
            id=self.new_record_id("read_observation"),
            operation_id=self.new_operation_id(),
            path=canonical,
            digest=digest_of_bytes(content),
            observed_at=self.now(),
            attempt_id=attempt.id,
            actor=actor.id,
            claim_generation=generation,
            line_range=tuple(line_range) if line_range is not None else None,
            write_authorizing=authorizing,
            claim_mutation_seq=mutation_seq,
            version_only=bool(version_only),
        )
        problems = observation.validate()
        if problems:
            raise InvalidRecord(
                f"invalid read observation: {'; '.join(problems)}",
                details={"problems": problems},
            )
        with self.store.transaction() as tx:
            tx.put(observation)
            tx.append_event(
                Event(
                    id=self.new_record_id("event"),
                    cursor=None,
                    kind_="read_observed",
                    category="read",
                    timestamp=observation.observed_at,
                    subject_ids=[attempt.id],
                    operation_id=observation.operation_id,
                    payload={
                        "path": canonical,
                        "attempt_id": attempt.id,
                        "digest": observation.digest,
                        "claim_generation": generation,
                        "line_range": list(observation.line_range) if observation.line_range else None,
                        "write_authorizing": authorizing,
                        "version_only": bool(version_only),
                    },
                    payload_version=EVENT_PAYLOAD_VERSION,
                )
            )
        return observation

    # -- guarded operations ----------------------------------------------

    def guarded(
        self,
        kind: str,
        *,
        attempt: WorkAttempt,
        ticket_id: Optional[str] = None,
        paths=None,
        claim_generation: Optional[int] = None,
        operation_id: Optional[str] = None,
    ) -> GuardedOperation:
        """Open a guarded operation. Requires an active attempt: a terminal one
        is refused before the transaction is even opened."""
        require_active_attempt(attempt)
        transaction = self.store.transaction()
        return GuardedOperation(
            self,
            transaction,
            kind=kind,
            attempt=attempt,
            actor=self.actor,
            ticket_id=ticket_id,
            paths=list(paths or []),
            claim_generation=claim_generation,
            operation_id=operation_id,
        )

    def apply_guards(
        self,
        *,
        claim: Optional[FileClaim],
        attempt: WorkAttempt,
        expected_generation: Optional[int] = None,
        expected_digest: Optional[str] = None,
        observed: Optional[str] = None,
        path: Optional[str] = None,
    ) -> FileClaim:
        """Run the claim/generation/digest guards in one place.

        A convenience so every mutation path checks the same things in the same
        order (active attempt, claim held, generation current, digest current)
        inside its transaction, rather than each caller assembling a different
        subset -- the likely source of a bypass."""
        require_active_attempt(attempt)
        held = require_claim_holder(
            claim, attempt=attempt, workspace_id=self.workspace.id, path=path
        )
        if expected_generation is not None:
            require_current_generation(expected_generation, held.generation)
        if observed is not None:
            require_expected_digest(observed, expected_digest, path=held.path)
        return held

    # -- recovery ---------------------------------------------------------

    def pending_recovery(self) -> List[RecoveryReport]:
        """Incomplete operations a later caller (a `doctor` command) must inspect.

        Inspection only: nothing here repairs bytes, and nothing runs on a
        timer. An empty list means "nothing pending", not "everything is safe"."""
        return list(self.store.recover_pending(self.workspace.id))

    def require_no_pending(self, operation_id: str) -> None:
        """Raise `RecoveryRequired` when `operation_id` has an unresolved report.

        A retry must inspect/repair first, because the interrupted operation may
        already have changed bytes."""
        for report in self.pending_recovery():
            if report.operation_id == operation_id:
                raise RecoveryRequired(
                    f"operation {operation_id} is {report.state}; inspect it before "
                    "retrying",
                    details={
                        "operation_id": operation_id,
                        "state": report.state,
                        "paths": list(report.paths),
                        "detail": report.detail,
                    },
                    bytes_may_have_changed=report.bytes_may_have_changed,
                )


# ---------------------------------------------------------------------------
# CLI/service construction (explicit workspace binding)
# ---------------------------------------------------------------------------

#: Re-exported from `arbite.workspace` for callers that imported it from here
#: while C03's derived-id stopgap lived in this module.
stable_workspace_id = workspace_module.stable_workspace_id


def _arbite_dir_for(project_root: str) -> Path:
    """The `.arbite` directory that anchors this workspace's stores.

    Both shipped sinks live under it, so it is where the workspace binding marker
    is written and read. `project_root` is the project root for the CLI; a caller
    that already passes the `.arbite` directory is handled too.
    """
    path = Path(project_root)
    if path.name == ".arbite":
        return path
    return path / ".arbite"


def coordination_service_for(
    sink,
    *,
    root: str,
    actor: Optional[Actor] = None,
    clock=None,
    rebind: bool = False,
) -> CoordinationService:
    """A `CoordinationService` for `sink`'s workspace rooted at `root`.

    This is C04's explicit binding, replacing C03's derived-id stopgap. On first
    use the workspace is bound to `sink`, and the binding is recorded both in the
    store (`StoreBinding`) and in `.arbite/workspace-binding.json` -- the marker a
    *different* sink selection discovers, which is what stops one workspace from
    quietly coordinating against two independently selected stores. A matching
    binding is idempotent; a different sink kind or location raises
    `StoreBindingConflict` unless `rebind=True` is passed explicitly and the
    previously bound store is quiescent (`arbite.workspace.ensure_binding`).

    `sink.coordination()` must exist: a sink that does not implement the
    shared-directory contract raises `UnsupportedCoordination` here rather than
    silently doing nothing. Both shipped sinks implement it.

    Nothing here sleeps, retries or scans: it is one binding write, one
    workspace-record write and (on first bind/rebind) one event, all one-shot.
    """
    store = sink.coordination()
    if store is None:
        raise UnsupportedCoordination(
            f"the {getattr(sink, 'kind', '?')} sink does not implement the "
            "shared-directory coordination contract, so ticket acquisition cannot "
            "be recorded; use a sink that does (file or sqlite)"
        )

    project_root = os.path.realpath(root)
    resolution = workspace_module.ensure_binding(
        _arbite_dir_for(project_root),
        root=project_root,
        sink_kind=sink.kind,
        location=str(sink.root),
        store=store,
        clock=clock,
        rebind=rebind,
    )
    moment = (clock or utc_now)()
    workspace = Workspace(
        id=resolution.workspace_id,
        root=project_root,
        created=resolution.binding.bound_at,
        updated=moment,
        store_binding=resolution.binding,
    )
    service = CoordinationService(workspace, store, actor=actor, clock=clock)
    # The authoritative per-store binding: idempotent for a matching binder, and a
    # conflict when this store was already bound to a different location.
    service.binding(sink.kind, str(sink.root))
    try:
        with store.transaction() as tx:
            tx.put(workspace)
    except CoordinationError:
        # Best-effort only: the binding is already authoritative and a duplicate
        # workspace record across racing processes is harmless (last write wins on
        # identical content).
        pass
    return service
