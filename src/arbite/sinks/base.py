"""The sink interface: the standardized CRUD surface every ticket store implements.

CRUD is the spine -- Create (`create`), Read (`get`/`get_many`/`exists`/`query`/
`notes`), Update (`update`/`add_note`), Delete (`remove`) -- plus the three
operations that plain CRUD cannot express honestly, each named rather than hidden:

- `update(ticket, expect=...)`, where `Expect` is a compare-and-swap token. A
  claim, a state transition and a plain edit are all "update this ticket", and
  the only thing that differs is what the caller is willing to overwrite. Making
  the expectation an argument (instead of a separate `claim()` method) means both
  sinks implement *one* write path, and a claim cannot bypass it.
- `bucket`/`move_to_bucket`, because "where is this ticket filed" is storage's
  business, not the schema's: it is a folder for the file sink and a column for
  SQLite, and it deliberately is not a ticket field.
- `check(fix)`, because the invariants worth checking are partly storage-specific
  (folder/frontmatter drift, leftover temp files) and partly universal (invalid
  field values, deadlocked dependencies). The universal half lives here in
  `common_problems()`; a sink implements `storage_problems()` for its own half.

What is *not* in this interface is as deliberate: no paths, no folder names, no
`status_dir()`. A status transition is `update()` with a new status, and how the
store then represents the change -- moving a file, writing a row -- is the sink's
private concern. That is precisely what let the file sink keep its
folder-follows-status behavior without any other code knowing about it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Optional

from .. import coordination, graph, schema
from ..artifacts import DEFAULT_MAX_ARTIFACT_BYTES, DEFAULT_MEDIA_TYPE
from ..errors import Conflict, InvalidRecord, TicketError, UnsupportedCoordination
from ..locking import DEFAULT_OPERATION_LOCK_TIMEOUT, NULL_OPERATION_LOCK, OperationLock
from ..query import TicketQuery, apply_limit, bucket_matches, resolve_id, sort_tickets
from ..schema import Ticket

# Sentinel for `Expect`: "do not check this field" is a different instruction
# from "check that it is None" (an unassigned ticket), and only an explicit
# sentinel can tell them apart.
UNSET = object()


@dataclass(frozen=True)
class Expect:
    """The compare-and-swap token for an update.

    `update(ticket, expect=Expect(status="open", assignee=None))` means "write
    this only if it is still open and unassigned" -- a claim. An unset field is
    not checked. Sinks enforce this *atomically*, which is the whole point: the
    file sink holds its coarse coordination lock across read -> check -> write,
    SQLite checks inside its `BEGIN IMMEDIATE` transaction, so two agents racing
    produce exactly one winner and one `Conflict`.

    `revision` is the optional third, weaker-than-it-sounds check: a status or
    assignee token cannot see a concurrent edit to an *unrelated* field, so a
    caller that wants to detect any intervening write passes the revision it
    read (see `TicketSink.revision`). It is enforced **only** when the caller
    passes it explicitly: an unset revision checks nothing, which is what keeps
    a plain `update()` last-write-wins, exactly as documented.
    """

    status: object = UNSET
    assignee: object = UNSET
    revision: object = UNSET

    @classmethod
    def unclaimed(cls, status: str = "open") -> "Expect":
        """The claim expectation: still in this status and owned by nobody."""
        return cls(status=status, assignee=None)

    def violated_by(self, ticket: Ticket) -> Optional[str]:
        """Why `ticket` does not satisfy this expectation, or None if it does."""
        if self.status is not UNSET and ticket.status != self.status:
            return f"status is '{ticket.status}', expected '{self.status}'"
        if self.assignee is not UNSET and ticket.assignee != self.assignee:
            got = ticket.assignee if ticket.assignee else "unassigned"
            want = self.assignee if self.assignee else "unassigned"
            return f"assignee is {got}, expected {want}"
        if self.revision is not UNSET:
            current = getattr(ticket, "revision", None)
            if current != self.revision:
                got = "absent" if current is None else current
                return f"revision is {got}, expected {self.revision}"
        return None


def enforce_expect(ticket: Ticket, expect: Optional[Expect]) -> None:
    """Raise `Conflict` unless `ticket` satisfies `expect`.

    A helper rather than a base-class responsibility because it must be called by
    each sink *inside* its own atomic write, not before it -- a check performed
    outside the critical section would just be a race with extra steps."""
    if expect is None:
        return
    reason = expect.violated_by(ticket)
    if reason is not None:
        raise Conflict(
            f"ticket {ticket.id} is not in the expected state ({reason}); "
            f"re-read it with 'arbite show {ticket.id}' and retry"
        )


def revision_for_insert(value) -> int:
    """The revision a newly stored ticket gets.

    A brand-new ticket starts at 1. An *explicit* incoming revision is preserved
    rather than reset, because `arbite migrate` copies tickets between sinks and
    the markdown form has to stay byte-identical: resetting a revision on insert
    would rewrite every migrated ticket's frontmatter. A malformed value is
    treated as absent, since a revision is store bookkeeping, not data."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return int(value)
    return 1


