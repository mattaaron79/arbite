"""The recoverable filesystem/sink mutation engine (planning key C05).

This module is the durability bridge the planning documents call for: the
workspace and the sink are separate durability domains, and no SQL transaction can
atomically commit a filesystem change *and* its receipt. The engine makes that gap
recoverable instead of pretending it is not there.

The protocol, in order, for every mutation
------------------------------------------

1. **Guards.** An active attempt, a held claim, the claim generation and the
   expected whole-file digest are all verified *inside* the operation lock,
   against the attempt and claim *as stored*: the objects a caller passes in were
   loaded before the lock and may already be dead (a close, release or takeover
   ended them), so they are handles and expectations, never proof. A mismatch
   changes nothing (`stale_read`, `attempt_inactive`, `claim_conflict`).
2. **Capacity.** The before- and after-bytes are checked against the explicit
   artifact size limit. Over the limit is `artifact_capacity` *before* any
   filesystem change.
3. **Artifacts.** The exact before- and after-bytes are stored content-addressed
   (once per digest) in the sink's coordination store.
4. **Intent.** An `OperationIntent` record (with the artifact references and the
   before/after versions) is committed durably. This is the last store write that
   is guaranteed to happen before the filesystem changes.
5. **Stage + apply.** The new bytes are staged beside the target and applied with
   a single atomic `os.replace` (or an `unlink` for remove, or a staged
   destination plus source removal for rename).
6. **Finalize.** The `OperationReceipt` and its `operation_recorded` event are
   written together, and the intent is marked `finalized`. The live claim's
   `observed_version` and `mutation_seq` are advanced, which consumes every read
   token taken before the change -- including the one that authorized it, even
   when the new bytes are identical to the old.

If the process dies (or the sink fails) between 5 and 6, the intent survives as
`pending` and the **next relevant operation reconciles it**: it observes the
filesystem and compares against the recorded before/after versions -- `applied`,
`reverted` or `drifted`. Nothing runs on a timer; there is no daemon. `drifted`
(observed bytes match neither version) is preserved and reported for an explicit
resolution: the engine never guesses, never overwrites and never releases
ownership as if the operation completed.

Operation ids are the retry key
-------------------------------

The caller supplies (or the service mints) one operation id per logical
operation. A retry with the same id finds the original intent/receipt and does
*not* apply the change a second time, so an idempotent replay cannot double-apply
an edit.

Fault injection
---------------

Every boundary above calls an optional `fault_injector(phase)` before/after it, so
a test can produce the exact interrupted states -- sink failure after
replacement, rename interrupted between its two paths, a crash after the intent
but before the apply -- on either sink. Outside tests nothing is injected.
"""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from . import application, coordination
from .application import (
    require_active_attempt,
    require_claim_holder,
    require_current_attempt,
    require_expected_digest,
)
from .artifacts import DEFAULT_MAX_ARTIFACT_BYTES, DEFAULT_MEDIA_TYPE, check_capacity
from .coordination import (
    ABSENT,
    EVENT_PAYLOAD_VERSION,
    Artifact,
    Event,
    OperationIntent,
    OperationReceipt,
    digest_of_bytes,
    is_digest_or_absent,
    utc_now,
)
from .errors import (
    ArtifactCapacityError,
    CoordinationError,
    CoordinationNotFound,
    DriftDetected,
    InvalidRecord,
    RecoveryRequired,
    SinkError,
    StaleRead,
    UnsupportedCoordination,
)
from .paths import MODE_CLAIM, resolve_target

# ---------------------------------------------------------------------------
# Fault-injection boundaries
# ---------------------------------------------------------------------------

#: Committed the intent (and artifacts) but not yet touched the filesystem.
FAULT_AFTER_INTENT = "after_intent"
#: Wrote the staged bytes, before any applied change.
FAULT_AFTER_STAGE = "after_stage"
#: About to perform the filesystem operation.
FAULT_BEFORE_APPLY = "before_apply"
#: For rename: the destination is committed but the source has not been removed --
#: the "interrupted between paths" state.
FAULT_AFTER_DEST_COMMITTED = "after_dest_committed"
#: The filesystem operation is done; the receipt is not yet written.
FAULT_AFTER_APPLY = "after_apply"
#: About to write the receipt/event.
FAULT_BEFORE_FINALIZE = "before_finalize"
#: The receipt and event are durable.
FAULT_AFTER_FINALIZE = "after_finalize"

