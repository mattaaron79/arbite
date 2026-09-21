"""The coordination store interface: records, counts, and the later slices' hooks.

`CoordinationStore` is a second, separately conformance-tested interface -- it is
*not* a superset of `TicketSink`, and it is deliberately not part of it. A ticket
is a document that belongs in git and is readable in `ls`; a claim is local runtime
state that must be discardable after a crash. Keeping them apart is what lets
coordination move to a database or a server later without touching ticket storage.
Both sinks implement this interface, and the conformance suite runs each test
against both, so "the file sink and SQLite mean the same thing" is checked rather
than asserted.

What each half of this file is:

- **Implemented now, by both backends:** single-record reads and writes, the
  counts a report needs, and the derived queries built from them (active claims,
  active attempts, pending operations). These are the primitives the whole proxy
  is expressed in.
- **Implemented now, once, on top of those primitives:** the transaction, the
  record revision counter, and the event cursor. `transaction()` gives an
  operation a store-local unit of work -- several records and the events that
  describe them, committed together or not at all -- with a real SQL transaction
  on one backend and a commit journal plus a process lock on the other
  (`CoordinationTransaction` below says exactly what both promise).
- **Implemented now: the recovery engine** (`coordination/recovery.py`), which is
  the file-operation intent journal this interface names. A *staged file write* is
  reconciled by comparing the bytes on disk with the two versions the receipt
  recorded: exactly one of "it happened", "it did not happen" or "these are
  somebody else's bytes", and only the first two are acted on. This is a different
  journal from the commit journal the file backend uses to make its own
  multi-record writes recoverable; that one is replayed automatically by the next
  write to the store, because finishing it is not a judgement call.
- **Shared semantics:** `record_problems` is implemented once, for both backends,
  through the recovery engine's findings, so integrity means one thing whichever
  store holds the records. Reporting reads; only `recover()` and `doctor --fix`
  write, and only where the answer is unambiguous.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Optional

from ..errors import CoordinationError, EvidenceRefused, RecordError, Stale
from .records import (
    ATTEMPT_ACTIVE,
    CLAIM_ACTIVE,
    EVENT_RESULT_KEY,
    EVENT_SUBJECT_KEY,
    RECORD_TYPES,
    Event,
    Record,
    Workspace,
    digest_bytes,
    new_id,
    record_type_of,
    short_digest,
    utc_now,
)
from .results import register_next_actions

#: The commit boundaries a fault-injection hook can be told about, in order. A
#: process killed at `COMMIT_STAGED` has recorded the unit of work but applied
#: none of it; one killed at `COMMIT_APPLIED` has applied all of it and not yet
#: finalised. Both must leave the store in a state a later operation makes whole
#: without guessing, which is the property the durability tests kill processes to
#: check.
COMMIT_STAGED = "commit_staged"
COMMIT_APPLIED = "commit_applied"

#: The largest single version arbite keeps as evidence: 64 MiB, whole, not a range.
#:
#: An explicit number rather than an implementation accident, because "store the bytes
#: this operation replaced" is a decision about a local, git-ignored, per-machine store:
#: without a stated bound, one enormous file would decide for every later view how much
#: has to be read and verified. It is a bound on *one stored version*, not on the store
#: (a store accumulates versions; retention and pruning are tic-008f's), and it is the
#: same number on both sinks, so "the file sink is more permissive" cannot become a
#: reason to move a project. Nothing here deletes anything: a version already stored
#: stays readable even under a later, smaller limit.
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024

#: Which versions arbite will not keep. A refusal rather than a truncation or a summary:
#: an operation whose evidence cannot be written **must not happen**, so the caller is
#: told to change the file outside the proxy instead of getting an unrecorded write.
SIZE_LIMIT_HINT = "keep files larger than the limit outside managed source paths"
SIZE_LIMIT_REASON = "evidence_too_large"

register_next_actions(SIZE_LIMIT_REASON, [SIZE_LIMIT_HINT])


def require_storable_artifact(digest: str, data: bytes) -> None:
    """Refuse a version this proxy will not keep, naming the size, the limit and the digest.

    Called by every backend's `put_artifact_bytes` (so a direct caller -- an import, a
    recovery pass, a test -- cannot store what the limit refuses) and by the mutation
    engine *before* it stores anything at all (so a refused operation leaves no partial
    evidence either). The rule is one function rather than one per sink because the
    answer to "may this be recorded" must not depend on where the bytes would go.
    """
    size = len(data)
    if size > MAX_ARTIFACT_BYTES:
        raise EvidenceRefused(
            f"the evidence for {short_digest(digest)} is {size} bytes, over the "
            f"{MAX_ARTIFACT_BYTES}-byte limit one stored version may have;\n"
            "nothing was changed",
            [SIZE_LIMIT_HINT],
            text_hint=f"next: {SIZE_LIMIT_HINT}",
        )


@dataclass(frozen=True)
class CoordinationInfo:
    """What a coordination store is, and how much is in it.

    `kind` and `root` name the backend, which is the fact `doctor` must state: a
    project whose tickets and whose coordination state live in different places
    needs to be able to say so out loud rather than imply it."""

    kind: str
    root: str
    claims_active: int = 0
    claims_released: int = 0
    attempts_active: int = 0
    events: int = 0
    receipts: int = 0
    pending_operations: int = 0
    artifacts: int = 0

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "root": self.root,
            "claims_active": self.claims_active,
            "claims_released": self.claims_released,
            "attempts_active": self.attempts_active,
            "events": self.events,
            "receipts": self.receipts,
            "pending_operations": self.pending_operations,
            "artifacts": self.artifacts,
        }

    def doctor_dict(self) -> dict:
        """The coordination section of the `doctor` report.

        A subset on purpose: `doctor` says which backend it is inspecting and the
        counts it acts on. The receipt and artifact totals belong to the change
        and retention views (tic-7c42, tic-008f), not to an integrity report."""
        return {
            "kind": self.kind,
            "root": self.root,
            "claims_active": self.claims_active,
            "events": self.events,
            "pending_operations": self.pending_operations,
        }


@dataclass(frozen=True)
class CommitResult:
    """What a commit did.

    `applied` is False only for the retry case: an operation id whose receipt is
    already in the store is not applied a second time, so a caller that re-runs an
    interrupted operation cannot duplicate its effects. `events` are the events the
    commit appended, with the cursors they were given."""

    applied: bool
    deduplicated: bool = False
    events: tuple = ()
    revisions: dict = field(default_factory=dict)


@dataclass(frozen=True)
class PendingWrite:
    """One buffered change, with the values the commit will store.

    `revision` and the event's `cursor` are *absolute* values assigned at commit
    time rather than deltas, which is what lets a journal replay them any number of
    times and land on the same state. `expect_revision` is the optimistic
    precondition a `replace_record` carries, checked in the same critical section
    as the write itself. `allocate_cursor` marks a write that *asked* for a cursor
    (`append_event`) rather than one that states its own (`put_record`, which is how
    an import places existing events at the cursors they already have)."""

    record_type: str
    record_id: str
    record: Optional[Record] = None
    expect_revision: Optional[int] = None
    revision: Optional[int] = None
    cursor: Optional[int] = None
    allocate_cursor: bool = False

    @property
    def is_delete(self) -> bool:
        return self.record is None

    @property
    def key(self) -> tuple:
        return (self.record_type, self.record_id)

    @property
    def document(self) -> Optional[dict]:
        """The stored document for this write, or None for a delete."""
        return None if self.record is None else self.record.to_dict()


class CoordinationTransaction:
    """A store-local unit of work: the record writes and event appends that commit
    together, or not at all.

    Writes are buffered, so an operation can build a whole state change and hand it
    to the backend as one unit -- SQLite opens a real transaction, the file backend
    writes a commit journal and applies it -- and either way there is exactly one
    commit point. Reads see the buffer first, so an operation can read back what it
    has just written.

    Three rules live here rather than in either backend, so both mean the same
    thing:

    - **Revisions are absolute.** Every write bumps its record's revision, assigned
      inside the backend's serialisation, so two writers that read revision 3 and
      change different fields cannot both commit: the loser gets `Stale` and
      nothing was written. A replay lands on the same numbers.
    - **A retried operation is a no-op.** Given an `operation_id`, a commit whose
      receipt is already in the store applies nothing and reports `deduplicated`.
    - **Cursors come from the store.** `append_event` takes the store's next event
      cursor, so a poll resuming from a cursor cannot skip an event and an event
      replayed from a journal keeps the cursor it was given.
    """

    def __init__(self, store: "CoordinationStore", operation_id: Optional[str] = None):
        self._store = store
        #: The caller's token for this unit of work, if it has one. It is what a
        #: retry presents to be recognised as the same operation.
        self.operation_id = operation_id
        self._pending: dict = {}
        self._order: list = []
        self.committed: Optional[CommitResult] = None

    # ------------------------------------------------------------------
    # Reading -- this transaction's own writes win
    # ------------------------------------------------------------------

    def find_record(self, record_type: str, record_id: str) -> Optional[Record]:
        """One record by id, or None when it is not there (see
        `CoordinationStore.find_record`), seeing this transaction's own writes."""
        self._check_open()
        _check_record_type(record_type)
        pending = self._pending.get((record_type, record_id))
        if pending is not None:
            return pending.record
        return self._store.find_record(record_type, record_id)

    def get_record(self, record_type: str, record_id: str) -> Record:
        """One record by id, raising when it is not there."""
        record = self.find_record(record_type, record_id)
        if record is None:
            raise CoordinationError(
                f"no {record_type} record {record_id} in {self._store.root}"
            )
        return record

    def revision(self, record_type: str, record_id: str) -> int:
        """The revision of a record as this transaction sees it: the stored one, or
        the one its own buffered write will produce.

        This is the token a `replace_record` presents, so an operation that reads a
        record, decides, and writes it back cannot silently overwrite a change made
        in between."""
        self._check_open()
        _check_record_type(record_type)
        pending = self._pending.get((record_type, record_id))
        if pending is not None and not pending.is_delete:
            # Its own write is one bump ahead of whatever is stored.
            return self._store.revision(record_type, record_id) + 1
        if pending is not None:
            return 0
        return self._store.revision(record_type, record_id)

    # ------------------------------------------------------------------
    # Writing -- buffered until commit
    # ------------------------------------------------------------------

    def put_record(self, record: Record) -> None:
        """Buffer `record` as this transaction's version of its id."""
        record.validate()
        self._buffer(record_type_of(record), record.id, record)

    def replace_record(self, record: Record, expect_revision: int) -> None:
        """Buffer a write that commits only while the record's revision is still
        `expect_revision`.

        The check runs at commit time, inside the backend's serialisation, which is
        what makes this a real optimistic write rather than a read followed by a
        hopeful write. `Stale` means another writer got there first and **nothing
        was written**, not even the parts of this transaction that came before."""
        record.validate()
        if not isinstance(expect_revision, int) or isinstance(expect_revision, bool) or expect_revision < 0:
            raise RecordError(
                f"an expected revision must be a non-negative integer, got {expect_revision!r}"
            )
        self._buffer(
            record_type_of(record), record.id, record, expect_revision=expect_revision
        )

    def delete_record(self, record_type: str, record_id: str) -> None:
        """Buffer the removal of one record. A record that is not there is not an
        error: the caller is establishing a state, not asserting one."""
        _check_record_type(record_type)
        self._buffer(record_type, record_id, None)

    def append_event(
        self,
        kind: str,
        category: str,
        *,
        subject=None,
        result=None,
        ticket_id=None,
        attempt_id=None,
        actor=None,
        operation_id=None,
        payload=None,
        recorded_at=None,
    ) -> Event:
        """Append one event to the store's stream, and return it.

        The cursor is the store's next one, and is assigned for real at commit:
        the object returned here carries the value the commit settled on, so a
        caller can print it after committing and trust what it printed. With an
        `operation_id`, the append is idempotent -- an event of the same kind with
        that operation id already in the store is returned unchanged, because a
        retried operation must not leave a second copy of its effects.

        `subject` and `result` are the two payload keys the events view renders
        (see `records.EVENT_SUBJECT_KEY`); any other payload keys are kept as
        given."""
        self._check_open()
        if operation_id is not None:
            existing = self._event_for_operation(operation_id, kind)
            if existing is not None:
                return existing
        event = Event(
            id=self._next_event_id(),
            cursor=self._provisional_cursor(),
            kind=kind,
            recorded_at=recorded_at or utc_now(),
            category=category,
            ticket_id=ticket_id,
            attempt_id=attempt_id,
            actor=actor,
            operation_id=operation_id,
            payload=_event_payload(payload, subject, result),
        )
        self._buffer("event", event.id, event, allocate_cursor=True)
        return event

    # ------------------------------------------------------------------
    # Committing
    # ------------------------------------------------------------------

    def commit(self) -> CommitResult:
        """Hand the buffered writes to the backend, which commits them as one."""
        self._check_open()
        if self.committed is not None:
            raise CoordinationError("this transaction has already committed")
        self.committed = self._store.commit_transaction(self)
        return self.committed

    def rollback(self) -> None:
        """Discard everything buffered here. Nothing has been written yet, so this
        is bookkeeping rather than an undo."""
        self._pending.clear()
        self._order.clear()

    def __enter__(self) -> "CoordinationTransaction":
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        """Commit on the way out, roll back if the body raised.

        A body that raised leaves nothing behind, which is what lets an operation
        be written as "read, check, write" and still be all-or-nothing."""
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        return False

    # ------------------------------------------------------------------
    # For the backends
    # ------------------------------------------------------------------

    @property
    def writes(self) -> list:
        """The buffered writes, in the order the caller made them."""
        return [self._pending[key] for key in self._order]

    @property
    def is_empty(self) -> bool:
        return not self._order

    def materialise(self, revision_of, next_cursor) -> list:
        """The buffered writes with their resulting revisions and cursors assigned.

        Called by the backend *inside* its serialisation, where the values it reads
        cannot change underneath it: `revision_of` answers the stored revision of a
        record and `next_cursor` the cursor the event stream will accept next.
        Raises `Stale` before anything is written when an `expect_revision` no
        longer holds."""
        writes = []
        cursor = next_cursor()
        for pending in self.writes:
            if pending.is_delete:
                writes.append(pending)
                continue
            current = revision_of(pending.record_type, pending.record_id)
            if pending.expect_revision is not None and current != pending.expect_revision:
                raise Stale(
                    f"{pending.record_type} {pending.record_id} is at revision {current}, "
                    f"not {pending.expect_revision}: another writer changed it first, so "
                    "this transaction wrote nothing",
                    reason="stale_revision",
                )
            if pending.allocate_cursor:
                # Appended, not placed: the cursor a caller was shown is replaced by
                # the one this commit can defend, and the object it holds is updated
                # so what it prints is what the store stores.
                pending.record.cursor = cursor
                writes.append(replace(pending, revision=current + 1, cursor=cursor))
                cursor += 1
            else:
                writes.append(replace(pending, revision=current + 1))
        return writes

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _buffer(self, record_type, record_id, record, expect_revision=None, allocate_cursor=False) -> None:
        self._check_open()
        key = (record_type, record_id)
        if key not in self._pending:
            self._order.append(key)
        self._pending[key] = PendingWrite(
            record_type=record_type,
            record_id=record_id,
            record=record,
            expect_revision=expect_revision,
            allocate_cursor=allocate_cursor,
        )

    def _check_open(self) -> None:
        if self.committed is not None:
            raise CoordinationError(
                "this transaction has already committed; build another one for more work"
            )

    def _next_event_id(self) -> str:
        taken = {event.id for event in self._store.events()}
        taken.update(
            write.record.id for write in self.writes if write.record is not None
        )
        return new_id("event", taken)

    def _provisional_cursor(self) -> int:
        """The cursor the event would take if it committed next.

        Provisional on purpose: the commit assigns the value it can defend, under
        the backend's serialisation, and updates the returned event. Two events
        buffered together are numbered in order here so their relative order is
        already right."""
        buffered = sum(1 for write in self.writes if isinstance(write.record, Event))
        return self._store.next_event_cursor() + buffered

    def _event_for_operation(self, operation_id: str, kind: str) -> Optional[Event]:
        """The event this operation already produced, if it has one."""
        for write in self.writes:
            if isinstance(write.record, Event) and write.record.operation_id == operation_id:
                if write.record.kind == kind:
                    return write.record
        for event in self._store.events():
            if event.operation_id == operation_id and event.kind == kind:
                return event
        return None