@dataclass
class Problem:
    """One integrity finding, as `arbite doctor` reports it.

    `kind` is a stable identifier (agents branch on it), `location` is where the
    ticket is in this sink, and `fixed` records whether `--fix` repaired it in
    this run."""

    kind: str
    detail: str
    ticket_id: Optional[str] = None
    location: Optional[str] = None
    fixed: bool = False

    def to_dict(self) -> dict:
        """The doctor `--json` shape. The `path` key is kept for compatibility
        with the pre-sink contract even though a non-file sink has no path."""
        return {
            "kind": self.kind,
            "detail": self.detail,
            "id": self.ticket_id,
            "path": self.location,
            "fixed": self.fixed,
        }


@dataclass(frozen=True)
class SinkInfo:
    """What a sink is, in storage-neutral terms. `docs.py` renders the
    agent-facing guide from this, so the guide can only claim what is true of the
    active sink (notably `status_is_location`)."""

    kind: str
    root: str
    status_is_location: bool
    supports_buckets: bool
    ticket_count: int = 0
    status_counts: dict = field(default_factory=dict)
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "root": self.root,
            "status_is_location": self.status_is_location,
            "supports_buckets": self.supports_buckets,
            "ticket_count": self.ticket_count,
            "status_counts": dict(self.status_counts),
            "details": dict(self.details),
        }


