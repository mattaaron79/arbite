"""The recoverable write protocol: stage the bytes, verify, apply, finalise.

This is the engine the file commands call (writes and edits in tic-60c7, create, remove
and rename in tic-74e2). It is deliberately *not* a command surface: no argparse, no
printing, no tokens of its own -- a caller passes the paths, the two versions it means,
the payload and the read token that authorises the change, and gets back a receipt.

One operation is five steps, and the order is the whole design:

1. **Verify, before anything is written.** The ticket must still be the ticket, the
   attempt must still be current, each path must still be claimed by that attempt at the
   generation the read token was taken under, the token must be an observation of that
   exact version, and the bytes on disk must still be that version. Every one of those
   refusals says **no bytes were changed**, because a caller that cannot tell "it
   refused" from "it may have written half a file" will re-apply blindly.
2. **Persist the intent and the evidence.** The before and after bytes are archived by
   digest, and the receipt -- kind, paths, both versions, the artifacts, the claim
   generation -- is committed as `pending`. Every version is judged *before* any of them
   is stored -- the bytes have to be there, and they have to be a size this proxy will
   keep (`store.MAX_ARTIFACT_BYTES`) -- so a version that cannot be written as evidence
   refuses the operation here, by name, **before** any byte of the project changes.
3. **Stage the new content** in the target's own directory, under a name carrying the
   operation id, so the replacement is one atomic `os.replace` and a crash leaves a
   recognisable leftover rather than a half-written target.
4. **Apply** the one filesystem change the operation is: the replacement, the rename, or
   the removal.
5. **Finalise** the receipt as succeeded and discard the staged copy.

The boundaries between those steps are named constants and the process can be killed at
each of them (`crash_hook`), which is what the durability tests do with real processes.
Whichever one is crossed, the answer is in the records: `coordination.recovery` compares
the bytes on disk with the two recorded versions and either finalises the operation
(there is nothing left to apply) or discards it (nothing was applied), and reports drift
when a third version is there instead. This module therefore never has to guess what it
was doing before it died -- and no automatic pass ever applies bytes on a dead
operation's behalf.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Optional

from ..errors import Busy, CoordinationError, NoClaim, Stale
from . import recovery
from .paths import modified_clock, probe
from .store import require_storable_artifact
from .records import (
    ABSENT,
    RECEIPT_KINDS,
    RECEIPT_PENDING,
    RECEIPT_SUCCEEDED,
    Artifact,
    OperationReceipt,
    WorkAttempt,
    digest_bytes,
    new_id,
    short_digest,
    utc_now,
)
from .results import REFUSAL_INDENT

#: The boundaries a fault-injection hook can be told about, in the order they are
#: crossed. Two of them are observably the same state (#1 and #2, #4 and #5) and are named
#: separately anyway, because "nothing staged yet", "staged but not applied", "applied but
#: not finalised" are the three states a human has to be able to reason about -- and the
#: ticket asks for a kill at each.
INTENT_PERSISTED = "intent_persisted"
STAGED = "staged"
BEFORE_REPLACE = "before_replace"
REPLACED = "replaced"
BEFORE_FINALIZE = "before_finalize"

BOUNDARIES = (INTENT_PERSISTED, STAGED, BEFORE_REPLACE, REPLACED, BEFORE_FINALIZE)

#: The ticket status in which a mutation may happen. A claim sets it, and any lifecycle
#: command that ends the work changes it, which is why one status check plus one attempt
#: check cover "this ticket still belongs to that attempt" (RC2: a close racing a write).
MUTABLE_STATUS = "in_progress"

#: The version a path has when it is not there. `records.ABSENT` spelled out here, because
#: this module reads it as *the operation's own* vocabulary: expect=absent is a create,
#: becomes=absent is a removal.
ABSENT_VERSION = ABSENT

#: The reasons the refusals below carry. The *reason* keys the `next:` line and the JSON
#: branch, while the clause stays outcome 5 for all of them (nothing changed; the fix is a
#: fresh read): a file that moved, a token that was spent and a ticket that closed are one
#: exit code and three different repairs, so they cannot share one hint.
REASON_STALE_VERSION = "stale_version"
REASON_STALE_TOKEN_SPENT = "stale_token_spent"
REASON_ATTEMPT_NOT_CURRENT = "attempt_not_current"

#: The reason WR4's refusal names inline, in the caller's own words. It is also the
#: registered key for the hint, so the word in the message and the branch in JSON agree.
REASON_NO_CLAIM = "no_claim"

#: The closing line of every refusal a mutation makes, on its own line because that is how the
#: frozen blocks print it. A caller has to be able to tell "it refused" from "it may have
#: written half a file": this is the sentence that makes the difference checkable.
NO_BYTES_CHANGED = "no bytes were changed"


def spent_message(observation) -> str:
    """The two lines a token a mutation already used produces (the frozen WR3 shape).

    One place for the words, because every mutation of every kind answers a replay the same
    way: one read token authorises exactly one mutation, the operation that used it is
    named, and nothing was changed by the replay. `already_spent` is the refusal that
    carries the standard repair; a caller that cannot take that repair -- a path that is no
    longer there has nothing to re-read -- builds its own hint from this sentence."""
    return (
        f"read token {observation.id} was already spent by {observation.spent_by}\n"
        f"{NO_BYTES_CHANGED}"
    )


def already_spent(observation, command: str) -> "Stale":
    """The refusal a token a mutation has already used produces (WR3).

    The spending operation is named, because "already spent" without it reads as a version
    mismatch and sends the caller looking for an edit nobody made: one read token authorises
    exactly one mutation, and this is the sentence that says which mutation it authorised.
    `command` is the read that takes a fresh one."""
    return Stale(
        spent_message(observation),
        reason=REASON_STALE_TOKEN_SPENT,
        next_actions=[command],
        text_hint=f"next: '{command}' for a fresh token",
    )


def read_command(ticket_id: str, attempt_id: str, path: str) -> str:
    """The read a mutation's refusals hand back: the exact command, not a description.

    Every stale refusal in this module exists to send the caller back to a fresh read
    under the same claim, so the sentence names the command with the ticket and the
    attempt already filled in -- a hint a caller can run rather than interpret."""
    return f"arbite file read {path} --ticket {ticket_id} --attempt {attempt_id}"


@dataclass(frozen=True)
class PathChange:
    """One path an operation is about: what the caller saw, and what it will become.

    `expect` is the whole-file version the caller verified (from its read token, and
    re-checked against the bytes at use time); `becomes` is the version the operation
    produces; `payload` is the bytes to stage when that version is content. A path with no
    payload is either a removal (`becomes` is absent) or part of a rename, where the bytes
    move rather than being written."""

    path: str
    expect: str
    becomes: str
    payload: Optional[bytes] = None
    token: Optional[str] = None

    @property
    def is_creation(self) -> bool:
        return self.expect == ABSENT_VERSION

    @property
    def is_removal(self) -> bool:
        return self.becomes == ABSENT_VERSION

    @property
    def stages_bytes(self) -> bool:
        return self.payload is not None


@dataclass(frozen=True)
class MutationRequest:
    """One operation's intent, before anything has been written.

    `operation_id` is the caller's token for the operation, and is what a retry presents
    to be recognised: minted here when the caller has none, kept by the caller when it is
    retrying deliberately."""

    kind: str
    ticket_id: str
    attempt_id: str
    actor: str
    changes: tuple
    operation_id: Optional[str] = None

    @property
    def paths(self) -> list:
        return [change.path for change in self.changes]


@dataclass(frozen=True)
class MutationOutcome:
    """What an `apply` did, and therefore what may be claimed about the bytes.

    `applied` is True only for a call that performed the filesystem change itself.
    `deduplicated` is the retry case: the receipt was already finalised, so nothing was
    done and nothing may be done -- a retried operation does not duplicate its effects.
    `recovered` is the crash case: a previous attempt had applied the bytes and died
    before finalising, so this call only recorded what was already true."""

    operation_id: str
    receipt: OperationReceipt
    applied: bool = False
    deduplicated: bool = False
    recovered: bool = False

    @property
    def paths(self) -> tuple:
        return tuple(self.receipt.paths)


class FileMutations:
    """The recoverable write protocol, for one ticket sink and one coordination store."""

    #: A fault-injection seam, None in production: called with each boundary name
    #: (`BOUNDARIES`) as it is reached, so a test can kill a process exactly there.
    crash_hook: Optional[Callable[[str], None]] = None

    def __init__(self, sink, lifecycle):
        self.sink = sink
        self.lifecycle = lifecycle
        self.app = lifecycle.app
        self.store = lifecycle.store
        self.project_root = self.app.project_root

    # ------------------------------------------------------------------
    # The operation
    # ------------------------------------------------------------------

    def apply(self, request: MutationRequest) -> MutationOutcome:
        """Carry out one operation, exactly once, however this call ends.

        The token the caller presents identifies the operation: presenting one whose
        receipt is already finalised changes nothing (that is what makes a retry safe),
        and presenting one whose receipt is still pending re-runs the operation -- after
        judging what the interrupted attempt left behind, so an operation whose bytes are
        already in place is *completed* rather than applied twice.

        Refusals raise: `Stale` (outcome 5) when the ticket, the attempt, the generation,
        the token or the bytes moved, `Busy` (outcome 4) when another attempt holds a
        path, and `CoordinationError` (outcome 1) when the operation cannot be recorded
        or what it left behind cannot be judged. Every refusal states whether bytes may
        already have changed, and which operation to inspect or retry.
        """
        self._require_kind(request)
        root = self.project_root
        operation_id = request.operation_id or self._new_operation_id()
        # The id is minted here when the caller has none, so everything below -- the
        # receipt, its events and the staged copies -- names the same operation.
        request = replace(request, operation_id=operation_id)

        existing = self.store.find_record("receipt", operation_id)
        if existing is not None and not existing.is_pending:
            # The operation already happened (or already failed): re-running it would be
            # the duplicated effect operation ids exist to prevent.
            return MutationOutcome(operation_id, existing, deduplicated=True)

        if existing is not None:
            # Judged before anything is written: this is the retry meeting its own earlier
            # attempt, and what that attempt left behind decides whether there is anything
            # left to do at all.
            judged = recovery.judge(existing, root)
            if judged.verdict == recovery.COMPLETED:
                # The bytes are already the recorded after version: the operation happened
                # and only its receipt was lost. Nothing is written twice.
                recovery.settle(self.store, existing, root)
                return MutationOutcome(
                    operation_id,
                    self.store.get_record("receipt", operation_id),
                    recovered=True,
                )
            if judged.verdict == recovery.DRIFT:
                raise CoordinationError(
                    f"{operation_id} staged a {existing.kind} of "
                    f"{', '.join(existing.paths)} and was not finalized; the bytes on disk "
                    "are neither the recorded before nor the after version, so this "
                    "operation was NOT re-applied -- inspect "
                    f"{', '.join(existing.paths)} by hand and re-read before retrying"
                )
            # Nothing was applied (or the bytes could not be judged): the operation runs
            # again from the top under the same id, and its intent commit replaces the
            # pending receipt. Nothing is recorded as failed for an operation that is
            # about to be applied.

        # Every other operation a dead run left behind is reconciled first: this is the
        # "next relevant operation" the durability rules reconcile on, and it happens
        # before this one stages anything of its own.
        recovery.reconcile(self.store, root, skip=(operation_id,))

        # Everything from the verification to the finalisation happens inside the store's
        # ephemeral operation lock, so a check cannot be overtaken between reading the
        # state and changing the bytes (see `CoordinationStore.operation_lock`).
        with self.store.operation_lock():
            ticket = self.sink.get(request.ticket_id, unique=True)
            attempt = self._require_current_attempt(ticket, request.attempt_id)
            claims = {
                change.path: self._require_path(ticket, attempt, change)
                for change in request.changes
            }
            self._require_one_generation(claims)
            before, data = self._confirm_versions(request, claims)
            receipt = self._build_receipt(request, operation_id, before, claims)
            artifacts = self._archive_evidence(receipt, data)
            receipt = replace(receipt, artifacts=artifacts)
            self._commit_intent(receipt, request)
            self._crash_point(INTENT_PERSISTED)
            self._stage(request)
            self._crash_point(STAGED)
            self._crash_point(BEFORE_REPLACE)
            self._perform(request)
            self._crash_point(REPLACED)
            self._crash_point(BEFORE_FINALIZE)
            receipt = self._finalise(receipt, request)
            self._discard_stages(request, operation_id)
        return MutationOutcome(operation_id, receipt, applied=True)

    # ------------------------------------------------------------------
    # 1. Verification
    # ------------------------------------------------------------------

    def _require_kind(self, request: MutationRequest) -> None:
        """Refuse an operation this engine cannot perform, before anything is read.

        The shape is deliberately rigid: one change per written or removed path, exactly
        two for a rename (source then destination). A multi-path operation would have a
        partial application to describe, and describing a partial application honestly is
        what this slice refuses to guess at."""
        kinds = [kind for kind in RECEIPT_KINDS if kind != "passthrough"]
        if request.kind not in kinds:
            raise CoordinationError(
                f"'{request.kind}' is not an operation this engine performs (it handles "
                f"{', '.join(kinds)})"
            )
        expected = 2 if request.kind == "rename" else 1
        if len(request.changes) != expected:
            raise CoordinationError(
                f"a '{request.kind}' takes {expected} path(s), got {len(request.changes)}"
            )

    def _require_current_attempt(self, ticket, attempt_id: str) -> WorkAttempt:
        """The attempt this operation names, refused unless it still owns the work.

        One message for the whole family, because the caller's response is the same for
        all of them: re-read the ticket, claim it again, and read the path again under the
        new claim. The closing line -- "no bytes were changed" -- is part of the message
        rather than the caller's job, because a refusal that *might* have written
        something is the one thing an agent cannot act on safely (RC2)."""
        attempt = self.store.get_attempt(attempt_id)
        if attempt is None:
            raise self._stale(f"attempt {attempt_id} does not exist in this store")
        if attempt.ticket_id != ticket.id:
            raise self._stale(
                f"attempt {attempt_id} belongs to {attempt.ticket_id}, not {ticket.id}"
            )
        if not attempt.is_active or ticket.status != MUTABLE_STATUS:
            raise self._no_longer_current(ticket, attempt)
        return attempt

    def _no_longer_current(self, ticket, attempt: WorkAttempt) -> Stale:
        """The stale refusal a revoked attempt or a moved ticket produces (WR5, RC2).

        The repair is not "re-read and retry" -- the bytes are frozen because the ticket
        moved on, and the only ways forward are to reopen it deliberately or to stop --
        so this carries its own reason and its own sentence rather than the generic
        stale hint. The reopen command is named with the ticket already filled in; the
        reason placeholder is the caller's to write."""
        reopen = f'arbite reopen {ticket.id} --reason "<why>"'
        return Stale(
            f"{self._not_current(ticket, attempt)}\n{NO_BYTES_CHANGED}",
            reason=REASON_ATTEMPT_NOT_CURRENT,
            next_actions=[reopen],
            text_hint=f"next: reopen the ticket ('{reopen}') and claim it again, or stop work on it",
        )

    def _not_current(self, ticket, attempt: WorkAttempt) -> str:
        """Why this attempt can no longer mutate, in the words RC2 freezes.

        The generation that is no longer current, and the ticket state that ended it. The
        time is the ticket's own stamp printed as the local reading it already is -- the
        ticket store keeps local timestamps, and converting one as if it were UTC would
        print a time seven hours off the record."""
        where = f"{ticket.id} {ticket.status}"
        if ticket.closed:
            where += f" {self._stamp_time(ticket.closed)}"
        return (
            f"attempt {attempt.id} generation {attempt.generation} is no longer current "
            f"({where})"
        )

    @staticmethod
    def _stamp_time(stamp: str) -> str:
        """The clock reading inside a ticket's own timestamp (`...T06:20:03` -> `06:20:03`)."""
        text = str(stamp)
        if "T" in text:
            return text.split("T", 1)[1][:8]
        return text

    def _stale(self, message: str) -> Stale:
        """A refusal that changed nothing, said in both halves the caller needs."""
        return Stale(f"{message}\n{NO_BYTES_CHANGED}", reason="stale_read")

    def _require_path(self, ticket, attempt: WorkAttempt, change: PathChange):
        """The live claim on a path, refused unless this attempt holds it.

        A busy path is outcome 4 rather than 5, because the caller's correct response is
        to pick other work rather than to re-read: the bytes are not theirs to write, and
        retrying will not change that. An unclaimed path is an *error* (WR4): no amount
        of re-reading makes the path the caller's, so the answer names the claim to take
        and says plainly that a read is not ownership."""
        active = self.store.claims_for_path(change.path)
        holder = active[0] if active else None
        if holder is not None and not holder.held_by(attempt.id):
            raise Busy(
                f"{change.path} is held by {holder.ticket_id} / {holder.attempt_id} "
                f"(generation {holder.generation})\nno bytes were changed",
                reason="file_busy",
            )
        if holder is None:
            claim_command = (
                f"arbite file claim {change.path} --ticket {ticket.id} --attempt {attempt.id}"
            )
            raise NoClaim(
                f"{ticket.id} / {attempt.id} does not hold a claim on {change.path} "
                f"({REASON_NO_CLAIM});\n"
                f"{REFUSAL_INDENT}a read does not authorize a write",
                [claim_command],
                # The frozen WR4 block quotes the command, as every hint in the document does.
                text_hint=f"next: '{claim_command}'",
            )
        return holder

    def _require_one_generation(self, claims) -> None:
        """Refuse a path set whose claims are not all the generation the read saw.

        A rename claims its source and its destination in one acquisition, so they share a
        generation; two generations mean one of the paths has changed hands since, and the
        operation would be writing under a token that authorises only part of it."""
        generations = {claim.generation for claim in claims.values()}
        if len(generations) > 1:
            raise self._stale(
                "the paths this operation names are claimed at different generations "
                f"({', '.join(str(value) for value in sorted(generations))}), so one of "
                "them has changed hands since you read it"
            )

    def _confirm_versions(self, request: MutationRequest, claims) -> tuple:
        """Check the token and the bytes for every path, and read the evidence.

        The order is the order a caller can act on it: the token has to exist, and to be
        *unspent* (one token authorises one mutation, and a replay is told which operation
        spent it), and to be *this* path's -- an observation taken elsewhere, or before the
        claim, authorises nothing -- and to describe the version the caller passed, and the
        bytes on disk have to still be that version. Every one of those is outcome 5 with
        nothing written; the reasons differ because the repairs do.

        Returns the recorded before versions and the bytes to archive, keyed by digest: two
        paths that share a version share one artifact, which is the point of storing content
        by its digest."""
        before, data = {}, {}
        for change in request.changes:
            observation = self._observation(change)
            claim = claims[change.path]
            if observation.is_spent:
                raise self._already_spent(observation, change, request)
            authorised = (
                observation.authorizes_creation(claim)
                if change.is_creation
                else observation.authorizes_write(claim)
            )
            if not authorised:
                raise self._stale(
                    f"read token {observation.id} does not authorize this attempt to "
                    f"change {change.path}"
                )
            if observation.digest != change.expect:
                raise self._stale(
                    f"read token {observation.id} observed {observation.digest} for "
                    f"{change.path}, not {change.expect}"
                )
            observed = probe(self.project_root, change.path)
            if observed.digest != change.expect:
                raise self._moved(change, observed, request)
            if change.payload is not None and digest_bytes(change.payload) != change.becomes:
                raise CoordinationError(
                    f"the payload for {change.path} is not the version the operation "
                    "claims to write, so recording it would be recording a lie"
                )
            before[change.path] = observed.digest
            if not change.is_creation:
                data[observed.digest] = (Path(self.project_root) / change.path).read_bytes()
            if change.payload is not None:
                data[change.becomes] = change.payload
        return before, data

    def _already_spent(self, observation, change: PathChange, request: MutationRequest) -> Stale:
        """Refuse a token a mutation has already used (WR3); see `already_spent`."""
        return already_spent(
            observation, read_command(request.ticket_id, request.attempt_id, change.path)
        )

    def _moved(self, change: PathChange, observed, request: MutationRequest) -> Stale:
        """Refuse bytes that moved after the read the caller planned against (WR2).

        Both versions are printed -- what the token observed and what is on disk -- with
        the file's own last-write time, because the caller has to judge whether its change
        still applies to the new text and neither digest means anything without the other.
        A path that was absent when the caller probed it is the same situation read the
        other way round, and says so."""
        command = read_command(request.ticket_id, request.attempt_id, change.path)
        when = modified_clock(self.project_root, change.path)
        stamp = f" (changed {when})" if when else ""
        if observed.digest == ABSENT_VERSION:
            moved = f"{change.path} was removed after you read it{stamp}"
        elif change.expect == ABSENT_VERSION:
            moved = (
                f"{change.path} was absent when you read it and is now "
                f"{short_digest(observed.digest)}{stamp}"
            )
        else:
            moved = (
                f"you read {short_digest(change.expect)} but the file is now "
                f"{short_digest(observed.digest)}{stamp}"
            )
        return Stale(
            f"{moved}\n{NO_BYTES_CHANGED}",
            reason=REASON_STALE_VERSION,
            next_actions=[command],
            text_hint=(
                f"next: '{command}',\n"
                "      re-apply your change, then write with the new token"
            ),
        )

    def _observation(self, change: PathChange):
        """The read observation a change presents as its token."""
        if not change.token:
            raise self._stale(
                f"no read token was presented for {change.path}, and a claim alone does "
                "not authorize a change"
            )
        observation = self.store.find_record("observation", change.token)
        if observation is None:
            raise self._stale(
                f"read token {change.token} does not exist in this store"
            )
        return observation

    def _build_receipt(self, request, operation_id, before, claims) -> OperationReceipt:
        """The pending receipt: the intent, with both versions and where they came from."""
        after = {change.path: change.becomes for change in request.changes}
        return OperationReceipt(
            id=operation_id,
            kind=request.kind,
            paths=list(after),
            result=RECEIPT_PENDING,
            recorded_at=utc_now(),
            ticket_id=request.ticket_id,
            attempt_id=request.attempt_id,
            actor=request.actor,
            before=before,
            after=after,
            claim_generation=next(iter(claims.values())).generation,
        )

    # ------------------------------------------------------------------
    # 2. Intent and evidence
    # ------------------------------------------------------------------

    def _archive_evidence(self, receipt: OperationReceipt, data: dict) -> list:
        """Store the before and after bytes, and return the artifact ids for the receipt.

        Content-addressed, so two receipts that share a version share the bytes. Two things
        are judged for the **whole operation** before any of it is stored: every version the
        receipt names has bytes supplied for it (recording intent without evidence is what
        would leave a change nobody can reproduce), and every one of those versions is a size
        this proxy keeps (`store.require_storable_artifact`). So a version that cannot be
        written refuses the operation here, by name, before any byte of the project -- and
        before any partial evidence of its own -- has changed."""
        wanted = sorted(
            {
                digest
                for digest in (*receipt.before.values(), *receipt.after.values())
                if digest != ABSENT_VERSION
            }
        )
        for digest in wanted:
            if digest not in data:
                raise CoordinationError(
                    f"no bytes were supplied for version {digest}, which this operation "
                    "records, so its evidence cannot be written; nothing was changed"
                )
            require_storable_artifact(digest, data[digest])

        artifacts = []
        existing = {artifact.digest: artifact for artifact in self.store.records("artifact")}
        for digest in wanted:
            try:
                self.store.put_artifact_bytes(digest, data[digest])
            except NotImplementedError as e:
                raise CoordinationError(
                    f"this operation cannot be recorded: {e}, so the evidence a mutation "
                    "must keep cannot be written; nothing was changed"
                )
            artifact = existing.get(digest)
            if artifact is None:
                artifact = Artifact(
                    id=new_id("artifact", {a.id for a in existing.values()}),
                    digest=digest,
                    size=len(data[digest]),
                    created=utc_now(),
                    operation_id=receipt.id,
                )
                self.store.put_record(artifact)
                existing[digest] = artifact
            artifacts.append(artifact.id)
        return sorted(artifacts)

    def _commit_intent(self, receipt: OperationReceipt, request: MutationRequest) -> None:
        """Commit the pending receipt and the event that says an operation began."""
        with self.store.transaction() as txn:
            txn.put_record(receipt)
            txn.append_event(
                f"{request.kind}.intent",
                "file",
                subject=", ".join(receipt.paths),
                result="staged",
                ticket_id=request.ticket_id,
                attempt_id=request.attempt_id,
                actor=request.actor,
                operation_id=receipt.id,
                payload={
                    "before": receipt.before,
                    "after": receipt.after,
                    "generation": receipt.claim_generation,
                },
            )

    # ------------------------------------------------------------------
    # 3. Staging
    # ------------------------------------------------------------------

    def _stage(self, request: MutationRequest) -> None:
        """Write each payload beside its target, and make it durable before the replace.

        The stage file *is* the operation's undo-free retry material: if the process dies
        after this, the intent and the bytes it meant to write are both on disk, and the
        reconciliation either completes the receipt (the replace happened) or discards the
        copy (it did not)."""
        for change in request.changes:
            if not change.stages_bytes:
                continue
            stage = recovery.stage_path(
                self.project_root, change.path, request.operation_id
            )
            self._write_stage(stage, change.payload, self._existing_mode(change.path))

    def _write_stage(self, stage: Path, payload: bytes, mode=None) -> None:
        """Create the stage file's content atomically, so a leftover is never half a file.

        A second name in the same directory, then `os.replace`: the stage file a later
        recovery sees is either complete or absent, and never a truncated version of the
        bytes an operation meant to write.

        The replacement carries the target's permission bits when it has any, because the
        replace would otherwise give a 0600 file the umask's default: an operation changes
        the *content* of a path, and a writer must not silently widen -- or narrow -- who can
        read it. Ownership is deliberately not carried over: that is not something an
        unprivileged process can promise, and nothing here claims it."""
        partial = stage.with_name(stage.name + ".partial")
        stage.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(partial, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            if mode is not None:
                os.chmod(partial, mode)
            os.replace(partial, stage)
        except BaseException:
            if partial.exists():
                partial.unlink()
            raise

    def _existing_mode(self, path: str):
        """The permission bits of the path a change replaces, or None for a creation.

        `None` lets the umask decide, which is exactly right for a file that is not there
        yet: nothing existed whose permissions could be preserved."""
        try:
            return stat.S_IMODE(os.stat(Path(self.project_root) / path).st_mode)
        except OSError:
            return None

    # ------------------------------------------------------------------
    # 4. The filesystem change
    # ------------------------------------------------------------------

    def _perform(self, request: MutationRequest) -> None:
        """Apply the one filesystem change this operation is.

        A mutation is deliberately *one* change: one replacement per written path, one
        rename, one removal per path. That is what keeps a partial application impossible
        to describe -- and where a system cannot promise it (a rename interrupted between
        its two names), the recovery engine reports drift instead of pretending."""
        if request.kind == "rename":
            source, dest = request.changes[0], request.changes[1]
            os.replace(
                Path(self.project_root) / source.path, Path(self.project_root) / dest.path
            )
            return
        for change in request.changes:
            target = Path(self.project_root) / change.path
            if change.is_removal:
                target.unlink()
                continue
            stage = recovery.stage_path(
                self.project_root, change.path, request.operation_id
            )
            os.replace(stage, target)

    # ------------------------------------------------------------------
    # 5. Finalisation
    # ------------------------------------------------------------------

    def _finalise(self, receipt: OperationReceipt, request: MutationRequest) -> OperationReceipt:
        """Record that the operation happened, and what it produced.

        A compare-and-swap on the receipt's revision, in one commit with the applied
        event: a receipt that a recovery pass already finalised is left exactly as it is,
        because two writers agreeing on the facts is not a reason to write the facts
        twice."""
        with self.store.transaction() as txn:
            current = txn.get_record("receipt", receipt.id)
            if not current.is_pending:
                return current
            final = replace(current, result=RECEIPT_SUCCEEDED)
            txn.replace_record(final, expect_revision=txn.revision("receipt", receipt.id))
            self._spend_tokens(txn, request, final)
            txn.append_event(
                f"{request.kind}.file",
                "file",
                subject=", ".join(current.paths),
                result=_applied_result(request),
                ticket_id=current.ticket_id,
                attempt_id=current.attempt_id,
                actor=current.actor,
                operation_id=current.id,
                payload={"after": current.after, "generation": current.claim_generation},
            )
        return final

    def _spend_tokens(self, txn, request: MutationRequest, receipt: OperationReceipt) -> None:
        """Mark the read tokens this operation used as spent, in the same commit.

        "One token authorises one mutation" is made true here, and it is made true
        *atomically*: the bytes, the receipt that records them and the token that paid
        for them commit together or not at all, so a second writer presenting the same
        token -- however soon it runs -- cannot find an unspent token to use. A token
        that is already spent is left alone: the operation that spent it is the one on
        record, and this commit is not the place to rewrite that."""
        tokens = {change.token for change in request.changes if change.token}
        for token in sorted(tokens):
            observation = txn.find_record("observation", token)
            if observation is None or observation.is_spent:
                continue
            txn.replace_record(
                observation.spent(receipt.id),
                expect_revision=txn.revision("observation", token),
            )

    def _discard_stages(self, request: MutationRequest, operation_id: str) -> None:
        """Remove the staged copies now that the target holds the bytes.

        Best-effort on purpose: the receipt is already finalised, the bytes are already at
        the target, and a leftover stage file is exactly what the next reconciliation
        cleans up. Failing here would be reporting a successful write as a failure."""
        for change in request.changes:
            stage = recovery.stage_path(self.project_root, change.path, operation_id)
            if stage.is_file():
                try:
                    stage.unlink()
                except OSError:
                    continue

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _crash_point(self, name: str) -> None:
        if self.crash_hook is not None:
            self.crash_hook(name)

    def _new_operation_id(self) -> str:
        """A token nothing else in this store has used.

        Checked against the receipts the store holds rather than assumed unique, because
        the id is what deduplicates a retry: two operations sharing one would look like
        one operation retried, and the second would silently do nothing."""
        taken = {receipt.id for receipt in self.store.records("receipt")}
        return new_id("receipt", taken)


def _applied_result(request: MutationRequest) -> str:
    """What the applied event's one-line result says: the new version, or what happened.

    A rename and a removal have no new version of their own, so they say what they did
    instead of printing a digest nobody can compare to a file."""
    if request.kind == "rename":
        return "moved"
    if request.kind == "remove":
        return "removed"
    first = request.changes[0]
    if first.becomes == ABSENT_VERSION:
        return "removed"
    return first.becomes
