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
- **Defined, not implemented: the transaction, revision and recovery hooks.** A
  multi-record write is *ordered* today -- the last record written is the commit
  point, so an interrupted operation leaves a recoverable prefix rather than a
  half-applied set -- but it is not atomic. Making it transactional, with a
  recoverable journal on the file backend and real transactions on SQLite, is
  tic-1a75 (C02) and tic-b03b (C05). Those methods raise `NotImplementedError`
  naming the ticket rather than pretending, and no command calls them yet.
- **Shared semantics:** `record_problems` is implemented once, from the records
  alone, so integrity means one thing on both backends. It reports; it does not
  repair. Repairing a coordination problem is the recovery engine's job
  (tic-b03b), and `doctor` exposes these findings with it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..errors import CoordinationError, RecordError
from ..sinks.base import Problem
from .records import ATTEMPT_ACTIVE, CLAIM_ACTIVE, RECORD_TYPES, Record, Workspace


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


class CoordinationStore(ABC):
    """Attempts, claims, read observations, receipts, artifacts and events.

    Subclasses implement the storage primitives -- `records`, `get_record`,
    `put_record`, `counts` -- and every question built on them is answered here,
    once, so the two backends cannot disagree about what "an active claim" is."""

    #: Short identifier matching the sink kind whose state this store holds.
    kind: str = "?"

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
        if record_type not in RECORD_TYPES:
            raise RecordError(
                f"unknown record type '{record_type}' (known: {', '.join(RECORD_TYPES)})"
            )
        return self._records(record_type)

    @abstractmethod
    def _records(self, record_type: str) -> list:
        """The backend's own read of one record type (see `records`)."""

    def get_record(self, record_type: str, record_id: str) -> Record:
        """One record by id. Raises `CoordinationError` when it is not there."""
        if record_type not in RECORD_TYPES:
            raise RecordError(
                f"unknown record type '{record_type}' (known: {', '.join(RECORD_TYPES)})"
            )
        return self._get_record(record_type, record_id)

    @abstractmethod
    def _get_record(self, record_type: str, record_id: str) -> Record:
        """The backend's own lookup by id (see `get_record`)."""

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

        Derived by filtering the stored claims rather than kept in a separate
        index, because the index the plan describes (a current-state claim set that
        a claim check does not rebuild from the log) is only worth its own file
        once acquisition exists (tic-9b57); the claim set itself is small, and the
        released records must stay regardless."""
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

    def put_workspace(self, workspace: Workspace) -> None:
        """Record the workspace binding, replacing any previous one.

        Replacement is the honest behaviour for a relocated root or a repointed
        store: the derived id changes with the identity, so the previous record is
        a different workspace rather than a mutation of this one, and a store keeps
        exactly one binding at a time. The superseded record is deleted rather than
        left beside the new one, because two bindings in one store is the state
        `get_workspace` has to refuse -- and refusing is worse than replacing when
        the caller is the command that establishes the binding."""
        for existing in self.records("workspace"):
            if existing.id != workspace.id:
                self.delete_record("workspace", existing.id)
        self.put_record(workspace)

    # ------------------------------------------------------------------
    # Integrity
    # ------------------------------------------------------------------

    def record_problems(self, ticket_ids=None) -> list:
        """Integrity findings for the coordination records, as `Problem`s.

        Checks that mean the same thing on every backend, computed from the
        records alone (plus the ticket ids when a caller can provide them, which is
        what makes "this attempt names a ticket that does not exist" answerable).
        Report-only: every finding here needs a judgement call about whether the
        bytes on disk are the before or the after version, and guessing is exactly
        what the durability rules forbid -- `arbite doctor` surfaces these and
        repairs only what is unambiguous once tic-b03b lands."""
        problems = []
        attempts = {attempt.id: attempt for attempt in self.records("attempt")}
        workspace = None
        try:
            workspace = self.get_workspace()
        except RecordError as e:
            problems.append(Problem("multiple_workspaces", str(e)))
        workspace_id = workspace.id if workspace is not None else None

        for claim in self.records("claim"):
            attempt = attempts.get(claim.attempt_id)
            if attempt is None:
                if claim.state == CLAIM_ACTIVE:
                    problems.append(
                        Problem(
                            "claim_without_attempt",
                            f"claim {claim.id} on {claim.path} names attempt "
                            f"{claim.attempt_id}, which does not exist in this store",
                            ticket_id=claim.ticket_id,
                        )
                    )
            elif claim.state == CLAIM_ACTIVE and attempt.state != ATTEMPT_ACTIVE:
                problems.append(
                    Problem(
                        "orphaned_claim",
                        f"claim {claim.id} on {claim.path} names attempt {claim.attempt_id}, "
                        f"which is not active (attempt is '{attempt.state}')",
                        ticket_id=claim.ticket_id,
                    )
                )
            if workspace_id is not None and claim.workspace_id != workspace_id:
                problems.append(
                    Problem(
                        "claim_for_another_workspace",
                        f"claim {claim.id} on {claim.path} names workspace "
                        f"{claim.workspace_id}, but this store records {workspace_id}",
                        ticket_id=claim.ticket_id,
                    )
                )

        known_tickets = set(ticket_ids) if ticket_ids is not None else None
        for attempt in self.records("attempt"):
            if workspace_id is not None and attempt.workspace_id != workspace_id:
                problems.append(
                    Problem(
                        "attempt_for_another_workspace",
                        f"attempt {attempt.id} names workspace {attempt.workspace_id}, but this "
                        f"store records {workspace_id}",
                        ticket_id=attempt.ticket_id,
                    )
                )
            if known_tickets is not None and attempt.ticket_id not in known_tickets:
                problems.append(
                    Problem(
                        "attempt_without_ticket",
                        f"attempt {attempt.id} names ticket {attempt.ticket_id}, which is not "
                        "in the ticket store",
                        ticket_id=attempt.ticket_id,
                    )
                )

        for receipt in self.pending_operations():
            problems.append(
                Problem(
                    "pending_operation",
                    f"{receipt.id} staged a {receipt.kind} of "
                    f"{', '.join(receipt.paths) or '(no path)'} and was not finalized",
                    ticket_id=receipt.ticket_id,
                )
            )

        cursors = {}
        for event in self.records("event"):
            cursors.setdefault(event.cursor, []).append(event.id)
        for cursor, event_ids in sorted(cursors.items()):
            if len(event_ids) > 1:
                problems.append(
                    Problem(
                        "duplicate_event_cursor",
                        f"cursor {cursor} is claimed by {', '.join(sorted(event_ids))} -- a "
                        "resumed poll would skip or replay one of them",
                    )
                )

        return problems

    # ------------------------------------------------------------------
    # Evidence bytes
    # ------------------------------------------------------------------

    def put_artifact_bytes(self, digest: str, data: bytes):
        """Store `data` as the content `digest` names, and return where it went.

        Content is stored once by digest, so two receipts that share a version share
        the bytes and an edit-then-revert keeps both versions once. Implemented by
        the file backend (a file named by the digest, beside the artifact records);
        the SQLite backend refuses it for now rather than inventing a second answer
        -- whether content lives in a BLOB column or a sidecar file is a storage
        decision the change-receipt slice makes once, with verification
        (tic-7c42)."""
        raise NotImplementedError(
            f"the '{self.kind}' coordination backend does not store artifact content yet "
            "(tic-7c42 decides how and verifies it)"
        )

    def get_artifact_bytes(self, digest: str) -> bytes:
        """The stored content for `digest`. Missing content is an error naming the
        digest: an artifact record whose bytes are gone is drift, not an empty file."""
        raise NotImplementedError(
            f"the '{self.kind}' coordination backend does not read artifact content yet "
            "(tic-7c42)"
        )

    # ------------------------------------------------------------------
    # Interfaces for later slices -- defined here, not implemented here
    # ------------------------------------------------------------------

    def transaction(self):
        """A store-local unit of work for a multi-record operation.

        **Not implemented yet: tic-1a75 (C02) implements it** -- a recoverable
        journal plus process serialization for the file backend, and real
        transactions for SQLite. Until then an application-layer operation orders
        its writes so the last one is the commit point, which leaves a recoverable
        prefix rather than a half-applied set, and says so rather than implying a
        guarantee it does not have."""
        raise NotImplementedError(
            "coordination transactions are not implemented yet (tic-1a75): a "
            "multi-record operation is ordered so its last write is the commit point"
        )

    def revision(self, record_type: str, record_id: str) -> int:
        """The revision counter of a record, for an optimistic write.

        **Not implemented yet: tic-1a75 (C02).** Every record carries the *schema*
        revision it was written under (see `records.COORDINATION_SCHEMA_REVISION`);
        a per-record counter a writer must present is part of the transactional
        store, so that two writers cannot lose each other's fields."""
        raise NotImplementedError(
            "record revisions are not implemented yet (tic-1a75): records carry their "
            "schema revision, but not a per-record counter"
        )

    def recover(self) -> list:
        """Reconcile operations that were staged and never finalised.

        **Not implemented yet: tic-b03b (C05)** owns the intent journal and the
        recovery engine. `pending_operations()` below reports what such a run would
        have to look at; nothing here inspects or changes bytes."""
        raise NotImplementedError(
            "recovery is not implemented yet (tic-b03b): 'advance with care' means "
            "reporting, never guessing which version is correct"
        )


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