class TicketSink(ABC):
    """A ticket store. Implementations: `FileSink`, `SqliteSink`.

    Subclasses implement the storage primitives (`ids`, `read`, `insert`,
    `update`, `remove`, `query`, `bucket`, `move_to_bucket`, `location`,
    `notes`, `storage_problems`); the verbs built on them (`get`, `create`,
    `add_note`, `check`, `describe`) are implemented once here so every sink
    answers identically to questions the schema already answers."""

    #: Short identifier used by config and shown by `arbite sink info`.
    kind: str = "?"
    #: True when a ticket's status is implied by *where* it is stored, which the
    #: docs use to decide whether to explain the folder rule.
    status_is_location: bool = False
    #: True when the sink can file a ticket somewhere other than its status
    #: location (the file sink's wishlist/ and planning/ folders).
    supports_buckets: bool = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @abstractmethod
    def init(self) -> None:
        """Create the store if it does not exist. Idempotent: `arbite init` is
        safe to re-run and must never destroy data."""

    @property
    @abstractmethod
    def root(self) -> str:
        """Human-readable location of the store (a directory or a database
        file), used in messages and in `SinkInfo`."""

    def describe(self) -> SinkInfo:
        tickets = self.query(TicketQuery())
        counts = {}
        for t in tickets:
            counts[t.status] = counts.get(t.status, 0) + 1
        return SinkInfo(
            kind=self.kind,
            root=str(self.root),
            status_is_location=self.status_is_location,
            supports_buckets=self.supports_buckets,
            ticket_count=len(tickets),
            status_counts=counts,
            details=self.details(),
        )

    def details(self) -> dict:
        """Extra sink-specific facts for `arbite sink info --json`. Empty by
        default; a database sink reports its schema version and path here."""
        return {}

    # ------------------------------------------------------------------
    # Storage primitives -- implemented by each sink
    # ------------------------------------------------------------------

    @abstractmethod
    def ids(self) -> list:
        """Every ticket id in the store, sorted. The inventory that id
        resolution, `new_id()` and the graph views are built from."""

    @abstractmethod
    def read(self, ticket_id: str) -> Ticket:
        """The ticket with this exact id. Raises TicketNotFound. Must not be
        called with a wildcard term -- `get()` resolves first."""

    @abstractmethod
    def insert(self, ticket: Ticket) -> Ticket:
        """Create a ticket. Raises Conflict if the id is already taken."""

    @abstractmethod
    def update(self, ticket: Ticket, expect: Optional[Expect] = None) -> Ticket:
        """Write a changed ticket back, honouring `expect` atomically.

        The one write path for every kind of change: a field edit, a status
        transition (the file sink relocates, the database sink does not) and a
        claim all arrive here. Raises Conflict when `expect` is not satisfied,
        TicketNotFound when the ticket is gone."""

    @abstractmethod
    def remove(self, ticket_id: str) -> None:
        """Delete a ticket outright. Irreversible, which is why the CLI gates it
        behind `--force`; the API itself does not second-guess the caller."""

    @abstractmethod
    def query(self, q: TicketQuery) -> list:
        """Every ticket matching `q`, in `q.order`, capped by `q.limit`.

        A sink may push predicates into its own storage (SQLite builds SQL), but
        must return exactly what the reference predicate
        `TicketQuery.matches()` selects -- the conformance suite asserts this."""

    @abstractmethod
    def bucket(self, ticket_id: str) -> Optional[str]:
        """Where the ticket is filed beyond its status: None when the sink keeps
        it at its status location, otherwise the bucket name ('' is the root)."""

    @abstractmethod
    def move_to_bucket(self, ticket_id: str, bucket: Optional[str]) -> Ticket:
        """File a ticket in `bucket`, or with None return it to its status
        location. Changes no field -- this is filing, not a state change."""

    @abstractmethod
    def location(self, ticket_id: str) -> str:
        """Where this ticket lives, as text. A path for the file sink, an
        identifier for a database sink -- always something a human can act on,
        and what `--json` reports as `path`."""

    def storage_locations(self, ticket_id: str) -> list:
        """Every location holding this id. Only differs from `[location(id)]`
        when a store can hold the same id twice, which is exactly the
        duplicate-id case `doctor` reports."""
        return [self.location(ticket_id)]

    def location_map(self, tickets) -> dict:
        """`{ticket_id: location}` for a batch of tickets.

        A batch method rather than a loop of `location()` in the caller because a
        sink that has to scan to answer (the file sink walks the tree) can then do
        it once: `--json` on a listing asks for every ticket's location at the same
        time, and a per-row scan would make that quadratic."""
        return {t.id: self.location(t.id) for t in tickets}

    @abstractmethod
    def notes(self, ticket_id: str) -> list:
        """The ticket's `## Notes` entries, in body order, as `schema.Note`.

        Implementations may parse the body each time (the reference) or read a
        derived index; either way the answer must match
        `schema.parse_notes(ticket.body)`, which is what makes an index safe."""

    @abstractmethod
    def storage_problems(self, fix: bool = False) -> list:
        """Integrity problems specific to this storage, already repaired when
        `fix` is true. The universal checks are added by `check()`."""

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def get(self, ticket_id: str, unique: bool = False) -> Ticket:
        """Resolve a wildcard term and return the ticket.

        An exact id wins outright; with `unique=True` an ambiguous term raises
        AmbiguousTicketId (mutating commands pass it, because guessing writes to
        the wrong ticket) while read-only callers get the first match."""
        return self.read(resolve_id(self.ids(), ticket_id, unique=unique))

    def get_many(self, ticket_ids: Iterable[str]) -> list:
        return [self.read(tid) for tid in ticket_ids]

    def exists(self, ticket_id: str) -> bool:
        return ticket_id in set(self.ids())

    def render(self, ticket: Ticket) -> str:
        """The ticket's canonical text. Implemented once, from the schema,
        because the text form belongs to the ticket and not to the store: this is
        what makes `arbite show`, `arbite migrate` and the byte-identical file
        round-trip possible across sinks."""
        return ticket.to_markdown()

    def query_one(self, q: TicketQuery) -> Optional[Ticket]:
        """The first ticket matching `q`, or None. Convenience for the many
        commands that want "the next one" without slicing a list themselves."""
        rows = self.query(q.evolve(limit=1))
        return rows[0] if rows else None

    # ------------------------------------------------------------------
    # Create verbs
    # ------------------------------------------------------------------

    def new_id(self) -> str:
        """A fresh id not present in the store. A racing creator can still mint
        the same id; `create()` turns that into a Conflict so the caller can
        simply try again rather than corrupting the store."""
        return schema.gen_id(set(self.ids()))

    def create(self, ticket: Ticket) -> Ticket:
        """Store a new ticket. Raises Conflict if its id is already taken."""
        if self.exists(ticket.id):
            raise Conflict(
                f"ticket {ticket.id} already exists in the {self.kind} sink; "
                "ticket ids are never reused"
            )
        return self.insert(ticket)

    # ------------------------------------------------------------------
    # Update verbs
    # ------------------------------------------------------------------

    def add_note(self, ticket_id: str, agent: str, message: str, note_date: Optional[str] = None) -> Ticket:
        """Append a timestamped, attributed note and bump `updated`.

        The note text is composed by the schema so the entry format stays
        identical to what the file sink has always written. This is a
        read-modify-write with no expectation: like the pre-sink implementation,
        two concurrent notes on one ticket resolve last-write-wins rather than
        being merged. Ticket *state* changes use `expect` and cannot race."""
        ticket = self.get(ticket_id, unique=True)
        schema.append_note(ticket, agent, message, note_date=note_date)
        ticket.updated = note_date or schema.now()
        return self.update(ticket)

    # ------------------------------------------------------------------
    # Integrity
    # ------------------------------------------------------------------

    def check(self, fix: bool = False) -> list:
        """Every integrity problem in the store: the universal checks plus this
        sink's storage-specific ones. Repairing is best-effort and only for the
        unambiguous cases -- anything needing a judgement call is reported.

        When the sink implements the shared-directory contract, the coordination
        doctor's store checks are appended too. The import is lazy (it would be a
        cycle at module load time) and the call is guarded by `is_initialised()`
        inside the doctor, so a legacy store with no coordination state contributes
        nothing and is never conjured into having any."""
        problems = common_problems(self.query(TicketQuery(buckets=("*",))), self)
        problems.extend(self.storage_problems(fix=fix))
        if self.coordination() is not None:
            from ..coordination_doctor import coordination_problems

            problems.extend(coordination_problems(self, fix=fix))
        return problems

    # ------------------------------------------------------------------
    # Shared-directory coordination (optional capability)
    # ------------------------------------------------------------------

    def revision(self, ticket_id: str) -> Optional[int]:
        """The ticket's explicit store-local revision, or None when there is none.

        Revisions are bumped by both sinks on every successful insert (to 1) and
        update (stored + 1), and a legacy ticket written before revisions existed
        has none -- which is why this returns `Optional[int]` rather than
        inventing a 0. `Expect(revision=N)` compares against this value."""
        return getattr(self.get(ticket_id), "revision", None)

    def coordination(self) -> Optional["CoordinationStore"]:
        """This sink's coordination store, or None when it does not implement the
        shared-directory contract.

        Deliberately separate from ticket CRUD and deliberately not abstract: the
        coordination surface (workspaces, attempts, claims, receipts, events) is
        an addition to a sink, not a requirement of the ticket interface, so a
        sink that has not implemented it returns None and existing commands keep
        working. Both shipped sinks now implement it with the same semantics --
        the file sink with a write-ahead journal and a coarse process lock, SQLite
        with real transactions -- and neither secretly depends on the other.
        The returned store is self-initialising: it creates its directories or
        tables lazily on first use, so merely asking for it touches nothing."""
        return None