FAULT_PHASES = (
    FAULT_AFTER_INTENT,
    FAULT_AFTER_STAGE,
    FAULT_BEFORE_APPLY,
    FAULT_AFTER_DEST_COMMITTED,
    FAULT_AFTER_APPLY,
    FAULT_BEFORE_FINALIZE,
    FAULT_AFTER_FINALIZE,
)

#: Marker for "the path could not be observed at all" (distinct from `ABSENT`).
UNKNOWN = "<unknown>"


class FaultInjected(Exception):
    """Raised by a test's fault injector to simulate a crash at one boundary.

    Deliberately *not* a `CoordinationError`: an injected crash is not a refusal
    by the engine, and it must leave the intent `pending` exactly as a real crash
    would, so the recovery path is exercised for real.
    """

    def __init__(self, phase: str):
        super().__init__(f"fault injected at {phase}")
        self.phase = phase


@dataclass(frozen=True)
class MutationResult:
    """The outcome of one mutation (or of a deduplicated replay of one)."""

    operation_id: str
    kind: str
    receipt: OperationReceipt
    paths: List[str] = field(default_factory=list)
    before: Dict[str, str] = field(default_factory=dict)
    after: Dict[str, str] = field(default_factory=dict)
    artifact_refs: List[str] = field(default_factory=list)
    #: True when *this* call changed bytes; False for a replay or a refusal.
    applied: bool = False
    #: True when an existing intent/receipt for this operation id was reused.
    deduplicated: bool = False
    #: True when reconciliation completed an interrupted operation.
    recovered: bool = False
    #: In-root directories a C08 caller created so this destination could exist.
    #: Empty for the engine's own calls; the mutation service fills it in.
    created_parents: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.receipt.result == "ok"