def _event_payload(payload, subject, result) -> dict:
    """The event's payload: whatever the caller passed, plus the two keys the
    events view renders when the caller has values for them."""
    merged = dict(payload or {})
    if subject is not None:
        merged[EVENT_SUBJECT_KEY] = str(subject)
    if result is not None:
        merged[EVENT_RESULT_KEY] = str(result)
    return merged


def _check_record_type(record_type: str) -> None:
    """Refuse an unknown record type by name, once, for every caller."""
    if record_type not in RECORD_TYPES:
        raise RecordError(
            f"unknown record type '{record_type}' (known: {', '.join(RECORD_TYPES)})"
        )


class CoordinationStore(ABC):
    """Attempts, claims, read observations, receipts, artifacts and events.

    Subclasses implement the storage primitives -- `records`, `get_record`,
    `put_record`, `counts`, `commit_transaction`, `revision` -- and every question
    built on them is answered here, once, so the two backends cannot disagree about
    what "an active claim" is."""

    #: Short identifier matching the sink kind whose state this store holds.
    kind: str = "?"

    #: A fault-injection seam, None in production: called with each commit boundary
    #: name (`COMMIT_STAGED`, `COMMIT_APPLIED`) as it is reached. The durability
    #: tests set it to kill a process at a boundary and prove the next operation
    #: makes the store whole; tic-b03b grows the same matrix for staged file writes.
    crash_hook: Optional[Callable[[str], None]] = None

    def _crash_point(self, name: str) -> None:
        if self.crash_hook is not None:
            self.crash_hook(name)

    # ------------------------------------------------------------------
    # Storage primitives -- implemented by each backend
    # ------------------------------------------------------------------

    @property
    @abstractmethod
    def root(self) -> str:
        """Where coordination state lives: a directory for the file backend, a
        database file for the SQLite backend."""

    @abstractmethod
    def init(self) -> None:
        """Create this backend's layout if it is missing. Idempotent, and it never
        removes or rewrites a record: re-running it must be safe on a store that
        already holds claims and receipts."""

    @abstractmethod
    def check_writable_layout(self) -> None:
        """Refuse (raising `CoordinationError`) when the layout cannot be created.

        A guard the application layer runs *before* an operation writes anything:
        failing after a partial write is how a store ends up claiming something it
        cannot support, and the durability rules say to fail before modifying bytes
        when the evidence cannot be stored."""

    def records(self, record_type: str) -> list:
        """Every stored record of `record_type`, in a deterministic order
        (by id; events by cursor).

        An unknown type is a `RecordError`, not an empty list: a typo that looked
        like "nothing happened yet" would be the worst possible answer. Checked
        once, here, so both backends refuse a wrong type identically."""
        _check_record_type(record_type)
        return self._records(record_type)

    @abstractmethod
    def _records(self, record_type: str) -> list:
        """The backend's own read of one record type (see `records`)."""

    def get_record(self, record_type: str, record_id: str) -> Record:
        """One record by id. Raises `CoordinationError` when it is not there."""
        _check_record_type(record_type)
        return self._get_record(record_type, record_id)

    @abstractmethod
    def _get_record(self, record_type: str, record_id: str) -> Record:
        """The backend's own lookup by id (see `get_record`)."""

    def find_record(self, record_type: str, record_id: str) -> Optional[Record]:
        """One record by id, or None when it is not there.

        "Is there one" is a question with a legitimate negative answer -- an
        attempt that has never run, a token nothing issued -- so it is a lookup
        rather than an error, and it is the call a writer makes inside a
        transaction before writing under an id it must not collide with."""
        _check_record_type(record_type)
        try:
            return self._get_record(record_type, record_id)
        except CoordinationError:
            return None

    def revision(self, record_type: str, record_id: str) -> int:
        """The revision counter of a record: how many writes it has taken.

        Explicit, because an optimistic write presents it: read the record, keep
        its revision, and hand it back through
        `transaction().replace_record(record, expect_revision)`. A record that is
        not there, and a record written before revisions existed, both read as 0 --
        which is the same thing to a writer, whose first write then makes it 1."""
        _check_record_type(record_type)
        return self._revision(record_type, record_id)

    @abstractmethod
    def _revision(self, record_type: str, record_id: str) -> int:
        """The backend's own revision lookup (see `revision`)."""

    def next_event_cursor(self) -> int:
        """The cursor an event appended right now would take.

        A hint rather than a promise: the commit assigns the value it can defend,
        under the backend's serialisation, so the cursor a caller sees on the event
        `append_event` returned is the committed one. Nothing may use this to
        reserve a cursor."""
        return self._next_event_cursor()

    @abstractmethod
    def _next_event_cursor(self) -> int:
        """The backend's own next-cursor answer (see `next_event_cursor`)."""

    def operation_lock(self):
        """Hold this store's cross-process mutex for the length of one file operation.

        A mutation verifies ownership, generations, read tokens and the bytes on disk and
        then changes those bytes, and the check and the change have to share one critical
        section: a lifecycle command landing between them is exactly the race the plan
        refuses to leave open (RC2 -- either the write completes first and the close records
        it, or the close wins and no observer mutates under the old token).

        The lock is the *ephemeral* kind -- an OS lock the kernel drops when its holder
        dies, held for one operation and never for a ticket's duration. The durable *claim*
        is the other kind and is never conflated with this one.

        The default is no lock, which is a statement about the backend rather than a
        convenience: a store whose records are the only shared state (SQLite: one database
        file, real transactions) has nothing extra to serialise, while the file backend --
        records beside the working tree, both reachable by a second arbite process --
        provides one. A backend that needs a lock implements it here."""
        return nullcontext()

    def transaction(self, operation_id: Optional[str] = None) -> CoordinationTransaction:
        """A store-local unit of work: records and their events, committed together.

        `operation_id` is the caller's token for the operation. Presenting one that
        already committed makes the commit a no-op, so re-running an interrupted
        operation cannot duplicate its effects. Where a caller has no operation id
        yet -- every command in this slice -- an operation is identified by what it
        writes instead."""
        return CoordinationTransaction(self, operation_id=operation_id)

    @abstractmethod
    def commit_transaction(self, transaction: CoordinationTransaction) -> CommitResult:
        """Commit `transaction`, atomically.

        Called by `CoordinationTransaction.commit`, and implemented by each backend
        because *how* the unit is made atomic is exactly what differs: SQLite opens
        a real transaction and rolls it back on failure, the file backend writes a
        commit journal, applies it, and removes it -- so a process killed at any
        point leaves either nothing or a journal the next write replays. Both must
        assign revisions and cursors inside their serialisation (through
        `transaction.materialise`) and must refuse a stale `expect_revision` before
        writing anything."""

    @abstractmethod
    def put_record(self, record: Record) -> None:
        """Store `record`, replacing a record of the same type and id.

        One record, one write, validated first: an invalid record never reaches
        storage. Making several writes arrive together is the transaction hook
        below (tic-1a75); until then a caller orders them so the last write is the
        commit point."""

    @abstractmethod
    def delete_record(self, record_type: str, record_id: str) -> None:
        """Remove one record, tolerating one that is not there.

        Deliberately not how state changes: a released claim and a finalised receipt
        are *kept* as history, and only a record that has been replaced outright --
        a workspace binding superseded by a relocated root -- is deleted. That is
        the only caller today; a later slice that needs one (migrations, tic-008f)
        gets it here rather than by reaching into a backend."""

    # ------------------------------------------------------------------
    # Derived queries -- implemented once, for every backend
    # ------------------------------------------------------------------

    @property
    def has_state(self) -> bool:
        """Whether anything has been recorded at all.

        False for a store whose coordination area exists but is empty, and for one
        that has never had coordination state -- both of which are ordinary (every
        project before its first attempt) rather than an error."""
        return any(self.records(record_type) for record_type in RECORD_TYPES)

    def info(self) -> CoordinationInfo:
        return CoordinationInfo(kind=self.kind, root=str(self.root), **self.counts())

    def active_claims(self) -> list:
        """The current claim index: every claim that currently owns a path.

        Derived by filtering the stored claims, which *is* the current-state index the
        plan asks for now that acquisition exists (tic-9b57): a path has exactly one
        claim record, whose id is derived from the workspace and the path, so a claim
        check never has to replay a log -- and the released records stay beside it as
        that path's history. The order is by record id, which is opaque; a report that
        a human reads sorts by path."""
        return [claim for claim in self.records("claim") if claim.state == CLAIM_ACTIVE]

    def claims_for_path(self, path: str) -> list:
        """Every *active* claim on `path` (normally zero or one)."""
        return [claim for claim in self.active_claims() if claim.path == path]

    def active_attempts(self, ticket_id: Optional[str] = None) -> list:
        """Active attempts, optionally for one ticket.

        The plan's "one active attempt per ticket" is an invariant of a
        *transition*, not of storage: two storage generations can briefly disagree
        while a takeover writes, so this reports what is there and the acquisition
        path (tic-cf9f) is what refuses a second one."""
        attempts = [a for a in self.records("attempt") if a.state == ATTEMPT_ACTIVE]
        if ticket_id is not None:
            attempts = [a for a in attempts if a.ticket_id == ticket_id]
        return attempts

    def get_attempt(self, attempt_id: str) -> Optional[Record]:
        """The attempt with this id, or None -- a lookup "does this attempt exist",
        which is what separates an orphaned claim from a claim naming nothing."""
        for attempt in self.records("attempt"):
            if attempt.id == attempt_id:
                return attempt
        return None

    def receipts(self) -> list:
        return self.records("receipt")

    def events(self) -> list:
        return self.records("event")

    def pending_operations(self) -> list:
        """Receipts that staged an operation and were never finalised.

        This is the fact `doctor` reports and that recovery works from; it is a
        read, so it is implemented here. Reconciling them -- deciding whether the
        bytes on disk are the before or the after version -- is the recovery
        engine's job (tic-b03b), which is why nothing here repairs anything."""
        return [receipt for receipt in self.receipts() if receipt.is_pending]

    def get_workspace(self) -> Optional[Workspace]:
        """The recorded workspace binding, if this store has one.

        Both backends hold at most one: the workspace is derived from the located
        `.arbite/` directory and the resolved sink, so a store belongs to one
        workspace at a time. A store that has never been initialised has none, and
        that is ordinary."""
        workspaces = self.records("workspace")
        if not workspaces:
            return None
        if len(workspaces) > 1:
            raise RecordError(
                f"this store holds {len(workspaces)} workspace records; a store belongs "
                "to exactly one workspace (two roots reaching one store is centralized "
                "storage, out of scope for this epic)"
            )
        return workspaces[0]

    def put_workspace(self, workspace: Workspace, txn=None) -> None:
        """Record the workspace binding, replacing any previous one.

        Replacement is the honest behaviour for a relocated root or a repointed
        store: the derived id changes with the identity, so the previous record is
        a different workspace rather than a mutation of this one, and a store keeps
        exactly one binding at a time. The superseded record is deleted rather than
        left beside the new one, because two bindings in one store is the state
        `get_workspace` has to refuse -- and refusing is worse than replacing when
        the caller is the command that establishes the binding.

        `txn` writes through a caller's transaction instead of committing each step,
        which is how an operation that replaces the binding *and* does something
        else stays one commit. Called without one, the replacement opens its own
        transaction -- so the removal and the new record still arrive together, and
        a store never holds two bindings, not even for an instant."""
        if txn is None:
            with self.transaction() as own:
                self.put_workspace(workspace, txn=own)
            return
        for existing in self.records("workspace"):
            if existing.id != workspace.id:
                txn.delete_record("workspace", existing.id)
        txn.put_record(workspace)

    # ------------------------------------------------------------------
    # Integrity
    # ------------------------------------------------------------------

    def record_problems(self, ticket_ids=None, operation_findings=None) -> list:
        """Integrity findings for the coordination records, as `Problem`s.

        One implementation for both backends, in the recovery engine, because a
        finding has to mean the same thing whichever store holds the records (see
        `coordination.recovery.findings`). Reporting *reads*: `pending_operation`
        findings are judged against the bytes on disk, and an operation whose bytes
        are exactly one of the two recorded versions is deliberately not reported --
        its repair is unambiguous and free, and `doctor` without `--fix` writes
        nothing. Only `recover()` and `doctor --fix` change anything.

        `ticket_ids` is the ticket store's own set, which is what makes "this
        attempt names a ticket that does not exist" answerable. `operation_findings`
        lets a caller that has already reconciled the pending operations pass its own
        rendering of that phase in, so a repair does not judge the same receipt twice."""
        from . import recovery

        return recovery.findings(
            self, ticket_ids=ticket_ids, operation_findings=operation_findings
        )

    # ------------------------------------------------------------------
    # Evidence bytes
    # ------------------------------------------------------------------

    def put_artifact_bytes(self, digest: str, data: bytes):
        """Store `data` as the content `digest` names, and return where it went.

        Content is stored once by digest, so two receipts that share a version share
        the bytes and an edit-then-revert keeps both versions once. Every backend
        implements this, because a mutation's evidence is not optional: the file
        backend writes a file named by the digest beside the artifact records, and the
        SQLite backend keeps the bytes in a BLOB column of its own database. Both call
        `require_storable_artifact` first, so the size limit is one rule rather than a
        per-sink temperament.

        A backend that could not store content refuses here, by name, and a mutation
        that reaches this refusal has not changed a byte of the project yet."""
        raise NotImplementedError(
            f"the '{self.kind}' coordination backend does not store artifact content, so "
            "the evidence this operation must keep cannot be written"
        )

    def get_artifact_bytes(self, digest: str) -> bytes:
        """The stored content for `digest`. Missing content is an error naming the
        digest: an artifact record whose bytes are gone is drift, not an empty file."""
        raise NotImplementedError(
            f"the '{self.kind}' coordination backend does not read artifact content"
        )

    def verify_artifact(self, digest: str) -> bytes:
        """Read `digest`'s content back and prove it *is* that content.

        Implemented once, here, because "the evidence still says what it said" has to
        mean the same thing whichever store holds it -- and because the check is a
        digest recomputation, not a backend trick. A mismatch is drift: the bytes are
        named by their digest, so content that hashes to something else is either
        corruption or somebody else's file, and neither may be served as the version a
        receipt recorded."""
        data = self.get_artifact_bytes(digest)
        actual = digest_bytes(data)
        if actual != digest:
            raise CoordinationError(
                f"artifact {digest} holds bytes with digest {short_digest(actual)}, so the "
                "stored evidence is not the version the receipt recorded"
            )
        return data

    # ------------------------------------------------------------------
    # Interfaces for later slices -- defined here, not implemented here
    # ------------------------------------------------------------------

    def recover(self, root=None) -> list:
        """Reconcile operations that were staged and never finalised.

        Every pending receipt is judged against the bytes on disk, and the two
        unambiguous answers are written: the recorded *after* version is there, so
        the operation happened and the receipt is finalised as succeeded (nothing is
        applied twice), or the recorded *before* version is there, so it did not
        happen, the staged copy is discarded and the receipt is finalised as failed.
        Bytes that match neither are left entirely alone -- receipt, staged copy and
        file -- and reported as drift, because which version is "correct" is not a
        question a recovery pass may answer.

        `root` is the project root the paths are relative to; it defaults to the
        store's own workspace binding, and a store that records none reports the
        operation as unjudgeable instead of guessing at a directory. Returns one
        `recovery.OperationJudgement` per operation looked at, in store order.

        Deliberately *not* the same thing as the commit journal the file backend
        replays by itself: finishing a half-applied multi-record commit is not a
        judgement call (the journal names the exact documents and revisions), while
        deciding what a staged file write left behind is, and this epic never guesses
        that."""
        from . import recovery

        return recovery.reconcile(self, root=root)


def open_coordination_store(sink) -> CoordinationStore:
    """The coordination store for a resolved ticket sink.

    Coordination state lives with the *store*, not with the project, so everyone
    holding the same store sees the same claims: the file sink keeps it in a
    `coordination/` directory beside its tickets (`.arbite/coordination/` for the
    default root), the SQLite sink keeps it in its own database. A sink kind with
    no coordination backend is refused by name -- the guide is meant to be trimmed
    for such a sink rather than to describe capabilities it does not have (see
    tic-6015).

    Imported lazily to keep this module free of a cycle with the backends, which
    import the interface above."""
    from .file_backend import COORDINATION_DIRNAME, FileCoordinationStore
    from .sqlite_backend import SqliteCoordinationStore

    kind = getattr(sink, "kind", None)
    if kind == "file":
        return FileCoordinationStore(Path(sink.root) / COORDINATION_DIRNAME)
    if kind == "sqlite":
        return SqliteCoordinationStore(sink.root)
    raise CoordinationError(
        f"the '{kind}' sink has no coordination backend, so claims, attempts and receipts "
        "cannot be recorded in this project"
    )