def common_problems(tickets: list, sink: Optional["TicketSink"] = None) -> list:
    """Integrity problems that mean the same thing in every sink.

    These are the invariants no command enforces because they can only be broken
    from *outside* arbite: a hand-edited field, a dependency on a ticket that was
    deleted, a loop of tickets waiting on each other. Storage-specific findings
    (folder drift, temp files, a stale note index) are a sink's own business.

    Cross-ticket checks need the whole set, so they are computed here rather than
    per ticket. `sink` is optional and only used to say *where* a duplicated id
    was found, which is the one finding whose value is entirely in its
    locations."""
    problems = []

    by_id = {}
    duplicates = {}
    for t in tickets:
        if t.id in by_id:
            duplicates.setdefault(t.id, 0)
            duplicates[t.id] += 1
        else:
            by_id[t.id] = t
    for tid, extra in sorted(duplicates.items()):
        where = ""
        if sink is not None:
            locations = sink.storage_locations(tid)
            if locations:
                where = f": {', '.join(locations)}"
        problems.append(
            Problem(
                "duplicate_id",
                f"{extra + 1} tickets share id {tid}{where} (resolve by hand -- arbite "
                "cannot know which is current)",
                ticket_id=tid,
            )
        )

    for t in tickets:
        for prop in ("status", "type", "tier"):
            value = getattr(t, prop, None)
            if value is None or schema.is_placeholder(value):
                # A TODO placeholder is pending triage work, not corruption.
                continue
            try:
                schema.validate_field(prop, str(value))
            except TicketError as e:
                problems.append(Problem("invalid_field", str(e), ticket_id=t.id))
        if t.priority is not None:
            try:
                schema.validate_field("priority", str(t.priority))
            except TicketError as e:
                problems.append(Problem("invalid_field", str(e), ticket_id=t.id))
        for date_field in schema.DATE_FIELDS:
            value = getattr(t, date_field, None)
            if value and not schema.DATE_PATTERN.match(str(value)):
                problems.append(
                    Problem(
                        "invalid_field",
                        f"{date_field} is not a valid date/timestamp "
                        f"(YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS): '{value}'",
                        ticket_id=t.id,
                    )
                )

        for dep in t.depends_on:
            if dep not in by_id:
                problems.append(
                    Problem(
                        "dangling_dependency",
                        f"depends_on '{dep}', which is not a known ticket "
                        "(readiness silently ignores it, so this ticket can look "
                        "workable when its real prerequisite is gone)",
                        ticket_id=t.id,
                    )
                )
        if t.id in t.depends_on:
            problems.append(Problem("self_dependency", "depends on itself", ticket_id=t.id))

        if t.status == "in_progress" and not t.assignee:
            problems.append(
                Problem(
                    "in_progress_unassigned",
                    "in_progress but has no assignee -- nobody is accountable for it and "
                    "'list next' will never offer it; release it or claim it",
                    ticket_id=t.id,
                )
            )
        if t.status == "blocked" and not t.blocked_by:
            problems.append(
                Problem(
                    "blocked_without_reason",
                    "blocked but blocked_by is empty -- nothing records what is stalling it",
                    ticket_id=t.id,
                )
            )
        if t.status == "closed" and not t.closed:
            problems.append(
                Problem("closed_without_date", "closed ticket has no 'closed' date", ticket_id=t.id)
            )
        if t.status != "closed" and t.closed:
            problems.append(
                Problem(
                    "closed_date_on_open_ticket",
                    f"not closed but has a 'closed' date of {t.closed}",
                    ticket_id=t.id,
                )
            )

    # Cycles are checked over the whole graph: an unsatisfiable loop means every
    # ticket in it is permanently unworkable, however it is filtered.
    for cycle in graph.live_cycles(by_id):
        problems.append(
            Problem(
                "dependency_cycle",
                "dependency cycle -- none of these can ever become workable: "
                + " -> ".join(cycle + [cycle[0]]),
                ticket_id=cycle[0],
            )
        )

    return problems