class MutationEngine:
    """Execute recoverable write/edit/remove/rename operations over the journal.

    Construct with the workspace's `CoordinationService` and `FileClaimService`
    (the caller has already claimed the paths). C07/C08 call `write`/`edit`/
    `remove`/`rename`; nothing here exposes a CLI command of its own.
    """

    def __init__(
        self,
        service,
        claims,
        *,
        root: Optional[str] = None,
        case_insensitive: Optional[bool] = None,
        max_artifact_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES,
        fault_injector=None,
        clock=None,
    ) -> None:
        self.service = service
        self.claims = claims
        self.workspace = service.workspace
        self.store = service.store
        self.root = str(root) if root is not None else claims.root
        self._case = case_insensitive if case_insensitive is not None else claims._case
        if not isinstance(max_artifact_bytes, int) or isinstance(max_artifact_bytes, bool) or max_artifact_bytes < 0:
            raise InvalidRecord(
                f"max_artifact_bytes must be a non-negative integer, got {max_artifact_bytes!r}"
            )
        self.max_artifact_bytes = max_artifact_bytes
        self._fault_injector = fault_injector
        self._clock = clock or utc_now

    # ------------------------------------------------------------------
    # time / ids / observation
    # ------------------------------------------------------------------

    def now(self) -> str:
        return self._clock()

    def _fault(self, phase: str) -> None:
        if phase not in FAULT_PHASES:  # pragma: no cover - internal misuse
            raise InvalidRecord(f"unknown fault phase {phase!r}")
        if self._fault_injector is not None:
            self._fault_injector(phase)

    def _resolve(self, path: str, *, mode: str = MODE_CLAIM):
        return resolve_target(self.root, path, mode=mode, case_insensitive=self._case)

    def _observe(self, path: str) -> str:
        """The current whole-file version of `path`: a digest, `ABSENT` or `UNKNOWN`."""
        try:
            target = self._resolve(path, mode=MODE_CLAIM)
        except CoordinationNotFound:
            return ABSENT
        except (UnsupportedCoordination, OSError):
            return UNKNOWN
        return target.digest

    @staticmethod
    def _read_bytes(absolute: str) -> bytes:
        with open(absolute, "rb") as handle:
            return handle.read()

    # ------------------------------------------------------------------
    # intents and receipts
    # ------------------------------------------------------------------

    def pending_intents(self) -> List[OperationIntent]:
        """Intents for this workspace that still need reconciling, oldest first."""
        with self.store.transaction(write=False) as tx:
            found = list(tx.find("operation_intent", workspace_id=self.workspace.id))
        active = [i for i in found if i.state in ("pending", "applied", "drifted")]
        return sorted(active, key=lambda i: (i.created, i.id))

    def _receipt(self, operation_id: str) -> Optional[OperationReceipt]:
        with self.store.transaction(write=False) as tx:
            return tx.get("operation_receipt", operation_id)

    def _new_intent(
        self,
        kind: str,
        attempt,
        operation_id: str,
        *,
        paths: List[str],
        before: Dict[str, str],
        after: Dict[str, str],
        artifacts: List[Artifact],
        claim_generation: Optional[int],
    ) -> OperationIntent:
        moment = self.now()
        intent = OperationIntent(
            id=self.service.new_record_id("operation_intent"),
            operation_id=operation_id,
            workspace_id=self.workspace.id,
            attempt_id=attempt.id,
            ticket_id=attempt.ticket_id,
            actor=self.service.actor.id,
            kind_=kind,
            created=moment,
            updated=moment,
            before=dict(before),
            after=dict(after),
            paths=list(paths),
            artifact_refs=[artifact.id for artifact in artifacts],
            claim_generation=claim_generation,
            state="pending",
        )
        problems = intent.validate()
        if problems:
            raise InvalidRecord(
                f"invalid operation intent: {'; '.join(problems)}",
                details={"problems": list(problems)},
            )
        return intent

    def _persist_intent(self, intent: OperationIntent, artifacts: List[Artifact]) -> None:
        with self.store.transaction() as tx:
            for artifact in artifacts:
                tx.put_if_absent(artifact)
            tx.put(intent)

    def _operation_event(self, receipt: OperationReceipt) -> Event:
        return Event(
            id=self.service.new_record_id("event"),
            cursor=None,
            kind_="operation_recorded",
            category="operation",
            timestamp=receipt.timestamp,
            subject_ids=[receipt.attempt_id, receipt.ticket_id],
            operation_id=receipt.id,
            payload={
                "operation_kind": receipt.operation_kind,
                "result": receipt.result,
                "paths": list(receipt.paths),
            },
            payload_version=EVENT_PAYLOAD_VERSION,
        )

    def _finalize(
        self,
        intent: OperationIntent,
        *,
        result: str = "ok",
        error: Optional[dict] = None,
        recovered: bool = False,
    ) -> OperationReceipt:
        """Write the receipt+event, advance the claim version, close the intent."""
        moment = self.now()
        receipt = OperationReceipt(
            id=intent.operation_id,
            attempt_id=intent.attempt_id,
            ticket_id=intent.ticket_id,
            actor=intent.actor,
            kind_=intent.kind_,
            timestamp=moment,
            before=dict(intent.before),
            after=dict(intent.after),
            paths=list(intent.paths),
            artifact_refs=list(intent.artifact_refs),
            claim_generation=intent.claim_generation,
            result=result,
            error=error,
        )
        final_state = "finalized" if result == "ok" else "reverted"
        with self.store.transaction() as tx:
            tx.put_if_absent(receipt)
            tx.append_event_once(self._operation_event(receipt))
            intent.state = final_state
            intent.updated = moment
            tx.put(intent)
            if result == "ok":
                self._advance_claims(tx, intent)
        return receipt

    def _advance_claims(self, tx, intent: OperationIntent) -> None:
        """Move each live claim to the recorded after-version and consume tokens.

        `observed_version` follows the bytes, and `mutation_seq` counts the
        mutation. The sequence is what makes a read token single-use: a later
        `require_write_authorization` refuses any observation recorded at an
        earlier sequence, so neither an identical-bytes write nor content that
        returns to an earlier version can revive a token that was already used."""
        for path, after in intent.after.items():
            history = tx.find("file_claim", workspace_id=self.workspace.id, path=path)
            mine = [
                claim
                for claim in history
                if claim.is_active and claim.attempt_id == intent.attempt_id
            ]
            if not mine:
                continue
            claim = max(mine, key=lambda c: c.generation)
            claim.observed_version = after
            claim.mutation_seq = (claim.mutation_seq or 0) + 1
            tx.put(claim)

    def _close_intent(self, intent: OperationIntent, state: str) -> None:
        with self.store.transaction() as tx:
            intent.state = state
            intent.updated = self.now()
            tx.put(intent)

    # ------------------------------------------------------------------
    # reconciliation (invoked by the next relevant operation -- never a timer)
    # ------------------------------------------------------------------

    def reconcile(self) -> List[coordination.RecoveryReport]:
        """Reconcile every incomplete intent for this workspace, then return.

        Runs at the start of every mutation, while holding the operation lock, so
        a later operation can never proceed past an unresolved drift. Raises
        `DriftDetected` when any intent's observed bytes match neither the before
        nor the after version; nothing is overwritten or released.
        """
        with self.store.operation_lock():
            return self._reconcile_locked()

    def _reconcile_locked(self) -> List[coordination.RecoveryReport]:
        reports: List[coordination.RecoveryReport] = []
        for intent in self.pending_intents():
            existing = self._receipt(intent.operation_id)
            if existing is not None:
                # The receipt is the authoritative finalize record; do not
                # re-litigate a completed or recorded-as-reverted operation.
                self._close_intent(intent, "finalized" if existing.result == "ok" else "reverted")
                continue
            self._cleanup_stage_files(intent)
            state, detail = self._classify(intent)
            if state == "applied":
                self._complete_applied(intent)
                reports.append(self._report(intent, "applied", detail, changed=True))
            elif state == "reverted":
                error = {
                    "code": "operation_reverted",
                    "message": detail,
                    "details": {"operation_id": intent.operation_id},
                    "retryable": True,
                    "bytes_may_have_changed": False,
                }
                self._finalize(intent, result="error", error=error)
                reports.append(self._report(intent, "reverted", detail, changed=False))
            else:
                self._mark_drifted(intent, detail)
                reports.append(self._report(intent, "drifted", detail, changed=True))
        drifted = [r for r in reports if r.state in ("drifted", "unknown")]
        if drifted:
            first = drifted[0]
            raise DriftDetected(
                f"operation {first.operation_id} left bytes that match neither its "
                f"recorded before nor after version ({first.detail}); evidence is "
                "preserved and an explicit resolution is required -- arbite will not "
                "guess, overwrite, or release ownership",
                details={
                    "operation_id": first.operation_id,
                    "paths": list(first.paths),
                    "state": first.state,
                    "detail": first.detail,
                    "intent_kind": first.operation_kind,
                },
                bytes_may_have_changed=True,
            )
        return reports

    def _classify(self, intent: OperationIntent) -> tuple:
        """The recovery state machine for one intent, from observed filesystem bytes."""
        observed = {path: self._observe(path) for path in intent.paths}
        kind = intent.kind_
        if kind in ("write", "edit"):
            path = intent.paths[0]
            before, after = intent.before[path], intent.after[path]
            seen = observed[path]
            if seen == after:
                return "applied", f"{path!r} matches the recorded after version"
            if seen == before:
                return "reverted", f"{path!r} still matches the recorded before version"
            return "drifted", f"{path!r} is {seen}, neither before {before} nor after {after}"
        if kind == "remove":
            path = intent.paths[0]
            before = intent.before[path]
            seen = observed[path]
            if seen == ABSENT:
                return "applied", f"{path!r} is absent, matching the recorded after version"
            if seen == before:
                return "reverted", f"{path!r} still matches the recorded before version"
            return "drifted", f"{path!r} is {seen}, not absent and not the recorded before"
        if kind == "rename":
            source, dest = intent.paths[0], intent.paths[1]
            before_s = intent.before[source]
            # The destination's *recorded* before-version: ABSENT for a plain
            # rename, the replaced file's digest when renaming over one.
            before_d = intent.before.get(dest, ABSENT)
            after_d = intent.after[dest]
            seen_s, seen_d = observed[source], observed[dest]
            # Checked first: when the moved bytes equal the replaced destination's,
            # "nothing happened yet" and "destination committed" look identical,
            # and the reading that removes nothing is the one that cannot be wrong.
            if seen_s == before_s and seen_d == before_d:
                return "reverted", "source and destination still match their recorded before versions"
            if seen_s == ABSENT and seen_d == after_d:
                return "applied", "source removed and destination holds the recorded version"
            if seen_s == before_s and seen_d == after_d:
                return (
                    "applied",
                    "destination committed but the source was not yet removed "
                    "(completing the interrupted rename)",
                )
            return (
                "drifted",
                f"rename state is source={seen_s}, destination={seen_d}; neither the "
                f"before ({before_s}/{before_d}) nor the after ({ABSENT}/{after_d})",
            )
        return "unknown", f"no recovery state machine for operation kind {kind!r}"

    def _complete_applied(self, intent: OperationIntent) -> None:
        """Finalize an applied intent, completing an unambiguous partial rename.

        Only reachable when the destination holds exactly the recorded after-digest
        and the source holds exactly the recorded before-digest, so removing the
        source completes the recorded intent rather than guessing."""
        if intent.kind_ == "rename":
            source, dest = intent.paths[0], intent.paths[1]
            if self._observe(source) != ABSENT and self._observe(dest) == intent.after[dest]:
                target = self._resolve(source, mode=MODE_CLAIM)
                os.unlink(target.absolute)
                self._fsync_dir(os.path.dirname(target.absolute))
        self._finalize(intent, result="ok", recovered=True)

    def _mark_drifted(self, intent: OperationIntent, detail: str) -> None:
        drift_event = Event(
            id="evt-" + hashlib.sha256(("drift:" + intent.operation_id).encode()).hexdigest()[:16],
            cursor=None,
            kind_="drift_detected",
            category="operation",
            timestamp=self.now(),
            subject_ids=[intent.attempt_id, intent.ticket_id],
            operation_id=None,
            payload={
                "operation_id": intent.operation_id,
                "operation_kind": intent.kind_,
                "paths": list(intent.paths),
                "detail": detail,
            },
            payload_version=EVENT_PAYLOAD_VERSION,
        )
        with self.store.transaction() as tx:
            intent.state = "drifted"
            intent.detail = detail
            intent.updated = self.now()
            tx.put(intent)
            tx.append_event_once(drift_event)

    def _report(self, intent: OperationIntent, state: str, detail: str, *, changed: bool):
        return coordination.RecoveryReport(
            workspace_id=self.workspace.id,
            operation_id=intent.operation_id,
            state=state,
            observed_at=self.now(),
            paths=list(intent.paths),
            bytes_may_have_changed=changed,
            detail=detail,
            kind_=intent.kind_,
        )

    # ------------------------------------------------------------------
    # staging helpers
    # ------------------------------------------------------------------

    def _staging_path(self, absolute: str, intent: OperationIntent) -> str:
        tag = intent.operation_id.split("-", 1)[-1]
        name = os.path.basename(absolute)
        return os.path.join(os.path.dirname(absolute) or ".", f".{name}.arbite-stage-{tag}")

    def _stage(self, absolute: str, data: bytes, intent: OperationIntent) -> str:
        """Write `data` to a staged sibling of `absolute`, fsynced, not yet applied."""
        path = self._staging_path(absolute, intent)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            self._cleanup_staged(path)
            raise
        return path

    def _cleanup_staged(self, path: str) -> None:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass

    def _cleanup_stage_files(self, intent: OperationIntent) -> None:
        """Remove any staging file an interrupted attempt may have left behind.

        Only removes the deterministic staging sibling for this intent's own
        operation id, so no unrelated path is ever touched."""
        for path in intent.paths:
            try:
                absolute = self._resolve(path, mode=MODE_CLAIM).absolute
            except (CoordinationError, OSError):
                absolute = os.path.join(self.root, *path.split("/"))
            self._cleanup_staged(self._staging_path(absolute, intent))

    @staticmethod
    def _fsync_dir(directory: str) -> None:
        if not directory:
            return
        try:
            fd = os.open(directory, os.O_RDONLY)
        except OSError:  # pragma: no cover - platform without dir fsync
            return
        try:
            os.fsync(fd)
        except OSError:  # pragma: no cover
            pass
        finally:
            os.close(fd)

    # ------------------------------------------------------------------
    # guards / dedup
    # ------------------------------------------------------------------

    def _dedup(self, operation_id: str, kind: str) -> Optional[MutationResult]:
        existing = self._receipt(operation_id)
        if existing is None:
            return None
        return MutationResult(
            operation_id=operation_id,
            kind=existing.operation_kind,
            receipt=existing,
            paths=list(existing.paths),
            before=dict(existing.before),
            after=dict(existing.after),
            artifact_refs=list(existing.artifact_refs),
            applied=False,
            deduplicated=True,
            recovered=False,
        )

    def _current_attempt(self, attempt):
        """The stored, still-active attempt (see `application.require_current_attempt`).

        Called inside the operation lock, which every lifecycle transition also
        takes, so the answer cannot change before this operation finishes."""
        with self.store.transaction(write=False) as tx:
            return require_current_attempt(tx, attempt)

    def _require_claim(self, attempt, path: str, claim=None):
        """The live claim on `path`, which `attempt` must hold.

        Always read from the store inside the operation lock. A `claim` the caller
        passes is only an expectation: if it is no longer the live claim (released,
        or superseded by a newer generation), the operation is refused rather than
        authorized by a token the store has already revoked."""
        held = require_claim_holder(
            self.claims.claim_for(path),
            attempt=attempt,
            workspace_id=self.workspace.id,
            path=path,
        )
        if claim is not None and (claim.id != held.id or claim.generation != held.generation):
            raise StaleRead(
                f"the claim presented for {path!r} (generation {claim.generation}) is "
                f"not the live claim (generation {held.generation}); re-read the file "
                "under the current claim",
                details={
                    "path": path,
                    "presented_claim": claim.id,
                    "presented_generation": claim.generation,
                    "current_claim": held.id,
                    "current_generation": held.generation,
                },
            )
        return held

    def _store_evidence(self, pieces, *, media_type: str):
        """Store each ``(bytes, role)`` pair content-addressed; return descriptors."""
        descriptors = []
        for data, role in pieces:
            check_capacity(data, self.max_artifact_bytes, role=role)
            descriptors.append(
                self.store.store_artifact_bytes(data, media_type=media_type)
            )
        return descriptors

    def _apply_failure(self, intent: OperationIntent, applied: bool, error: Exception):
        if applied:
            return RecoveryRequired(
                f"operation {intent.operation_id} changed bytes on disk but its "
                f"receipt could not be finalized ({error}); the filesystem may "
                "already hold the change -- inspect and reconcile before retrying",
                details={
                    "operation_id": intent.operation_id,
                    "intent_id": intent.id,
                    "paths": list(intent.paths),
                },
                bytes_may_have_changed=True,
            )
        return error

    # ------------------------------------------------------------------
    # public mutations
    # ------------------------------------------------------------------

    def write(
        self,
        attempt,
        path: str,
        content: bytes,
        *,
        claim=None,
        operation_id: Optional[str] = None,
        expected_digest: Optional[str] = None,
        media_type: Optional[str] = None,
        authorize=None,
    ) -> MutationResult:
        """Create or replace `path` with the whole `content`.

        `authorize`, when given, is called with the held claim *inside* the
        operation lock, after the claim/holder guard and before any digest check
        or filesystem work. C07 passes a closure that runs
        `application.require_write_authorization` for the presented read token, so
        a pre-claim, foreign, old-generation or already-consumed observation is
        refused here with no bytes changed.
        """
        return self._replace(
            "write",
            attempt,
            path,
            content,
            claim=claim,
            operation_id=operation_id,
            expected_digest=expected_digest,
            media_type=media_type or DEFAULT_MEDIA_TYPE,
            require_existing=False,
            authorize=authorize,
        )

    def edit(
        self,
        attempt,
        path: str,
        new_content: bytes,
        *,
        claim=None,
        operation_id: Optional[str] = None,
        expected_digest: Optional[str] = None,
        media_type: Optional[str] = None,
        authorize=None,
    ) -> MutationResult:
        """Replace an *existing* `path` with `new_content` (C07 computes the bytes).

        `authorize` has the same inside-the-lock meaning as in `write`."""
        return self._replace(
            "edit",
            attempt,
            path,
            new_content,
            claim=claim,
            operation_id=operation_id,
            expected_digest=expected_digest,
            media_type=media_type or DEFAULT_MEDIA_TYPE,
            require_existing=True,
            authorize=authorize,
        )

    def _replace(
        self,
        kind: str,
        attempt,
        path: str,
        content: bytes,
        *,
        claim,
        operation_id: Optional[str],
        expected_digest: Optional[str],
        media_type: str,
        require_existing: bool,
        authorize=None,
    ) -> MutationResult:
        require_active_attempt(attempt)
        operation_id = operation_id or self.service.new_operation_id()
        with self.store.operation_lock():
            self._reconcile_locked()
            replay = self._dedup(operation_id, kind)
            if replay is not None:
                return replay

            attempt = self._current_attempt(attempt)
            target = self._resolve(path, mode=MODE_CLAIM)
            canonical = target.relative
            held = self._require_claim(attempt, canonical, claim)
            # C07's read-token check runs here: inside the operation lock, after
            # the holder guard, before any digest check or filesystem work. A
            # refusal is a `StaleRead` with no bytes changed.
            if authorize is not None:
                authorize(held)
            observed = target.digest
            require_expected_digest(observed, held.observed_version, path=canonical)
            if expected_digest is not None:
                require_expected_digest(observed, expected_digest, path=canonical)
            if require_existing and observed == ABSENT:
                raise CoordinationNotFound(
                    f"cannot {kind} {canonical!r}: the file does not exist; use a "
                    "whole-file write to create it",
                    details={"path": canonical},
                )

            after_digest = digest_of_bytes(content)
            pieces = []
            before_bytes = b""
            if observed != ABSENT:
                before_bytes = self._read_bytes(target.absolute)
                pieces.append((before_bytes, "before"))
            pieces.append((content, "after"))
            artifacts = self._store_evidence(pieces, media_type=media_type)

            intent = self._new_intent(
                kind,
                attempt,
                operation_id,
                paths=[canonical],
                before={canonical: observed},
                after={canonical: after_digest},
                artifacts=artifacts,
                claim_generation=held.generation,
            )
            self._persist_intent(intent, artifacts)
            self._fault(FAULT_AFTER_INTENT)

            staged = self._stage(target.absolute, content, intent)
            if observed != ABSENT:
                # Preserve the existing file's supported permissions.
                mode = stat.S_IMODE(os.stat(target.absolute).st_mode)
                os.chmod(staged, mode)
            self._fault(FAULT_AFTER_STAGE)
            self._fault(FAULT_BEFORE_APPLY)
            try:
                os.replace(staged, target.absolute)
            except OSError as error:
                self._cleanup_staged(staged)
                raise SinkError(
                    f"could not apply {kind} to {canonical!r}: {error}"
                ) from error
            self._fsync_dir(os.path.dirname(target.absolute))
            self._fault(FAULT_AFTER_APPLY)

            self._fault(FAULT_BEFORE_FINALIZE)
            try:
                receipt = self._finalize(intent, result="ok")
            except CoordinationError as error:
                raise self._apply_failure(intent, True, error) from error
            self._fault(FAULT_AFTER_FINALIZE)
            return MutationResult(
                operation_id=operation_id,
                kind=kind,
                receipt=receipt,
                paths=[canonical],
                before=dict(intent.before),
                after=dict(intent.after),
                artifact_refs=list(intent.artifact_refs),
                applied=True,
            )

    def remove(
        self,
        attempt,
        path: str,
        *,
        claim=None,
        operation_id: Optional[str] = None,
        expected_digest: Optional[str] = None,
        media_type: Optional[str] = None,
        authorize=None,
    ) -> MutationResult:
        """Delete `path`, preserving its bytes as evidence.

        `authorize`, when given, has the same inside-the-lock meaning as in
        `write`/`edit`: C08 passes a closure that runs
        `application.require_write_authorization` for the presented read token, so
        a removal needs a fresh post-claim read rather than merely the claim."""
        require_active_attempt(attempt)
        operation_id = operation_id or self.service.new_operation_id()
        with self.store.operation_lock():
            self._reconcile_locked()
            replay = self._dedup(operation_id, "remove")
            if replay is not None:
                return replay

            attempt = self._current_attempt(attempt)
            target = self._resolve(path, mode=MODE_CLAIM)
            canonical = target.relative
            held = self._require_claim(attempt, canonical, claim)
            if authorize is not None:
                authorize(held)
            observed = target.digest
            require_expected_digest(observed, held.observed_version, path=canonical)
            if expected_digest is not None:
                require_expected_digest(observed, expected_digest, path=canonical)
            if observed == ABSENT:
                raise CoordinationNotFound(
                    f"cannot remove {canonical!r}: the file does not exist",
                    details={"path": canonical},
                )

            removed_bytes = self._read_bytes(target.absolute)
            artifacts = self._store_evidence(
                [(removed_bytes, "before")], media_type=media_type or DEFAULT_MEDIA_TYPE
            )
            intent = self._new_intent(
                "remove",
                attempt,
                operation_id,
                paths=[canonical],
                before={canonical: observed},
                after={canonical: ABSENT},
                artifacts=artifacts,
                claim_generation=held.generation,
            )
            self._persist_intent(intent, artifacts)
            self._fault(FAULT_AFTER_INTENT)
            self._fault(FAULT_BEFORE_APPLY)
            try:
                os.unlink(target.absolute)
            except OSError as error:
                raise SinkError(
                    f"could not remove {canonical!r}: {error}"
                ) from error
            self._fsync_dir(os.path.dirname(target.absolute))
            self._fault(FAULT_AFTER_APPLY)

            self._fault(FAULT_BEFORE_FINALIZE)
            try:
                receipt = self._finalize(intent, result="ok")
            except CoordinationError as error:
                raise self._apply_failure(intent, True, error) from error
            self._fault(FAULT_AFTER_FINALIZE)
            return MutationResult(
                operation_id=operation_id,
                kind="remove",
                receipt=receipt,
                paths=[canonical],
                before=dict(intent.before),
                after=dict(intent.after),
                artifact_refs=list(intent.artifact_refs),
                applied=True,
            )

    def rename(
        self,
        attempt,
        source: str,
        destination: str,
        *,
        source_claim=None,
        dest_claim=None,
        operation_id: Optional[str] = None,
        expected_digest: Optional[str] = None,
        dest_expected: Optional[str] = None,
        media_type: Optional[str] = None,
        source_authorize=None,
    ) -> MutationResult:
        """Move `source` to `destination`, recording both paths and both versions.

        The destination is committed first (staged then atomically replaced) and the
        source is removed second, so an interruption between the two paths is a
        real, recoverable state that reconciliation recognizes and completes.

        `source_authorize`, when given, is the inside-the-lock read-token check C08
        passes for the source path (`dest_expected` remains the destination's
        version rule: `ABSENT` to require a create, or an explicit digest to
        authorize overwriting an existing destination).
        """
        require_active_attempt(attempt)
        operation_id = operation_id or self.service.new_operation_id()
        with self.store.operation_lock():
            self._reconcile_locked()
            replay = self._dedup(operation_id, "rename")
            if replay is not None:
                return replay

            attempt = self._current_attempt(attempt)
            source_target = self._resolve(source, mode=MODE_CLAIM)
            dest_target = self._resolve(destination, mode=MODE_CLAIM)
            source_key, dest_key = source_target.relative, dest_target.relative
            if source_key == dest_key:
                raise UnsupportedCoordination(
                    f"rename source and destination are the same path {source_key!r}",
                    details={"path": source_key},
                )
            held_source = self._require_claim(attempt, source_key, source_claim)
            held_dest = self._require_claim(attempt, dest_key, dest_claim)
            if source_authorize is not None:
                source_authorize(held_source)

            observed_source = source_target.digest
            if observed_source == ABSENT:
                raise CoordinationNotFound(
                    f"cannot rename {source_key!r}: the source does not exist",
                    details={"path": source_key},
                )
            require_expected_digest(observed_source, held_source.observed_version, path=source_key)
            if expected_digest is not None:
                require_expected_digest(observed_source, expected_digest, path=source_key)
            observed_dest = dest_target.digest
            wanted_dest = dest_expected if dest_expected is not None else held_dest.observed_version
            require_expected_digest(observed_dest, wanted_dest, path=dest_key)

            move_bytes = self._read_bytes(source_target.absolute)
            pieces = [(move_bytes, "before")]
            if observed_dest != ABSENT:
                # Overwriting an existing destination destroys bytes that were never
                # read through the proxy: store them so the receipt is honest about
                # what the rename replaced, not only what it moved.
                pieces.append((self._read_bytes(dest_target.absolute), "destination_before"))
            artifacts = self._store_evidence(
                pieces, media_type=media_type or DEFAULT_MEDIA_TYPE
            )
            intent = self._new_intent(
                "rename",
                attempt,
                operation_id,
                paths=[source_key, dest_key],
                before={source_key: observed_source, dest_key: observed_dest},
                after={source_key: ABSENT, dest_key: observed_source},
                artifacts=artifacts,
                claim_generation=held_source.generation,
            )
            self._persist_intent(intent, artifacts)
            self._fault(FAULT_AFTER_INTENT)

            staged = self._stage(dest_target.absolute, move_bytes, intent)
            mode = stat.S_IMODE(os.stat(source_target.absolute).st_mode)
            os.chmod(staged, mode)
            self._fault(FAULT_AFTER_STAGE)
            self._fault(FAULT_BEFORE_APPLY)
            try:
                os.replace(staged, dest_target.absolute)
            except OSError as error:
                self._cleanup_staged(staged)
                raise SinkError(
                    f"could not rename {source_key!r} to {dest_key!r}: {error}"
                ) from error
            self._fsync_dir(os.path.dirname(dest_target.absolute))
            # The "interrupted between paths" boundary: destination committed,
            # source not yet removed.
            self._fault(FAULT_AFTER_DEST_COMMITTED)
            try:
                os.unlink(source_target.absolute)
            except OSError as error:
                raise SinkError(
                    f"could not complete rename {source_key!r} -> {dest_key!r} "
                    f"(destination committed): {error}"
                ) from error
            self._fsync_dir(os.path.dirname(source_target.absolute))
            self._fault(FAULT_AFTER_APPLY)

            self._fault(FAULT_BEFORE_FINALIZE)
            try:
                receipt = self._finalize(intent, result="ok")
            except CoordinationError as error:
                raise self._apply_failure(intent, True, error) from error
            self._fault(FAULT_AFTER_FINALIZE)
            return MutationResult(
                operation_id=operation_id,
                kind="rename",
                receipt=receipt,
                paths=[source_key, dest_key],
                before=dict(intent.before),
                after=dict(intent.after),
                artifact_refs=list(intent.artifact_refs),
                applied=True,
            )


__all__ = [
    "FAULT_AFTER_APPLY",
    "FAULT_AFTER_DEST_COMMITTED",
    "FAULT_AFTER_FINALIZE",
    "FAULT_AFTER_INTENT",
    "FAULT_AFTER_STAGE",
    "FAULT_BEFORE_APPLY",
    "FAULT_BEFORE_FINALIZE",
    "FAULT_PHASES",
    "FaultInjected",
    "MutationEngine",
    "MutationResult",
    "UNKNOWN",
]