class CoordinationTransaction(ABC):
    """One serialized, multi-record unit of work against a coordination store.

    Every shared-directory operation that touches more than one record -- a claim
    plus its event plus the attempt's activity time, a write plus its receipt and
    its artifacts -- runs inside a transaction, so a crash leaves all of the
    store-local state or none of it. The seam is deliberately a coarse record
    store (`put`/`get`/`find`/`append_event`) rather than an SQL surface: how the
    records are laid out is the sink's business, which is exactly what lets the
    file sink journal while SQLite uses real transactions.

    This is an interface for later slices; it implements no storage. `commit` and
    `rollback` are explicit so a caller can inspect before deciding, and the
    context-manager form rolls back on an exception.
    """

    @abstractmethod
    def put(self, record, *, expect_revision: Optional[int] = None) -> object:
        """Store (insert or replace) one coordination record, keyed by
        `(record.kind, record.record_id)`. Returns the stored record.

        Every stored record carries an explicit, store-local revision, monotonic
        per `(kind, record_id)`: 1 on first store and +1 on every successful put.
        When `expect_revision` is not None and does not equal the current stored
        revision, raise `errors.CoordinationConflict` (retryable, with the
        kind/record_id/expected/current in `details`) and write nothing. A `None`
        expectation is last-write-wins, exactly as an unguarded ticket update is.
        """

    @abstractmethod
    def get(self, kind: str, record_id: str):
        """The record of `kind` with this id, or None."""

    @abstractmethod
    def find(self, kind: str, **fields) -> list:
        """Records of `kind` whose attributes equal every given field. The one
        query primitive, kept intentionally small: richer discovery is built on
        top by the application layer, not pushed into each sink."""

    @abstractmethod
    def append_event(self, event) -> object:
        """Append an event, assigning its per-store monotonic cursor if unset.

        Two parts of the contract a sink must honour, both of which make retries
        safe: the cursor is per store, monotonic and unique (assigned under the
        sink's own serialization), and appending an event whose `id` *or* whose
        non-None `operation_id` is already stored returns the existing event and
        appends nothing. Append-only is an application contract: a store exposes
        no event deletion, and this is not a claim of tamper-proofing."""

    @abstractmethod
    def commit(self) -> None:
        """Make every change in this transaction durable together."""

    @abstractmethod
    def rollback(self) -> None:
        """Discard every change in this transaction, leaving prior state intact."""

    # -- concrete helpers (no storage of their own) ------------------------

    def revision_of(self, kind: str, record_id: str) -> int:
        """The current stored revision of `(kind, record_id)`, 0 when absent.

        The base implementation cannot answer -- a revision is a property of the
        sink's storage, not of the record -- so it raises. A sink with revisions
        (both shipped sinks) overrides it; the *contract* is fixed here so the
        application layer and the conformance suite can rely on 0 meaning
        "absent"."""
        raise UnsupportedCoordination(
            f"this coordination transaction cannot report revisions for {kind} "
            f"{record_id}; revision_of() must be overridden by a sink that stores them"
        )

    def put_if_absent(self, record):
        """`(stored_record, created)` -- store `record` only if it is not there.

        This is the operation-id dedup primitive: because
        `OperationReceipt.id` *is* the operation id, a retry of the same operation
        id finds the original receipt and writes nothing (`created=False`), so a
        replay can never double-apply an operation's evidence."""
        existing = self.get(record.kind, record.record_id)
        if existing is not None:
            return existing, False
        return self.put(record), True

    def append_event_once(self, event):
        """Append `event` unless its id or its operation id is already stored.

        Deduplication by id and by non-None `operation_id` is what lets a retry of
        the same logical operation leave exactly one event behind: the retried
        call builds a fresh event object, but the operation id points back at the
        original, which is returned instead of appending a second event."""
        existing = self.get("event", event.id)
        if existing is not None:
            return existing
        operation_id = getattr(event, "operation_id", None)
        if operation_id:
            matches = self.find("event", operation_id=operation_id)
            if matches:
                return _lowest_cursor(matches)
        return self.append_event(event)

    def __enter__(self) -> "CoordinationTransaction":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        return False


def _lowest_cursor(events: list):
    """The event a dedup lookup should return when several match: the first one
    appended, so a retry gets the *original* rather than whichever the sink
    happened to list first."""
    return sorted(
        events, key=lambda e: (getattr(e, "cursor", None) is None, getattr(e, "cursor", None) or 0)
    )[0]


class CoordinationStore(ABC):
    """The storage surface the shared-directory application layer requires.

    Separate from `TicketSink` because a sink implements it incrementally:
    `TicketSink.coordination()` returns None until a sink does, and no existing
    ticket command depends on it. Later slices add this to both `FileSink` (a
    process-safe journal plus a coarse local coordination lock that survives
    process death without inventing an agent-staleness policy) and `SqliteSink`
    (real transactions for store-local state), with identical semantics.

    Recovery is inspection-only here. `recover_pending` reports incomplete
    operations; it never repairs bytes, never runs on a timer, and never infers
    that a stopped worker is dead. Nothing in this interface holds a lock for a
    ticket's whole duration: durable claims do that job.
    """

    @abstractmethod
    def bind_store(self, binding) -> object:
        """Record `binding` as the workspace's authoritative store.

        Idempotent for an identical binding; raises `StoreBindingConflict` when a
        different sink or location is already authoritative for the workspace,
        because one workspace cannot safely coordinate against two stores."""

    @abstractmethod
    def store_binding(self, workspace_id: str):
        """The authoritative `StoreBinding` for a workspace, or None."""

    @abstractmethod
    def transaction(self, write: bool = True) -> "CoordinationTransaction":
        """Open a serialized transaction.

        Concurrent transactions must serialize so a claim's check-and-insert
        cannot interleave; `write=False` may return a read-only view. A
        transaction is not held for an agent's whole ticket duration."""

    @abstractmethod
    def recover_pending(self, workspace_id: str) -> list:
        """`coordination.RecoveryReport` entries for incomplete operations in a
        workspace, for a caller (a later `doctor`) to inspect and reconcile.

        Inspection only: it must not repair anything, and it must not hide an
        unattributed journal from a caller who is looking for one."""

    def contract_version(self) -> int:
        """The coordination contract this store implements."""
        return coordination.CONTRACT_VERSION

    # -- content-addressed artifacts (planning key C05) -------------------
    #
    # Concrete, not abstract, on purpose: a store that predates the artifact
    # journal (or a lightweight in-memory store in a test) still satisfies the
    # store contract and simply refuses to store evidence, which the mutation
    # engine turns into "fail before modifying bytes". Both shipped sinks override
    # these with real content-addressed storage.

    def artifact_limit(self) -> int:
        """Maximum size (bytes) of one artifact this store will accept."""
        return DEFAULT_MAX_ARTIFACT_BYTES

    def store_artifact_bytes(self, data: bytes, *, media_type: str = DEFAULT_MEDIA_TYPE):
        """Store content once by digest; return its `Artifact` descriptor.

        The blob is content-addressed: identical bytes are stored once and the
        descriptor's id is derived from the digest. The `Artifact` *record* is
        persisted by the caller (the mutation engine writes it together with the
        operation intent, so record and intent commit as one unit).
        """
        raise UnsupportedCoordination(
            f"the {getattr(self, 'kind', '?')} coordination store does not implement "
            "content-addressed artifact storage, so mutation evidence cannot be "
            "recorded; refusing the operation before modifying any bytes"
        )

    def read_artifact_bytes(self, digest: str) -> Optional[bytes]:
        """Stored content for `digest`, or None when absent.

        Implementations must verify the content against `digest` before returning
        it and raise `errors.ArtifactCorrupt` on a mismatch."""
        raise UnsupportedCoordination(
            f"the {getattr(self, 'kind', '?')} coordination store does not implement "
            "artifact storage"
        )

    def has_artifact(self, digest: str) -> bool:
        """True when content for `digest` is stored."""
        raise UnsupportedCoordination(
            f"the {getattr(self, 'kind', '?')} coordination store does not implement "
            "artifact storage"
        )

    # -- the coarse operation lock (planning key C05) ---------------------

    def operation_lock_path(self) -> Optional[str]:
        """Path of this store's coarse operation lock, or None when it has none.

        The default is None, which yields a no-op lock: a store that does not
        implement cross-process operation serialization says so by omission rather
        than pretending. Both shipped sinks return a real lock-file path.
        """
        return None

    def operation_lock(self, timeout: Optional[float] = None):
        """A coarse, process-safe lock serializing filesystem operations.

        Held for one operation (intent -> apply -> receipt), never for an agent's
        whole ticket duration. A `flock`-backed lock is released by the operating
        system if the process dies, so no stale-lock policy is needed.
        """
        path = self.operation_lock_path()
        if path is None:
            return NULL_OPERATION_LOCK
        return OperationLock(
            path, timeout=DEFAULT_OPERATION_LOCK_TIMEOUT if timeout is None else timeout
        )

    def event_log(self, category: Optional[str] = None, after_cursor: Optional[int] = None) -> list:
        """The store's events, sorted by cursor, optionally filtered.

        A concrete convenience over `find("event", ...)`: it exists so a caller can
        ask "what happened after cursor N" without knowing how a sink lays events
        out. It opens a short read-only transaction rather than reaching into
        storage, which keeps the one-query-primitive rule (`find`) intact."""
        with self.transaction(write=False) as tx:
            events = list(tx.find("event"))
        if category is not None:
            events = [e for e in events if e.category == category]
        if after_cursor is not None:
            events = [e for e in events if e.cursor is not None and e.cursor > after_cursor]
        return sorted(events, key=lambda e: (e.cursor is None, e.cursor or 0))

    def query_events(
        self,
        *,
        after_cursor: Optional[int] = None,
        limit: Optional[int] = None,
        categories: Optional[Iterable[str]] = None,
        kinds: Optional[Iterable[str]] = None,
        subject_ids: Optional[Iterable[str]] = None,
    ) -> Dict[str, Any]:
        """One ordered page of this store's durable event stream (planning key B06).

        The public "what happened after cursor N" query, and the only thing the
        event/progress services call. Returns::

            {"events": [Event, ...],   # ordered by cursor, at most `limit`
             "next_cursor": int,       # resume *after* this cursor
             "has_more": bool,         # the page was cut short by `limit`
             "scanned": int}           # events examined after `after_cursor`

        `next_cursor` is the cursor of the last event the page *consumed*, never
        merely the last one it returned: with more matching events behind the cut
        it is the last returned cursor (so nothing is skipped), and otherwise it is
        the store's newest cursor at read time (so a filter does not re-scan traffic
        it already walked past). Filters are applied to the whole ordered stream
        before the limit, so pages of a filtered stream are as resumable as pages of
        the unfiltered one.

        The read is a short read-only transaction, which is what makes the answer
        both consistent and honest about finality: the file sink replays a leftover
        write-ahead journal forward at the start of every transaction (so an
        interrupted transaction's events are reconciled before they are reported,
        and a half-applied one is never visible), and SQLite commits atomically, so
        a half-applied transaction never exists to read. Events whose cursor is
        unset are not pageable and are omitted -- an unassigned cursor is a doctor
        finding (`inspect_events`), not a query result.
        """
        if limit is not None and int(limit) < 1:
            raise InvalidRecord(
                f"an event query limit must be a positive integer, got {limit!r}"
            )
        start = 0 if after_cursor is None else int(after_cursor)
        if not self.is_initialised():
            # A never-used store answers empty without creating anything.
            return {"events": [], "next_cursor": start, "has_more": False, "scanned": 0}
        with self.transaction(write=False) as tx:
            events = list(tx.find("event"))
        ordered = sorted(
            (e for e in events if e.cursor is not None), key=lambda e: e.cursor
        )
        newest = ordered[-1].cursor if ordered else 0
        ordered = [e for e in ordered if e.cursor > start]
        wanted_categories = set(categories or ())
        wanted_kinds = set(kinds or ())
        wanted_subjects = set(subject_ids or ())
        matching = [
            e for e in ordered
            if (not wanted_categories or e.category in wanted_categories)
            and (not wanted_kinds or e.kind_ in wanted_kinds)
            and (not wanted_subjects or bool(wanted_subjects & set(e.subject_ids)))
        ]
        page = matching if limit is None else matching[: int(limit)]
        has_more = len(page) < len(matching)
        next_cursor = page[-1].cursor if has_more else newest
        return {
            "events": page,
            "next_cursor": next_cursor,
            "has_more": has_more,
            "scanned": len(ordered),
        }

    # -- event cursors and the namespace registry (planning key C11) ------
    #
    # Cursors are store-local monotonic integers, so a cursor of 1 in two stores
    # names two unrelated events. Exporting or importing an event stream therefore
    # has to say which store a cursor came from; the namespace surface below makes
    # that explicit. It is concrete-not-abstract for the same reason as artifacts:
    # a lightweight/in-memory store stays constructible and simply refuses the
    # operations it does not implement.

    def cursor_namespace(self) -> str:
        """A stable namespace identifying *this* store's event cursors.

        The value names the store kind and its canonical location, e.g.
        `file:<realpath of coordination dir>` or `sqlite:<realpath of db file>`.
        It must be derived without creating anything on disk, and it must be
        stable across processes for one location. The default raises because a
        namespace is a property of the sink's storage; both shipped sinks
        override it.
        """
        raise UnsupportedCoordination(
            f"the {getattr(self, 'kind', '?')} coordination store does not identify "
            "its cursor namespace; cursor_namespace() must be overridden by a sink "
            "that stores events"
        )

    def is_initialised(self) -> bool:
        """Whether coordination state exists, evaluated *without creating any*.

        The probe must not create a directory, open a database in a way that
        creates the file, or trigger a lazy `init()`: it answers a question, it
        does not start anything. The default is `True` -- an in-memory or unknown
        store is trivially usable -- while a shipped sink overrides it with a
        non-creating probe so a caller can tell "no store yet" from "an empty
        store".
        """
        return True

    def namespaces(self) -> list:
        """The retained namespace registry, sorted by namespace.

        Each entry has the shape documented by
        `coordination_storage.namespace_record`. The default is empty: a store
        that does not track imports has imported nothing.
        """
        return []

    def record_namespace(
        self,
        namespace: str,
        *,
        imported_at=None,
        event_count: int = 0,
        cursor_map=None,
        source_contract_version=None,
    ) -> dict:
        """Upsert one namespace-registry entry, keyed by `namespace`.

        `cursor_map` maps a namespace's source cursors to this store's
        destination cursors; on a repeated import for the same namespace the maps
        are unioned (the new destination wins for a shared source cursor),
        `event_count` is *cumulative*, and `imported_at`/`source_contract_version`
        reflect the most recent import. Returns the stored entry, whose shape is::

            {"namespace": str,
             "imported_at": <UTC timestamp>,
             "event_count": int,
             "cursor_map": {str(source_cursor): int(destination_cursor)},
             "source_contract_version": int}

        The default raises (mirroring `store_artifact_bytes`): a store that does
        not track imports cannot pretend it did.
        """
        raise UnsupportedCoordination(
            f"the {getattr(self, 'kind', '?')} coordination store does not implement "
            "the namespace registry, so an import cursor map cannot be recorded"
        )

    # -- integrity inspection and maintenance (planning key C11) ----------
    #
    # This is the surface a later `coordination_doctor` uses to look at *stored*
    # state without knowing any SQL or file layout. Every method here is concrete
    # with a safe default, exactly like `event_log`/`operation_lock_path`: a sink
    # that cannot answer says so by returning the honest empty answer rather than
    # raising, so an in-memory store stays constructible and a doctor can call
    # every probe unconditionally.
    #
    # Hard rule for the whole group: **none of them may create state.** A probe
    # that lazily initialised a store would turn "nothing here yet" into "now
    # there is a store", so `inspect_*`/`pending_journals`/`missing_*` must work
    # on a never-used location and return empty. The only two exceptions are
    # explicit *maintenance* methods, and each may write only by re-deriving
    # something that already exists: `replay_journals` forward-applies existing
    # intents, and `rebuild_event_operation_index` recreates derived index data.
    # Neither invents an operation, repairs a record or touches evidence.

    def binding_location(self) -> str:
        """The `location` string a `StoreBinding` for THIS store carries.

        `application.coordination_service_for` calls
        `ensure_binding(..., location=str(sink.root))`, so a stored binding's
        `location` is definitionally this string for each sink -- the `.arbite`
        directory for the file sink, the database path for SQLite. Exposing it
        here lets a doctor compare the authoritative binding marker against the
        store that is actually open without knowing what `root` means for a kind.
        The default is `str(self.root)`, which is exactly what both shipped sinks
        already pass. Creates nothing."""
        return str(self.root)

    def binding_marker_path(self) -> Optional[str]:
        """Absolute path of this store's `.arbite/workspace-binding.json`, or None.

        The marker (`arbite.workspace`) is the one place a conflicting sink
        selection is visible *before* any coordination record is written, so a
        doctor needs to find it from the store alone. A store whose marker
        location is unknown returns None rather than guessing, so "unknown" stays
        distinguishable from "no marker". Creates nothing."""
        return None

    def inspect_records(self) -> list:
        """Every stored record-envelope, UNVALIDATED, for integrity inspection.

        Each entry is a plain dict::

            {"kind": str, "record_id": str, "revision": int | None,
             "payload": <parsed JSON value or None>, "error": str | None}

        sorted by `(kind, record_id)`. A malformed envelope or unreadable JSON
        must never raise: the reason goes in `error`, `revision` is None, and the
        entry is kept -- *showing* the corruption is the whole point. Returns `[]`
        and creates nothing when `is_initialised()` is False. The default is `[]`:
        a store with no raw storage of its own has nothing to inspect."""
        return []

    def inspect_events(self) -> list:
        """Every stored event-envelope, WITHOUT dedup or validation.

        Each entry is a plain dict::

            {"cursor": int | None, "payload": <parsed JSON or None>, "error": str | None}

        sorted by cursor (an unknown cursor sorts last) and then by stored order.
        Duplicate cursors or ids stay visible -- a duplicate is a finding, not
        something to collapse. A malformed payload never raises; its reason goes
        in `error`. Returns `[]` and creates nothing when `is_initialised()` is
        False. The default is `[]` for the same reason as `inspect_records`."""
        return []

    def pending_journals(self) -> list:
        """Operation ids of leftover write-ahead journals. Inspection only.

        A journal is a not-yet-confirmed intent (the file sink's crash-recovery
        artifact); naming it lets a doctor report an incomplete transaction
        without guessing. This method must **never replay anything** and must
        create nothing. The default is `[]`: a store whose commits are atomic
        (SQLite) has no journal to leave behind."""
        return []

    def replay_journals(self) -> list:
        """Replay leftover journals FORWARD and return the operation ids seen.

        The only method in this group that may write, and only by forward-applying
        intents that already exist: it never invents work, never repairs a record
        and never re-decides a cursor. It is idempotent -- a second call finds the
        applied journals gone. The default is `[]`: a store with atomic commits
        has nothing to replay."""
        return []

    def missing_event_operation_indexes(self) -> list:
        """Operation ids of stored events whose DERIVED per-operation index is absent.

        Some sinks keep a small derived index (operation id -> event) so a retried
        operation can find its original event in O(1) instead of scanning. That
        index is *derived*, so its loss is repairable from the event itself; this
        probe reports which entries are missing. Inspection only -- it repairs
        nothing. A sink with no derived index always returns `[]`, which is also
        the default."""
        return []

    def rebuild_event_operation_index(self, operation_id: str) -> bool:
        """Rebuild the DERIVED per-operation index for `operation_id`.

        Returns True when it wrote the index, False when there is nothing to
        rebuild -- no stored event carries that operation id, or the index already
        exists. It must never touch the event itself (the index is a projection,
        the event is the truth) and never creates a store. The default is False:
        a sink with no derived index has nothing to rebuild."""
        return False


def filter_tickets(tickets: list, q: TicketQuery, buckets: Optional[dict] = None) -> list:
    """The reference in-memory implementation of `TicketQuery`: filter with
    `matches()`, apply the bucket part of the query from `buckets`
    (`{ticket_id: bucket_or_None}`), sort by the canonical order, cap.

    Provided so a sink with no query engine of its own (the file sink) has one
    correct implementation to call, and so a sink *with* one (SQLite) has
    something to be tested against rather than merely believed."""
    q = q.normalized()
    buckets = buckets or {}
    rows = [
        t for t in tickets if q.matches(t) and bucket_matches(buckets.get(t.id), q.buckets)
    ]
    return apply_limit(sort_tickets(rows, q.order), q.limit)
